from typing import Any, Iterable, Sequence, List
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
    Redmineチケットの履歴から、6000文字に収まる範囲の質問／回答ブロックを抽出し、
    caseid整合性チェックを含むステータス判定を行う。

    出力:
    {
      "entries": [
        {"type": "question|answer", "text": "<ログ除去済み本文>", "created_on": "<ISO日時>"},
        ...
      ],                     # 直近から最大6000文字分
      "status": "<status文字列>"
    }

    --- status 一覧 ---
        ok                      : 正常（質問・回答抽出成功、caseid一致）
        no_answer_found         : 回答（Answer）が存在しない
        unanswered_new_question : 回答後に新しい質問があり、対応回答がない
        incomplete              : 質問または回答が欠落している
        caseid_field_missing    : custom_fieldsにcaseid項目が存在しない
        caseid_missing          : 回答冒頭3行以内に10桁数字がない（内部メモなど）
        caseid_mismatch         : 回答冒頭に10桁数字はあるが、自分のcaseidが含まれない（誤送信の可能性）
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

    def trim_entries_by_chars(entries: List[dict]) -> List[dict]:
        total_chars = 0
        trimmed = []
        for entry in reversed(entries):
            text = entry.get("text", "") or ""
            entry_len = len(text)
            remaining = MAX_TOTAL_CHARS - total_chars
            if remaining <= 0:
                break
            if entry_len == 0:
                trimmed.append(entry)
                continue
            if entry_len > remaining:
                truncated = dict(entry)
                truncated["text"] = text[:remaining]
                trimmed.append(truncated)
                total_chars = MAX_TOTAL_CHARS
                break
            trimmed.append(entry)
            total_chars += entry_len
        return list(reversed(trimmed))

    for entry in _normalize_entries(inputs):
        issue = entry.get("issue", {}) if isinstance(entry, dict) else {}
        journals = issue.get("journals", []) if isinstance(issue, dict) else []
        description = issue.get("description", "") or ""
        issue_created = issue.get("created_on", "")

        # ---- 履歴一覧を構築（ログ・コードブロック除外）----
        all_entries = []
        if keyword_question in str(description):
            desc_text = extract_after_last_separator(description)
            if desc_text:
                all_entries.append({
                    "type": "question",
                    "text": remove_logs(desc_text),
                    "created_on": issue_created
                })

        try:
            journals = sorted(journals, key=lambda x: x.get("created_on", ""))
        except Exception:
            pass

        last_answer_index = None
        last_answer_raw = ""

        for idx, journal in enumerate(journals):
            notes = str(journal.get("notes", "")) or ""
            created_on = journal.get("created_on", "")
            if not notes.strip():
                continue

            is_question = keyword_question in notes
            is_answer = keyword_answer in notes

            if is_question:
                q_raw = extract_after_last_separator(notes)
                if q_raw:
                    all_entries.append({
                        "type": "question",
                        "text": remove_logs(q_raw),
                        "created_on": created_on
                    })

            if is_answer:
                a_raw = extract_after_last_separator(notes)
                if a_raw:
                    all_entries.append({
                        "type": "answer",
                        "text": remove_logs(a_raw),
                        "created_on": created_on
                    })
                last_answer_index = idx
                last_answer_raw = a_raw or ""

        previous_question_raw = ""
        if last_answer_index is not None:
            for j in range(last_answer_index - 1, -1, -1):
                notes = str(journals[j].get("notes", "")) or ""
                if keyword_question in notes:
                    previous_question_raw = extract_after_last_separator(notes) or ""
                    break
        if not previous_question_raw and keyword_question in str(description):
            previous_question_raw = extract_after_last_separator(description) or ""

        def _extract_caseid() -> str:
            custom_fields = issue.get("custom_fields", [])
            if isinstance(custom_fields, dict):
                custom_fields = [custom_fields]
            for cf in custom_fields:
                if not isinstance(cf, dict):
                    continue
                if cf.get("name") == "caseid":
                    return str(cf.get("value", "")).strip()
            return ""

        # ---- ステータス判定（回答有無→未回答→caseid整合性）----
        status = None
        if last_answer_index is None:
            status = "no_answer_found"
        else:
            unanswered_new_question = False
            for j in range(last_answer_index + 1, len(journals)):
                notes = str(journals[j].get("notes", "")) or ""
                if keyword_question in notes:
                    has_following_answer = any(
                        keyword_answer in str(k.get("notes", "")) for k in journals[j + 1:]
                    )
                    if not has_following_answer:
                        unanswered_new_question = True
                    break

            if unanswered_new_question:
                status = "unanswered_new_question"
            else:
                caseid = _extract_caseid()
                if not caseid:
                    status = "caseid_field_missing"
                else:
                    lines = str(last_answer_raw).strip().splitlines()
                    first3 = "\n".join(lines[:3])
                    found_caseids = re.findall(r"\d{10}", first3)
                    if not found_caseids:
                        status = "caseid_missing"
                    elif caseid not in found_caseids:
                        status = "caseid_mismatch"
                    elif not last_answer_raw or not previous_question_raw:
                        status = "incomplete"
                    else:
                        status = "ok"

        if status is None:
            status = "incomplete"

        # ---- 直近から6000文字に収まるよう entries を圧縮 ----
        trimmed_entries = trim_entries_by_chars(all_entries)

        return {
            "entries": trimmed_entries,
            "status": status
        }

    return {
        "entries": [],
        "status": "incomplete"
    }
