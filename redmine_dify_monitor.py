#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json
import logging
import time
from datetime import datetime, timezone
from dateutil import parser
import os
import re
from logging.handlers import RotatingFileHandler
import traceback
import signal
import sys
import sqlite3

# --- 設定 ---
REDMINE_URL = os.getenv("REDMINE_URL", "http://localhost:3000")
REDMINE_API_KEY = os.getenv("REDMINE_API_KEY", "your_redmine_api_key")

DIFY_API_URL = os.getenv("DIFY_API_URL", "http://localhost:5001/v1/workflows/execute")
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "your_dify_api_key")

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "https://graph.microsoft.com/...")
TEAMS_WEBHOOK_SECONDARY_URL = os.getenv("TEAMS_WEBHOOK_SECONDARY_URL", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))  # 秒単位
STATE_DB = "/var/lib/redmine_dify_monitor/processed_issues.db"
LOG_FILE = "/var/log/redmine_dify_monitor/redmine_dify_monitor.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(os.path.dirname(STATE_DB), exist_ok=True)
LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").upper()
try:
    LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME)
    if not isinstance(LOG_LEVEL, int):
        raise AttributeError
    _LOG_LEVEL_INVALID = False
except AttributeError:
    LOG_LEVEL = logging.INFO
    _LOG_LEVEL_INVALID = LOG_LEVEL_NAME != "INFO"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler()  # ← docker logs に出す
    ]
)
if _LOG_LEVEL_INVALID:
    logging.warning(f"LOG_LEVEL '{LOG_LEVEL_NAME}' は不正です。INFO を使用します。")
logging.info(f"ログ初期化完了！ (LOG_LEVEL={logging.getLevelName(LOG_LEVEL)})")

