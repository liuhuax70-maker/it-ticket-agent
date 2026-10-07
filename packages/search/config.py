"""OpenSearch 配置。"""

from __future__ import annotations

from packages.common.settings import BaseAppSettings


class OpenSearchSettings(BaseAppSettings):
    service_name: str = "retrieval"

    opensearch_url: str = "http://localhost:9200"
    opensearch_index: str = "chunks"
    # standard 开箱即用；装了 IK 插件的集群改成 ik_max_word 可获得中文分词
    opensearch_analyzer: str = "standard"
    opensearch_username: str = ""
    opensearch_password: str = ""
    opensearch_timeout: float = 20.0
