from typing import Any, Iterable, Sequence


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
    RedmineチケットJSONから「最新質問・回答ブロック」と「直前ブロック」を抽出。
    Question / Answer の固定表記を前提とした軽量版。
    """

    keyword_answer = "Answer"
    keyword_question = "Question"
    separator = "-------------------------------------------"

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

    for entry in _normalize_entries(inputs):
        issue = entry.get("issue", {}) if isinstance(entry, dict) else {}
        journals = issue.get("journals", []) if isinstance(issue, dict) else []
        description = issue.get("description", "") or ""

        try:
            journals = sorted(journals, key=lambda x: x.get("created_on", ""))
        except Exception:
            pass

        # === journals から抽出 ===
        answers = [
            extract_after_last_separator(j.get("notes", ""))
            for j in journals
            if keyword_answer in str(j.get("notes", ""))
        ]
        questions = [
            extract_after_last_separator(j.get("notes", ""))
            for j in journals
            if keyword_question in str(j.get("notes", ""))
        ]

        # === description に質問がある場合 ===
        has_desc_question = keyword_question in str(description)
        desc_question = extract_after_last_separator(description) if has_desc_question else ""

        # === 回答群 ===
        last_answer = answers[-1] if answers else ""
        prev_answer = "\n---\n".join(answers[:-1]) if len(answers) > 1 else ""

        # === 質問群 ===
        all_questions = []
        if desc_question:
            all_questions.append(desc_question)
        all_questions.extend(questions)

        if not all_questions:
            last_question = ""
            prev_question = ""
        elif len(all_questions) == 1:
            last_question = all_questions[0]
            prev_question = ""
        else:
            last_question = all_questions[-1]
            prev_question = "\n---\n".join(all_questions[:-1])

        # === ステータス ===
        if not last_answer or not last_question:
            status = "incomplete"
        else:
            status = "ok"

        return {
            "status": status,
            "last_answer": last_answer,
            "last_question": last_question,
            "prev_answer": prev_answer,
            "prev_question": prev_question,
        }

    return {
        "status": "incomplete",
        "last_answer": "",
        "last_question": "",
        "prev_answer": "",
        "prev_question": "",
    }
