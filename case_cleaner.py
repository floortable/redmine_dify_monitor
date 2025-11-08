#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import shutil
import logging

# 削除対象ルートディレクトリ（.env の CASE_ROOT で上書き可能）
CASE_ROOT = os.getenv("CASE_ROOT", "/var/lib/redmine_dify_monitor/casefiles")


def cleanup_case_directory(caseid: str, *, ticket_id: str | int | None = None) -> bool:
    """caseidディレクトリを削除する。caseidが無ければFalse。"""
    try:
        if not caseid:
            logging.info("case_cleaner: caseid 未指定のため削除スキップ。")
            return False

        target_dir = os.path.join(CASE_ROOT, str(caseid))
        if not os.path.exists(target_dir):
            logging.info(f"case_cleaner: caseid={caseid} のディレクトリが存在しません: {target_dir}")
            return False

        shutil.rmtree(target_dir)
        suffix = f" (ticket#{ticket_id})" if ticket_id else ""
        logging.info(f"✅ case_cleaner: caseid={caseid} のディレクトリ削除成功: {target_dir}{suffix}")
        return True

    except Exception as e:
        suffix = f" (ticket#{ticket_id})" if ticket_id else ""
        logging.error(f"case_cleaner: caseid={caseid or 'N/A'} のディレクトリ削除失敗{suffix}: {e}")
        return False
