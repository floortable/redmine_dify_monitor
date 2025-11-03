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


def main(inputs: Any) -> dict:
    """
    RedmineチケットJSONから質問（Question）と回答（Answer）を抽出する。
    caseidの一致検証や未回答検知、自由形式でのcaseid記載に対応。

    --- 処理概要 ---
    1️⃣ journalsから最後の回答（Answer）を抽出
    2️⃣ その直前の質問（Question）を特定
    3️⃣ 回答後に新しい質問がある場合 → status="unanswered_new_question"
    4️⃣ 回答冒頭3行以内に10桁数字（caseid候補）をすべて抽出
        ・自分のcaseidが含まれていればOK
        ・他案件番号のみなら誤送信（caseid_mismatch）
        ・数字なしなら内部メモ扱い（caseid_missing）
    5️⃣ custom_fieldsにcaseid自体がない場合 → caseid_field_missing
    6️⃣ 抽出結果が欠けていれば incomplete、全て正常なら ok

    --- 出力 ---
    {
        "last_answer": <回答本文>,
        "previous_question": <質問本文>,
        "status": <状態文字列>
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

    keyword_answer = "Answer"
    keyword_question = "Question"
    separator = "-------------------------------------------"

    def extract_after_last_separator(text) -> str:
        """<pre>タグを除去し、最後の区切り線以降の本文を抽出"""
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

        # ---- journalsを作成日時で昇順ソート（時系列乱れ対策） ----
        try:
            journals = sorted(journals, key=lambda x: x.get("created_on", ""))
        except Exception:
            pass

        # ==== 質問・回答ペアの特定 ====

        # ---- ① 最後の回答（Answer）を抽出 ----
        for i, journal in enumerate(journals):
            notes = journal.get("notes", "") or ""
            if keyword_answer in str(notes):
                last_answer_index = i
                last_answer = extract_after_last_separator(notes)

        if last_answer_index is None:
            return {
                "last_answer": "",
                "previous_question": "",
                "status": "no_answer_found"
            }

        # ---- ② 回答後に新しい質問があり、対応回答がない場合 → unanswered_new_question ----
        for j in range(last_answer_index + 1, len(journals)):
            notes = journals[j].get("notes", "") or ""
            if keyword_question in str(notes):
                has_following_answer = any(
                    keyword_answer in str(k.get("notes", "")) for k in journals[j + 1:]
                )
                if not has_following_answer:
                    return {
                        "last_answer": "",
                        "previous_question": "",
                        "status": "unanswered_new_question"
                    }

        # ---- ③ 回答直前の質問を探索 ----
        for j in range(last_answer_index - 1, -1, -1):
            notes = journals[j].get("notes", "") or ""
            if keyword_question in str(notes):
                previous_question = extract_after_last_separator(notes)
                break
        if previous_question is None and keyword_question in str(description):
            previous_question = extract_after_last_separator(description)
        previous_question = previous_question or ""

        # ==== caseid 整合性チェック ====

        # ---- ④ custom_fields から caseid を取得 ----
        caseid = None
        for cf in issue.get("custom_fields", []):
            if cf.get("name") == "caseid":
                caseid = str(cf.get("value", "")).strip()
                break

        if not caseid:
            return {
                "last_answer": "",
                "previous_question": "",
                "status": "caseid_field_missing"
            }

        # ---- ⑤ 回答冒頭3行以内から10桁数字(caseid候補群)を抽出 ----
        lines = str(last_answer).strip().splitlines()
        first3 = "\n".join(lines[:3])
        found_caseids = re.findall(r"\d{10}", first3)

        if not found_caseids:
            # 数字が1つもない → caseid未記載（内部メモなど）
            return {
                "last_answer": last_answer,
                "previous_question": previous_question,
                "status": "caseid_missing"
            }

        # ---- ⑥ 自分のcaseidが含まれているか確認 ----
        if caseid not in found_caseids:
            # 他番号のみ → 誤送信の可能性
            return {
                "last_answer": "",
                "previous_question": "",
                "status": "caseid_mismatch"
            }

        # ---- ⑦ 質問または回答が欠落している場合 ----
        if not last_answer or not previous_question:
            return {
                "last_answer": "",
                "previous_question": "",
                "status": "incomplete"
            }

        # ---- ⑧ 正常終了 ----
        return {
            "last_answer": last_answer,
            "previous_question": previous_question,
            "status": "ok"
        }

    # ---- 入力データなし ----
    return {
        "last_answer": "",
        "previous_question": "",
        "status": "incomplete"
    }
