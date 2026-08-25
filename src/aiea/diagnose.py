"""诊断编排：把 S0–S5 跑在任意客户的真实上传材料上。

与 seed.py 的关系：seed.py 是预置演示（材料与场景都为讲清设计而精心构造），
本模块是**生产路径**——场景来自 derive.py 的推导，没有任何内置场景。

产出的 report 与 seed.py 同 schema，因此前端 9 个视图无需改动。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from .agents import generate_counter_review, generate_insights
from .clients import ClientRegistry, safe_slug
from .config import default_workspace_root
from .derive import derive_scenarios
from .evidence import closure_rate
from .feasibility import DIMENSIONS
from .guardrails import guardian_review
from .intake import list_materials, parse_bytes
from .knowledge import KnowledgeBase
from .models import DeliveryForm, EvidenceGrade, Quadrant, Stage
from .pipeline import Diagnosis
from .roi import aggregate_dedup
from .telemetry import Tracer, customer_progress_line, daily_brief
from .tools import TOOL_REGISTRY, ToolContext
from .workspace import Workspace

WORKDAYS_PER_MONTH = 22

# §11.3.2 人力成本：区间 + 公开薪酬基准，写入假设清单供客户校正
HOURLY_COST: dict[str, tuple[float, float]] = {
    "客服专员": (36.0, 47.0),
    "财务专员": (45.0, 62.0),
    "销售助理": (39.0, 52.0),
    "运营专员": (38.0, 50.0),
}
IMPL_COST: dict[str, tuple[float, float]] = {
    "small": (9000.0, 18000.0),
    "medium": (16000.0, 34000.0),
    "large": (28000.0, 60000.0),
}

# 能力检索关键词：按岗位给一个合理的默认需求描述
_CAPABILITY_QUERY = {
    "客服专员": "结构化信息抽取 录入 转录 工单 分类",
    "财务专员": "表格数据比对与异常标注 对账 差异 财务",
    "销售助理": "结构化信息抽取 抽取 转录 台账",
    "运营专员": "长文本摘要与分类 摘要 分类",
}


class DiagnosisNotReady(RuntimeError):
    """材料不足或客户不存在。给结构化提示，而不是崩栈。"""


def _feasibility_from_signals(card: dict[str, Any]) -> dict[str, float]:
    """七维打分由材料信号推导，而不是让模型自评（rubric 权重属冻结区外但需人工批准）。

    打分口径（1=很容易，5=很难）：
    - 数据可得性：有时间戳=2，有明细无时间戳=4，仅汇总/纪要=5
    - 系统集成度：涉及系统数越多越难
    - 流程标准化：可量化=3，不可量化=4（连痕迹都没有说明流程未定义）
    - 其余取中位并按证据等级微调
    """
    grade = card["evidence_grade"]
    quantifiable = card["quantifiable"]
    systems = max(len(card.get("systems") or []), 1)

    data_avail = {"A": 2.0, "B": 4.0, "C": 5.0}[grade]
    integration = min(2.0 + (systems - 1) * 1.0, 5.0)
    standardization = 3.0 if quantifiable else 4.0
    acceptance = 2.0 if grade == "A" else 3.0
    capability = 2.0 if quantifiable else 3.0
    compliance = 2.0
    maintenance = min(2.0 + (systems - 1) * 0.5, 4.0)

    return {
        "数据可得性": data_avail,
        "系统集成度": integration,
        "流程标准化程度": standardization,
        "人员接受度": acceptance,
        "AI能力匹配度": capability,
        "合规风险": compliance,
        "维护成本": maintenance,
    }


def _impl_tier(card: dict[str, Any]) -> str:
    systems = len(card.get("systems") or [])
    if not card["quantifiable"]:
        return "medium"
    if systems >= 3:
        return "large"
    if systems <= 1:
        return "small"
    return "medium"


def run_diagnosis(
    *, tenant: str, root: Path | str | None = None, llm: Any | None = None
) -> dict[str, Any]:
    """对指定客户跑一次完整诊断（S0–S5）并落盘。"""
    root = root if root is not None else default_workspace_root()
    checked = safe_slug(tenant)
    if checked is None:
        raise DiagnosisNotReady("客户标识不合法")

    reg = ClientRegistry(root=root)
    profile = reg.get(checked)
    if profile is None:
        raise DiagnosisNotReady(f"客户 {tenant} 不存在，请先建档")

    records = list_materials(root=root, slug=checked)
    accepted = [r for r in records if r.get("stored_as")]
    if not accepted:
        raise DiagnosisNotReady(
            "尚未上传任何可用材料。请至少上传一份业务系统导出（CSV，最好含时间戳列）——"
            "没有材料就没有证据，本工具不会凭空生成结论。"
        )

    base = Path(root) / checked
    ws = Workspace(tenant=checked, root=root)
    tracer = Tracer(session_id=f"{checked}-{date.today().isoformat()}", tenant=checked, out_dir=base / "trace")
    kb = KnowledgeBase.load_seed()
    as_of = profile.as_of or date.today().isoformat()
    ctx = ToolContext(tenant=checked, workspace=ws, kb=kb, tracer=tracer, as_of=as_of)
    diag = Diagnosis(tenant=checked, workspace=ws, kb=kb, tracer=tracer)

    security = {"injection_attempts_detected": 0, "injection_escaped": 0, "rejected_files": []}

    # ------------------------------------------------------------------ S0
    diag.step(Stage.S0, "确认口径与数据可得性")
    grades = [r.get("reachable_grade") for r in accepted if r.get("reachable_grade")]
    best = "C"
    for g in ("A", "B", "C"):
        if g in grades:
            best = g
            break
    delivery = {"A": DeliveryForm.FULL, "B": DeliveryForm.LIMITED, "C": DeliveryForm.LIGHT}[best]

    availability = "；".join(
        f"{r['filename']}（{r.get('row_count', 0)} 条"
        + (f"，含时间戳 {', '.join(r.get('timestamp_columns') or [])}" if r.get("timestamp_columns") else "，无时间戳")
        + "）"
        for r in accepted
    )
    scope_result = TOOL_REGISTRY["scope_define"](
        ctx,
        client_name=profile.name,
        departments=profile.departments or ["未声明"],
        as_of=as_of,
        data_availability=availability,
        headcount=profile.headcount,
        industry=profile.industry,
        excluded=profile.excluded,
    )
    if not scope_result.ok:
        raise DiagnosisNotReady(scope_result.next_action or scope_result.note)

    # ------------------------------------------------------------------ S1 解析
    diag.step(Stage.S1, "解析已上传材料")
    parsed = []
    for rec in accepted:
        path = base / "materials" / rec["stored_as"]
        if not path.exists():
            continue
        pm = parse_bytes(path.read_bytes(), filename=rec["stored_as"])
        parsed.append(pm)
        if pm.injection_suspected:
            security["injection_attempts_detected"] += 1
            tracer.event("guardrail_triggered", {"layer": "输入校验", "action": "附件指令样式文本降级为纯数据"})

    # ------------------------------------------------------------------ S2 推导
    diag.step(Stage.S2, "从材料推导任务卡")
    derived = derive_scenarios(parsed, llm=llm, as_of=as_of)
    cards = derived["cards"]
    evidence = derived["evidence"]
    parents = derived["parents"]
    gaps = list(derived["gaps"])

    if not cards:
        raise DiagnosisNotReady(
            "已上传的材料无法推导出任何可分析的操作环节。"
            "通常是因为导出只有汇总数字——请提供含单条记录的明细导出。"
        )

    # 落盘任务卡（走 schema 校验：无证据引用直接拒写）
    for card in cards:
        payload = {k: v for k, v in card.items() if k in {
            "card_id", "parent_id", "name", "operator", "systems", "status_quo", "frequency_desc",
            "minutes_per_run", "monthly_minutes", "evidence_grade", "work_form", "benefit_composition",
            "departments_merged", "intervention", "expected_effect", "dependency",
            "landing_dependency", "evidence_refs", "conflict", "conflict_note", "requires_human",
        }}
        res = TOOL_REGISTRY["taskcard_upsert"](ctx, card=payload)
        if not res.ok:
            raise DiagnosisNotReady(f"场景卡未通过校验：{res.note}")

    # ------------------------------------------------------------------ S3 能力与可行性
    diag.step(Stage.S3, "能力匹配与七维可行性")
    capabilities: dict[str, Any] = {}
    feasibility: dict[str, Any] = {}
    for card in cards:
        cid = card["card_id"]
        query = _CAPABILITY_QUERY.get(card["role"], "结构化信息抽取")
        cap = TOOL_REGISTRY["capability_match"](ctx, need=query)
        capabilities[cid] = cap.data.get("matches", [])[:1]
        fr = TOOL_REGISTRY["feasibility_score"](ctx, card_id=cid, scores=_feasibility_from_signals(card))
        if not fr.ok:
            raise DiagnosisNotReady(f"可行性打分失败：{fr.note}")
        feasibility[cid] = fr.data["feasibility"].model_dump(mode="json")

    # ------------------------------------------------------------------ S4 ROI
    diag.step(Stage.S4, "分级 ROI 估算")
    bench_service = TOOL_REGISTRY["benchmark_lookup"](ctx, query="客服 工单 工时 基准")
    bench_recon = TOOL_REGISTRY["benchmark_lookup"](ctx, query="对账 财务 工时 基准")
    bench_cost = TOOL_REGISTRY["benchmark_lookup"](ctx, query="人力成本 单价 薪酬 基准")
    bench_impl = TOOL_REGISTRY["benchmark_lookup"](ctx, query="实施成本 订阅 集成 成本 基准")

    roi_map: dict[str, Any] = {}
    roi_objects = []
    for card in cards:
        cid = card["card_id"]
        gate = diag.enter_stage_for_card(
            Stage.S4,
            evidence_grade=EvidenceGrade(card["evidence_grade"]),
            quantifiable=card["quantifiable"],
        )
        cap = capabilities[cid][0] if capabilities[cid] else None
        auto_range = tuple(cap["automation_rate_range"]) if cap else (0.4, 0.6)
        role = card["role"] if card["role"] in HOURLY_COST else "运营专员"
        tier = _impl_tier(card)
        card["impl"] = tier

        assumptions = [gate.note] if not gate.ok else [
            f"{role}综合成本 ¥{HOURLY_COST[role][0]:.0f}–{HOURLY_COST[role][1]:.0f}/小时（公开薪酬基准 × 社保福利系数）",
            f"自动化率 {auto_range[0]:.0%}–{auto_range[1]:.0%}（依据能力库 {cap['version'] if cap else 'n/a'}）",
        ]
        if card.get("minutes_note"):
            assumptions.append(card["minutes_note"])

        r = TOOL_REGISTRY["roi_estimate"](
            ctx, card_id=cid, monthly_minutes=card["monthly_minutes"],
            work_form=card["work_form"], evidence_grade=card["evidence_grade"],
            hourly_cost_range=HOURLY_COST[role], automation_rate_range=auto_range,
            implementation_cost_range=IMPL_COST[tier],
            assumptions=assumptions,
        )
        if not r.ok:
            raise DiagnosisNotReady(f"ROI 估算被拒：{r.note}｜下一步：{r.next_action}")
        roi = r.data["roi"]
        roi_objects.append(roi)
        roi_map[cid] = roi.model_dump(mode="json")

    aggregate = aggregate_dedup(roi_objects)

    # ------------------------------------------------------------------ 优先级矩阵
    benefits = [
        (roi_map[c["card_id"]]["tiers"][0]["monthly_saving_low"] or 0.0)
        if roi_map[c["card_id"]]["tiers"] else 0.0
        for c in cards
    ]
    quantified = sorted(b for b in benefits if b > 0)
    benefit_threshold = quantified[len(quantified) // 2] if quantified else 0.0
    difficulty_threshold = 3.0

    matrix = []
    for card, benefit in zip(cards, benefits):
        cid = card["card_id"]
        difficulty = feasibility[cid]["weighted_difficulty"] or 5.0
        q = diag.quadrant(
            benefit=benefit, difficulty=difficulty,
            benefit_threshold=benefit_threshold, difficulty_threshold=difficulty_threshold,
        )
        matrix.append(
            {
                "card_id": cid, "name": card["name"], "benefit": round(benefit, 2),
                "difficulty": difficulty, "quadrant": q.value,
                "evidence_grade": card["evidence_grade"],
                "action": {
                    Quadrant.DO_FIRST.value: "进 90 天路线图第一批",
                    Quadrant.PLAN.value: "拆成阶段，先做可验证的一小步",
                    Quadrant.OPPORTUNISTIC.value: "有余力时做，不占主资源",
                    Quadrant.DO_NOT.value: "不建议做",
                }[q.value],
                "reason_if_not": (
                    "收益不足以覆盖集成与长期维护投入" if q is Quadrant.DO_NOT else ""
                ),
            }
        )

    # ------------------------------------------------------------------ S5 反评审与洞察
    diag.step(Stage.S5, "独立上下文反评审")
    cr_input = TOOL_REGISTRY["counter_review"](
        ctx, cards=cards, evidence=evidence,
        reasoning_chain="（存在但按设计被丢弃）",
    )
    counter_review = generate_counter_review(cards, evidence, llm=llm)

    raw_insights = generate_insights(cards, llm=llm)
    insights = []
    for item in raw_insights:
        r = TOOL_REGISTRY["insight_propose"](
            ctx,
            statement=item["statement"],
            basis=item.get("basis", ""),
            verification_suggestion=item["verification_suggestion"],
        )
        if r.ok:
            insights.append(r.data["insight"])

    # ------------------------------------------------------------------ 门禁与记分卡
    render = TOOL_REGISTRY["report_render"](ctx, cards=cards)
    traceability = diag.traceability_rate(cards)

    guardian_checks = []
    for card in cards:
        roi = roi_map[card["card_id"]]
        if roi["amount"]:
            sentence = (
                f"{card['name']}：预计可省约 ¥{roi['tiers'][0]['monthly_saving_low']:.0f}–"
                f"{roi['tiers'][-1]['monthly_saving_high']:.0f}/月（证据 {'、'.join(card['evidence_refs'])}）"
            )
            v = guardian_review(
                statement=sentence, evidence_grade=EvidenceGrade(card["evidence_grade"]), has_citation=True
            )
            guardian_checks.append({"card_id": card["card_id"], "sentence": sentence, "approved": v.approved})
    for ins in insights:
        v = guardian_review(statement=ins["statement"], evidence_grade=EvidenceGrade.C, has_citation=False)
        guardian_checks.append({"insight_id": ins["insight_id"], "approved": v.approved})

    # ------------------------------------------------------------------ 假设清单
    roles_used = sorted({c["role"] for c in cards if c["role"] in HOURLY_COST})
    cost_lines = "、".join(
        f"{r} ¥{HOURLY_COST[r][0]:.0f}–{HOURLY_COST[r][1]:.0f}/小时" for r in roles_used
    ) or "按公开薪酬基准区间取值"
    assumptions = [
        f"人力成本单价：{cost_lines}（含社保福利系数 1.3–1.45）——来源：公开行业薪酬基准，可替换为你们的真实数字",
        "折现系数：批量作业与连续作业按 100% 计入，真碎片不计入（判定依据见证据台账的时间戳分析）",
        "实施成本区间：单场景 ¥9,000–60,000（含订阅费、集成人力、培训、年度维护），集成人力通常是订阅费的数倍",
        f"数据口径：基于客户提供的 {len(accepted)} 份材料（{'、'.join(r['filename'] for r in accepted)}），AS_OF {as_of}",
        "AI 能力判定：依据能力库版本 v3（AS_OF: 2026-07-01）",
        "以上任一假设变化都会改变结论。本报告为决策参考，非投资承诺或收益保证。",
    ]
    if profile.out_of_scope:
        assumptions.insert(0, f"规模提示：{profile.scope_note}")

    scorecard = {
        "evidence_traceability": traceability,
        "scenarios_total": len(cards),
        "scenarios_quantified": len([c for c in cards if roi_map[c["card_id"]]["amount"] is not None]),
        "scenarios_direction_only": len([c for c in cards if roi_map[c["card_id"]]["direction_only"]]),
        "evidence_closure_rate": closure_rate(
            filled=len([e for e in evidence if e["grade"] in ("A", "B")]), total=max(len(evidence), 1)
        ),
        "conflicts_escalated": len([c for c in cards if c["conflict"]]),
        "grade_distribution": dict(Counter(c["evidence_grade"] for c in cards)),
        "work_form_distribution": dict(Counter(c["work_form"] for c in cards)),
    }

    if scorecard["scenarios_quantified"] == 0:
        tracer.event("insufficient_data", {"tool": "metric_probe", "reason": "无可量化场景"})
    metrics = tracer.metrics()
    brief = daily_brief([
        {
            "client": profile.name,
            "scenarios_total": len(cards),
            "scenarios_solid": scorecard["scenarios_quantified"],
            "gaps": [g["material"] for g in gaps[:2]],
            "cost_usd": 0.0,
            "avg_cost_usd": 0.0,
            "no_grounding": metrics["no_grounding_count"],
            "conflicts": scorecard["conflicts_escalated"],
        }
    ])

    quantified_flows = sorted({c["business_flow"] for c in cards if c["quantifiable"]})
    pending_flows = sorted({c["business_flow"] for c in cards if not c["quantifiable"]})

    report: dict[str, Any] = {
        "client": {
            "client_name": profile.name,
            "short_name": profile.name,
            "industry": profile.industry,
            "headcount": profile.headcount,
            "as_of": as_of,
            "departments": profile.departments,
            "excluded": profile.excluded,
            "background": profile.background or f"{profile.industry or '未声明行业'}，{profile.headcount or '未声明'} 人规模。",
            "out_of_scope": profile.out_of_scope,
            "scope_note": profile.scope_note,
            "systems": [{"name": r["filename"], "exportable": True, "has_timestamps": bool(r.get("timestamp_columns"))} for r in accepted],
            "admission": {"has_exportable_system": True, "verdict": f"{best} 级可达 → {delivery.value}"},
        },
        "delivery_form": delivery.value,
        "admission_probe": {
            "reachable_grade": best,
            "delivery_form": delivery.value,
            "accepted": True,
            "explanation": f"依据已上传的 {len(accepted)} 份材料判定：可达 {best} 级证据，交付形态为{delivery.value}。",
        },
        "scope": scope_result.data["scope"],
        "material_checklist": TOOL_REGISTRY["material_request"](
            ctx, business_flows=list(quantified_flows + pending_flows)[:3]
        ).data.get("items", []),
        "parents": parents,
        "cards": cards,
        "evidence": evidence,
        "capabilities": capabilities,
        "feasibility": feasibility,
        "roi": roi_map,
        "aggregate": aggregate,
        "matrix": matrix,
        "matrix_thresholds": {
            "benefit": round(benefit_threshold, 2),
            "difficulty": difficulty_threshold,
            "benefit_basis": "本客户已量化场景收益的中位数（绝对阈值会随客户规模失准）",
            "difficulty_basis": "七维加权难度 1–5 的中点",
        },
        "roadmap": diag.roadmap([
            {"card_id": m["card_id"], "name": m["name"], "quadrant": m["quadrant"], "monthly_saving": m["benefit"]}
            for m in matrix
        ]),
        "counter_review": counter_review,
        "counter_review_isolation": cr_input.data["isolation"],
        "insights": insights,
        "gaps": gaps,
        "assumptions": assumptions,
        "scorecard": scorecard,
        "security": security,
        "guardian_checks": guardian_checks,
        "feedback": ws.list_feedback(),
        "render_gate": {
            "body_ids": [c["card_id"] for c in render.data["body"]],
            "greyed_ids": [c["card_id"] for c in render.data["greyed_out"]],
            "grey_reason": render.data["grey_reason"],
        },
        "observability": {
            "metrics": metrics,
            "daily_brief": brief,
            "customer_progress": customer_progress_line(
                done=quantified_flows or ["材料解析"], pending=pending_flows
            ),
            "playbook_candidate": {},
        },
        "benchmarks": {
            "service": bench_service.data.get("hits", []),
            "reconcile": bench_recon.data.get("hits", []),
            "cost": bench_cost.data.get("hits", []),
            "implementation": bench_impl.data.get("hits", []),
            "usage_rule": "基准只做横向对照，不填 ROI 空缺",
        },
        "naming_source": derived["naming_source"],
    }

    ws.write_json("REPORT.json", report)
    ws.write_text("FINDINGS.md", _render_findings(report))
    tracer.flush()
    reg.update(checked, status="diagnosed", reachable_grade=best, delivery_form=delivery.value)
    return report


def _render_findings(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['client']['client_name']} — AI 提效场景诊断报告",
        "",
        f"> 交付形态：{report['delivery_form']}　|　数据口径 AS_OF：{report['scope']['as_of']}",
        "> 本报告为决策参考，非投资承诺或收益保证。",
        "",
        "## 本报告的关键假设",
        "",
    ]
    for i, a in enumerate(report["assumptions"], 1):
        lines.append(f"{i}. {a}")
    lines += ["", "## 第一部分：数据结论（有证据支撑）", ""]
    for parent in report["parents"]:
        lines += [f"### {parent['business_outcome']}", f"*{parent['why_painful']}*", ""]
        for cid in parent["child_ids"]:
            card = next((c for c in report["cards"] if c["card_id"] == cid), None)
            if card is None:
                continue
            roi = report["roi"][cid]
            if roi["amount"] is not None:
                money = (
                    f"预计可省约 ¥{roi['tiers'][0]['monthly_saving_low']:,.0f}–"
                    f"{roi['tiers'][-1]['monthly_saving_high']:,.0f}/月"
                )
            elif roi["direction_only"]:
                money = "仅方向性判断，不给金额"
            else:
                money = "区间估算"
            lines.append(
                f"- **{card['name']}**（{card['evidence_grade']} 级证据，{card['work_form']}）："
                f"{card['status_quo']}。{money}。证据：{'、'.join(card['evidence_refs'])}"
            )
        lines.append("")
    lines += ["## 第二部分：基于经验的判断（无数据支撑）", ""]
    for ins in report["insights"]:
        lines += [
            f"- **{ins['statement']}**",
            f"  - 依据：{ins['basis']}",
            f"  - 建议验证方式：{ins['verification_suggestion']}",
            f"  - {ins['label']}",
        ]
    lines += ["", "## 未获取的材料及其影响", ""]
    for g in report["gaps"]:
        lines.append(f"- {g['material']}：{g['status']}。影响：{g['impact']}")
    return "\n".join(lines) + "\n"


def load_report(*, tenant: str, root: Path | str | None = None) -> dict[str, Any] | None:
    """读盘取已生成的报告。落盘即唯一真相源，服务重启不丢。"""
    root = root if root is not None else default_workspace_root()
    checked = safe_slug(tenant)
    if checked is None:
        return None
    path = Path(root) / checked / "REPORT.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
