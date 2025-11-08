#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Tuple

from dateutil import parser

logger = logging.getLogger(__name__)

_PRAGMAS: Tuple[Tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """必要なPRAGMAを適用し、失敗した場合は警告ログを出す。"""
    for pragma, value in _PRAGMAS:
        try:
            conn.execute(f"PRAGMA {pragma}={value};")
        except sqlite3.Error as exc:
            logger.warning("PRAGMA %s=%s の設定に失敗しました: %s", pragma, value, exc)


@contextmanager
def open_db(db_path: str) -> Iterable[sqlite3.Connection]:
    """WALなどのPRAGMAを適用した状態でコネクションを管理する。"""
    conn = sqlite3.connect(db_path)
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


def init_state_db(db_path: str) -> None:
    """state DBの初期化（テーブル作成まで）を行う。"""
    try:
        with open_db(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_issues (
                    issue_id TEXT PRIMARY KEY,
                    updated_on TEXT NOT NULL,
                    last_seen_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
                """
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("状態DB初期化に失敗しました: %s", exc)
        raise


def load_processed_issues(db_path: str) -> Dict[str, str]:
    """issue_id → updated_on の辞書を返す。"""
    try:
        init_state_db(db_path)
    except Exception:
        return {}

    try:
        with open_db(db_path) as conn:
            cursor = conn.execute("SELECT issue_id, updated_on FROM processed_issues")
            return {issue_id: updated_on for issue_id, updated_on in cursor.fetchall()}
    except sqlite3.Error as exc:
        logger.error("状態DBの読み込みに失敗しました: %s", exc)
        return {}


def save_processed_issue(db_path: str, issue_id: str, updated_on: str) -> None:
    """チケットの処理済み状態を挿入または更新する。"""
    try:
        with open_db(db_path) as conn:
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
    except sqlite3.Error as exc:
        logger.error("状態DBの更新に失敗しました(issue_id=%s): %s", issue_id, exc)


def delete_processed_issue(db_path: str, issue_id: str) -> None:
    """指定チケットを状態DBから削除する。存在しなくても成功扱い。"""
    try:
        with open_db(db_path) as conn:
            conn.execute("DELETE FROM processed_issues WHERE issue_id = ?", (str(issue_id),))
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("状態DBの削除に失敗しました(issue_id=%s): %s", issue_id, exc)


def prune_stale_issues(db_path: str, max_age_days: int = 180) -> int:
    """updated_onが一定期間より古いレコードを削除し、削除数を返す。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    removed = 0
    try:
        with open_db(db_path) as conn:
            cursor = conn.execute("SELECT issue_id, updated_on FROM processed_issues")
            stale_ids = []
            for issue_id, updated_on in cursor.fetchall():
                if not updated_on:
                    continue
                try:
                    dt = parser.parse(updated_on)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                except Exception:
                    continue
                if dt < cutoff:
                    stale_ids.append(issue_id)

            for issue_id in stale_ids:
                conn.execute("DELETE FROM processed_issues WHERE issue_id = ?", (issue_id,))
            if stale_ids:
                conn.commit()
            removed = len(stale_ids)
    except sqlite3.Error as exc:
        logger.error("状態DBの古いレコード削除に失敗しました: %s", exc)
    return removed
