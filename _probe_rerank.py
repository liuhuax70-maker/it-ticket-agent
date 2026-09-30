"""临时探针 v2：关闭 thinking 后再试（用完即删）。"""

import json
import time

import httpx

BASE = "http://127.0.0.1:11434"
MODEL = "dengcao/Qwen3-Reranker-4B:Q5_K_M"

INSTRUCTION = "Given a Chinese IT support query, retrieve passages that answer the query."
SYS_YESNO = (
    'Judge whether the Document meets the requirements based on the Query and the Instruct provided. '
    'Note that the answer can only be "yes" or "no".'
)

QUERY = "ERR-4041 令牌过期怎么办"
DOCS = {
    "相关": "ERR-4041 表示令牌已过期。请在客户端执行「退出登录」后重新登录。",
    "无关": "系统支持 Chrome 100 及以上版本，不支持 IE 浏览器。",
}


def yesno_prompt(query: str, doc: str) -> str:
    return f"<Instruct>: {INSTRUCTION}\n<Query>: {query}\n<Document>: {doc}"


def score_prompt(query: str, doc: str) -> str:
    return (
        "判断文档与查询的相关性，只输出一个 0 到 100 的整数，不要解释。\n"
        f"查询：{query}\n文档：{doc}\n分数："
    )


def post(path: str, payload: dict) -> dict:
    start = time.perf_counter()
    resp = httpx.post(f"{BASE}{path}", json=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    data["_elapsed"] = round(time.perf_counter() - start, 2)
    return data


print("=== A: generate + think=false + yes/no ===")
for label, doc in DOCS.items():
    d = post("/api/generate", {
        "model": MODEL,
        "prompt": yesno_prompt(QUERY, doc),
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 8},
    })
    print(f"  [{label}] {d['_elapsed']:5.2f}s response={d.get('response')!r} thinking={d.get('thinking')!r}")

print()
print("=== B: generate + think=false + 0-100 分数 ===")
for label, doc in DOCS.items():
    d = post("/api/generate", {
        "model": MODEL,
        "prompt": score_prompt(QUERY, doc),
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 16},
    })
    print(f"  [{label}] {d['_elapsed']:5.2f}s response={d.get('response')!r}")

print()
print("=== C: chat + think=false + yes/no ===")
for label, doc in DOCS.items():
    d = post("/api/chat", {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYS_YESNO},
            {"role": "user", "content": yesno_prompt(QUERY, doc)},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 8},
    })
    msg = d.get("message", {})
    print(f"  [{label}] {d['_elapsed']:5.2f}s content={msg.get('content')!r} thinking={msg.get('thinking')!r}")

print()
print("=== 原始响应键（方案 A 相关文档）===")
d = post("/api/generate", {
    "model": MODEL, "prompt": yesno_prompt(QUERY, DOCS["相关"]),
    "stream": False, "think": False, "options": {"temperature": 0, "num_predict": 8},
})
print(json.dumps({k: v for k, v in d.items() if k != "_elapsed"}, ensure_ascii=False))
