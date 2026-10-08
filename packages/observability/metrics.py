"""进程内指标采集与 Prometheus 文本导出。

**为什么不引 prometheus_client**：本项目需要的只是「计数器 / 仪表 / 直方图」三种原语
和一段文本曝露格式，自己实现约 150 行且完全可控；引一个依赖会让镜像多一份供应链风险，
而它真正值钱的部分（多进程聚合、pushgateway、自定义 collector）我们都没用到。
取舍与本项目自写 RAGAS 裁判适配器一致。

**为什么必须做路径归一化**：`/documents/d_830daea6` 这类路径如果原样当标签，
每个文档都会生成一个时间序列，几次爬取就能把 Prometheus 打爆（基数爆炸）。
所以标签取**路由模板**（`/documents/{doc_id}`）而不是实际 URL；匹配不到路由的一律
归到 `<unmatched>`。

指标语义（支撑验收里的可用性 / 错误率 / P95 / 拒答率）：

    http_requests_total{service,method,route,status}        请求计数，按状态码分桶 → 可用性与错误率
    http_request_duration_seconds{service,method,route}     延迟直方图 → P95/P99
    http_requests_in_progress{service}                      并发请求数 → 拥塞判断
    rag_answer_total{outcome}                               answered/refused/error → 拒答率与错误率
    rag_cache_lookups_total{result}                         缓存命中率
    rag_acl_missing_total                                   检索未携带 ACL 的次数（**生产必须为 0**）
"""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

# 延迟分桶：从 5ms 到 30s。本系统 P50 约 2s（本地模型），所以 1~5s 之间给足分辨率；
# 下限 5ms 用于捕捉缓存命中这类快速路径。
_LATENCY_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

# 自观测路径不计入指标，否则抓取本身会污染延迟分布与请求计数
_EXCLUDED_PATHS = {"/metrics"}


