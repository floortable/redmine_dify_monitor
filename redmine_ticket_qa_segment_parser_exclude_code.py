from typing import Any, Iterable, Sequence
import re


def _normalize_entries(inputs: Any) -> Iterable[dict]:
    """Dify入力の揺れを吸収してイテレーション可能な形に揃える。"""
    if isinstance(inputs, dict):
        candidate = inputs.get("inputs", inputs)
    else:
        candidate = inputs

    if candidate is None:
        return ()

    if isinstance(candidate, dict):
        return (candidate,)

    if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
        return (entry for entry in candidate if isinstance(entry, dict))

    return ()


def main(inputs: Any):
    """
    RedmineチケットJSONから、質問・回答の履歴を時系列順に抽出する。
    ログやスタックトレースなどノイズを削除する（要約は行わない）。

    出力形式:
    {
      "entries": [
        {"type": "question", "text": "...", "created_on": "..."},
        {"type": "answer", "text": "...", "created_on": "..."},
        ...
      ],
      "status": "ok"
    }
    """

    keyword_question = "Question"
    keyword_answer = "Answer"
    separator = "-------------------------------------------"
    MAX_ENTRIES = 10  # トークン削減用：履歴の最大件数

    def extract_after_last_separator(text: str) -> str:
        """<pre>や```を除去し、最後の区切り線以降を抽出"""
        if not text:
            return ""
        clean = (
            str(text)
            .replace("<pre>", "")
            .replace("</pre>", "")
            .replace("```", "")
            .strip()
        )
        if separator in clean:
            clean = clean.split(separator)[-1]
        return clean.strip()

    def remove_logs(text: str) -> str:
        """
        syslogやコードブロックのようなログ行を削除・置換。
        - 日時やログレベルを含む行
        - JSONやbase64のような行
        - 長すぎる行 (>200文字)
        """
        if not text:
            return ""
        lines = text.splitlines()
        filtered = []
        for line in lines:
            # syslog / timestamp / log level
            if re.match(r"^\s*(\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|INFO|ERROR|DEBUG|TRACE)", line):
                continue
            # JSONっぽい or base64っぽい
            if re.match(r"^\s*[{\[].*[}\]]\s*$", line):
                continue
            if len(line.strip()) > 200:
                # 1行が非常に長い（バイナリorトレース）
                continue
            filtered.append(line)
        cleaned = "\n".join(filtered).strip()
        return cleaned if cleaned else "[ログ省略]"

    all_entries = []

    for entry in _normalize_entries(inputs):
        issue = entry.get("issue", {}) if isinstance(entry, dict) else {}
        journals = issue.get("journals", []) if isinstance(issue, dict) else []
        description = issue.get("description", "") or ""
        issue_created = issue.get("created_on", "")

        # ---- descriptionを質問として先頭に追加（あれば）----
        if keyword_question in str(description):
            text = extract_after_last_separator(description)
            if text:
                all_entries.append({
                    "type": "question",
                    "text": remove_logs(text),
                    "created_on": issue_created
                })

        # ---- journalsを時系列順にソート ----
        try:
            journals = sorted(journals, key=lambda x: x.get("created_on", ""))
        except Exception:
            pass

        # ---- journalsから質問・回答を抽出 ----
        for j in journals:
            notes = str(j.get("notes", "")) or ""
            created_on = j.get("created_on", "")
            if not notes.strip():
                continue  # 空ノート・内部メモはスキップ

            if keyword_question in notes:
                q_text = extract_after_last_separator(notes)
                if q_text:
                    all_entries.append({
                        "type": "question",
                        "text": remove_logs(q_text),
                        "created_on": created_on
                    })

            elif keyword_answer in notes:
                a_text = extract_after_last_separator(notes)
                if a_text:
                    all_entries.append({
                        "type": "answer",
                        "text": remove_logs(a_text),
                        "created_on": created_on
                    })

        # ---- 長すぎる場合は直近 MAX_ENTRIES 件のみ保持 ----
        if len(all_entries) > MAX_ENTRIES:
            all_entries = all_entries[-MAX_ENTRIES:]

        status = "ok" if all_entries else "incomplete"

        return {
            "entries": all_entries,
            "status": status
        }

    return {
        "entries": [],
        "status": "incomplete"
    }
