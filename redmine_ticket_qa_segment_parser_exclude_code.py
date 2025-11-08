from typing import Any, Iterable, Sequence
import re

def _normalize_entries(inputs: Any) -> Iterable[dict]:
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
    Redmineチケットの履歴から質問／回答を抽出し、
    全体文字数で上限を制御（長文を含む場合でも安全にトークン削減）。

    出力:
    {
      "entries": [...],
      "status": "ok" or "incomplete"
    }
    """

    keyword_question = "Question"
    keyword_answer = "Answer"
    separator = "-------------------------------------------"
    MAX_TOTAL_CHARS = 6000  # ← 全履歴の合計文字数上限

    def extract_after_last_separator(text: str) -> str:
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
        if not text:
            return ""
        lines = text.splitlines()
        filtered = []
        for line in lines:
            # syslog形式、長すぎる行、JSONなどを除外
            if re.match(r"^\s*(\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|INFO|ERROR|DEBUG|TRACE)", line):
                continue
            if re.match(r"^\s*[{\[].*[}\]]\s*$", line):
                continue
            if len(line.strip()) > 200:
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

        # descriptionを質問として登録
        if keyword_question in str(description):
            text = extract_after_last_separator(description)
            if text:
                all_entries.append({
                    "type": "question",
                    "text": remove_logs(text),
                    "created_on": issue_created
                })

        # journals を昇順に並べ替え
        try:
            journals = sorted(journals, key=lambda x: x.get("created_on", ""))
        except Exception:
            pass

        for j in journals:
            notes = str(j.get("notes", "")) or ""
            created_on = j.get("created_on", "")
            if not notes.strip():
                continue

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

        # --- 🔽 総文字数制限処理 ---
        total_chars = 0
        trimmed_entries = []
        for e in reversed(all_entries):  # 直近の履歴から逆順に積み上げ
            entry_len = len(e["text"])
            if total_chars + entry_len > MAX_TOTAL_CHARS:
                break
            trimmed_entries.append(e)
            total_chars += entry_len

        # 元の時系列順に戻す
        trimmed_entries = list(reversed(trimmed_entries))

        status = "ok" if trimmed_entries else "incomplete"
        return {
            "entries": trimmed_entries,
            "status": status
        }

    return {
        "entries": [],
        "status": "incomplete"
    }