def _escape_label(value: str) -> str:
    """按 Prometheus 文本格式转义标签值。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float) -> str:
    """整数值不带小数点（更贴近计数器语义，也便于人读）。"""
    if value == int(value):
        return str(int(value))
    return repr(value)


def _label_key(label_names: tuple[str, ...], labels: Mapping[str, str] | None) -> tuple[str, ...]:
    """把标签字典压成可哈希的键。

    缺标签补空串而不是报错：指标采集不该让业务请求失败——观测代码把请求打挂
    比没有指标更糟。
    """
    if not label_names:
        return ()
    labels = labels or {}
    return tuple(str(labels.get(name, "")) for name in label_names)


def _render_labels(labels: Mapping[str, str], extra: Mapping[str, str] | None = None) -> str:
    items = dict(labels)
    if extra:
        items.update(extra)
    if not items:
        return ""
    body = ",".join(f'{key}="{_escape_label(str(val))}"' for key, val in sorted(items.items()))
    return "{" + body + "}"


class Counter:
    """单调递增计数器。"""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, labels: Mapping[str, str] | None = None, value: float = 1.0) -> None:
        key = _label_key(self.label_names, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def lines(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            labels = dict(zip(self.label_names, key, strict=True))
            out.append(f"{self.name}{_render_labels(labels)} {_format_value(value)}")
        return out

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


class Gauge:
    """可增可减的瞬时值。"""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def set(self, labels: Mapping[str, str] | None = None, value: float = 0.0) -> None:  # noqa: A003
        with self._lock:
            self._values[_label_key(self.label_names, labels)] = value

    def inc(self, labels: Mapping[str, str] | None = None, value: float = 1.0) -> None:
        key = _label_key(self.label_names, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def dec(self, labels: Mapping[str, str] | None = None, value: float = 1.0) -> None:
        self.inc(labels, -value)

    def lines(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            labels = dict(zip(self.label_names, key, strict=True))
            out.append(f"{self.name}{_render_labels(labels)} {_format_value(value)}")
        return out

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


class Histogram:
    """累积直方图（Prometheus 的 ``*_bucket`` 语义）。

    只保留桶计数与 sum/count：分位数由 Prometheus 端用 ``histogram_quantile`` 算，
    不在进程内算分位数——进程内算分位数既不能跨副本聚合，也丢掉了分布形状。
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: Iterable[float] = _LATENCY_BUCKETS,
    ) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._totals: dict[tuple[str, ...], int] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, labels: Mapping[str, str] | None = None) -> None:
        key = _label_key(self.label_names, labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._totals[key] = self._totals.get(key, 0) + 1

    def lines(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self._lock:
            keys = sorted(self._counts)
            snapshot = {
                key: (list(self._counts[key]), self._sums.get(key, 0.0), self._totals.get(key, 0))
                for key in keys
            }
        for key in keys:
            counts, total_sum, total_count = snapshot[key]
            labels = dict(zip(self.label_names, key, strict=True))
            for index, bound in enumerate(self.buckets):
                extra = {"le": _format_value(bound)}
                out.append(f"{self.name}_bucket{_render_labels(labels, extra)} {counts[index]}")
            # +Inf 桶等于总观测数，必须显式输出
            out.append(f"{self.name}_bucket{_render_labels(labels, {'le': '+Inf'})} {total_count}")
            out.append(f"{self.name}_sum{_render_labels(labels)} {_format_value(total_sum)}")
            out.append(f"{self.name}_count{_render_labels(labels)} {total_count}")
        return out

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()
            self._sums.clear()
            self._totals.clear()


class MetricsRegistry:
    """一组指标的集合，负责拼装成 Prometheus 文本曝露格式。"""

    def __init__(self) -> None:
        self.counters: dict[str, Counter] = {}
        self.gauges: dict[str, Gauge] = {}
        self.histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> Counter:
        with self._lock:
            if name not in self.counters:
                self.counters[name] = Counter(name, help_text, label_names)
            return self.counters[name]

    def gauge(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> Gauge:
        with self._lock:
            if name not in self.gauges:
                self.gauges[name] = Gauge(name, help_text, label_names)
            return self.gauges[name]

    def histogram(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: Iterable[float] = _LATENCY_BUCKETS,
    ) -> Histogram:
        with self._lock:
            if name not in self.histograms:
                self.histograms[name] = Histogram(name, help_text, label_names, buckets)
            return self.histograms[name]

    def _all(self) -> list[Counter | Gauge | Histogram]:
        """三种指标一起处理。

        显式标注返回类型：否则会被推断成 ``list[object]``，`.lines()` 就过不了类型检查。
        """
        return [*self.counters.values(), *self.gauges.values(), *self.histograms.values()]

    def expose(self) -> str:
        lines: list[str] = []
        for metric in self._all():
            lines.extend(metric.lines())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """清空全部指标。仅测试用：同一个进程里跑多个 app 时，
        累积值会让断言依赖执行顺序。"""
        for metric in self._all():
            metric.clear()


# ---------------- 进程内单例 ----------------
# 每个服务是独立进程，所以各进程各有一份；不做多进程聚合（那需要 pushgateway 或
# 暴露在同一个进程里，属于部署形态问题，不是这里的职责）。
REGISTRY = MetricsRegistry()


def _http_route_template(request: Request) -> str:
    """取路由模板作为标签值。

    优先用 Starlette 匹配到的路由模板（``/documents/{doc_id}``）；
    未匹配到（404、或中间件比路由更外层时）退回 ``<unmatched>``，
    **绝不用原始 URL**——那会让每个 document_id / 查询串都变成独立时间序列。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    return "<unmatched>"


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录请求计数、延迟直方图与在途请求数。

    放在**最外层**（最后添加），这样 401/429 这类被拒请求也会被计入——
    可用性与错误率必须覆盖"被拒"这一大类，否则监控会漏掉最该告警的部分。
    """

    def __init__(self, app: Any, *, service: str, registry: MetricsRegistry | None = None) -> None:
        super().__init__(app)
        self.service = service
        self.registry = registry or REGISTRY
        self._requests = self.registry.counter(
            "http_requests_total",
            "HTTP 请求总数（按服务/方法/路由/状态码）",
            ("service", "method", "route", "status"),
        )
        self._duration = self.registry.histogram(
            "http_request_duration_seconds",
            "HTTP 请求耗时（秒）",
            ("service", "method", "route"),
        )
        self._in_progress = self.registry.gauge(
            "http_requests_in_progress", "当前在处理中的请求数", ("service",)
        )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path in _EXCLUDED_PATHS:
            return await call_next(request)

        labels = {"service": self.service}
        self._in_progress.inc(labels)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            # 异常也要计数，否则 5xx 在指标里会凭空消失（错误率偏低 = 假达标）
            self._requests.inc(
                {
                    "service": self.service,
                    "method": request.method,
                    "route": _http_route_template(request),
                    "status": "500",
                }
            )
            self._duration.observe(
                time.perf_counter() - started,
                {
                    "service": self.service,
                    "method": request.method,
                    "route": _http_route_template(request),
                },
            )
            raise
        finally:
            self._in_progress.dec(labels)

        route = _http_route_template(request)
        self._duration.observe(
            time.perf_counter() - started,
            {"service": self.service, "method": request.method, "route": route},
        )
        self._requests.inc(
            {
                "service": self.service,
                "method": request.method,
                "route": route,
                "status": str(status),
            }
        )
        return response


# ---------------- 领域指标 ----------------
# 定义在共享模块里，保证名字与语义在各服务间一致（各服务自己起名必然漂移）。

ANSWER_COUNTER = REGISTRY.counter(
    "rag_answer_total",
    "问答结果计数（answered=作答 / refused=拒答 / error=失败）",
    ("outcome",),
)

CACHE_LOOKUP_COUNTER = REGISTRY.counter(
    "rag_cache_lookups_total",
    "查询缓存查找次数（hit/miss/skip）",
    ("result",),
)

ACL_MISSING_COUNTER = REGISTRY.counter(
    "rag_acl_missing_total",
    "检索请求未携带 ACL 的次数——生产环境必须恒为 0，否则是权限下推链路断裂",
)

# 提示注入尝试。计数**不表示攻击得逞**——检测只负责让它可见（见
# packages/security/injection.py 的取舍说明：检测不阻断）。
INJECTION_SUSPECTED_COUNTER = REGISTRY.counter(
    "rag_injection_suspected_total",
    "疑似提示注入次数（按来源与规则分类）",
    ("source", "rule"),
)

# LLM 成本（美元）。token 数不等于钱：不同模型差一个数量级，
# 只有折算成金额才能回答"这个功能每天烧多少钱""该不该给它换个便宜模型"。
LLM_COST_COUNTER = REGISTRY.counter(
    "rag_llm_cost_usd_total",
    "LLM 调用累计成本（美元，按模型单价折算）",
    ("model", "tenant"),
)


def metrics_endpoint(registry: MetricsRegistry | None = None, token: str = ""):
    """构造 ``/metrics`` 端点。

    ``token`` 非空时要求 ``Authorization: Bearer <token>``：指标会暴露内部路由与流量形态，
    在没有网络隔离的环境下不应裸奔。默认空 = 不校验（本地开发）。
    """
    reg = registry or REGISTRY

    async def _metrics(request: Request) -> Response:
        if token:
            header = request.headers.get("authorization", "")
            if header != f"Bearer {token}":
                return PlainTextResponse("forbidden\n", status_code=403)
        return PlainTextResponse(reg.expose(), media_type="text/plain; version=0.0.4")

    return _metrics


def install_metrics(
    app: Any,
    service: str,
    *,
    settings: Any | None = None,
    token: str = "",
    registry: MetricsRegistry | None = None,
) -> bool:
    """给 FastAPI app 挂上指标中间件与 ``/metrics`` 端点；返回是否真的挂载。

    ``settings`` 用鸭子类型读取（只需要 ``metrics_enabled`` / ``metrics_token`` 两个字段），
    这样调用方一行就能接入，不必在每个服务里重复解包配置：

        install_metrics(app, SERVICE_RETRIEVAL, settings=settings)

    注意中间件顺序：starlette **越晚添加越靠外层**，所以本函数要在
    其它 ``add_middleware`` 之后调用，才能把被拒请求也统计进去。
    """
    if settings is not None:
        if not bool(getattr(settings, "metrics_enabled", True)):
            return False
        token = token or str(getattr(settings, "metrics_token", "") or "")

    reg = registry or REGISTRY
    app.add_middleware(MetricsMiddleware, service=service, registry=reg)
    app.add_route("/metrics", metrics_endpoint(reg, token), include_in_schema=False)
    return True


__all__ = [
    "ACL_MISSING_COUNTER",
    "ANSWER_COUNTER",
    "CACHE_LOOKUP_COUNTER",
    "INJECTION_SUSPECTED_COUNTER",
    "MetricsMiddleware",
    "MetricsRegistry",
    "REGISTRY",
    "install_metrics",
    "metrics_endpoint",
]
