"""检索调参文件加载测试。"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings, load_retriever_overrides

from packages.contracts import RetrieveMode

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_loads_repo_default_tuning_file() -> None:
    overrides = load_retriever_overrides(REPO_ROOT / "configs" / "retrievers" / "default.yaml")
    assert overrides["rrf_k"] == 60
    assert overrides["retrieve_mode"] == "hybrid"
    assert overrides["rerank_enabled"] is False


def test_missing_file_returns_empty() -> None:
    assert load_retriever_overrides(REPO_ROOT / "configs" / "retrievers" / "nope.yaml") == {}


def test_unknown_keys_are_ignored(tmp_path) -> None:
    path = tmp_path / "tuning.yaml"
    path.write_text(
        "retrieval:\n  top_k: 7\n  milvus_uri: http://evil:19530\n  password: secret\n",
        encoding="utf-8",
    )
    overrides = load_retriever_overrides(path)
    assert overrides == {"top_k": 7}, "白名单外的键（含连接串/口令）不得被接受"


def test_overrides_take_precedence_over_env(tmp_path, monkeypatch) -> None:
    path = tmp_path / "tuning.yaml"
    path.write_text("retrieval:\n  top_k: 9\n  rerank_enabled: true\n", encoding="utf-8")

    settings = Settings(top_k=3, rerank_enabled=False, retriever_config_path=str(path))
    effective = settings.with_overrides()
    assert effective.top_k == 9
    assert effective.rerank_enabled is True
    # 未覆盖的字段保持 .env 的值
    assert settings.top_k == 3


def test_malformed_yaml_falls_back_to_env(tmp_path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("retrieval: [unclosed\n", encoding="utf-8")
    assert load_retriever_overrides(path) == {}


def test_default_mode_falls_back_to_hybrid() -> None:
    assert Settings(retrieve_mode="bogus").default_mode() is RetrieveMode.hybrid
    assert Settings(retrieve_mode="keyword").default_mode() is RetrieveMode.keyword
