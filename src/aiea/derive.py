"""场景推导：从解析出的材料信号推出任务卡。

这是「工具」与「demo」的分界线：卡片必须由材料算出来，不内置任何默认场景。

分工（刻意划得很死）：
- **数字**全部由 evidence.py / 本模块的聚簇计算得出，模型碰不到
- **命名**（业务结果名、场景名、操作者）交给 LLM，因为这是语言判断而非计算
- 模型引用了不存在的列 → 丢弃该条（防工具误用式幻觉）
- 无 LLM 或 LLM 失败 → 确定性兜底命名，推导本身不受影响

证据等级由材料形态决定，不由模型自评：
- 有时间戳明细 → A 级，且可判定作业形态
- 有明细无时间戳 → B 级，作业形态**按保守处理为真碎片**并说明原因（没有痕迹就是没有）
- 仅汇总 / 纪要 → C 级，零工时、仅方向
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .evidence import judge_work_form
from .intake import ParsedMaterial
from .llm import CostBreakerTripped, LLMResponseFormatError
from .models import EvidenceGrade, InterventionMode, SourceType, WorkForm

# 观测窗口内的记录折算为月度的基准（22 个工作日）
WORKDAYS_PER_MONTH = 22
# 单条操作的兜底耗时（分钟）：仅当无法从时间戳间隔推算时使用，且会在卡上标注
FALLBACK_MINUTES_PER_RUN = 2.0
# 一个类别至少这么多条记录才单独成场景，避免把长尾噪声拆成一堆卡
MIN_RECORDS_PER_SCENARIO = 8
MAX_SCENARIOS = 8

_NEVER_FALLBACK = (CostBreakerTripped, ImportError, TypeError, AssertionError)
_MAY_FALLBACK = (LLMResponseFormatError,)

NAMING_SYSTEM = """你是一名精益顾问，正在把客户导出数据里的「活动簇」翻译成人能看懂的业务场景名。

给你的每一簇都已经算好了频次与工时——**你不要给任何数字**，只负责命名与描述。

对每一簇输出：
- scenario_name：动词+对象的操作序列名，如「微信咨询转录进工单」，不超过 14 字
- business_outcome：这件事服务于哪个业务结果（老板视角），如「客户咨询得到回复」，不超过 12 字
- operator：谁在做，如「客服专员」
- role：岗位归类，只能从 客服专员 / 财务专员 / 销售助理 / 运营专员 中选一个
- status_quo：一句话说清现在是怎么做的，不超过 40 字

