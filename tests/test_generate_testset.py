"""评估集生成器的纯函数单元测试（不调用模型、不连 Milvus）。"""

import json

from evaluation.generate_testset import (
    _extract_json_array,
    _has_invented_error_code,
    _is_lexical,
    _looks_answerable,
    _normalize,
    load_existing,
    write_testset,
)


# ---------------- 归一化 ----------------


def test_normalize_strips_whitespace_and_punctuation():
    assert _normalize("ERR-4041 表示\n令牌已过期。") == "ERR-4041表示令牌已过期"


def test_normalize_makes_comparison_punctuation_insensitive():
    """模型摘录时可能带/不带标点，归一化后应能匹配。"""
    assert _normalize("令牌已过期。") == _normalize("令牌已过期")


# ---------------- JSON 抽取 ----------------


def test_extract_json_array_parses_plain_array():
    raw = '[{"q": "ERR-4041 是什么", "a": "令牌已过期"}]'
    assert _extract_json_array(raw)[0]["q"] == "ERR-4041 是什么"


def test_extract_json_array_tolerates_surrounding_text():
    """模型常加解释文字或代码块围栏。"""
    raw = '好的，结果如下：\n```json\n[{"q": "a", "a": "b"}]\n```\n希望有帮助'
    assert _extract_json_array(raw) == [{"q": "a", "a": "b"}]


def test_extract_json_array_returns_empty_on_invalid():
    assert _extract_json_array("这不是 JSON") == []
    assert _extract_json_array('[{"q": "a",}]') == []


def test_extract_json_array_ignores_non_dict_items():
    raw = '["字符串", {"q": "a", "a": "b"}, 42]'
    assert _extract_json_array(raw) == [{"q": "a", "a": "b"}]


# ---------------- 类型判定 ----------------


def test_is_lexical_only_for_error_codes():
    """错误码才是字面匹配强项；版本号提问本质是语义化的。"""
    assert _is_lexical("ERR-4041 是什么原因？") is True
    assert _is_lexical("升级 v1.3.0 后被强制退出登录是怎么回事？") is False
    assert _is_lexical("令牌过期了怎么办？") is False


# ---------------- 负样本领域隔离 ----------------


def test_looks_answerable_detects_domain_terms():
    assert _looks_answerable("VPN 连不上怎么办？") is True
    assert _looks_answerable("登录时报 ERR-7001 怎么处理？") is True


def test_unrelated_question_is_not_answerable():
    assert _looks_answerable("年假有几天，怎么申请？") is False
    assert _looks_answerable("班车几点发车？") is False


# ---------------- 编造标识校验 ----------------


def test_invented_error_code_is_detected():
    """模型会编造知识库里没有的错误码，这类样本必须剔除。"""
    content = "ERR-7001 表示个人证书已过期，证书有效期为 12 个月。"

    assert _has_invented_error_code("ERR-EXPIRE-NOTICE 是什么意思？", content) is True
    assert _has_invented_error_code("ERR-7001 是什么意思？", content) is False
    assert _has_invented_error_code("证书过期了怎么办？", content) is False


def test_version_abbreviation_is_not_flagged():
    """版本号是连续区间，「v5.1」属于对「v5.0.1 至 v5.2.0」的合理简写，不应误杀。"""
    content = "本手册适用于终端防护客户端 v5.0.1 至 v5.2.0。"

    assert _has_invented_error_code("我现在的客户端是 v5.1 版本，能用这本手册吗？", content) is False


# ---------------- 落盘 ----------------


def test_write_testset_numbers_manual_and_synthetic_separately(tmp_path):
    items = [
        {
            "id": "",
            "type": "semantic",
            "question": "q1",
            "reference_answer": "a1",
            "expected_chunk_ids": ["c1"],
            "source": "manual",
        },
        {
            "id": "",
            "type": "negative",
            "question": "n1",
            "reference_answer": "a2",
            "expected_chunk_ids": [],
            "source": "synthetic",
        },
        {
            "id": "",
            "type": "lexical",
            "question": "q2",
            "reference_answer": "a3",
            "expected_chunk_ids": ["c2"],
            "source": "synthetic",
        },
    ]

    path = tmp_path / "testset.jsonl"
    write_testset(path, items)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert [row["id"] for row in rows] == ["Q01", "S001", "S002"]


def test_load_existing_skips_blank_and_malformed_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"id": "Q01", "type": "semantic", "question": "a", "reference_answer": "b", "expected_chunk_ids": [], "source": "manual"}\n'
        "\n"
        "{坏行}\n",
        encoding="utf-8",
    )

    assert len(load_existing(path)) == 1
