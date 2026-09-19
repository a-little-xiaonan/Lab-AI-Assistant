"""文本清洗：保守原则——宁可少清洗，不可误删正文。

MVP 只做无争议的字符级卫生处理；页眉页脚识别误删风险高，仅由 PDF 加载器
做"纯页码行"剔除（有页上下文，误判率低），完整页眉页脚识别后置。
"""
from __future__ import annotations

import re

# 控制字符（保留 \n \t \r）
_ILLEGAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text: str) -> str:
    if not text:
        return ""
    # 1. 统一换行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 2. 移除非法控制字符与替换符（乱码残留）
    text = _ILLEGAL_CONTROL.sub("", text)
    text = text.replace("�", "")
    # 3. 行尾空白
    lines = [line.rstrip() for line in text.split("\n")]
    # 4. 连续空行折叠为单个空行（段落分隔）
    result: list[str] = []
    blank = False
    for line in lines:
        if line.strip() == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        result.append(line)
    return "\n".join(result).strip()