严格要求：
- column 与 value 必须原样引用我给你的值，不得改写或编造
- 不要输出任何金额、工时、频次数字
- 只输出 JSON：{"activities":[{"column":"...","value":"...","scenario_name":"...","business_outcome":"...","operator":"...","role":"...","status_quo":"..."}]}"""

_ROLE_WHITELIST = ("客服专员", "财务专员", "销售助理", "运营专员")

# 列名 → 默认岗位猜测（兜底命名用）
_ROLE_HINTS = (
    (("ticket", "工单", "咨询", "客服", "service", "channel"), "客服专员"),
    (("recon", "对账", "invoice", "开票", "финанс", "amount", "金额", "财务"), "财务专员"),
    (("sales", "销售", "order", "订单", "客户跟单"), "销售助理"),
)


# 切分列的取向：按"做的是什么事"切分，而不是"从哪来"或"谁做的"。
# 渠道与人名不是活动划分——按它们切会把同一件事拆成多个场景，把不同的事并成一个。
_ACTIVITY_COL = re.compile(
    r"(categ|type|kind|reason|topic|action|业务|类别|类型|事项|动作|原因|环节|工序)", re.IGNORECASE
)
_NOT_ACTIVITY_COL = re.compile(
    r"(channel|source|handler|owner|operator|agent|assignee|user|name|渠道|来源|处理人|负责人|经办|客服|销售员|姓名|_id$|^id$|no$|编号)",
    re.IGNORECASE,
)


def _split_score(col: str, values: dict[str, int]) -> float:
    """给候选切分列打分。分数越高越适合作为活动划分依据。"""
    if _NOT_ACTIVITY_COL.search(col):
        return -1.0
    n_values = len(values)
    if not (1 <= n_values <= 8):
        return -1.0
    score = 1.0
    if _ACTIVITY_COL.search(col):
        score += 3.0
    # 取值分布过于集中（单值占比 >95%）说明区分度低，但仍可用（整份材料一个场景）
    total = sum(values.values()) or 1
    top_share = max(values.values()) / total
    if n_values > 1 and top_share < 0.95:
        score += 1.0
    return score


def _guess_role(hint: str) -> str:
    low = hint.lower()
    for keys, role in _ROLE_HINTS:
        if any(k.lower() in low for k in keys):
            return role
    return "运营专员"


@dataclass
class ActivityCluster:
    """一簇同类活动：来自某个分类列的某个取值，或整份材料。"""

    key: str
    column: str
    value: str
    material: ParsedMaterial
    record_count: int
    timestamps: list[str]
    minutes_per_run: float | None
    minutes_source: str


def _minutes_from_pairs(material: ParsedMaterial) -> tuple[float | None, str]:
    """若同时存在两个时间戳列（如创建/首响），用其间隔推算单次耗时。"""
    cols = material.timestamp_columns
    if len(cols) < 2:
        return None, ""
    a_vals = material.timestamps.get(cols[0], [])
    b_vals = material.timestamps.get(cols[1], [])
    deltas: list[float] = []
    for a, b in zip(a_vals, b_vals):
        try:
            ta = datetime.fromisoformat(a[:19])
            tb = datetime.fromisoformat(b[:19])
        except ValueError:
            continue
        diff = (tb - ta).total_seconds() / 60.0
        if 0 < diff <= 240:
            deltas.append(diff)
    if len(deltas) < 3:
        return None, ""
    avg = sum(deltas) / len(deltas)
    return round(avg, 2), f"由 {cols[0]} 与 {cols[1]} 的间隔推算（{len(deltas)} 对样本）"


def _cluster(
    materials: list[ParsedMaterial],
) -> tuple[list[ActivityCluster], list[dict[str, Any]]]:
    """把材料切成活动簇。优先按分类列的取值切分，否则整份材料算一簇。

    第二个返回值是**被丢弃的活动**：样本太少不适合单独成场景是合理的判断，
    但静默省略等于漏判（§19.1 三分类里的第一类）。因此必须登记并回报给客户。
    """
    clusters: list[ActivityCluster] = []
    dropped: list[dict[str, Any]] = []

    for m in materials:
        if m.kind != "csv" or m.row_count == 0:
            continue

        per_run, src = _minutes_from_pairs(m)
        ts_col = m.timestamp_columns[0] if m.timestamp_columns else ""
        all_ts = m.timestamps.get(ts_col, []) if ts_col else []

        # 按打分选切分列：优先"活动类型"类列，排除渠道/人名/ID。
        # 单取值列同样有效——它说明这份导出整体对应一个场景。
        candidates = [
            (col, counts, _split_score(col, counts))
            for col, counts in m.categorical_counts.items()
        ]
        candidates = [c for c in candidates if c[2] > 0]
        candidates.sort(key=lambda t: (-t[2], t[0]))
        split_col, best_counts = ("", {})
        if candidates:
            split_col, best_counts = candidates[0][0], candidates[0][1]

        if split_col:
            grouped_ts = m.timestamps_by.get(split_col, {})
            made = False
            # 按记录数从多到少，取最痛的几个
            ordered = sorted(best_counts.items(), key=lambda kv: -kv[1])[:MAX_SCENARIOS]
            for value, count in ordered:
                if count < MIN_RECORDS_PER_SCENARIO:
                    dropped.append(
                        {
                            "file": m.filename,
                            "column": split_col,
                            "value": value,
                            "record_count": count,
                            "reason": (
                                f"仅 {count} 条记录，低于单独成场景的阈值"
                                f"（{MIN_RECORDS_PER_SCENARIO} 条），样本不足以支撑量化"
                            ),
                        }
                    )
                    continue
                clusters.append(
                    ActivityCluster(
                        key=f"{m.filename}:{split_col}:{value}",
                        column=split_col,
                        value=value,
                        material=m,
                        record_count=count,          # 精确计数，不分摊
                        timestamps=grouped_ts.get(value, []),
                        minutes_per_run=per_run,
                        minutes_source=src,
                    )
                )
                made = True
            if made:
                continue

        clusters.append(
            ActivityCluster(
                key=f"{m.filename}:*",
                column="",
                value="",
                material=m,
                record_count=m.row_count,
                timestamps=all_ts,
                minutes_per_run=per_run,
                minutes_source=src,
            )
        )

    for extra in clusters[MAX_SCENARIOS:]:
        dropped.append(
            {
                "file": extra.material.filename,
                "column": extra.column,
                "value": extra.value,
                "record_count": extra.record_count,
                "reason": (
                    f"本次只深挖最痛的 {MAX_SCENARIOS} 个环节（父层只讲最痛 2–3 条的延伸），"
                    "该环节优先级靠后，未做深度取证"
                ),
            }
        )
    return clusters[:MAX_SCENARIOS], dropped


def _name_with_llm(clusters: list[ActivityCluster], llm: Any) -> tuple[dict[str, dict[str, str]], str]:
    """让模型给活动簇命名。返回 (key → 命名字段, 来源)。"""
    payload = {
        "clusters": [
            {
                "column": c.column or "(整份材料)",
                "value": c.value or c.material.filename,
                "file": c.material.filename,
                "columns_available": c.material.columns,
            }
            for c in clusters
        ]
    }
    try:
        data = llm.complete_json(
            [
                {"role": "system", "content": NAMING_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            role="primary",
            stage="S2",
            temperature=0.0,
            max_tokens=1800,
        )
    except _MAY_FALLBACK:
        return {}, "fallback"
    except _NEVER_FALLBACK:
        raise
    except Exception:
        return {}, "fallback"

    named: dict[str, dict[str, str]] = {}
    for raw in (data or {}).get("activities", []):
        col = (raw.get("column") or "").strip()
        val = (raw.get("value") or "").strip()
        name = (raw.get("scenario_name") or "").strip()
        if not name:
            continue
        # 模型必须原样引用给定的列与值；编造的一律丢弃
        match = next(
            (
                c
                for c in clusters
                if (c.column == col or (not c.column and col in ("", "(整份材料)")))
                and (c.value == val or (not c.value and val == c.material.filename))
            ),
            None,
        )
        if match is None:
            continue
        role = raw.get("role") if raw.get("role") in _ROLE_WHITELIST else _guess_role(match.column or match.material.filename)
        named[match.key] = {
            "name": name[:24],
            "business_outcome": (raw.get("business_outcome") or "业务流程完成").strip()[:20],
            "operator": (raw.get("operator") or role).strip()[:16],
            "role": role,
            "status_quo": (raw.get("status_quo") or "").strip()[:80],
        }
    return named, ("llm" if named else "fallback")


def _fallback_name(c: ActivityCluster) -> dict[str, str]:
    role = _guess_role(c.column or c.material.filename)
    subject = c.value or c.material.filename.rsplit(".", 1)[0]
    return {
        "name": f"{subject}相关记录人工处理"[:24],
        "business_outcome": {
            "客服专员": "客户咨询得到回复",
            "财务专员": "账目核对完成",
            "销售助理": "订单跟到交付",
        }.get(role, "业务流程完成"),
        "operator": role,
        "role": role,
        "status_quo": f"依据 {c.material.filename} 的记录，该环节目前由人工逐条处理",
    }


def derive_scenarios(
    materials: list[ParsedMaterial], *, llm: Any | None = None, as_of: str = ""
) -> dict[str, Any]:
    """从材料推导场景卡、父场景与证据台账。"""
    evidence: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    csv_materials = [m for m in materials if m.kind == "csv" and m.row_count > 0 and not m.summary_only]
    summary_materials = [m for m in materials if m.kind == "csv" and m.summary_only]
    text_materials = [m for m in materials if m.kind == "text"]

    # ---- 证据台账：每份材料一条，等级由材料形态决定 ----
    def add_ev(m: ParsedMaterial, idx: int) -> str:
        eid = f"e{idx:02d}"
        if m.kind == "text":
            src, grade, reason = (
                SourceType.MEETING_NOTES,
                EvidenceGrade.C,
                "纪要类文档记录决策与结论、非操作机制，仅用于定位痛点，不用于量化",
            )
            sample = None
        elif m.summary_only:
            src, grade, reason = (
                SourceType.SELF_REPORT,
                EvidenceGrade.C,
                "仅有汇总数字、无单条明细，无法回推频次与耗时，只能给方向",
            )
            sample = m.row_count
        elif m.timestamp_columns:
            src, grade, reason = (
                SourceType.TIMESTAMP_EXPORT,
                EvidenceGrade.A,
                f"含单条记录与时间戳列（{'、'.join(m.timestamp_columns)}），可直接数频次并推算耗时",
            )
            sample = m.row_count
        else:
            src, grade, reason = (
                SourceType.SUPPLEMENT_FORM,
                EvidenceGrade.B,
                "有单条明细但无时间戳，可给频次、耗时需补数表，止步 B 级",
            )
            sample = m.row_count
        evidence.append(
            {
                "evidence_id": eid,
                "source_type": src.value,
                "origin": f"客户上传材料 {m.filename}",
                "obtained_at": as_of or "",
                "as_of": as_of or "",
                "grade": grade.value,
                "grade_reason": reason,
                "sample_size": sample,
                "supports": [],
                "conflict": False,
                "conflict_note": "",
            }
        )
        return eid

    ev_by_file: dict[str, str] = {}
    for i, m in enumerate(materials, start=1):
        ev_by_file[m.filename] = add_ev(m, i)

    if not materials:
        gaps.append(
            {
                "material": "任何可用的业务系统导出",
                "why_requested": "用于识别哪些环节可以自动化，并算清频次与耗时",
                "status": "未获取——尚未上传任何材料",
                "impact": "无法产出任何量化结论；请先上传至少一份含时间戳的导出（CSV）",
                "affected_cards": [],
            }
        )
        return {
            "cards": [], "parents": [], "evidence": [], "gaps": gaps,
            "dropped": [], "naming_source": "none",
        }

    # ---- 活动簇 → 命名 ----
    clusters, dropped = _cluster(csv_materials)
    naming_source = "fallback"
    named: dict[str, dict[str, str]] = {}
    if clusters and llm is not None:
        named, naming_source = _name_with_llm(clusters, llm)

    # ---- 逐簇建卡：数字全部算出来 ----
    cards: list[dict[str, Any]] = []
    for idx, c in enumerate(clusters, start=1):
        meta = named.get(c.key) or _fallback_name(c)
        per_run = c.minutes_per_run
        minutes_note = c.minutes_source

        if c.timestamps:
            verdict = judge_work_form(c.timestamps, minutes_per_run=per_run or FALLBACK_MINUTES_PER_RUN)
            work_form = verdict.work_form
            grade = EvidenceGrade.A
            forensics = verdict.note
            if per_run is None:
                per_run = FALLBACK_MINUTES_PER_RUN
                minutes_note = (
                    f"无成对时间戳可推算单次耗时，按 {FALLBACK_MINUTES_PER_RUN} 分钟/次的保守假设估算，"
                    "建议用补数表或工时记录校正"
                )
        else:
            # 没有痕迹就是没有：不许声称作业形态，按保守处理
            work_form = WorkForm.FRAGMENTED
            grade = EvidenceGrade.B
            per_run = per_run or FALLBACK_MINUTES_PER_RUN
            forensics = (
                "该材料无时间戳列，无法验证操作是否聚集在同一时间窗内，"
                "按保守规则不计入 ROI；补一份含时间戳的导出即可升级为可量化。"
            )
            minutes_note = minutes_note or "无时间戳，耗时为保守假设"

        # 频次折算月度：观测到的记录数按工作日归一
        span_days = 0
        if c.timestamps:
            try:
                stamps = sorted(datetime.fromisoformat(t[:19]) for t in c.timestamps)
                span_days = max((stamps[-1] - stamps[0]).days + 1, 1)
            except ValueError:
                span_days = 0
        monthly_records = (
            c.record_count * (WORKDAYS_PER_MONTH / min(span_days, 31)) if span_days else c.record_count
        )
        monthly_minutes = round(monthly_records * per_run, 1)

        # 观测窗过短时外推会放大偏差（旺季/淡季、月初/月末都会失真）——必须写进假设
        if span_days and span_days < 5:
            factor = WORKDAYS_PER_MONTH / min(span_days, 31)
            extrapolation = (
                f"观测窗仅覆盖 {span_days} 天，按 ×{factor:.1f} 外推到月度；"
                "外推偏差较大（旺淡季与月内分布都会失真），建议索取覆盖完整月份的导出以校正"
            )
            minutes_note = f"{minutes_note}；{extrapolation}" if minutes_note else extrapolation

        card_id = f"s-{idx:02d}"
        eid = ev_by_file.get(c.material.filename, "")
        cards.append(
            {
                "card_id": card_id,
                "parent_id": "",
                "name": meta["name"],
                "operator": meta["operator"],
                "systems": [c.material.filename.rsplit(".", 1)[0]],
                "status_quo": meta["status_quo"] or f"依据 {c.material.filename} 的记录，该环节由人工逐条处理",
                "frequency_desc": (
                    f"{c.record_count} 条（材料实数"
                    + (f"，覆盖 {span_days} 天，折算约 {monthly_records:.0f} 条/月" if span_days else "")
                    + "）"
                ),
                "minutes_per_run": per_run,
                "monthly_minutes": monthly_minutes,
                "evidence_grade": grade.value,
                "work_form": work_form.value,
                "benefit_composition": "单点",
                "departments_merged": 1,
                "intervention": InterventionMode.ASSIST.value,
                "expected_effect": ["省时"],
                "dependency": "独立",
                "landing_dependency": "需确认该环节的数据可稳定获取",
                "evidence_refs": [eid] if eid else [],
                "conflict": False,
                "conflict_note": "",
                "requires_human": False,
                "quantifiable": work_form is not WorkForm.FRAGMENTED,
                "business_flow": meta["business_outcome"],
                "department": meta["role"],
                "role": meta["role"],
                "impl": "medium",
                "forensics_note": forensics,
                "minutes_note": minutes_note,
                "source_column": c.column,
                "source_value": c.value,
            }
        )

    # ---- 汇总类与纪要类材料：只给方向，零工时 ----
    for m in summary_materials + text_materials:
        idx = len(cards) + 1
        eid = ev_by_file.get(m.filename, "")
        role = _guess_role(m.filename)
        cards.append(
            {
                "card_id": f"s-{idx:02d}",
                "parent_id": "",
                "name": f"{m.filename.rsplit('.', 1)[0]}所述环节"[:24],
                "operator": role,
                "systems": [m.filename.rsplit(".", 1)[0]],
                "status_quo": (
                    "仅有汇总数字或文字描述，无单条明细可回推操作序列"
                    if m.summary_only
                    else "材料为文字描述，用于定位痛点，不能用于量化"
                ),
                "frequency_desc": "无法量化（无单条明细）",
                "minutes_per_run": None,
                "monthly_minutes": 0.0,
                "evidence_grade": EvidenceGrade.C.value,
                "work_form": WorkForm.FRAGMENTED.value,
                "benefit_composition": "单点",
                "departments_merged": 1,
                "intervention": InterventionMode.ASSIST.value,
                "expected_effect": ["省时"],
                "dependency": "独立",
                "landing_dependency": "需先让该环节留下可分析的操作痕迹",
                "evidence_refs": [eid] if eid else [],
                "conflict": False,
                "conflict_note": "",
                "requires_human": False,
                "quantifiable": False,
                "business_flow": "待确认的业务结果",
                "department": role,
                "role": role,
                "impl": "medium",
                "forensics_note": "C 级证据：不给数字，只给方向。",
                "minutes_note": "",
                "source_column": "",
                "source_value": "",
            }
        )
        gaps.append(
            {
                "material": f"{m.filename} 对应环节的明细导出（含时间戳）",
                "why_requested": "用于把该环节从定性描述变成可量化场景",
                "status": "未获取——当前只有汇总数字或文字描述",
                "impact": "该环节只能给方向性判断，不计入 ROI 汇总",
                "affected_cards": [f"s-{idx:02d}"],
            }
        )

    # ---- 父场景归组 ----
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in cards:
        groups.setdefault(c["business_flow"], []).append(c)

    parents: list[dict[str, Any]] = []
    for pidx, (outcome, children) in enumerate(groups.items(), start=1):
        pid = f"p-{pidx:02d}"
        for c in children:
            c["parent_id"] = pid
        total = round(sum(c["monthly_minutes"] for c in children), 1)
        parents.append(
            {
                "parent_id": pid,
                "business_outcome": outcome,
                "business_flow": outcome,
                "why_painful": (
                    f"该业务结果下有 {len(children)} 个环节仍由人工逐条处理，"
                    f"合计约 {total / 60:.1f} 小时/月"
                    if total
                    else f"该业务结果下有 {len(children)} 个环节缺少可量化痕迹，需先补材料"
                ),
                "child_ids": [c["card_id"] for c in children],
                "total_monthly_minutes": total,
            }
        )

    # 证据台账回填 supports
    for c in cards:
        for ref in c["evidence_refs"]:
            for e in evidence:
                if e["evidence_id"] == ref:
                    e["supports"].append(f"{c['card_id']}.月度工时")

    # 无任何可量化场景时显式报缺口
    if not any(c["quantifiable"] for c in cards):
        gaps.append(
            {
                "material": "含时间戳的业务系统导出",
                "why_requested": "时间戳是唯一能同时给出频次与耗时的证据类型",
                "status": "未获取——现有材料都无法支撑量化",
                "impact": "本次只能出轻量咨询（场景清单 + 方向），无法给任何 ROI 金额",
                "affected_cards": [c["card_id"] for c in cards],
            }
        )

    # 被丢弃的活动显式回报：客户有权知道有哪些环节被判为"样本不足"
    for d in dropped:
        gaps.append(
            {
                "material": f"{d['value'] or d['file']} 环节的更长时间范围导出",
                "why_requested": "该环节在本次材料中样本不足，无法单独量化",
                "status": f"已识别但未深挖——{d['reason']}",
                "impact": "该环节未进入场景清单；若实际频次更高，请提供覆盖更长时间的导出",
                "affected_cards": [],
            }
        )

    return {
        "cards": cards,
        "parents": parents,
        "evidence": evidence,
        "gaps": gaps,
        "dropped": dropped,
        "naming_source": naming_source,
    }
