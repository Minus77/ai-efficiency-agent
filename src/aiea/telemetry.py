"""可观测性（§10）。

采用 OpenTelemetry GenAI 语义约定（`gen_ai.*`，semconv 1.44），不自造字段——
任何 OTel 后端可直接接入。本实现落 JSONL，字段命名对齐，可直接改导出器。

对外一律自然语言化（§10.6）：完整 trace/指标/replay 属内部后台。
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import default_workspace_root
from .guardrails import redact_pii


@dataclass
class Span:
    kind: str
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def set(self, key: str, value: Any) -> None:
        self.attributes[key] = value


@dataclass
class Tracer:
    """一次诊断一棵 span 树（Session 为 root，跨天可续）。"""

    session_id: str
    tenant: str
    out_dir: Path | str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    _costs: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir if self.out_dir is not None else default_workspace_root())
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # -- span ---------------------------------------------------------------
    @contextmanager
    def span(self, kind: str, *, name: str, attrs: dict[str, Any] | None = None) -> Iterator[Span]:
        s = Span(kind=kind, name=name, attributes=dict(attrs or {}))
        started = time.perf_counter()
        try:
            yield s
        finally:
            self.records.append(
                {
                    "type": "span",
                    "kind": kind,
                    "name": name,
                    "gen_ai.session.id": self.session_id,
                    "tenant": self.tenant,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "attributes": s.attributes,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
            )

    def tool_call(self, tool_name: str, *, arguments: dict[str, Any], result_size: int, status: str) -> None:
        """工具 span：入参只存哈希，出参只存大小与状态（§10.1、§10.4）。"""
        raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        self.records.append(
            {
                "type": "span",
                "kind": "tool",
                "name": tool_name,
                "gen_ai.session.id": self.session_id,
                "tenant": self.tenant,
                "attributes": {
                    "gen_ai.tool.name": tool_name,
                    "tool.call.arguments_hash": digest,
                    "tool.call.arguments_keys": sorted(arguments.keys()),
                    "tool.call.result.size": result_size,
                    "tool.call.status": status,
                },
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
        )

    def event(self, name: str, attrs: dict[str, Any] | None = None) -> None:
        self.records.append(
            {
                "type": "event",
                "name": name,
                "gen_ai.session.id": self.session_id,
                "tenant": self.tenant,
                "attributes": attrs or {},
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
        )

    def record_cost(self, *, stage: str, usd: float, cache_read_tokens: int = 0, input_tokens: int = 0) -> None:
        self._costs.append(
            {
                "stage": stage,
                "usd": usd,
                "gen_ai.usage.cache_read.input_tokens": cache_read_tokens,
                "gen_ai.usage.input_tokens": input_tokens,
            }
        )

    # -- 指标（§10.2） -------------------------------------------------------
    def metrics(self) -> dict[str, Any]:
        events = [r for r in self.records if r["type"] == "event"]

        def count(name: str) -> int:
            return sum(1 for e in events if e["name"] == name)

        cost_by_stage: dict[str, float] = {}
        cache_read = 0
        total_input = 0
        for c in self._costs:
            cost_by_stage[c["stage"]] = round(cost_by_stage.get(c["stage"], 0.0) + c["usd"], 4)
            cache_read += c["gen_ai.usage.cache_read.input_tokens"]
            total_input += c["gen_ai.usage.input_tokens"] + c["gen_ai.usage.cache_read.input_tokens"]

        tool_calls = [r for r in self.records if r.get("kind") == "tool"]
        no_grounding = count("no_grounding")
        insufficient = count("insufficient_data")
        honesty_total = no_grounding + insufficient
        denominator = max(len(tool_calls), 1)

        return {
            "no_grounding_count": no_grounding,
            "insufficient_data_count": insufficient,
            # §10.5：诚实信号率过低反而可疑（说明模型在硬编答案）
            "honesty_signal_rate": round(honesty_total / denominator, 4),
            "guardrail_triggered_count": count("guardrail_triggered"),
            "breaker_tripped_count": count("cost_breaker_tripped"),
            "user_pushback_count": count("user_pushback"),
            "conflict_count": count("evidence_conflict"),
            "tool_call_count": len(tool_calls),
            "cost_by_stage": cost_by_stage,
            "cost_total_usd": round(sum(cost_by_stage.values()), 4),
            "cache_hit_rate": round(cache_read / total_input, 4) if total_input else 0.0,
        }

    def flush(self) -> Path:
        path = Path(self.out_dir) / f"trace-{self.session_id}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for rec in self.records:
                fh.write(redact_pii(json.dumps(rec, ensure_ascii=False)) + "\n")
        return path


# ---------------------------------------------------------------------------
# 自然语言化观测层（§10.6）
# ---------------------------------------------------------------------------
def daily_brief(runs: list[dict[str, Any]]) -> str:
    """给顾问的每日简报：可读文字，而非仪表盘数字。禁用任何内部术语。"""
    if not runs:
        return "昨天没有跑诊断，系统空闲。"

    smooth: list[str] = []
    need_eyes: list[str] = []
    system_issues: list[str] = []
    total_cost = 0.0
    avg_total = 0.0

    for r in runs:
        client = r["client"]
        total_cost += float(r.get("cost_usd", 0.0))
        avg_total += float(r.get("avg_cost_usd", 0.0))
        solid = r.get("scenarios_solid", 0)
        total = r.get("scenarios_total", 0)
        smooth.append(f"{client} 的材料比较齐全，{total} 个环节里有 {solid} 个拿到了扎实数据")
        gaps = r.get("gaps") or []
        if gaps:
            need_eyes.append(
                f"{client} 有 {total - solid} 个环节因为缺少「{'、'.join(gaps)}」只能给方向性判断，"
                f"建议再向他们要一份这个导出"
            )
        if r.get("conflicts"):
            need_eyes.append(f"{client} 有 {r['conflicts']} 处客户说法和系统记录不一致，需要你拍板")
        if r.get("no_grounding"):
            system_issues.append(f"有 {r['no_grounding']} 次没查到可靠的行业参考资料，已记为待补资料")

    parts = [f"昨天跑了 {len(runs)} 家诊断。"]
    if smooth:
        parts.append("顺利的部分：" + "；".join(smooth) + "。")
    if need_eyes:
        parts.append("需要你看一眼的：" + "；".join(need_eyes) + "。")
    if system_issues:
        parts.append("系统自己的问题：" + "；".join(system_issues) + "。")

    cost_note = f"花费：合计约 ¥{total_cost * 7.2:.0f}"
    if avg_total and total_cost > avg_total * 1.2:
        cost_note += "，比平均水平略高，原因是材料批次偏多、存在重复解析"
    parts.append(cost_note + "。")
    return " ".join(parts)


def customer_progress_line(*, done: list[str], pending: list[str]) -> str:
    """给客户的一句话式进度：不暴露任何内部逻辑。"""
    done_txt = "、".join(done) if done else "暂无"
    if pending:
        return f"我们已经聊清楚{done_txt}这几块，还想了解{'、'.join(pending)}。"
    return f"我们已经聊清楚{done_txt}这几块，材料够用了，正在整理结论。"


_ALERT_TEMPLATES = {
    "faithfulness_score": (
        "报告里有 {unsupported_sentences} 句话找不到证据支撑，已自动标灰，建议你复核第 {section} 节。"
    ),
    "cache_hit_rate": "系统的重复内容复用率掉下来了，通常是提示里带了变动信息，成本会明显上升，建议排查。",
    "cost_breaker": "有一次诊断的花费超过了正常水平的几倍并已暂停，这通常意味着材料被重复解析或检索绕圈，需要排查。",
    "playbook_regression": "上一批经验更新后，回归测试有指标退步超过 5%，已自动阻断这批更新并回滚。",
}


def translate_alert(metric: str, *, value: float, threshold: float, details: dict[str, Any] | None = None) -> str:
    """告警也自然语言化：不是 faithfulness_score < 0.7（§10.6）。"""
    tmpl = _ALERT_TEMPLATES.get(metric)
    if tmpl is None:
        return "有一项内部检查没达到预期，已记录待排查。"
    return tmpl.format(**(details or {}))
