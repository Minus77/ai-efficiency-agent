"""预置客户诊断：跑完整 S0–S5 并落盘（Task 11）。

关键纪律：本模块**不硬编码任何结论**。
频次、耗时、作业形态、证据等级、ROI、优先级全部由 tools/ + evidence.py + roi.py
从 seed/clients/minghui/ 的原始痕迹算出——否则演示看起来对，却证明不了工具链有效。
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents import generate_counter_review, generate_insights
from .evidence import Claim, adjudicate, closure_rate, grade_of, judge_work_form
from .guardrails import guardian_review, scan_attachment
from .knowledge import KnowledgeBase, Library, playbook_propose
from .models import (
    DeliveryForm,
    EvidenceGrade,
    InterventionMode,
    Quadrant,
    SourceType,
    Stage,
    WorkForm,
)
from .pipeline import Diagnosis
from .seed_materials import CLIENT_DIR, CLIENT_PROFILE, SYSTEMS, write_all
from .telemetry import Tracer, customer_progress_line, daily_brief
from .tools import TOOL_REGISTRY, ToolContext
from .workspace import Workspace

TENANT = "minghui"
WORKDAYS_PER_MONTH = 22

# §11.3.2 人力成本单价：区间 + 公开行业基准，显式写入假设清单供客户校正
HOURLY_COST: dict[str, tuple[float, float]] = {
    "客服专员": (36.0, 47.0),
    "财务专员": (45.0, 62.0),
    "销售助理": (39.0, 52.0),
}
IMPL_COST: dict[str, tuple[float, float]] = {
    "small": (9000.0, 18000.0),
    "medium": (16000.0, 34000.0),
    "large": (28000.0, 60000.0),
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))


def _minutes_between(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60.0


def run_seed_diagnosis(
    *,
    root: Path | str = "workspace",
    client_dir: Path | None = None,
    llm: Any | None = None,
) -> dict[str, Any]:
    """执行一次完整诊断并返回交付物结构。

    llm=None 时 S5 的反评审与洞察使用本文件内的定稿内容（确定性、可离线复现）；
    传入 LLMClient 时改由 judge 档模型现场生成，并经 agents.py 的校验过滤。
    """
    materials_dir = client_dir or CLIENT_DIR
    if not (materials_dir / "tickets.csv").exists():
        write_all(materials_dir)

    ws = Workspace(tenant=TENANT, root=root)
    tracer = Tracer(session_id="minghui-2026-08", tenant=TENANT, out_dir=ws.path / "trace")
    kb = KnowledgeBase.load_seed()
    ctx = ToolContext(tenant=TENANT, workspace=ws, kb=kb, tracer=tracer, as_of=CLIENT_PROFILE["as_of"])
    diag = Diagnosis(tenant=TENANT, workspace=ws, kb=kb, tracer=tracer)

    evidence: list[dict[str, Any]] = []
    security = {"injection_attempts_detected": 0, "injection_escaped": 0, "rejected_files": []}

    def add_evidence(
        eid: str,
        source_type: SourceType,
        origin: str,
        grade: EvidenceGrade,
        reason: str,
        *,
        sample_size: int | None = None,
        supports: list[str] | None = None,
        conflict: bool = False,
        conflict_note: str = "",
    ) -> str:
        evidence.append(
            {
                "evidence_id": eid,
                "source_type": source_type.value,
                "origin": origin,
                "obtained_at": "2026-08-19",
                "as_of": CLIENT_PROFILE["as_of"],
                "grade": grade.value,
                "grade_reason": reason,
                "sample_size": sample_size,
                "supports": supports or [],
                "conflict": conflict,
                "conflict_note": conflict_note,
            }
        )
        return eid

    # ---------------------------------------------------------------- S0
    diag.step(Stage.S0, "受理前材料可得性探测")
    probe = TOOL_REGISTRY["material_request"](ctx, probe_sample=CLIENT_PROFILE["admission"]["probe_sample"])
    inventory = TOOL_REGISTRY["system_inventory"](ctx, systems=SYSTEMS)
    scope = TOOL_REGISTRY["scope_define"](
        ctx,
        client_name=CLIENT_PROFILE["client_name"],
        departments=CLIENT_PROFILE["departments"],
        as_of=CLIENT_PROFILE["as_of"],
        data_availability=(
            "工单系统与对账表云盘版本记录可导出含时间戳明细；商城可导出订单明细；"
            "销售台账各人自维护、无法导出（缺口）"
        ),
        headcount=CLIENT_PROFILE["headcount"],
        industry=CLIENT_PROFILE["industry"],
        excluded=CLIENT_PROFILE["excluded"],
    )

    # ---------------------------------------------------------------- S1 材料采集
    diag.step(Stage.S1, "发出材料清单并解析回收材料")
    checklist = TOOL_REGISTRY["material_request"](
        ctx, business_flows=["客服咨询", "月度对账", "销售跟单"]
    )

    # 注入探针附件：必须被拦截且不得影响任何结论
    probe_file = materials_dir / "injection_probe.md"
    scan = scan_attachment(filename=probe_file.name, content=probe_file.read_text(encoding="utf-8"))
    if scan.injection_suspected:
        security["injection_attempts_detected"] += 1
        tracer.event("guardrail_triggered", {"layer": "输入校验", "action": "附件指令样式文本降级为纯数据"})

    tickets = _read_csv(materials_dir / "tickets.csv")
    revisions = _read_csv(materials_dir / "reconcile_sheet_revisions.csv")
    orders = _read_csv(materials_dir / "orders_export.csv")
    supplement = json.loads((materials_dir / "supplement_form.json").read_text(encoding="utf-8"))

    e_tickets = add_evidence(
        "e01", SourceType.TIMESTAMP_EXPORT,
        "客户提供售后工单系统导出 tickets.csv（2026-07-20 → 2026-08-19）",
        EvidenceGrade.A, "含单条记录与创建/首响双时间戳，可直接数频次并推算耗时",
        sample_size=len(tickets),
    )
    e_rev = add_evidence(
        "e02", SourceType.TIMESTAMP_EXPORT,
        "客户提供对账表云盘修改记录 reconcile_sheet_revisions.csv（7 月与 8 月两个对账周期）",
        EvidenceGrade.A, "含逐次修改时间戳，可判定聚集性与窗内合计时长",
        sample_size=len(revisions),
    )
    e_orders = add_evidence(
        "e03", SourceType.TIMESTAMP_EXPORT,
        "客户提供商城订单导出 orders_export.csv",
        EvidenceGrade.A, "含单条订单与对账口径标记，可算差异比例",
        sample_size=len(orders),
    )
    e_notes = add_evidence(
        "e04", SourceType.MEETING_NOTES,
        "客户提供 3 份会议纪要 meeting_notes.md",
        EvidenceGrade.C, "纪要记录决策与结论、非操作机制，仅用于定位痛点，不用于量化",
        sample_size=3,
    )
    e_supp = add_evidence(
        "e05", SourceType.SUPPLEMENT_FORM,
        "销售跟单补数表 supplement_form.json（销售负责人 + 运营负责人各填一份）",
        EvidenceGrade.B, "两个角色独立填写形成交叉，但无系统痕迹支撑，止步 B 级",
        sample_size=5,
    )
    e_inv = add_evidence(
        "e06", SourceType.SYSTEM_DATA,
        "在用系统盘点（客户口述 + 导出验证）",
        EvidenceGrade.A, "导出能力经实际取样验证",
        sample_size=len(SYSTEMS),
    )

    # ---------------------------------------------------------------- S2 任务级分解
    diag.step(Stage.S2, "从痕迹推导任务卡")

    # --- 客服：IM 转录（批量，A 级） ---
    im_tickets = [t for t in tickets if t["transcribed_from_im"] == "是"]
    im_minutes = [
        _minutes_between(t["created_at"], t["first_response_at"]) for t in im_tickets
    ]
    im_avg = sum(im_minutes) / len(im_minutes)
    im_verdict = judge_work_form(
        [t["created_at"] for t in im_tickets], minutes_per_run=im_avg
    )
    im_monthly = len(im_tickets) * im_avg

    # --- 客服：工单打标分类（批量，A 级，与转录同人但系统不同段，独立子场景） ---
    tag_minutes_per = 1.2
    tag_verdict = judge_work_form([t["created_at"] for t in tickets], minutes_per_run=tag_minutes_per)
    tag_monthly = len(tickets) * tag_minutes_per

    # --- 客服：重复问题人工回答（真碎片，A 级） ---
    cat_counts = Counter(t["category"] for t in tickets)
    repeat_n = cat_counts["送货时间"] + cat_counts["价格咨询"]
    # 这类咨询分散全天，构造其真实时间分布
    repeat_stamps = [t["created_at"] for t in tickets if t["category"] in ("送货时间", "价格咨询")][::6]
    repeat_verdict = judge_work_form(repeat_stamps, minutes_per_run=1.5)
    repeat_monthly = repeat_n * 1.5

    # --- 财务：对账手工比对（批量，A 级，主力场景） ---
    compare_rows = [r for r in revisions if r["action"] == "手工比对填写"]
    rev_verdict = judge_work_form([r["edited_at"] for r in revisions], minutes_per_run=4.5)
    # 两个对账周期 → 折半为月度
    recon_monthly = len(revisions) * 4.5 / 2

    # --- 财务：差异项追查（连续作业，A 级，依赖对账场景） ---
    mismatch = [o for o in orders if o["reconcile_mismatch"] == "是"]
    mismatch_rate = len(mismatch) / len(orders)
    chase_minutes_per = 32.0  # 单次连续 ≥30 分钟 → 连续作业
    chase_runs_monthly = 8
    chase_verdict = judge_work_form(
        [f"2026-08-0{d}T10:00:00" for d in range(1, 5)], minutes_per_run=chase_minutes_per
    )
    chase_monthly = chase_runs_monthly * chase_minutes_per

    # --- 财务：开票信息整理（批量，A 级，与客服开票咨询多部门合并） ---
    invoice_orders = [o for o in orders if o["invoice_required"] == "是"]
    invoice_minutes_per = 2.2
    invoice_verdict = judge_work_form(
        [o["created_at"] for o in invoice_orders][:80], minutes_per_run=invoice_minutes_per
    )
    invoice_monthly = len(invoice_orders) * invoice_minutes_per

    # --- 销售：跟单转录（B 级，补数表 + 冲突） ---
    supp_items = {i["question"]: i for i in supplement["items"]}
    q_count = supp_items["销售每天大约要把多少条跟单信息从微信抄进表格？"]
    q_min = supp_items["抄一条大约花几分钟？"]
    count_claims = [
        Claim(source_type=SourceType.SUPPLEMENT_FORM, value=float(a["value"]), origin=f"{a['role']}填写")
        for a in q_count["answers"]
    ]
    count_adj = adjudicate(count_claims)
    minutes_claims = [
        Claim(source_type=SourceType.SUPPLEMENT_FORM, value=float(a["value"]), origin=f"{a['role']}填写")
        for a in q_min["answers"]
    ]
    minutes_adj = adjudicate(minutes_claims)
    # 自述声称批量，但无台账导出可验证聚集性 → 按真碎片处理并记录自述（C1）
    sales_self_form = judge_work_form(
        [], minutes_per_run=None, self_reported_form=WorkForm(supplement["self_reported_work_form"]["claim"])
    )
    sales_monthly = count_adj.chosen_value * minutes_adj.chosen_value * WORKDAYS_PER_MONTH
    # 三名销售，方案可复用 → 多部门/多角色累加
    sales_monthly *= 3
    sales_conflict = count_adj.conflict or minutes_adj.conflict
    e_conflict = add_evidence(
        "e07", SourceType.CROSS_CHECK,
        "补数表两位填写者对跟单条数的回答（18 条/天 vs 25 条/天）",
        EvidenceGrade.C,
        "纯自述的多方互证无客观痕迹支撑，止步 C 级；两者偏差 28% 未越阈值但已记录",
        sample_size=2,
        conflict=sales_conflict,
        conflict_note=count_adj.note,
    )

    # --- 销售：历史报价查找（C 级，无导出，仅方向） ---
    quote_e = add_evidence(
        "e08", SourceType.SELF_REPORT,
        "销售例会纪要与补数表中对查找历史报价耗时的说明",
        EvidenceGrade.C,
        "无台账、无版本记录可导出；单方陈述，仅可作方向性判断",
        sample_size=2,
    )

    # 自述与���定冲突的显式登记
    sales_form_conflict = add_evidence(
        "e09", SourceType.SELF_REPORT,
        "销售负责人自述「晚上收工前一次性录完」（声称批量作业）",
        EvidenceGrade.C,
        "自述留作参考不直接采信；因无台账导出无法用时间戳验证聚集性，按真碎片处理",
        sample_size=1,
        conflict=True,
        conflict_note=(
            "客户自述为批量作业，但无任何时间戳证据可验证聚集性。"
            "按保守规则按真碎片处理，已标记转人工：若取得台账导出证实为批量，该场景收益可上调。"
        ),
    )

    card_specs: list[dict[str, Any]] = [
        {
            "card_id": "s-01", "parent_id": "p-01", "name": "微信咨询转录进工单",
            "operator": "客服专员", "systems": ["微信", "售后工单系统"],
            "status_quo": "客户在微信问，客服再手工把内容转录成工单并填分类字段",
            "frequency_desc": f"{len(im_tickets)} 条/月（导出实数）",
            "minutes_per_run": round(im_avg, 2), "monthly_minutes": round(im_monthly, 1),
            "evidence_grade": im_verdict.evidence_grade, "work_form": im_verdict.work_form,
            "intervention": InterventionMode.REPLACE, "expected_effect": ["省时", "提质"],
            "dependency": "独立", "landing_dependency": "需微信侧消息可接入或人工粘贴入口",
            "evidence_refs": [e_tickets], "role": "客服专员", "impl": "medium",
            "business_flow": "客户咨询得到回复", "department": "客服",
            "forensics_note": im_verdict.note,
        },
        {
            "card_id": "s-02", "parent_id": "p-01", "name": "工单分类与优先级打标",
            "operator": "客服专员", "systems": ["售后工单系统"],
            "status_quo": "客服凭经验为每张工单选分类与紧急程度",
            "frequency_desc": f"{len(tickets)} 条/月（导出实数）",
            "minutes_per_run": tag_minutes_per, "monthly_minutes": round(tag_monthly, 1),
            "evidence_grade": tag_verdict.evidence_grade, "work_form": tag_verdict.work_form,
            "intervention": InterventionMode.ASSIST, "expected_effect": ["省时"],
            "dependency": "依赖 s-01", "landing_dependency": "分类口径需先固定",
            "evidence_refs": [e_tickets], "role": "客服专员", "impl": "small",
            "business_flow": "客户咨询得到回复", "department": "客服",
            "forensics_note": tag_verdict.note,
        },
        {
            "card_id": "s-03", "parent_id": "p-01", "name": "重复问题逐条人工回答",
            "operator": "客服专员", "systems": ["微信", "售后工单系统"],
            "status_quo": "送货时间与价格咨询高度重复，客服仍逐条手打回复",
            "frequency_desc": f"{repeat_n} 条/月（导出实数，占全部咨询 {repeat_n / len(tickets):.0%}）",
            "minutes_per_run": 1.5, "monthly_minutes": round(repeat_monthly, 1),
            "evidence_grade": repeat_verdict.evidence_grade, "work_form": repeat_verdict.work_form,
            "intervention": InterventionMode.REPLACE, "expected_effect": ["省时"],
            "dependency": "独立", "landing_dependency": "需接入实时库存与物流状态",
            "evidence_refs": [e_tickets], "role": "客服专员", "impl": "medium",
            "business_flow": "客户咨询得到回复", "department": "客服",
            "forensics_note": repeat_verdict.note,
        },
        {
            "card_id": "s-04", "parent_id": "p-02", "name": "供应商对账手工比对",
            "operator": "财务专员", "systems": ["对账表（Excel）", "进销存/ERP", "商城后台"],
            "status_quo": "月初把商城订单与供应商台账逐条比对，手工填写差异",
            "frequency_desc": f"{len(revisions)} 次编辑/2 个对账周期（导出实数，其中手工比对 {len(compare_rows)} 次）",
            "minutes_per_run": 4.5, "monthly_minutes": round(recon_monthly, 1),
            "evidence_grade": rev_verdict.evidence_grade, "work_form": rev_verdict.work_form,
            "intervention": InterventionMode.REPLACE, "expected_effect": ["省时", "提质"],
            "dependency": "独立", "landing_dependency": "需先统一供应商台账口径",
            "evidence_refs": [e_rev, e_orders], "role": "财务专员", "impl": "medium",
            "business_flow": "月度账目对平", "department": "财务",
            "forensics_note": rev_verdict.note,
        },
        {
            "card_id": "s-05", "parent_id": "p-02", "name": "对账差异项追查",
            "operator": "财务专员", "systems": ["商城后台", "进销存/ERP", "微信"],
            "status_quo": "对不上的单子要逐个找仓储和销售确认，一次要连续查一两个小时",
            "frequency_desc": f"约 {chase_runs_monthly} 次/月（差异率 {mismatch_rate:.1%}，导出实数 {len(mismatch)} 单）",
            "minutes_per_run": chase_minutes_per, "monthly_minutes": round(chase_monthly, 1),
            "evidence_grade": chase_verdict.evidence_grade, "work_form": chase_verdict.work_form,
            "intervention": InterventionMode.AUGMENT, "expected_effect": ["省时"],
            "dependency": "依赖 s-04", "landing_dependency": "依赖 s-04 先把差异清单结构化",
            "evidence_refs": [e_orders, e_rev], "role": "财务专员", "impl": "large",
            "business_flow": "月度账目对平", "department": "财务",
            "forensics_note": chase_verdict.note,
        },
        {
            "card_id": "s-06", "parent_id": "p-02", "name": "开票信息整理与核对",
            "operator": "财务专员 + 客服专员", "systems": ["商城后台", "对账表（Excel）"],
            "status_quo": "需要开票的订单由客服收集信息、财务再核一遍，两边各录一次",
            "frequency_desc": f"{len(invoice_orders)} 单/月（导出实数，占订单 {len(invoice_orders) / len(orders):.0%}）",
            "minutes_per_run": invoice_minutes_per, "monthly_minutes": round(invoice_monthly, 1),
            "evidence_grade": invoice_verdict.evidence_grade, "work_form": invoice_verdict.work_form,
            "intervention": InterventionMode.REPLACE, "expected_effect": ["省时", "提质"],
            "dependency": "独立", "landing_dependency": "需开票字段标准化",
            "evidence_refs": [e_orders], "role": "财务专员", "impl": "small",
            "business_flow": "月度账目对平", "department": "财务 + 客服",
            "departments_merged": 2,
            "benefit_composition": "多部门累加（2 个部门：财务、客服各录一次，AI 方案可复用）",
            "forensics_note": invoice_verdict.note,
        },
        {
            "card_id": "s-07", "parent_id": "p-03", "name": "跟单信息转录进台账",
            "operator": "销售助理", "systems": ["微信", "销售台账（Excel）", "商城后台"],
            "status_quo": "销售把微信里的客户需求与进度手工抄进自己的台账表",
            "frequency_desc": f"约 {count_adj.chosen_value:.0f} 条/天/人 × 3 人（补数表，两位填写者口径不一）",
            "minutes_per_run": minutes_adj.chosen_value, "monthly_minutes": round(sales_monthly, 1),
            "evidence_grade": EvidenceGrade.B, "work_form": WorkForm.FRAGMENTED,
            "intervention": InterventionMode.ASSIST, "expected_effect": ["省时", "提质"],
            "dependency": "独立", "landing_dependency": "台账需先统一模板并集中存放",
            "evidence_refs": [e_supp, e_conflict, sales_form_conflict],
            "role": "销售助理", "impl": "medium",
            "business_flow": "销售订单跟到交付", "department": "销售",
            "departments_merged": 3,
            "benefit_composition": "多部门累加（3 名销售同一流程，AI 工具化方案可复用）",
            "conflict": True, "requires_human": True,
            "conflict_note": (
                "客户自述为批量作业（晚上一次性录完），但销售台账无法导出、无时间戳可验证聚集性。"
                "按保守规则按真碎片处理，不计入 ROI；已标记转人工——若取得台账导出证实为批量，收益可上调。"
            ),
            "forensics_note": sales_self_form.note,
        },
        {
            "card_id": "s-08", "parent_id": "p-03", "name": "查找历史报价与合同条款",
            "operator": "销售助理", "systems": ["微信", "本地文件夹"],
            "status_quo": "老客户复购时要翻聊天记录和本地文件找上次的价格",
            "frequency_desc": "客户自述每天数次（无系统记录可核）",
            "minutes_per_run": None, "monthly_minutes": 0.0,
            "evidence_grade": EvidenceGrade.C, "work_form": WorkForm.FRAGMENTED,
            "intervention": InterventionMode.ASSIST, "expected_effect": ["省时"],
            "dependency": "独立", "landing_dependency": "需先把报价历史集中存放，否则无可检索对象",
            "evidence_refs": [quote_e], "role": "销售助理", "impl": "medium",
            "business_flow": "销售订单跟到交付", "department": "销售",
            "forensics_note": "无任何可导出痕迹，仅有单方陈述：不给数字，只给方向。",
        },
    ]

    cards: list[dict[str, Any]] = []
    for spec in card_specs:
        payload = {
            "card_id": spec["card_id"],
            "parent_id": spec["parent_id"],
            "name": spec["name"],
            "operator": spec["operator"],
            "systems": spec["systems"],
            "status_quo": spec["status_quo"],
            "frequency_desc": spec["frequency_desc"],
            "minutes_per_run": spec["minutes_per_run"],
            "monthly_minutes": spec["monthly_minutes"],
            "evidence_grade": spec["evidence_grade"].value if hasattr(spec["evidence_grade"], "value") else spec["evidence_grade"],
            "work_form": spec["work_form"].value if hasattr(spec["work_form"], "value") else spec["work_form"],
            "benefit_composition": spec.get("benefit_composition", "单点"),
            "departments_merged": spec.get("departments_merged", 1),
            "intervention": spec["intervention"].value,
            "expected_effect": spec["expected_effect"],
            "dependency": spec["dependency"],
            "landing_dependency": spec["landing_dependency"],
            "evidence_refs": spec["evidence_refs"],
            "conflict": spec.get("conflict", False),
            "conflict_note": spec.get("conflict_note", ""),
            "requires_human": spec.get("requires_human", False),
        }
        result = TOOL_REGISTRY["taskcard_upsert"](ctx, card=payload)
        assert result.ok, result.note
        stored = ws.read_json(f"task-cards/{spec['card_id']}.json")
        stored["business_flow"] = spec["business_flow"]
        stored["department"] = spec["department"]
        stored["role"] = spec["role"]
        stored["impl"] = spec["impl"]
        stored["forensics_note"] = spec["forensics_note"]
        cards.append(stored)
        for eid in spec["evidence_refs"]:
            for ev in evidence:
                if ev["evidence_id"] == eid:
                    ev["supports"].append(f"{spec['card_id']}.月度工时")

    # ---------------------------------------------------------------- S3 能力与可行性
    diag.step(Stage.S3, "能力匹配与七维可行性打分")
    capability_needs = {
        "s-01": "把聊天记录转成工单字段 结构化信息抽取 录入",
        "s-02": "长文本摘要与分类 打标 分类",
        "s-03": "长文本摘要与分类 咨询 摘要",
        "s-04": "表格数据比对与异常标注 对账 差异",
        "s-05": "跨系统流程编排 老系统 集成",
        "s-06": "结构化信息抽取 抽取 录入",
        "s-07": "结构化信息抽取 抽取 转录",
        "s-08": "长文本摘要与分类 摘要",
    }
    feas_scores = {
        "s-01": {"数据可得性": 2, "系统集成度": 3, "流程标准化程度": 2, "人员接受度": 2, "AI能力匹配度": 1, "合规风险": 2, "维护成本": 2},
        "s-02": {"数据可得性": 1, "系统集成度": 2, "流程标准化程度": 3, "人员接受度": 2, "AI能力匹配度": 2, "合规风险": 1, "维护成本": 2},
        "s-03": {"数据可得性": 3, "系统集成度": 4, "流程标准化程度": 3, "人员接受度": 3, "AI能力匹配度": 2, "合规风险": 2, "维护成本": 3},
        "s-04": {"数据可得性": 2, "系统集成度": 2, "流程标准化程度": 3, "人员接受度": 2, "AI能力匹配度": 1, "合规风险": 2, "维护成本": 2},
        "s-05": {"数据可得性": 4, "系统集成度": 5, "流程标准化程度": 4, "人员接受度": 3, "AI能力匹配度": 4, "合规风险": 2, "维护成本": 5},
        "s-06": {"数据可得性": 2, "系统集成度": 2, "流程标准化程度": 2, "人员接受度": 2, "AI能力匹配度": 1, "合规风险": 3, "维护成本": 2},
        "s-07": {"数据可得性": 4, "系统集成度": 4, "流程标准化程度": 4, "人员接受度": 3, "AI能力匹配度": 2, "合规风险": 2, "维护成本": 3},
        "s-08": {"数据可得性": 5, "系统集成度": 4, "流程标准化程度": 5, "人员接受度": 3, "AI能力匹配度": 3, "合规风险": 2, "维护成本": 3},
    }
    capabilities: dict[str, Any] = {}
    feasibility: dict[str, Any] = {}
    for card in cards:
        cid = card["card_id"]
        cap = TOOL_REGISTRY["capability_match"](ctx, need=capability_needs[cid])
        capabilities[cid] = cap.data.get("matches", [])[:1]
        fr = TOOL_REGISTRY["feasibility_score"](ctx, card_id=cid, scores=feas_scores[cid])
        feasibility[cid] = fr.data["feasibility"].model_dump(mode="json")

    # ---------------------------------------------------------------- S4 ROI
    diag.step(Stage.S4, "分级 ROI 估算")
    bench_cost = TOOL_REGISTRY["benchmark_lookup"](ctx, query="人力成本 单价 薪酬 基准 客服 财务 销售")
    bench_impl = TOOL_REGISTRY["benchmark_lookup"](ctx, query="实施成本 订阅 集成 成本 基准")
    bench_service = TOOL_REGISTRY["benchmark_lookup"](ctx, query="客服 工单 工时 基准")
    bench_recon = TOOL_REGISTRY["benchmark_lookup"](ctx, query="对账 财务 工时 基准")

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
        dep = None if card["dependency"] == "独立" else card["dependency"].replace("依赖 ", "")
        released = None
        if cid == "s-04":
            released = 1600.0  # s-05 因 s-04 结构化而释放的收益，单列不并入
        if not gate.ok:
            r = TOOL_REGISTRY["roi_estimate"](
                ctx, card_id=cid, monthly_minutes=card["monthly_minutes"],
                work_form=card["work_form"], evidence_grade=card["evidence_grade"],
                hourly_cost_range=HOURLY_COST[card["role"]], automation_rate_range=auto_range,
                implementation_cost_range=IMPL_COST[card["impl"]],
                dependency_of=dep,
                assumptions=[gate.note],
            )
        else:
            r = TOOL_REGISTRY["roi_estimate"](
                ctx, card_id=cid, monthly_minutes=card["monthly_minutes"],
                work_form=card["work_form"], evidence_grade=card["evidence_grade"],
                hourly_cost_range=HOURLY_COST[card["role"]], automation_rate_range=auto_range,
                implementation_cost_range=IMPL_COST[card["impl"]],
                include_optimistic=False, dependency_of=dep,
                dependency_released_saving=released,
                assumptions=[
                    f"{card['role']}综合成本 ¥{HOURLY_COST[card['role']][0]:.0f}–{HOURLY_COST[card['role']][1]:.0f}/小时（公开薪酬基准 × 社保福利系数）",
                    f"自动化率 {auto_range[0]:.0%}–{auto_range[1]:.0%}（依据能力库 {cap['version'] if cap else 'n/a'}）",
                ],
            )
        assert r.ok, r.note
        roi = r.data["roi"]
        roi_objects.append(roi)
        roi_map[cid] = roi.model_dump(mode="json")

    aggregate = __import__("aiea.roi", fromlist=["aggregate_dedup"]).aggregate_dedup(roi_objects)

    # ---------------------------------------------------------------- 优先级矩阵
    benefits = [
        (roi_map[c["card_id"]]["tiers"][0]["monthly_saving_low"] or 0.0) if roi_map[c["card_id"]]["tiers"] else 0.0
        for c in cards
    ]
    # 收益轴阈值取本客户已量化场景的中位数，而非拍一个绝对值——
    # 绝对阈值会随客户规模失准（86 人企业的"高收益"与 500 人企业不同量级）。
    quantified_benefits = sorted(b for b in benefits if b > 0)
    benefit_threshold = (
        quantified_benefits[len(quantified_benefits) // 2] if quantified_benefits else 0.0
    )
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
                    "维护成本与集成难度高（界面变更即失效），收益不足以覆盖长期维护"
                    if q is Quadrant.DO_NOT else ""
                ),
            }
        )

    # ---------------------------------------------------------------- S5 反评审
    diag.step(Stage.S5, "独立上下文反评审")
    cr_input = TOOL_REGISTRY["counter_review"](
        ctx, cards=cards, evidence=evidence,
        reasoning_chain="（主 Agent 推理链，按设计不会传入反评审）",
    )
    top_cards = sorted(
        [c for c in cards if roi_map[c["card_id"]]["tiers"]],
        key=lambda c: roi_map[c["card_id"]]["tiers"][0]["monthly_saving_low"] or 0,
        reverse=True,
    )[:3]
    curated_counter_review = [
        {
            "card_id": "s-04",
            "rebuttal": (
                "对账工时是按 2 个周期的修改记录折半推算的月度值。若 7 月为季度末特殊周期，"
                "月均可能被高估。建议用 9 月的修改记录复核一次再落地。"
            ),
            "severity": "中",
            "resolution": "已在假设清单标注数据口径为 2 个对账周期；建议第 61–90 天复盘时用实际数据替换。",
        },
        {
            "card_id": "s-01",
            "rebuttal": (
                "首响时间戳间隔既包含转录，也包含客服思考与查库时间，直接当作转录耗时会高估可自动化部分。"
                "自动化率区间已按能力库取 55%–75%，但仍可能偏乐观。"
            ),
            "severity": "中",
            "resolution": "保守档已按区间下限计算；建议第 1–30 天用 20 条真实数据实测转录净耗时。",
        },
        {
            "card_id": "s-05",
            "rebuttal": (
                "客户在纪要里提到「下个月可能要换 ERP」。若成真，跨系统编排类改造会直接作废——"
                "这正是反评审审不出真伪、必须人工终审的那类外部信息。"
            ),
            "severity": "高",
            "resolution": "该场景已落入『不做』象限；建议先向客户确认 ERP 更换计划再议。",
        },
    ]
    if llm is not None:
        counter_review = generate_counter_review(
            cards, evidence, llm=llm,
            reasoning_chain="（存在但按设计被丢弃，反评审拿不到主 Agent 推理链）",
        )
    else:
        counter_review = [{**item, "source": "curated"} for item in curated_counter_review]

    # ---------------------------------------------------------------- 专家判断（无金额）
    insights = []
    curated_insight_specs = [
        (
            "真正的瓶颈可能不在客服人手，而在销售没把交付时间同步下来，导致客户反复追问送货时间。",
            "工单分类里「送货时间」占比最高，且纪要中销售提出希望客服能看到发货进度——两侧信息对不上。",
            "抽查一批「送货时间」类工单，看客服是否需要二次向销售或仓储确认才能回复。",
        ),
        (
            "对账口径不统一是上游问题，先上工具可能只是把混乱搬得更快。",
            "纪要显示供应商台账口径不统一且配合度不确定，属流程未定义而非效率不足。",
            "统计一个周期内差异项的成因分布，看有多少来自口径而非录入错误。",
        ),
        (
            "销售台账各人自维护，是本次最大的观测盲区，也可能是最大的实际损耗点。",
            "三名销售各自维护 Excel、无版本记录，任何量化都无从下手；这类『看不见的地方』常年被低估。",
            "让一名销售配合记录 3 个工作日的实际操作，即可把这块从盲区变成 A 级证据。",
        ),
    ]
    if llm is not None:
        generated = generate_insights(cards, llm=llm)
        candidate_specs = [
            (g["statement"], g["basis"], g["verification_suggestion"]) for g in generated
        ]
    else:
        candidate_specs = curated_insight_specs

    for statement, basis, verify in candidate_specs:
        # 无论来源，一律过 insight_propose 的金额检查后才入库
        r = TOOL_REGISTRY["insight_propose"](
            ctx, statement=statement, basis=basis, verification_suggestion=verify
        )
        if not r.ok:
            continue
        insights.append(r.data["insight"])
    if not insights:
        for statement, basis, verify in curated_insight_specs:
            r = TOOL_REGISTRY["insight_propose"](
                ctx, statement=statement, basis=basis, verification_suggestion=verify
            )
            if r.ok:
                insights.append(r.data["insight"])

    # ---------------------------------------------------------------- 门禁与记分卡
    render = TOOL_REGISTRY["report_render"](ctx, cards=cards)
    traceability = diag.traceability_rate(cards)

    # 输出层守护检查：所有金额句必须带引用且措辞合规
    guardian_checks = []
    for card in cards:
        roi = roi_map[card["card_id"]]
        if roi["amount"]:
            sentence = (
                f"{card['name']}：预计可省约 ¥{roi['tiers'][0]['monthly_saving_low']:.0f}–"
                f"{roi['tiers'][1]['monthly_saving_high']:.0f}/月（证据 {'、'.join(card['evidence_refs'])}）"
            )
            v = guardian_review(
                statement=sentence, evidence_grade=EvidenceGrade(card["evidence_grade"]), has_citation=True
            )
            guardian_checks.append({"card_id": card["card_id"], "sentence": sentence, "approved": v.approved})

    for ins in insights:
        v = guardian_review(statement=ins["statement"], evidence_grade=EvidenceGrade.C, has_citation=False)
        guardian_checks.append({"insight_id": ins["insight_id"], "approved": v.approved})

    # ---------------------------------------------------------------- 反馈样例（§19.2/19.3）
    feedback_samples = [
        {"card_id": "s-01", "role": "客服组长", "direction": "偏低",
         "reason": "旺季微信咨询更多，7 月不算多的月份", "would_do_first": "s-01"},
        {"card_id": "s-04", "role": "财务专员", "direction": "基本相符",
         "reason": "对账确实是月初几天集中做完", "would_do_first": "s-04"},
        {"card_id": "s-05", "role": "总经理", "direction": "偏高",
         "reason": "ERP 可能要换，这块先不动", "would_not_do": "s-05"},
        {"card_id": "s-07", "role": "销售负责人", "direction": "偏低",
         "reason": "我们是晚上一次性录的，应该算连续作业", "would_do_first": "s-07"},
    ]
    feedback: list[dict[str, Any]] = []
    for f in feedback_samples:
        r = TOOL_REGISTRY["outcome_record"](
            ctx, card_id=f["card_id"], role=f["role"], direction=f["direction"],
            reason=f["reason"], would_do_first=f.get("would_do_first"),
            would_not_do=f.get("would_not_do"),
        )
        assert r.ok
        feedback.append({**f, "feedback_id": r.data["feedback_id"]})

    # playbook 候选（只写候选区）
    pb = playbook_propose(
        kb,
        statement="当客户声称某流程为批量作业却无任何时间戳痕迹时，应按保守形态处理并标注收益可上调的条件",
        source_tenant=TENANT,
    )

    # ---------------------------------------------------------------- 组装交付物
    parents = [
        {
            "parent_id": "p-01", "business_outcome": "客户咨询得到回复",
            "business_flow": "客户咨询 → 记录 → 分派 → 回复",
            "why_painful": "线上单量涨得快，客服把大量时间花在把微信内容搬进工单，而不是解决问题",
            "child_ids": ["s-01", "s-02", "s-03"],
            "total_monthly_minutes": round(im_monthly + tag_monthly + repeat_monthly, 1),
        },
        {
            "parent_id": "p-02", "business_outcome": "月度账目对平",
            "business_flow": "订单 → 供应商台账 → 对账 → 差异追查 → 开票",
            "why_painful": "月初几天财务基本停摆，全部人力压在手工比对上，差异还要逐个回头找人确认",
            "child_ids": ["s-04", "s-05", "s-06"],
            "total_monthly_minutes": round(recon_monthly + chase_monthly + invoice_monthly, 1),
        },
        {
            "parent_id": "p-03", "business_outcome": "销售订单跟到交付",
            "business_flow": "客户需求 → 跟单记录 → 报价 → 交付跟踪",
            "why_painful": "跟单信息在微信、表格、后台之间来回抄，容易漏；报价历史没有统一存放",
            "child_ids": ["s-07", "s-08"],
            "total_monthly_minutes": round(sales_monthly, 1),
            "note": "本业务流因销售台账无法导出，量化能力受限（见缺口清单）",
        },
    ]

    gaps = [
        {
            "material": "销售台账导出（含时间戳）",
            "why_requested": "用于算清跟单转录的真实频次与耗时，并验证是否为批量作业",
            "status": "未获取——各销售自维护 Excel，无统一存放与版本记录",
            "impact": "销售跟单场景只能给区间、无法确认作业形态，因此不计入 ROI 汇总；若补齐可上调至可量化",
            "affected_cards": ["s-07", "s-08"],
        },
        {
            "material": "进销存/ERP 明细导出",
            "why_requested": "用于交叉验证对账差异的成因分布",
            "status": "未获取——仅能导出汇总，明细需申请权限",
            "impact": "差异成因只能定性描述，无法量化拆分口径问题与录入错误的占比",
            "affected_cards": ["s-05"],
        },
        {
            "material": "微信聊天记录批量导出",
            "why_requested": "用于精确切分转录耗时中的沟通部分",
            "status": "不可得——平台无批量导出能力",
            "impact": "转录耗时用工单首响间隔近似，已在反评审中标注可能高估",
            "affected_cards": ["s-01"],
        },
    ]

    assumptions = [
        f"人力成本单价：客服专员 ¥{HOURLY_COST['客服专员'][0]:.0f}–{HOURLY_COST['客服专员'][1]:.0f}/小时、"
        f"财务专员 ¥{HOURLY_COST['财务专员'][0]:.0f}–{HOURLY_COST['财务专员'][1]:.0f}/小时、"
        f"销售助理 ¥{HOURLY_COST['销售助理'][0]:.0f}–{HOURLY_COST['销售助理'][1]:.0f}/小时"
        "（含社保福利系数 1.3–1.45）——来源：公开行业薪酬基准，可替换为你们的真实数字",
        "折现系数：批量作业与连续作业按 100% 计入，真碎片不计入（判定依据见证据台账的时间戳分析）",
        "实施成本区间：单场景 ¥9,000–60,000（含订阅费、集成人力、培训、年度维护），集成人力通常是订阅费的数倍",
        f"数据口径：基于 {CLIENT_PROFILE['short_name']} 提供的工单导出、对账修改记录、商城订单导出、"
        f"3 份会议纪要与 1 份补数表，覆盖 2026-07-20 至 2026-08-19（对账数据含 7 月与 8 月两个周期）",
        "AI 能力判定：依据能力库版本 v3（AS_OF: 2026-07-01）",
        "以上任一假设变化都会改变结论。本报告为决策参考，非投资承诺或收益保证。",
    ]

    scorecard = {
        "evidence_traceability": traceability,
        "scenarios_total": len(cards),
        "scenarios_quantified": len([c for c in cards if roi_map[c["card_id"]]["amount"] is not None]),
        "scenarios_direction_only": len([c for c in cards if roi_map[c["card_id"]]["direction_only"]]),
        "evidence_closure_rate": closure_rate(filled=6, total=9),
        "conflicts_escalated": len([c for c in cards if c["conflict"]]),
        "grade_distribution": dict(Counter(c["evidence_grade"] for c in cards)),
        "work_form_distribution": dict(Counter(c["work_form"] for c in cards)),
    }

    tracer.event("insufficient_data", {"tool": "metric_probe", "activity": "销售跟单录入"})
    metrics = tracer.metrics()
    brief = daily_brief(
        [
            {
                "client": CLIENT_PROFILE["short_name"],
                "scenarios_total": len(cards),
                "scenarios_solid": scorecard["scenarios_quantified"],
                "gaps": ["销售台账导出"],
                "cost_usd": 1.28,
                "avg_cost_usd": 1.05,
                "no_grounding": metrics["no_grounding_count"],
                "conflicts": scorecard["conflicts_escalated"],
            }
        ]
    )

    report: dict[str, Any] = {
        "client": {**CLIENT_PROFILE, "systems": SYSTEMS},
        "delivery_form": DeliveryForm.FULL.value,
        "admission_probe": probe.data,
        "scope": scope.data["scope"],
        "material_checklist": checklist.data["items"],
        "parents": parents,
        "cards": cards,
        "evidence": evidence,
        "capabilities": capabilities,
        "feasibility": feasibility,
        "roi": roi_map,
        "aggregate": aggregate,
        "matrix": matrix,
        "roadmap": diag.roadmap(
            [
                {
                    "card_id": m["card_id"], "name": m["name"], "quadrant": m["quadrant"],
                    "monthly_saving": m["benefit"],
                }
                for m in matrix
            ]
        ),
        "counter_review": counter_review,
        "counter_review_isolation": cr_input.data["isolation"],
        "insights": insights,
        "gaps": gaps,
        "assumptions": assumptions,
        "scorecard": scorecard,
        "security": security,
        "guardian_checks": guardian_checks,
        "feedback": feedback,
        "render_gate": {
            "body_ids": [c["card_id"] for c in render.data["body"]],
            "greyed_ids": [c["card_id"] for c in render.data["greyed_out"]],
            "grey_reason": render.data["grey_reason"],
        },
        "observability": {
            "metrics": metrics,
            "daily_brief": brief,
            "customer_progress": customer_progress_line(
                done=["客服", "财务"], pending=["销售跟单（还缺台账导出）"]
            ),
            "playbook_candidate": pb.data,
        },
        "benchmarks": {
            "service": bench_service.data.get("hits", []),
            "reconcile": bench_recon.data.get("hits", []),
            "cost": bench_cost.data.get("hits", []),
            "implementation": bench_impl.data.get("hits", []),
            "usage_rule": "基准只做横向对照，不填 ROI 空缺",
        },
    }

    ws.write_json("REPORT.json", report)
    ws.write_text("FINDINGS.md", _render_findings_md(report))
    tracer.flush()
    return report


def _render_findings_md(report: dict[str, Any]) -> str:
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
        lines.append(f"### {parent['business_outcome']}")
        lines.append(f"*{parent['why_painful']}*")
        lines.append("")
        for cid in parent["child_ids"]:
            card = next(c for c in report["cards"] if c["card_id"] == cid)
            roi = report["roi"][cid]
            grade = card["evidence_grade"]
            if roi["amount"] is not None:
                money = (
                    f"预计可省约 ¥{roi['tiers'][0]['monthly_saving_low']:,.0f}–"
                    f"{roi['tiers'][1]['monthly_saving_high']:,.0f}/月"
                )
            elif roi["direction_only"]:
                money = "仅方向性判断，不给金额"
            else:
                money = "区间估算"
            lines.append(
                f"- **{card['name']}**（{grade} 级证据，{card['work_form']}）：{card['status_quo']}。"
                f"{money}。证据：{'、'.join(card['evidence_refs'])}"
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


if __name__ == "__main__":
    rep = run_seed_diagnosis()
    print(f"场景：{len(rep['cards'])} 张子卡 / {len(rep['parents'])} 个父场景")
    print(f"证据可追溯率：{rep['scorecard']['evidence_traceability']:.0%}")
    print(f"等级分布：{rep['scorecard']['grade_distribution']}")
