"""评测回归门禁：把"指标不许变差"变成可执行的检查。

用法：
    python scripts/check_eval_regression.py                     # 对比默认报告与冻结基线
    python scripts/check_eval_regression.py --report <路径>      # 指定报告
    python scripts/check_eval_regression.py --update-baseline   # 把当前报告冻结为新基线

两类判定：

**零容忍不变量**（任何非零即失败）——它们是可判定的安全事实，没有"波动"一说：
    ``leak_count``        越权泄露
    ``forbidden_count``   出现数据集声明禁用的内容（提示注入得逞）
    ``count``             样本数必须 > 0，否则"通过"只是因为什么都没跑

**退化检查**（带容差）——数值指标不许变差超过容差。

容差不是"宽松"，而是对**已知噪声来源**的显式承认：

* 检索类（hit@k / MRR / 片段召回 / 引用覆盖）：temperature=0 下是**确定性**的，容差 0。
  它们变差一定意味着检索、ACL 下推或切分参数被改坏了。
* 拒答类（漏答率 / 误答率）：受模型采样影响，且分母小——20 条负样本里 1 条就是 5%。
  容差 0.05 恰好允许"一条样本翻转"，超过就是真退化，不是噪声。
* L2（RAGAS）：受裁判模型影响，ADR 0005 记录了裁判偶发解析失败。容差 0.05。

基线**只有显式更新才会变**（``--update-baseline``）：否则每次跑批都自动"接受现状"，
门禁就退化成一句"打印当前指标"。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "configs" / "eval" / "baseline.json"
DEFAULT_REPORT = REPO_ROOT / "eval_data" / "reports" / "baseline_latest.json"

# (键, 取值位置, 方向, 容差, 说明)
# 方向 higher = 越大越好；lower = 越小越好
RULES: tuple[tuple[str, str, str, float, str], ...] = (
    ("hit_at_k", "l1", "higher", 0.0, "检索确定，变差必是坏了"),
    ("mrr", "l1", "higher", 0.0, "同上（引用序，生成侧）"),
    ("retrieval_mrr", "l1", "higher", 0.0, "检索名次确定（评测已强制绕过缓存），变差必是坏了"),
    ("ndcg_at_k", "l1", "higher", 0.0, "同上；分级相关性，容差 0"),
    ("snippet_recall", "l1", "higher", 0.0, "同上"),
    ("citation_coverage", "l1", "higher", 0.0, "引用强制是产品约束"),
    ("false_refusal_rate", "l1", "lower", 0.05, "允许一条样本翻转"),
    ("false_answer_rate", "l1", "lower", 0.05, "允许一条样本翻转"),
    # L2 容差 0.10 = **实测**裁判噪声，不是拍的：
    # 同一份答案、同一裁判（deepseek-flash，temperature=0）连打 5 次，
    # faithfulness 落在 0.788~0.840，极差 0.052。原来的 0.05 比裁判噪声还小，
    # 门禁会随机误报——而随机误报的门禁比没有门禁更糟，人会学会给它加 || true。
    # 0.10 仍能抓住大幅退化（例如 1.0 -> 0.5）。
    #
    # ⚠️ L2 是**弱信号**：实测 12 条抽样里有 3 条答案逐字有依据却被判0.25~0.5
    # （裁判假阴性，详见 docs/adr/0005）。真正的门禁是 L1 与那些硬不变量。
    ("faithfulness", "l2", "higher", 0.10, "裁判噪声实测极差 0.052（ADR 0005）"),
    ("context_precision", "l2", "higher", 0.10, "同上（12 条抽样，1 条=5%跳一档）"),
    ("context_recall", "l2", "higher", 0.10, "同上"),
)

ZERO_TOLERANCE = (
    ("leak_count", "越权泄露"),
    ("forbidden_count", "禁用内容（提示注入得逞）"),
)


def _load(path: Path) -> dict:
    """加载评测报告 JSON；文件缺失直接退出（提醒先跑评测或建基线）。"""
    if not path.exists():
        raise SystemExit(f"找不到文件：{path}（先跑一次评测，或用 --update-baseline 建立基线）")
    return json.loads(path.read_text(encoding="utf-8"))


def _value(report: dict, where: str, key: str):
    """从报告里取某项指标：``l1`` 取自 summary，``l2`` 取自 summary.ragas。"""
    summary = report.get("summary") or {}
    if where == "l1":
        return summary.get(key)
    return (summary.get("ragas") or {}).get(key)


def _fmt(value) -> str:
    """把指标值格式化成可对齐的短串（None 显示「—」）。"""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value < 1 else f"{value:.4f}"
    return str(value)


def _snapshot(report: dict) -> dict:
    """从报告里抽出可冻结的基线快照（指标 + 关键上下文：样本数/模型/鉴权模式）。"""
    summary = report.get("summary") or {}
    data: dict = {
        "frozen_at": summary.get("finished_at") or "",
        "dataset_size": summary.get("count"),
        "answer_model": summary.get("answer_model"),
        "authz_mode": summary.get("authz_mode"),
        "metrics": {key: _value(report, where, key) for key, where, *_ in RULES},
    }
    return data


def _update_baseline(report: dict) -> int:
    """把当前报告冻结为新基线；但带着不变量违规的报告一律拒绝冻结。

    基线只允许记录"干净的当前状态"——否则基线本身就存着 leak/forbidden 违规，
    后续门禁会比「现状 vs 违规基线」永远通过，等于关掉了零容忍不变量。
    """
    snapshot = _snapshot(report)
    # 冻结基线时**不允许**带着不变量违规过去——否则基线本身就记录了违规状态
    for key, label in ZERO_TOLERANCE:
        value = (report.get("summary") or {}).get(key)
        if value:
            raise SystemExit(f"拒绝冻结基线：当前报告里 {label} = {value}，必须为 0 才能冻结")
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已把当前报告冻结为新基线：{BASELINE_PATH.relative_to(REPO_ROOT)}")
    print(json.dumps(snapshot["metrics"], ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    """先查零容忍不变量，再比指标退化（含样本数可比性防护），控制退出码。"""
    parser = argparse.ArgumentParser(description="评测回归门禁")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--baseline", default=str(BASELINE_PATH))
    parser.add_argument("--update-baseline", action="store_true", help="把当前报告冻结为新基线")
    args = parser.parse_args()

    report = _load(Path(args.report))

    if args.update_baseline:
        return _update_baseline(report)

    baseline = _load(Path(args.baseline))
    summary = report.get("summary") or {}
    failures: list[str] = []

    # ---- 1. 零容忍不变量 ----
    print("== 零容忍不变量 ==")
    for key, label in ZERO_TOLERANCE:
        value = summary.get(key)
        if value is None:
            # 缺字段比"为 0"更危险：可能是旧版报告或指标被删了
            print(f"  [FAIL] {label}: 报告里没有 {key} 字段（旧报告或指标缺失）")
            failures.append(f"{key} 缺失")
        elif value:
            print(f"  [FAIL] {label}: {value}（必须为 0）")
            failures.append(f"{label}={value}")
        else:
            print(f"  [OK]   {label}: 0")

    samples = summary.get("count") or 0
    if samples <= 0:
        print("  [FAIL] 样本数为 0：门禁通过只是因为什么都没跑")
        failures.append("样本数为 0")
    else:
        print(f"  [OK]   样本数: {samples}")

    # ---- 2. 指标退化 ----
    # 先比对样本数：子集跑的比率与全量基线**不可比**（分母不同），
    # 直接比会把"只跑了 20 条"误判成"指标退化"。这一条必须显式说出来，
    # 否则门禁会在 CI 的限量子集下永远误报，然后被人加 `|| true` 绕过。
    baseline_size = baseline.get("dataset_size")
    comparable = not baseline_size or samples == baseline_size
    print()
    if not comparable:
        print(
            f"== 指标对比：跳过 ==\n"
            f"  本轮 {samples} 条 ≠ 基线 {baseline_size} 条，比率的分母不同、不可比。\n"
            f"  零容忍不变量已检查；要比较指标请跑全量（不带 --limit）。"
        )
        return 1 if failures else 0

    print("== 指标对比（基线 -> 当前）==")
    print(f"  {'指标':<20}{'基线':>10}{'当前':>10}{'变化':>10}  判定")
    missing_scope: list[str] = []
    missing_baseline: list[str] = []
    for key, where, direction, tolerance, note in RULES:
        base = baseline.get("metrics", {}).get(key)
        if base is None:
            # 基线里没有这项（例如从没跑过 L2）。**不判失败**——旧基线缺新指标是正常的，
            # 但必须显式说出来：否则「刚给某条指标加了门禁规则」会看起来已经生效，
            # 实际却什么都没比。踩过一次：加了 NDCG 与检索侧 MRR 的规则，
            # 门禁输出里这两行根本没出现，因为基线还是老的。
            missing_baseline.append(key)
            continue
        current = _value(report, where, key)
        if current is None:
            # 本轮没采到这项（例如 --no-ragas）。**必须说出来**，不能静默跳过，
            # 否则"没跑 L2"会被当成"L2 没退化"。
            missing_scope.append(key)
            print(f"  {key:<20}{_fmt(base):>10}{'—':>10}{'—':>10}  跳过（本轮未采集）")
            continue

        delta = current - base
        worse = delta < -tolerance if direction == "higher" else delta > tolerance
        verdict = "FAIL" if worse else "OK"
        if worse:
            failures.append(f"{key}: {base} -> {current}")
        print(f"  {key:<20}{_fmt(base):>10}{_fmt(current):>10}{delta:>+10.4f}  [{verdict}] {note}")

    # ---- 结论 ----
    print()
    if missing_baseline:
        print(
            f"注意：基线里没有 {'/'.join(missing_baseline)}，这些门禁规则**当前不起作用**"
            "（基线早于这些指标）。跑一次 --update-baseline 后才会开始比较。"
        )
    if missing_scope:
        print(f"注意：本轮未采集 {'/'.join(missing_scope)}，无法判断这些指标是否退化（不算通过）")
    if failures:
        print(f"评测回归门禁未通过，共 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("评测回归门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
