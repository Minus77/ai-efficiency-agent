"""工具层（§6）。

ACI 原则：设计的是"一个人类分析师会怎么切分这件事"，不是把 API 端点包一层。
三条硬纪律在本层落实，而不是靠 prompt 叮嘱：
1. metric_probe 能理直气壮说"取不到"（insufficient_data 是一等公民）
2. roi_estimate 纯函数化，缺参报结构化错误并指明去哪个工具取
3. 返回语义名不返回 ID，concise 模式省 token
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..evidence import (
    Claim,
    adjudicate,
    grade_of,
    judge_work_form,
    probe_material_reachability,
)
from ..feasibility import feasibility_score as _feasibility_score
from ..guardrails import contains_money, scan_attachment, tenant_filter
from ..knowledge import KnowledgeBase, Library
from ..models import (
    EvidenceGrade,
    Insight,
    SourceType,
    TaskCard,
    ToolResult,
    WorkForm,
)
from ..roi import roi_estimate as _roi_estimate
from ..workspace import Workspace


@dataclass
class ToolContext:
    """工具运行上下文。tenant 用于强制隔离，无例外（§13.3）。"""

    tenant: str
    workspace: Workspace
    kb: KnowledgeBase
    tracer: Any | None = None
    llm: Any | None = None
    as_of: str = field(default_factory=lambda: date.today().isoformat())


# ---------------------------------------------------------------------------
# S0 立项
# ---------------------------------------------------------------------------
def scope_define(
    ctx: ToolContext,
    *,
    client_name: str,
    departments: list[str],
    as_of: str,
    data_availability: str = "",
    headcount: int | None = None,
    industry: str = "",
    excluded: list[str] | None = None,
) -> ToolResult:
    """强制含 AS_OF、部门边界、数据可得性声明（§6）。"""
    if not as_of:
        return ToolResult.invalid(
            "缺少 AS_OF（数据口径日期）",
            next_action="必须显式给出 AS_OF（数据口径日期），它是全部结论的时效基准，如 as_of='2026-08-20'",
        )
    if not departments:
        return ToolResult.invalid(
            "缺少部门边界",
            next_action="20–200 人企业应聚焦 2–3 个业务流；200 人以上须做部门级而非全公司诊断",
        )
    if not data_availability:
        return ToolResult.invalid(
            "缺少数据可得性声明",
            next_action="先调 material_request(probe_sample=...) 做受理前探测，确认可达证据级别与交付形态",
        )

    scope = {
        "client_name": client_name,
        "industry": industry,
        "headcount": headcount,
        "departments": departments,
        "excluded": excluded or [],
        "as_of": as_of,
        "data_availability": data_availability,
        "autonomy": "L1 只读 + 建议，人工拍板",
        "scope_note": "本报告为决策参考，非投资承诺或收益保证。",
    }
    ctx.workspace.write_json("scope.json", scope)
    md = [
        f"# 诊断口径 SCOPE（{client_name}）",
        "",
        f"- AS_OF：{as_of}",
        f"- 行业 / 规模：{industry or '未声明'} / {headcount or '未声明'} 人",
        f"- 覆盖部门：{'、'.join(departments)}",
        f"- 明确排除：{'、'.join(excluded or []) or '无'}",
        f"- 数据可得性：{data_availability}",
        "- 自主度：L1 只读 + 建议，人工拍板（『该不该上 AI』是客户的经营决策）",
        "",
        "> 本报告为决策参考，非投资承诺或收益保证。",
    ]
    ctx.workspace.write_text("SCOPE.md", "\n".join(md))
    return ToolResult.success({"scope": scope}, note="口径已落盘，后续全部结论以此 AS_OF 为时效基准")


# ---------------------------------------------------------------------------
# 材料采集（§12.2 材料清单驱动，不做多轮追问）
# ---------------------------------------------------------------------------
_MATERIAL_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "客服咨询": [
        {
            "name": "售后/客服工单导出（最近 1–3 个月，CSV，含创建与首响时间）",
            "purpose": "用它算清咨询量和每条的处理时长，比估算准得多，也决定这块能不能给出金额",
            "priority": 1,
            "yields": "频次（记录条数）+ 单次耗时（时间戳间隔）→ A 级证据",
            "path": "R1",
        },
        {
            "name": "任意 2 次与客服相关的会议纪要",
            "purpose": "用来看你们自己觉得痛在哪，帮我们选对要深挖的环节",
            "priority": 3,
            "yields": "痛点定位（不用于算数字）→ C 级证据",
            "path": "R5",
        },
    ],
    "月度对账": [
        {
            "name": "对账表的历史版本或修改记录（含修改时间）",
            "purpose": "用它判断对账是不是集中在几天里成批做完，这直接决定这块收益能不能算进去",
            "priority": 1,
            "yields": "作业形态（聚集性）+ 频次 → A 级证据",
            "path": "R1",
        },
        {
            "name": "商城/ERP 订单导出（同期）",
            "purpose": "用它和对账表比对，算出需要人工核的差异比例",
            "priority": 2,
            "yields": "单量与差异率 → A 级证据",
            "path": "R1",
        },
    ],
    "销售跟单": [
        {
            "name": "销售台账或跟单记录导出",
            "purpose": "用它算清跟单录入的频次和耗时；缺了它这块只能给方向、给不了数字",
            "priority": 1,
            "yields": "频次 + 耗时 → A 级证据",
            "path": "R1",
        },
        {
            "name": "补数表（5–8 个纯数字填空）",
            "purpose": "台账导不出来时的兜底，只填数字，不占你们太多时间",
            "priority": 2,
            "yields": "频次与耗时估值 → B 级证据",
            "path": "R2",
        },
    ],
}

_GENERIC_MATERIALS = [
    {
        "name": "在用系统清单（哪些系统、谁在用、能不能导出）",
        "purpose": "用来判断哪些环节可以接得上，哪些只能先人工过渡",
        "priority": 2,
        "yields": "系统盘点与集成缺口",
        "path": "R1",
    }
]


def material_request(
    ctx: ToolContext,
    *,
    business_flows: list[str] | None = None,
    probe_sample: dict[str, bool] | None = None,
) -> ToolResult:
    """返回材料清单 + 每份材料能算出什么，不做多轮追问（§6、§12.2）。

    probe_sample 为受理前探测模式（§17.1.1）：用一份 7 天样本判定可达证据级别与交付形态。
    """
    if probe_sample is not None:
        probe = probe_material_reachability(
            has_records=bool(probe_sample.get("has_records")),
            has_timestamps=bool(probe_sample.get("has_timestamps")),
            structured=bool(probe_sample.get("structured", True)),
        )
        return ToolResult.success(
            {
                "reachable_grade": probe.grade.value,
                "delivery_form": probe.delivery_form.value,
                "accepted": probe.accepted,
                "explanation": probe.note,
            },
            note="受理前探测：交付形态在受理时即与客户约定，避免期望错配",
        )

    items: list[dict[str, Any]] = []
    for flow in business_flows or []:
        items.extend(_MATERIAL_TEMPLATES.get(flow, []))
    items.extend(_GENERIC_MATERIALS)
    items.sort(key=lambda i: i["priority"])
    return ToolResult.success(
        {
            "items": items,
            "opening": "你把这些材料给我，我告诉你哪些环节 AI 能帮上忙，不用你懂技术。",
            "collection_rule": "一次性说清要什么；拿到后我会告诉你还缺什么、缺了会影响哪块结论。",
        },
        note="材料清单驱动：每份材料都说明能算出什么，客户配合意愿更高",
    )


# ---------------------------------------------------------------------------
# R1 单据考古
# ---------------------------------------------------------------------------
def document_forensics(
    ctx: ToolContext,
    *,
    path: str,
    timestamp_column: str | None = None,
    minutes_per_run: float | None = None,
    self_reported_form: str | None = None,
) -> ToolResult:
    """从单据/导出反推频次与耗时（§3 R1，A 级主力证据）。"""
    p = Path(path)
    if not p.exists():
        return ToolResult.insufficient(
            f"文件不存在：{path}",
            next_action="请客户重新上传该导出；若系统导不出，改发 R2 补数表（5–8 个纯数字填空）",
        )

    raw = p.read_text(encoding="utf-8", errors="replace")
    scan = scan_attachment(filename=p.name, content=raw[:20000], size_bytes=p.stat().st_size)
    if not scan.allow_as_data:
        return ToolResult.invalid(
            scan.note,
            next_action="这份文件读不出来或不被接受，能否换成纯值 CSV 或 Excel 后重传",
        )
    if ctx.tracer is not None and scan.injection_suspected:
        ctx.tracer.event("guardrail_triggered", {"layer": "工具出参", "action": "附件指令样式文本降级为纯数据"})

    base_payload: dict[str, Any] = {
        "file": p.name,
        "injection_suspected": scan.injection_suspected,
        "used_as_instruction": False,  # 材料内容一律不作为指令执行
        "untrusted": True,
    }

    if timestamp_column is None:
        return ToolResult.success(
            {**base_payload, "excerpt_path": ctx.workspace.spill(p.name, raw[:4000])},
            source=[p.name],
            note="未指定时间戳列，仅做安全扫描与摘要落盘；量化需指定 timestamp_column",
        )

    try:
        rows = list(csv.DictReader(raw.splitlines()))
    except Exception:
        return ToolResult.insufficient(
            "文件无法按 CSV 解析",
            next_action="请确认导出为标准 CSV（首行表头）；解析失败不猜测内容",
        )
    if not rows or timestamp_column not in (rows[0].keys() if rows else {}):
        return ToolResult.insufficient(
            f"未找到时间戳列 {timestamp_column}",
            next_action=f"可用列为 {list(rows[0].keys()) if rows else []}；确认列名或改发补数表",
        )

    stamps = [r[timestamp_column] for r in rows if r.get(timestamp_column)]
    verdict = judge_work_form(
        stamps,
        minutes_per_run=minutes_per_run,
        self_reported_form=WorkForm(self_reported_form) if self_reported_form else None,
    )
    if ctx.tracer is not None and verdict.requires_human:
        ctx.tracer.event("evidence_conflict", {"file": p.name, "reason": "自述与时间戳判定不一致"})

    return ToolResult.success(
        {
            **base_payload,
            "record_count": len(stamps),
            "work_form": verdict.work_form.value,
            "discount": verdict.discount,
            "evidence_grade": verdict.evidence_grade.value,
            "windows": verdict.windows[:5],
            "requires_human": verdict.requires_human,
            "explanation": verdict.note,
        },
        source=[p.name],
        sample_size=len(stamps),
        note="频次从记录条数直接数，耗时/形态从时间戳分布推算",
    )


def process_search(
    ctx: ToolContext, *, query: str, paths: list[str], max_snippets: int = 8
) -> ToolResult:
    """跨源检索活动痕迹，只回相关片段（对标 search_logs 而非 read_logs，§6）。"""
    snippets: list[dict[str, Any]] = []
    total_matches = 0
    for path in paths:
        p = Path(path)
        if not p.exists():
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if query in line:
                total_matches += 1
                if len(snippets) < max_snippets:
                    snippets.append({"file": p.name, "line": lineno, "text": line[:200]})
    if not snippets:
        return ToolResult.insufficient(
            f"在给定材料中未检索到与「{query}」相关的痕迹",
            next_action="扩大关键词或索取覆盖更长时间范围的导出；不得据此推断数值",
        )
    return ToolResult.success(
        {"snippets": snippets, "total_matches": total_matches, "truncated": total_matches > len(snippets)},
        source=[Path(p).name for p in paths],
        sample_size=total_matches,
        note="只回相关片段，不回全文——避免把原始明细灌进上下文",
    )


def system_inventory(ctx: ToolContext, *, systems: list[dict[str, Any]]) -> ToolResult:
    """盘点在用系统与集成缺口，返回语义名而非 UUID（§6 取舍 3）。"""
    cleaned = []
    for s in systems:
        cleaned.append(
            {
                "name": s.get("name", "未命名系统"),
                "used_by": s.get("used_by", ""),
                "exportable": bool(s.get("exportable")),
                "has_timestamps": bool(s.get("has_timestamps", s.get("exportable"))),
                "integration_gap": s.get("integration_gap", "无 API，需手工导出" if not s.get("api") else ""),
            }
        )
    exportable = [s["name"] for s in cleaned if s["exportable"]]
    admission = bool(exportable)
    return ToolResult.success(
        {
            "systems": cleaned,
            "exportable_systems": exportable,
            "admission_passed": admission,
            "admission_note": (
                "至少 1 个在用系统且能导出 → 满足硬准入门槛"
                if admission
                else "无任何可导出系统 → 应拒接或转轻量咨询：流程未成型时 AI 只会放大失能"
            ),
        },
        note="返回语义名不返回 ID，降低检索幻觉",
    )


def metric_probe(
    ctx: ToolContext,
    *,
    activity: str,
    records: list[dict[str, Any]] | None = None,
    source: str = "",
    cross_claims: list[dict[str, Any]] | None = None,
) -> ToolResult:
    """必须返回来源与样本量；取不到返回 insufficient_data（§6 取舍 1）。"""
    if cross_claims:
        claims = [
            Claim(
                source_type=SourceType(c["source_type"]),
                value=float(c["value"]),
                origin=c.get("origin", ""),
            )
            for c in cross_claims
        ]
        adj = adjudicate(claims)
        if ctx.tracer is not None and adj.conflict:
            ctx.tracer.event("evidence_conflict", {"activity": activity, "divergence": adj.divergence})
        return ToolResult.success(
            {
                "activity": activity,
                "value": adj.chosen_value,
                "chosen_source": adj.chosen_source.value,
                "evidence_grade": adj.grade.value,
                "conflict": adj.conflict,
                "requires_human": adj.requires_human,
                "divergence": adj.divergence,
                "explanation": adj.note,
                "considered": adj.considered,
            },
            source=[adj.chosen_origin],
            sample_size=len(claims),
            note="按固定裁决序择一，冲突不取均值",
        )

    if not records:
        return ToolResult.insufficient(
            f"活动「{activity}」没有可用的客观记录",
            next_action=(
                f"先索取该环节的时间戳导出；确实拿不到则发 R2 补数表（纯数字填空）取 B 级；"
                f"两者皆无则该场景仅给方向性判断并在报告列为缺口"
            ),
        )

    minutes = [float(r["minutes"]) for r in records if r.get("minutes") is not None]
    if not minutes:
        return ToolResult.insufficient(
            f"活动「{activity}」的记录中没有耗时字段",
            next_action="索取含时间戳的导出以推算耗时；不得用行业基准填补该空缺",
        )
    avg = sum(minutes) / len(minutes)
    return ToolResult.success(
        {
            "activity": activity,
            "count": len(minutes),
            "avg_minutes": round(avg, 2),
            "min_minutes": round(min(minutes), 2),
            "max_minutes": round(max(minutes), 2),
            "evidence_grade": EvidenceGrade.A.value if source else EvidenceGrade.B.value,
        },
        source=[source] if source else [],
        sample_size=len(minutes),
        note="来源与样本量随返回，便于台账回指",
    )


def taskcard_upsert(ctx: ToolContext, *, card: dict[str, Any]) -> ToolResult:
    """schema 校验：无证据引用直接拒写（§11.1）。"""
    try:
        model = TaskCard(**card)
    except Exception as err:
        return ToolResult.invalid(
            f"场景卡不合规：{err}",
            next_action=(
                "无证据引用的场景卡不得进入清单；先调 document_forensics / metric_probe 取证，"
                "再把证据编号写入 evidence_refs"
            ),
        )
    ctx.workspace.write_json(f"task-cards/{model.card_id}.json", model.model_dump(mode="json"))
    return ToolResult.success(
        {"card_id": model.card_id, "quantifiable": model.quantifiable},
        note="已落盘；任务卡是唯一真相源，会话上下文可随时丢弃重建",
    )


def benchmark_lookup(ctx: ToolContext, *, query: str, library: str = "benchmark") -> ToolResult:
    """查基准/案例，每条带出处与时效（§6、§11.3.6）。"""
    guard = tenant_filter(requested_tenant=ctx.tenant, session_tenant=ctx.tenant)
    if not guard.ok:  # pragma: no cover
        return guard
    lib = Library(library)
    result = ctx.kb.search(query, library=lib, as_of=ctx.as_of)
    if result.code.value == "no_grounding":
        if ctx.tracer is not None:
            ctx.tracer.event("no_grounding", {"library": library, "query": query})
        return result
    result.note = (
        "基准只做横向对照，绝不填 ROI 空缺——严禁基准数字出现在结论位。" + (result.note or "")
    )
    return result


def capability_match(ctx: ToolContext, *, need: str, name_products: bool = False) -> ToolResult:
    """匹配 AI 能力边界（版本化，防"AI 万能"幻觉，§18）。"""
    result = ctx.kb.search(need, library=Library.CAPABILITY, as_of=ctx.as_of, top_k=3)
    if result.code.value == "no_grounding":
        if ctx.tracer is not None:
            ctx.tracer.event("no_grounding", {"library": "capability", "need": need})
        return ToolResult.no_grounding(
            f"能力库中未匹配到「{need}」对应的能力类型",
            next_action="该需求可能超出当前能力库版本覆盖；标为待复核，不得假定 AI 可以做到",
        )
    matches = [
        {
            "capability": h["capability"],
            "maturity": h.get("text", "")[:0] or h["status"],
            "description": h["text"],
            "known_limits": h["known_limits"],
            "selection_criteria": h["selection_criteria"],
            "automation_rate_range": h["automation_rate_range"],
            "version": h["version"],
            "as_of": h["published_at"],
        }
        for h in result.data["hits"]
        if h.get("capability")
    ]
    if not matches:
        return ToolResult.no_grounding(
            f"能力库中未匹配到「{need}」",
            next_action="标为待复核，不得假定 AI 可以做到",
        )
    payload: dict[str, Any] = {
        "matches": matches,
        "product_naming": "默认只给能力类型与选型标准，不点名产品（点名即构成事实上的背书责任）",
    }
    if name_products:
        payload["product_naming"] = (
            "用户明确要求点名产品时：给出当前最准确答案并强制标注『仅供参考，非推荐或背书』"
            f"、评估日期 AS_OF={ctx.as_of}、『产品能力与价格变化快，落地前请自行复核』"
        )
    return ToolResult.success(payload, source=result.source, note=f"能力库版本随判定记录，便于日后解释")


def feasibility_score(ctx: ToolContext, *, card_id: str, scores: dict[str, float]) -> ToolResult:
    """七维 rubric，返回分项 + 缺失项，不返回单一总分（§6）。"""
    return _feasibility_score(card_id=card_id, scores=scores)


def roi_estimate(
    ctx: ToolContext,
    *,
    card_id: str,
    monthly_minutes: float | None = None,
    work_form: str | None = None,
    evidence_grade: str | None = None,
    hourly_cost_range: tuple[float, float] | None = None,
    automation_rate_range: tuple[float, float] | None = None,
    implementation_cost_range: tuple[float, float] | None = None,
    include_optimistic: bool = False,
    dependency_of: str | None = None,
    dependency_released_saving: float | None = None,
    assumptions: list[str] | None = None,
) -> ToolResult:
    """纯函数：只接受显式传入基线，缺参报结构化错误（§6 取舍 2）。"""
    return _roi_estimate(
        card_id=card_id,
        monthly_minutes=monthly_minutes,
        work_form=WorkForm(work_form) if work_form else None,
        evidence_grade=EvidenceGrade(evidence_grade) if evidence_grade else None,
        hourly_cost_range=tuple(hourly_cost_range) if hourly_cost_range else None,  # type: ignore[arg-type]
        automation_rate_range=tuple(automation_rate_range) if automation_rate_range else None,  # type: ignore[arg-type]
        implementation_cost_range=tuple(implementation_cost_range) if implementation_cost_range else None,  # type: ignore[arg-type]
        include_optimistic=include_optimistic,
        dependency_of=dependency_of,
        dependency_released_saving=dependency_released_saving,
        assumptions=assumptions,
    )


def insight_propose(
    ctx: ToolContext, *, statement: str, basis: str, verification_suggestion: str
) -> ToolResult:
    """Q8 专用：低证据高价值洞察，强制标注为专家判断且不得含金额（§7、§11.6）。"""
    if contains_money(statement):
        return ToolResult.invalid(
            "专家判断区不得出现具体金额或 ROI 数字",
            next_action="改写为方向与验证路径；金额只能出现在有证据支撑的第一部分",
        )
    if not verification_suggestion.strip():
        return ToolResult.invalid(
            "缺少建议的验证方式",
            next_action="每条经验判断必须写明如何验证，否则无法与数据结论区分",
        )
    insight = Insight(
        insight_id=f"i-{abs(hash(statement)) % 9973:04d}",
        statement=statement,
        basis=basis,
        verification_suggestion=verification_suggestion,
    )
    return ToolResult.success(
        {"insight": insight.model_dump(mode="json")},
        note="已归入独立附录『基于经验的判断（无数据支撑）』，与数据结论物理隔离",
    )


def counter_review(
    ctx: ToolContext,
    *,
    cards: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    reasoning_chain: str = "",
) -> ToolResult:
    """独立上下文，输出最强反驳。只看任务卡与证据台账（§5 Isolate）。

    reasoning_chain 参数被显式丢弃——看了主 Agent 推理链会认同其错误前提（context poisoning）。
    """
    payload = {
        "cards": [
            {
                "card_id": c.get("card_id"),
                "name": c.get("name"),
                "monthly_minutes": c.get("monthly_minutes"),
                "evidence_grade": c.get("evidence_grade"),
                "evidence_refs": c.get("evidence_refs", []),
            }
            for c in cards
        ],
        "evidence": [
            {"evidence_id": e.get("evidence_id"), "grade": e.get("grade"), "origin": e.get("origin")}
            for e in evidence
        ],
        "instruction": (
            "针对每个场景给出最强反驳：数字是否站得住、是否漏了上游瓶颈、落地是否有硬约束。"
            "已知局限：反评审只能审内部一致性，审不了真伪，不能替代人工终审。"
        ),
        "isolation": "本次输入只含任务卡与证据台账；主 Agent 的推理过程未被传入",
    }
    return ToolResult.success(payload, note="反评审输入已剥离主 Agent 推理链，避免认同其错误前提")


def outcome_record(
    ctx: ToolContext,
    *,
    card_id: str,
    role: str,
    direction: str,
    would_do_first: str | None = None,
    would_not_do: str | None = None,
    reason: str = "",
) -> ToolResult:
    """Q4 专用：采集落地结果与用户反馈。角色必填（§19.2 G3）。"""
    if not role.strip():
        return ToolResult.invalid(
            "角色必填",
            next_action="不同角色的偏差方向已知（老板高估可行性、执行者低估收益），缺角色则无法反向校正",
        )
    if direction not in ("偏高", "偏低", "基本相符", "没说到点上"):
        return ToolResult.invalid(
            f"direction 取值非法：{direction}",
            next_action="只接受 偏高 / 偏低 / 基本相符 / 没说到点上——不问『准不准』，避免面子偏差",
        )
    record = {
        "feedback_id": f"f-{card_id}-{abs(hash(role + direction + reason)) % 997:03d}",
        "card_id": card_id,
        "role": role,
        "direction": direction,
        "would_do_first": would_do_first,
        "would_not_do": would_not_do,
        "reason": reason,
        "created_at": ctx.as_of,
    }
    ctx.workspace.write_json(f"feedback/{record['feedback_id']}.json", record)
    return ToolResult.success(
        {"feedback_id": record["feedback_id"]},
        note="同一场景的多条反馈全部保留，冲突不合并——冲突本身是信号",
    )


def report_render(ctx: ToolContext, *, cards: list[dict[str, Any]]) -> ToolResult:
    """未过验证门禁的场景标灰，不入正文（§6）。"""
    body: list[dict[str, Any]] = []
    greyed: list[dict[str, Any]] = []
    for c in cards:
        gated = bool(c.get("evidence_refs")) and c.get("evidence_grade") in ("A", "B") and c.get("quantifiable", True)
        (body if gated else greyed).append(c)
    return ToolResult.success(
        {
            "body": body,
            "greyed_out": greyed,
            "grey_reason": "证据等级为 C、缺证据引用或不可量化的场景一律标灰，不进正文的量化结论",
        },
        note="标灰不是删除：缺口对客户可见，但不进可决策的数字区",
    )


TOOL_REGISTRY: dict[str, Callable[..., ToolResult]] = {
    "scope_define": scope_define,
    "material_request": material_request,
    "document_forensics": document_forensics,
    "process_search": process_search,
    "system_inventory": system_inventory,
    "metric_probe": metric_probe,
    "taskcard_upsert": taskcard_upsert,
    "benchmark_lookup": benchmark_lookup,
    "capability_match": capability_match,
    "feasibility_score": feasibility_score,
    "roi_estimate": roi_estimate,
    "insight_propose": insight_propose,
    "counter_review": counter_review,
    "outcome_record": outcome_record,
    "report_render": report_render,
}
