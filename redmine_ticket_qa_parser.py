from typing import Any, Iterable, Sequence


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


def main(inputs: Any) -> dict:
    """
    RedmineのチケットJSONから、
    Question（質問）とAnswer（回答）を抽出するDify用スクリプト。

    条件:
    - 最後のAnswer（回答）とその直前のQuestion（質問）を抽出。
    - 回答が存在しない場合は中断。
    - 最後の回答より後に新しい質問がある場合（未回答）も中断。
    - <pre>タグ除去、最後の区切り線以降の本文のみ抽出。
    出力:
      last_answer: 回答テキスト
      previous_question: 質問テキスト
      status: 状態 (ok / no_answer_found / unanswered_new_question / incomplete)
    """
    keyword_answer = "Answer"   # 回答
    keyword_question = "Question" # 質問
    separator = "-------------------------------------------"

    def extract_after_last_separator(text) -> str:
        """<pre>タグを除去し、最後の-------------------------------------------以降を抽出"""
        if text is None:
            return ""
        clean = str(text).replace("<pre>", "").replace("</pre>", "").strip()
        if separator in clean:
            clean = clean.split(separator)[-1]
        return clean.strip()

    for entry in _normalize_entries(inputs):
        issue = entry.get("issue", {}) if isinstance(entry, dict) else {}
        journals = issue.get("journals", []) if isinstance(issue, dict) else []
        description = issue.get("description", "") or ""
        last_answer_index = None
        last_answer = ""
        previous_question = None

        # ---- journals を created_on 昇順に整える（乱れ対策）----
        try:
            journals = sorted(journals, key=lambda x: x.get("created_on", ""))
        except Exception:
            # ソート不能でも処理継続
            pass

        # --- 1️⃣ journals内で最後のAnswer（回答）を特定 ---
        for i, journal in enumerate(journals):
            notes = journal.get("notes", "") or ""
            if keyword_answer in str(notes):
                last_answer_index = i
                last_answer = extract_after_last_separator(notes)

        # --- 2️⃣ 回答が存在しない場合 ---
        if last_answer_index is None:
            return {
                "last_answer": "",
                "previous_question": "",
                "status": "no_answer_found"
            }

        # --- 3️⃣ 回答後に新しい質問があるが、それに対応する回答がない場合（未回答） ---
        has_unanswered_question = False
        for j in range(last_answer_index + 1, len(journals)):
            notes = journals[j].get("notes", "") or ""
            text = str(notes)

            # 回答後に新しい質問がある
            if keyword_question in text:
                # その後に新しい回答があるなら無視（正常）
                has_following_answer = any(
                    keyword_answer in str(k.get("notes", "")) for k in journals[j + 1 :]
                )
                if not has_following_answer:
                    has_unanswered_question = True
                    break

        if has_unanswered_question:
            return {
                "last_answer": "",
                "previous_question": "",
                "status": "unanswered_new_question"
            }

        # --- 4️⃣ 回答の直前にある質問を探索 ---
        for j in range(last_answer_index - 1, -1, -1):
            notes = journals[j].get("notes", "") or ""
            if keyword_question in str(notes):
                previous_question = extract_after_last_separator(notes)
                break

        # --- 5️⃣ journalsになければ description から抽出 ---
        if previous_question is None and keyword_question in str(description):
            previous_question = extract_after_last_separator(description)
        if not previous_question:
            previous_question = ""

        # --- 6️⃣ どちらかが欠けている場合は不完全扱い ---
        if not last_answer or not previous_question:
            return {
                "last_answer": "",
                "previous_question": "",
                "status": "incomplete"
            }

        # --- 7️⃣ 正常完了 ---
        return {
            "last_answer": last_answer,
            "previous_question": previous_question,
            "status": "ok"
        }

    # --- 対象データが存在しない場合 ---
    return {
        "last_answer": "",
        "previous_question": "",
        "status": "incomplete"
    }
