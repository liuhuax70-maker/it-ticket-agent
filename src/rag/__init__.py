"""permission-aware-rag 核心包。

分层与依赖方向（铁律，见开发流程文档 §4）::

    data → indexing → retrieval → generation → api

上层可调用下层，下层绝不反向 import 上层；``config`` 可被任意层读取。
"""

__version__ = "0.1.0"
