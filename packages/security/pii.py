"""PII 脱敏：日志与 trace 上报前调用，避免原文外泄。"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("ID_CARD", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("BANK_CARD", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
]


def redact(text: str, mask: str = "***") -> str:
    """把常见 PII 替换为 ``[TYPE:mask]``。"""
    if not text:
        return text
    result = text
    for name, pattern in _PATTERNS:
        result = pattern.sub(f"[{name}:{mask}]", result)
    return result


def contains_pii(text: str) -> bool:
    """是否含有任一已知 PII 模式（不返回命中类型，仅做布尔判定，用于"该不该脱敏"的快筛）。"""
    return any(pattern.search(text) for _, pattern in _PATTERNS)
