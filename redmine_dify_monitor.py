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

# --- 設定 ---
REDMINE_URL = os.getenv("REDMINE_URL", "http://localhost:3000")
REDMINE_API_KEY = os.getenv("REDMINE_API_KEY", "your_redmine_api_key")

DIFY_API_URL = os.getenv("DIFY_API_URL", "http://localhost:5001/v1/workflows/execute")
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "your_dify_api_key")

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "https://graph.microsoft.com/...")
TEAMS_WEBHOOK_SECONDARY_URL = os.getenv("TEAMS_WEBHOOK_SECONDARY_URL", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))  # 秒単位
STATE_FILE = "/var/lib/redmine_dify_monitor/processed_issues.json"
LOG_FILE = "/var/log/redmine_dify_monitor/redmine_dify_monitor.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler()  # ← docker logs に出す
    ]
)
logging.info("ログ初期化完了！")

# --- 状態ロード/保存 ---
def load_processed_issues():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logging.error(f"{STATE_FILE} が破損しています。バックアップを作成して再初期化します。")
        os.rename(STATE_FILE, STATE_FILE + ".bak")
        return {}

def save_processed_issues(data):
    tmpfile = STATE_FILE + ".tmp"
    try:
        with open(tmpfile, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmpfile, STATE_FILE)
    except Exception as e:
        logging.error(f"状態保存失敗: {e}")

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
            return None
    except Exception as e:
        logging.error(f"Dify呼び出し失敗: {e}")
        return None

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

        text = outputs.get("text") or outputs.get("text_1") or outputs.get("gpt") or outputs.get("gemma") or ""
        if not text:
            return None

        decoded = safe_decode_dify_text(text)
        cleaned = decoded.strip()

        # --- 🚫 無効な応答を除外 ---
        if not cleaned or cleaned in ["", "null", "None"] or re.fullmatch(r"\d+", cleaned):
            logging.info(f"Dify応答が無効または数字のみのためスキップ: {repr(cleaned)}")
            return None

        return cleaned
    
    except Exception as e:
        logging.error(f"Dify応答解析エラー: {e}")
        return None
    
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
                result_text = call_dify(issue_id)
                if not result_text:
                    logging.info("Dify応答なし、スキップ")
                    processed[str(issue_id)] = updated_on
                    save_processed_issues(processed)
                    continue

                #if result and result["査閲結果"] == "却下":
                #    update_redmine_status(issue_id, 5)  # “差し戻し” のステータスIDに置き換え

                result = parse_dify_result(result_text)
                if result and result["査閲結果"] != "不明":
                    post_to_teams(issue, result)
                    logging.info(f"Teamsに投稿: {result['査閲結果']} ({subject})")

                # 更新時刻を記録
                processed[str(issue_id)] = updated_on
                save_processed_issues(processed)

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