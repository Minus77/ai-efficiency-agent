"""效果衡量（§19.4 业务指标、§9.3 落地结果）。

地基是一条硬规则：**无改造前基线的指标一律不采信**。
没有基线的"改善"无法证明是改造带来的，只能证明"现在是这个数"。

因此本模块的核心不是算改善率，而是**先拒绝算**：
- 指标不在白名单（尤其经营结果类）→ 拒
- 没有改造前基线 → 拒给任何改善率，只回报"测不了"
- 样本量过小 → 算但标注低置信

基线不可变：重复记录产生新版本并保留前版，客户复议时能翻账。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .clients import safe_slug
from .config import default_workspace_root
from .models import ResultCode

BASELINE_FILE = "baselines.json"
MEASURE_FILE = "measurements.json"

# 样本量低于此值标注低置信（不是拒绝，而是明码标出不确定性）
MIN_RELIABLE_SAMPLE = 20

# §19.4 只采信与场景强直接关联的过程指标
# value 表示"数值越高是否越好"，方向不能一刀切：
# 处理时长降低是改善，处理单量升高才是改善
ALLOWED_METRICS: dict[str, bool] = {
    "该环节处理时长": False,
    "该环节处理单量": True,
    "该环节返工率": False,
    "该环节错误率": False,
    "首响时长": False,
    "差异单量": False,
    "跟单响应延迟": False,
    "开票单量": True,
    "对账差异率": False,
}

# 明确不采信：营收波动原因太多，拿它校准会训出错误关联
BANNED_METRICS: tuple[str, ...] = (
    "营收", "利润", "利润率", "毛利", "人力成本占比", "客单价", "市场份额",
    "revenue", "profit", "margin",
)


def _base(root: Path | str | None, slug: str) -> Path | None:
    checked = safe_slug(slug)
    if checked is None:
        return None
    base = Path(root if root is not None else default_workspace_root()) / checked
    return base if (base / "client.json").exists() else None


def _validate_metric(metric: str) -> tuple[bool, str, str]:
    """返回 (是否合法, note, next_action)。"""
    for banned in BANNED_METRICS:
        if banned in metric or banned.lower() in metric.lower():
            return (
                False,
                f"「{metric}」属于经营结果指标，不予采信。营收波动的原因太多，"
                "拿它校准场景识别会训出错误关联。",
                f"请改用过程指标：{'、'.join(list(ALLOWED_METRICS)[:4])} 等",
            )
    if metric not in ALLOWED_METRICS:
        return (
            False,
            f"「{metric}」不在可采信的过程指标白名单内。",
            f"可用指标：{'、'.join(ALLOWED_METRICS)}",
        )
    return True, "", ""


def _read(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _write(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_value(
    value: float | None, timestamps: list[str] | None, metric: str
) -> tuple[float | None, int, str]:
    """把 value 或 timestamps 归一成 (数值, 样本量, 说明)。"""
    if value is not None:
        return float(value), 0, "由调用方直接提供"
    if timestamps:
        count = len(timestamps)
        if ALLOWED_METRICS.get(metric) is True or "单量" in metric:
            return float(count), count, f"由 {count} 条时间戳记录直接计数"
        # 时长类：用相邻间隔的中位数近似
        try:
            parsed = sorted(datetime.fromisoformat(t[:19]) for t in timestamps)
        except ValueError:
            return None, 0, "时间戳无法解析"
        gaps = [
            (b - a).total_seconds() / 60.0
            for a, b in zip(parsed, parsed[1:])
            if 0 < (b - a).total_seconds() / 60.0 <= 240
        ]
        if not gaps:
            return None, 0, "时间戳间隔无法推算"
        gaps.sort()
        median = gaps[len(gaps) // 2]
        return round(median, 2), len(gaps), f"由 {len(gaps)} 个相邻间隔的中位数推算"
    return None, 0, ""


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------
def capture_baseline(
    *,
    root: Path | str | None = None,
    slug: str,
    card_id: str,
    metric: str,
    value: float | None = None,
    timestamps: list[str] | None = None,
    sample_size: int | None = None,
    source: str,
    note: str = "",
) -> dict[str, Any]:
    """记录改造前基线。基线不可变：重复记录产生新版本，旧版保留。"""
    base = _base(root, slug)
    if base is None:
        return {"ok": False, "note": f"客户不存在：{slug}", "next_action": "请先建档"}

    ok, bad_note, bad_next = _validate_metric(metric)
    if not ok:
        return {"ok": False, "note": bad_note, "next_action": bad_next}

    resolved, derived_sample, how = _resolve_value(value, timestamps, metric)
    if resolved is None:
        return {
            "ok": False,
            "note": "既没给 value 也没给可用的 timestamps",
            "next_action": "请传入 value（直接数值）或 timestamps（由连接器拉取的时间戳列）",
        }

    final_sample = int(sample_size if sample_size is not None else derived_sample)
    path = base / BASELINE_FILE
    items = _read(path)
    version = sum(1 for i in items if i["card_id"] == card_id and i["metric"] == metric) + 1

    record = {
        "baseline_id": f"b-{card_id}-{metric}-{version}",
        "card_id": card_id,
        "metric": metric,
        "value": resolved,
        "sample_size": final_sample,
        "source": source,
        "how": how,
        "note": note,
        "version": version,
        "captured_at": date.today().isoformat(),
        "higher_is_better": ALLOWED_METRICS[metric],
    }
    items.append(record)
    _write(path, items)
    return {"ok": True, **record}


def list_baselines(*, root: Path | str | None = None, slug: str) -> list[dict[str, Any]]:
    base = _base(root, slug)
    return _read(base / BASELINE_FILE) if base else []


def latest_baseline(
    *, root: Path | str | None = None, slug: str, card_id: str, metric: str
) -> dict[str, Any] | None:
    matches = [
        b for b in list_baselines(root=root, slug=slug)
        if b["card_id"] == card_id and b["metric"] == metric
    ]
    return max(matches, key=lambda b: b["version"]) if matches else None


# ---------------------------------------------------------------------------
# 后测
# ---------------------------------------------------------------------------
def measure_effect(
    *,
    root: Path | str | None = None,
    slug: str,
    card_id: str,
    metric: str,
    value: float | None = None,
    timestamps: list[str] | None = None,
    sample_size: int | None = None,
    source: str,
    note: str = "",
) -> dict[str, Any]:
    """改造后复测并与基线对比。无基线一律不给改善率。"""
    base = _base(root, slug)
    if base is None:
        return {
            "code": ResultCode.INVALID_PARAMS.value,
            "improvement_pct": None,
            "direction": "无法判断",
            "note": f"客户不存在：{slug}",
            "next_action": "请先建档",
        }

    ok, bad_note, bad_next = _validate_metric(metric)
    if not ok:
        return {
            "code": ResultCode.INVALID_PARAMS.value,
            "improvement_pct": None,
            "direction": "无法判断",
            "note": bad_note,
            "next_action": bad_next,
        }

    resolved, derived_sample, how = _resolve_value(value, timestamps, metric)
    if resolved is None:
        return {
            "code": ResultCode.INVALID_PARAMS.value,
            "improvement_pct": None,
            "direction": "无法判断",
            "note": "既没给 value 也没给可用的 timestamps",
            "next_action": "请传入 value 或 timestamps",
        }

    final_sample = int(sample_size if sample_size is not None else derived_sample)
    measured = {
        "value": resolved,
        "sample_size": final_sample,
        "source": source,
        "how": how,
        "measured_at": date.today().isoformat(),
    }

    baseline = latest_baseline(root=root, slug=slug, card_id=card_id, metric=metric)
    if baseline is None:
        # 这是本模块最重要的分支：没有基线就不给改善率
        return {
            "code": ResultCode.INSUFFICIENT_DATA.value,
            "card_id": card_id,
            "metric": metric,
            "baseline": None,
            "measured": measured,
            "improvement_pct": None,
            "direction": "无法判断",
            "low_confidence": True,
            "note": (
                f"该环节没有改造前基线，只能说明「现在是 {resolved}」，"
                "无法证明这是改造带来的变化。无基线的改善一律不采信。"
            ),
            "next_action": (
                "改造前先调 capture_baseline 记一次基线；"
                "若改造已开始，可用连接器拉取改造前时间段的数据补录基线"
            ),
        }

    before = float(baseline["value"])
    higher_better = bool(baseline.get("higher_is_better", ALLOWED_METRICS[metric]))
    if before == 0:
        pct = None
        direction = "无法判断"
    else:
        raw = (resolved - before) / before * 100.0
        pct = round(raw if higher_better else -raw, 2)
        if abs(pct) < 1.0:
            direction = "基本持平"
        elif pct > 0:
            direction = "改善"
        else:
            direction = "退步"

    low_conf = (
        final_sample < MIN_RELIABLE_SAMPLE
        or int(baseline.get("sample_size") or 0) < MIN_RELIABLE_SAMPLE
    )
    notes = [
        f"基线 {before}（{baseline['source']}，样本 {baseline['sample_size']}）"
        f" → 后测 {resolved}（{source}，样本 {final_sample}）"
    ]
    if low_conf:
        notes.append(
            f"样本量偏小（低于 {MIN_RELIABLE_SAMPLE}），结论不确定性较高，"
            "建议积累更长时间范围后复测"
        )
    if direction == "退步":
        notes.append("方向为退步：需排查是改造本身的问题，还是外部因素（如业务量激增）")

    record = {
        "code": ResultCode.OK.value,
        "card_id": card_id,
        "metric": metric,
        "baseline": baseline,
        "measured": measured,
        "improvement_pct": pct,
        "direction": direction,
        "low_confidence": low_conf,
        "note": "；".join(notes),
        "next_action": "",
    }

    path = base / MEASURE_FILE
    items = _read(path)
    items.append({k: v for k, v in record.items() if k != "baseline"} | {
        "baseline_id": baseline["baseline_id"]
    })
    _write(path, items)
    return record


def list_measurements(*, root: Path | str | None = None, slug: str) -> list[dict[str, Any]]:
    base = _base(root, slug)
    return _read(base / MEASURE_FILE) if base else []


def effect_summary(*, root: Path | str | None = None, slug: str) -> dict[str, Any]:
    """汇总一个客户的衡量状态，供前端效果页直接渲染。"""
    baselines = list_baselines(root=root, slug=slug)
    measurements = list_measurements(root=root, slug=slug)

    by_card: dict[str, dict[str, Any]] = {}
    for b in baselines:
        key = f"{b['card_id']}::{b['metric']}"
        cur = by_card.get(key)
        if cur is None or b["version"] > cur["version"]:
            by_card[key] = b

    measured_keys = {f"{m['card_id']}::{m['metric']}" for m in measurements}
    improved = [m for m in measurements if m.get("direction") == "改善"]
    regressed = [m for m in measurements if m.get("direction") == "退步"]

    return {
        "baselines": list(by_card.values()),
        "measurements": measurements,
        "pending": [
            {"card_id": v["card_id"], "metric": v["metric"], "baseline": v["value"]}
            for k, v in by_card.items()
            if k not in measured_keys
        ],
        "improved_count": len(improved),
        "regressed_count": len(regressed),
        "allowed_metrics": list(ALLOWED_METRICS),
        "rule": (
            "只采信与场景强直接关联的过程指标，且必须有改造前基线。"
            "无基线的改善无法证明是改造带来的，一律不采信。"
        ),
    }
