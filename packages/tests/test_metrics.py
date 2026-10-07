"""指标模块测试。

重点在**基数控制**与**计数完整性**两件事：
    1. 标签必须取路由模板而非实际 URL——否则 /documents/<每个文档> 都会生成一条时间序列；
    2. 被拒请求（401/429/404）与抛异常的请求都必须被计入——漏掉它们会让错误率偏低，
       而"错误率偏低"正是监控造假的最常见形式。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from packages.observability.metrics import (
    Counter,
    Histogram,
    MetricsRegistry,
    install_metrics,
)


def _app(registry: MetricsRegistry, *, token: str = "") -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=500, detail="炸了")

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("未捕获异常")

    install_metrics(app, "test-svc", token=token, registry=registry)
    return app


def _series(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(needle)]


def test_route_label_uses_template_not_raw_path() -> None:
    """两个不同的 item_id 必须落到**同一条**时间序列（基数控制）。"""
    registry = MetricsRegistry()
    client = TestClient(_app(registry))
    client.get("/items/d_aaa")
    client.get("/items/d_bbb")

    body = client.get("/metrics").text
    rows = _series(body, "http_requests_total{")
    assert len(rows) == 1, f"期望合并为一条序列，实际: {rows}"
    assert 'route="/items/{item_id}"' in rows[0]
    assert "d_aaa" not in body and "d_bbb" not in body, "标签里绝不能出现真实 ID"
    assert 'status="200"' in rows[0]


def test_unmatched_path_is_collapsed() -> None:
    """404 的随机 URL 不能各占一条序列。"""
    registry = MetricsRegistry()
    client = TestClient(_app(registry))
    client.get("/nope/1")
    client.get("/nope/2")

    rows = _series(client.get("/metrics").text, "http_requests_total{")
    assert len(rows) == 1
    assert 'route="<unmatched>"' in rows[0]
    assert 'status="404"' in rows[0]


def test_explicit_http_error_is_counted_with_real_status() -> None:
    registry = MetricsRegistry()
    client = TestClient(_app(registry), raise_server_exceptions=False)
    client.get("/boom")

    body = client.get("/metrics").text
    assert any(
        'status="500"' in line and 'route="/boom"' in line
        for line in _series(body, "http_requests_total{")
    )


def test_unhandled_exception_is_counted_as_500() -> None:
    """未捕获异常若不计数，5xx 会在指标里凭空消失——错误率就假达标了。"""
    registry = MetricsRegistry()
    client = TestClient(_app(registry), raise_server_exceptions=False)
    response = client.get("/crash")
    assert response.status_code == 500

    body = client.get("/metrics").text
    assert any(
        'status="500"' in line and 'route="/crash"' in line
        for line in _series(body, "http_requests_total{")
    )
    # 抛异常的请求同样要进延迟直方图，否则延迟分布里只有成功请求（P95 会假优）
    assert any(
        'route="/crash"' in line for line in _series(body, "http_request_duration_seconds_count{")
    )


def test_duration_histogram_is_cumulative_with_inf_bucket() -> None:
    registry = MetricsRegistry()
    client = TestClient(_app(registry))
    client.get("/items/1")
    client.get("/items/2")

    body = client.get("/metrics").text
    buckets = _series(body, "http_request_duration_seconds_bucket{")
    assert any('le="+Inf"' in row for row in buckets), "必须显式输出 +Inf 桶"
    # 桶计数单调不减，且 +Inf 等于总观测数
    counts = [int(row.rsplit(" ", 1)[1]) for row in buckets]
    assert counts == sorted(counts), "累积桶必须单调不减"
    assert counts[-1] == 2
    # 标签按字母序输出（刻意的：保证同一组标签每次曝露的顺序一致，便于 diff 与断言）
    assert (
        'http_request_duration_seconds_count{method="GET",route="/items/{item_id}",'
        'service="test-svc"} 2' in body
    )
    assert _series(body, "http_request_duration_seconds_sum{")


def test_metrics_endpoint_is_excluded_from_its_own_counters() -> None:
    registry = MetricsRegistry()
    client = TestClient(_app(registry))
    client.get("/items/1")
    client.get("/metrics")
    client.get("/metrics")

    rows = _series(client.get("/metrics").text, "http_requests_total{")
    assert all("/metrics" not in row for row in rows), "抓取自身不应产生序列"


def test_in_progress_gauge_returns_to_zero() -> None:
    registry = MetricsRegistry()
    client = TestClient(_app(registry))
    client.get("/items/1")
    body = client.get("/metrics").text
    assert 'http_requests_in_progress{service="test-svc"} 0' in body


def test_metrics_token_is_enforced_when_configured() -> None:
    """指标会暴露内部路由与流量形态，配置了 token 就必须校验。"""
    registry = MetricsRegistry()
    client = TestClient(_app(registry, token="s3cret"))
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 403
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_label_values_are_escaped() -> None:
    """标签值含引号/反斜杠时必须转义，否则整个曝露格式被破坏、抓取端解析报错。"""
    registry = MetricsRegistry()
    counter = registry.counter("probe_total", "探针", ("path",))
    counter.inc({"path": 'a"b\\c'})
    body = registry.expose()
    assert 'path="a\\"b\\\\c"' in body


def test_counter_reset_clears_values() -> None:
    counter = Counter("c_total", "计数")
    counter.inc()
    assert any(row.startswith("c_total ") for row in counter.lines())
    counter.clear()
    assert not any(row.startswith("c_total ") for row in counter.lines())


def test_histogram_observe_boundaries() -> None:
    """边界值归入 `<=` 的桶（Prometheus 语义），别写成 `<`。"""
    histogram = Histogram("h_seconds", "耗时", ("k",), buckets=(0.1, 1.0))
    histogram.observe(0.1, {"k": "x"})
    body = "\n".join(histogram.lines())
    assert 'le="0.1"} 1' in body, "等于桶上界必须计入该桶"


# ---------------- 告警规则与指标的一致性 ----------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_alert_rules_only_reference_metrics_we_actually_export() -> None:
    """告警表达式里引用的每个指标名都必须真实存在。

    为什么值得为它写测试：告警配错指标名**不会报错**——表达式照常求值，只是永远
    匹配不到数据，于是告警永远不触发。等到出事时才发现"监控挂了"，是最坏的一种
    静默失效（与"越权泄露躲在 CACHE_ENABLED=false 后面"是同一类问题）。

    本测试从警报文件的 expr 里抽出形如 ``xxx_total`` / ``xxx_seconds`` 的标识符，
    逐个对照指标注册表（含直方图派生的 _bucket/_sum/_count）。
    """
    import yaml

    from packages.observability.metrics import REGISTRY

    # http_* 指标是在**中间件构造时**注册的，而 starlette 的中间件是惰性实例化的：
    # 不建一次栈，注册表里就只有模块级定义的那几个领域指标。
    # （这也说明 service 启动后指标才齐全——实测 /metrics 确有 http_requests_total。）
    app = FastAPI()
    install_metrics(app, "alert-check")
    app.build_middleware_stack()
    known = set(REGISTRY.counters) | set(REGISTRY.gauges) | set(REGISTRY.histograms)
    for name in list(known):
        known |= {f"{name}_bucket", f"{name}_sum", f"{name}_count"}

    path = _repo_root() / "infra" / "monitoring" / "alerts.yml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))

    exprs = [rule["expr"] for group in rules["groups"] for rule in group["rules"]]
    assert exprs, "告警文件里没有取到任何表达式，检查解析路径"

    pattern = re.compile(r"\b([a-z][a-z0-9_]*(?:_total|_seconds|_seconds_bucket|_count|_sum))\b")
    referenced = {m.group(1) for expr in exprs for m in pattern.finditer(expr)}
    # 上述正则会把 le/instance 之类排除掉；这里只校验"像指标"的标识符
    unknown = sorted(name for name in referenced if name not in known)
    assert not unknown, f"告警引用了不存在的指标（会永不触发）: {unknown}"

    # 反向检查：最该被监控的安全不变量不能漏
    assert any("rag_acl_missing_total" in expr for expr in exprs), (
        "缺少「无 ACL 请求」告警——这是权限系统的绝对不变量"
    )