# --- 状態ロード/保存 ---
def init_state_db():
    try:
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_issues (
                    issue_id TEXT PRIMARY KEY,
                    updated_on TEXT NOT NULL,
                    last_seen_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
                """
            )
    except sqlite3.Error as e:
        logging.error(f"状態DB初期化失敗: {e}")
        raise

def load_processed_issues():
    try:
        init_state_db()
    except Exception:
        return {}

    try:
        with sqlite3.connect(STATE_DB) as conn:
            cursor = conn.execute("SELECT issue_id, updated_on FROM processed_issues")
            return {issue_id: updated_on for issue_id, updated_on in cursor.fetchall()}
    except sqlite3.Error as e:
        logging.error(f"状態DB読み込み失敗: {e}")
        return {}

def upsert_processed_issue(issue_id, updated_on):
    try:
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute(
                """
                INSERT INTO processed_issues (issue_id, updated_on, last_seen_at)
                VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                ON CONFLICT(issue_id) DO UPDATE SET
                    updated_on=excluded.updated_on,
                    last_seen_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                """,
                (str(issue_id), updated_on),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"状態DB更新失敗(issue_id={issue_id}): {e}")

# --- タイムゾーン対応 ---
def normalize_timestamp(ts):
    try:
        return parser.parse(ts).astimezone(timezone.utc).isoformat()
    except Exception:
        return ts
    
# --- Redmine チケット取得 ---
def get_recent_issues():
    params = {"key": REDMINE_API_KEY, "status_id": "*", "sort": "updated_on:desc", "limit": 10}
    for attempt in range(2):
        try:
            resp = requests.get(f"{REDMINE_URL}/issues.json", params=params, timeout=30)
            resp.raise_for_status()
            return resp.json().get("issues", [])
        except (requests.exceptions.RequestException, ValueError) as e:
            wait = 4 ** attempt
            logging.warning(f"Redmine取得失敗({attempt+1}/2): {e}")
            time.sleep(wait)
    return []

# --- Redmine 差し戻しステータスに更新 ---
def update_redmine_status(issue_id, status_id):
    url = f"{REDMINE_URL}/issues/{issue_id}.json"
    payload = {"issue": {"status_id": status_id}}
    headers = {"X-Redmine-API-Key": REDMINE_API_KEY, "Content-Type": "application/json"}
    try:
        requests.put(url, headers=headers, json=payload, timeout=10).raise_for_status()
        logging.info(f"Redmineチケット #{issue_id} のステータスを更新しました。")
    except Exception as e:
        logging.error(f"Redmineステータス更新失敗: {e}")

# --- Dify 応答デコード ---
def safe_decode_dify_text(text: str) -> str:
    # もし \x?? パターンが含まれていたらエスケープ解除を試みる
    if "\\x" in text:
        try:
            return text.encode("latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except Exception:
            pass  # 失敗したらそのまま返す
    return text

# --- Dify 呼び出し ---
def call_dify(ticket_id):
    DIFY_HEADERS = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    payload = {"inputs": {"ticketid": ticket_id, "LLM": "GPT"}, "response_mode": "blocking", "user": "redmine-monitor"}

    logging.debug(f"Dify呼び出し開始 URL={DIFY_API_URL}")
    logging.debug(f"Difyリクエストヘッダ: {json.dumps(DIFY_HEADERS, ensure_ascii=False, indent=2)}")
    logging.debug(f"Difyリクエストペイロード: {json.dumps(payload, ensure_ascii=False, indent=2)}")

    try:
        resp = requests.post(DIFY_API_URL, headers=DIFY_HEADERS, json=payload, timeout=360)
        resp.raise_for_status()
        try:
            data = resp.json()
            logging.debug(f"Dify応答(JSON): {json.dumps(data, ensure_ascii=False, indent=2)}")
        except json.JSONDecodeError:
            logging.error(f"Dify応答がJSONとして解釈できません: {resp.text[:200]}")
            return None, None
    except Exception as e:
        logging.error(f"Dify呼び出し失敗: {e}")
        return None, None

    try:
        raw_outputs = data.get("data", {}).get("outputs", "")
        if isinstance(raw_outputs, str):
            try:
                outputs = json.loads(raw_outputs)
            except Exception:
                # ダブルJSONエンコード対策
                try:
                    outputs = json.loads(json.loads(raw_outputs))
                except Exception:
                    outputs = {}
        elif isinstance(raw_outputs, dict):
            outputs = raw_outputs
        else:
            outputs = {}

        status = outputs.get("status")
        if status and status != "ok":
            if status == "caseid_mismatch":
                logging.warning(f"Dify応答ステータスがcaseid_mismatch: チケットID={ticket_id}")
            else:
                logging.info(f"Dify応答ステータスが非OKのためスキップ: status={status}")
            return None, status

        text = outputs.get("text") or outputs.get("text_1") or outputs.get("gpt") or outputs.get("gemma") or ""
        if not text:
            return None, status

        decoded = safe_decode_dify_text(text)
        cleaned = decoded.strip()

        # --- 🚫 無効な応答を除外 ---
        if not cleaned or cleaned in ["", "null", "None"] or re.fullmatch(r"\d+", cleaned):
            logging.info(f"Dify応答が無効または数字のみのためスキップ: {repr(cleaned)}")
            return None, status

        return cleaned, status or "ok"
    
    except Exception as e:
        logging.error(f"Dify応答解析エラー: {e}")
        return None, None
    
# --- Dify結果解析 ---
def parse_dify_result(text):
    logging.debug("=== parse_dify_result 開始 ===")

    # バイト列（\xE6形式）で渡されるケースへの対応
    if isinstance(text, (bytes, bytearray)):
        try:
            text = text.decode("utf-8", errors="replace")
            logging.debug("textをUTF-8としてデコードしました。")
        except Exception as e:
            logging.debug(f"textのデコードに失敗: {e}")

    # None や空文字対策
    if not text or str(text).strip() in ["", "null", "None"]:
        logging.debug(f"textが空または不正: {repr(text)}")
        logging.debug("=== parse_dify_result 結果: 不明 ===")
        return "不明"

    # テキストを一旦ログに出して確認
    logging.debug(f"Dify応答本文: {repr(text[:300])}")  # 長文の場合は先頭300文字のみ出す

    if not text or text.strip() in ["", "null", "None"] or re.fullmatch(r"\d+", text.strip()):
        logging.info("Dify応答が空または数字のみです。スキップします。")
        logging.debug("=== parse_dify_result 結果: None ===")
        return None
    m_result = re.search(r"(査閲結果|結果)[:：]\s*(承認|却下)", text)
    m_reason = re.search(r"(理由|原因)[:：]\s*(.+)", text)
    logging.debug(f"m_result: {m_result.group(0) if m_result else 'None'}")
    logging.debug(f"m_reason: {m_reason.group(0) if m_reason else 'None'}")

    if not m_result:
        logging.debug("査閲結果の正規表現にマッチしませんでした。")
        logging.debug("=== parse_dify_result 結果: 不明 ===")
        return {"査閲結果": "不明", "理由": "判定なし"}

    result = m_result.group(2)
    reason = m_reason.group(2).strip() if m_reason else "理由なし"

    logging.debug(f"抽出結果 → 査閲結果: {result}, 理由: {reason}")
    logging.debug("=== parse_dify_result 正常終了 ===")

    return {"査閲結果": m_result.group(2), "理由": m_reason.group(2).strip() if m_reason else "理由なし"}

# --- Teams投稿 ---
def post_to_teams(issue, result):
    """Adaptive CardをTeamsに投稿"""
    ticket_id = issue["id"]
    subject = issue["subject"]
    m_result = result["査閲結果"]
    m_reason = result["理由"]

    # メインWebhook
    webhooks = [TEAMS_WEBHOOK_URL]

    # 却下時のみ追加の通知先も設定
    if m_result == "却下" and TEAMS_WEBHOOK_SECONDARY_URL:
        webhooks.append(TEAMS_WEBHOOK_SECONDARY_URL)

    # デザイン設定
    if m_result == "却下":
        color = "Attention"
        accent_color = "#D13438"  # 赤
        emoji = "❌"
        bg_style = {
            "type": "Container",
            "items": [
                {"type": "TextBlock", "text": f"{emoji} **チケット却下**", "size": "Large", "weight": "Bolder", "color": "Attention"},
                {"type": "TextBlock", "text": f"[Redmine チケット #{ticket_id}]({REDMINE_URL}/issues/{ticket_id})", "wrap": True, "spacing": "Small"},
                {"type": "TextBlock", "text": f"件名：{subject}", "wrap": True, "spacing": "Small"},
                {
                    "type": "Container",
                    "style": "emphasis",
                    "items": [
                        {"type": "TextBlock", "text": "却下理由", "weight": "Bolder", "color": "Attention"},
                        {"type": "TextBlock", "text": m_reason, "wrap": True, "spacing": "Small"},
                    ],
                    "bleed": True
                }
            ],
            "bleed": True
        }
    elif m_result == "承認":
        color = "Good"
        accent_color = "#107C10"
        emoji = "✅"
        bg_style = {
            "type": "Container",
            "items": [
                {"type": "TextBlock", "text": f"{emoji} **チケット承認**", "size": "Large", "weight": "Bolder", "color": "Good"},
                {"type": "TextBlock", "text": f"Redmine チケット #{ticket_id}", "wrap": True, "spacing": "Small"},
                {"type": "TextBlock", "text": f"件名：{subject}", "wrap": True, "spacing": "Small"},
                {"type": "TextBlock", "text": f"理由：{m_reason}", "wrap": True, "spacing": "Small"},
            ],
            "bleed": True
        }
    else:
        color = "Default"
        accent_color = "#767676"
        emoji = "❔"
        bg_style = {
            "type": "Container",
            "items": [
                {"type": "TextBlock", "text": f"{emoji} 判定不明", "size": "Large", "weight": "Bolder"},
                {"type": "TextBlock", "text": f"[Redmine チケット #{ticket_id}]({REDMINE_URL}/issues/{ticket_id})", "wrap": True, "spacing": "Small"},
                {"type": "TextBlock", "text": f"件名：{subject}", "wrap": True},
            ]
        }

    # AdaptiveCard本体
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [bg_style],
            }
        }]
    }

    # 🔍 DEBUG ログに出力
    logging.debug(f"送信カード内容:\n{json.dumps(card, ensure_ascii=False, indent=2)}")

    # 複数Webhookに送信
    for webhook in webhooks:
        for attempt in range(3):
            try:
                resp = requests.post(webhook, json=card, timeout=10)
                resp.raise_for_status()
                logging.info(f"Teams送信成功 ({m_result}) → {webhook}")
                break
            except Exception as e:
                wait = 2 ** attempt
                logging.warning(f"Teams送信失敗({attempt+1}/3): {e}")
                time.sleep(wait)

def post_caseid_mismatch_alert(issue):
    """caseidが一致しない場合の高優先度アラート"""
    ticket_id = issue["id"]
    subject = issue["subject"]
    webhooks = [TEAMS_WEBHOOK_URL]
    if TEAMS_WEBHOOK_SECONDARY_URL:
        webhooks.append(TEAMS_WEBHOOK_SECONDARY_URL)

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "Container",
                        "style": "attention",
                        "bleed": True,
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "🚨 受付番号不一致の可能性",
                                "size": "Large",
                                "weight": "Bolder",
                                "color": "Attention"
                            },
                            {
                                "type": "TextBlock",
                                "text": f"[Redmine チケット #{ticket_id}]({REDMINE_URL}/issues/{ticket_id})",
                                "wrap": True,
                                "spacing": "Small"
                            },
                            {
                                "type": "TextBlock",
                                "text": f"件名：{subject}",
                                "wrap": True,
                                "spacing": "Small"
                            },
                            {
                                "type": "TextBlock",
                                "text": "Difyが caseid mismatch を検知しました。異なる受付番号への回答が申告されています。至急確認してください。",
                                "wrap": True,
                                "spacing": "Medium",
                                "color": "Attention"
                            }
                        ]
                    }
                ]
            }
        }]
    }

    logging.debug(f"caseid mismatch アラートカード:\n{json.dumps(card, ensure_ascii=False, indent=2)}")

    for webhook in webhooks:
        for attempt in range(3):
            try:
                resp = requests.post(webhook, json=card, timeout=10)
                resp.raise_for_status()
                logging.info(f"Teams送信成功 (caseid_mismatch) → {webhook}")
                break
            except Exception as e:
                wait = 2 ** attempt
                logging.warning(f"Teams送信失敗(caseid_mismatch {attempt+1}/3): {e}")
                time.sleep(wait)

# --- SIGTERM対応 ---
def handle_shutdown(signum, frame):
    logging.info(f"停止シグナル({signum})を受信しました。終了します。")
    sys.exit(0)

# --- メインループ ---
def main():
    processed = load_processed_issues()

    while True:
        try:
            issues = get_recent_issues()
            for issue in issues:
                issue_id = issue["id"]
                updated_on = issue["updated_on"]
                subject = issue["subject"]

                updated_on = normalize_timestamp(issue["updated_on"])
                last_time = processed.get(str(issue_id))
                if last_time == updated_on:
                    continue  # 変更なし → スキップ

                logging.info(f"🆕 処理対象チケット: #{issue_id} ({subject}) → Dify解析開始")
                result_text, dify_status = call_dify(issue_id)
                if dify_status == "caseid_mismatch":
                    logging.warning(f"caseid mismatch 検知: チケット #{issue_id} ({subject})")
                    post_caseid_mismatch_alert(issue)
                    processed[str(issue_id)] = updated_on
                    upsert_processed_issue(issue_id, updated_on)
                    continue
                if dify_status and dify_status != "ok":
                    processed[str(issue_id)] = updated_on
                    upsert_processed_issue(issue_id, updated_on)
                    continue
                if not result_text:
                    logging.info("Dify応答なし、スキップ")
                    processed[str(issue_id)] = updated_on
                    upsert_processed_issue(issue_id, updated_on)
                    continue

                #if result and result["査閲結果"] == "却下":
                #    update_redmine_status(issue_id, 5)  # “差し戻し” のステータスIDに置き換え

                result = parse_dify_result(result_text)
                if result and result["査閲結果"] != "不明":
                    post_to_teams(issue, result)
                    logging.info(f"Teamsに投稿: {result['査閲結果']} ({subject})")

                # 更新時刻を記録
                processed[str(issue_id)] = updated_on
                upsert_processed_issue(issue_id, updated_on)

        except Exception as e:
            logging.error(f"メインループエラー: {e}\n{traceback.format_exc()}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown)
    try:
        main()
    except KeyboardInterrupt:
        logging.info("停止要求を受信しました。終了します。")
        exit(0)
