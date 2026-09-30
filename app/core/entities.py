"""实体抽取：版本号与错误码。

放在 `app/core` 而不是 `ingestion` 里，是为了让
「入库切分」与「引用回链校验」共用同一套规则，同时避免
`app/*` 反向依赖 `ingestion/*` 造成分层倒置。

用途：
- 入库时抽取为 Milvus 标量字段（支持按错误码过滤）；
- 生成后校验「回答里的版本号/错误码是否真的在被引用片段中出现」（防数字漂移）。
"""

import re

#: 版本号，如 v1.2.3 / 1.2
VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+){1,3}\b")
#: 错误码，如 ERR-4041 / E4041 / ERR_4041
ERROR_CODE_RE = re.compile(r"\b(?:ERR|E)[-_]?\d{3,5}\b", re.IGNORECASE)


def extract_entities(text: str) -> set[str]:
    """抽取文本中的全部版本号与错误码。"""
    if not text:
        return set()
    entities = {match.group(0) for match in VERSION_RE.finditer(text)}
    entities.update(match.group(0) for match in ERROR_CODE_RE.finditer(text))
    return entities


def extract_metadata(text: str) -> dict:
    """抽取首个版本号与错误码（入库用的标量字段）。"""
    version = VERSION_RE.search(text or "")
    error_code = ERROR_CODE_RE.search(text or "")
    return {
        "version": version.group(0) if version else None,
        "error_code": error_code.group(0) if error_code else None,
    }
