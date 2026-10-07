"""OpenSearch（BM25 全文检索）适配。"""

from packages.search.config import OpenSearchSettings
from packages.search.opensearch import OpenSearchStore

__all__ = ["OpenSearchSettings", "OpenSearchStore"]
