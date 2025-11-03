import re
from typing import Any, Dict

def main(inputs: Any) -> Dict[str, Any]:
    """
    LLMブロックの出力（査閲結果）を解析し、
    「不明／承認／却下」に対応する変数を返す。

    --- 入力例 ---
    {
      "finish_reason": "stop",
      "text": "査閲結果：承認\n理由：回答内容が正確でした。"
    }

    --- 出力例 ---
    {
      "status": "ok",
      "result_label": "承認",
      "result_reason": "回答内容が正確でした。",
      "result_code": 1
    }

    --- result_code ---
        0: 不明
        1: 承認
        2: 却下
        -1: パース失敗
    """
    text = ""
    if isinstance(inputs, dict):
        text = str(inputs.get("text", "")).strip()
    elif isinstance(inputs, str):
        text = inputs.strip()
    else:
        text = str(inputs).strip()

    if not text:
        return {
            "status": "error",
            "result_label": "",
            "result_reason": "",
            "result_code": -1
        }

    # 正規表現で「査閲結果」「理由」を抽出
    match = re.search(r"査閲結果[:：]\s*([^\n\r]+)", text)
    label = match.group(1).strip() if match else ""

    reason_match = re.search(r"理由[:：]\s*(.+)", text, re.DOTALL)
    reason = reason_match.group(1).strip() if reason_match else ""

    # 結果を正規化（日本語以外でも誤認しにくいように）
    label_norm = re.sub(r"\s", "", label)
    reason = reason.replace("\r", "").replace("\n", " ").strip()

    if "承認" in label_norm:
        code = 1
    elif "却下" in label_norm:
        code = 2
    elif "不明" in label_norm:
        code = 0
    else:
        code = -1

    return {
        "status": "ok" if code >= 0 else "parse_error",
        "result_label": label_norm or "",
        "result_reason": reason,
        "result_code": code
    }