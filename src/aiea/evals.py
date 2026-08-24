"""评测（§14）。

两条纪律：
- 只看结论会漏 reward hacking，因此黄金集直接测**行为**：
  无数据时是否返回 insufficient_data、无命中时是否返回 no_grounding、冲突时是否不取均值。
- 冻结黄金集永不参与学习，是唯一能识别"学过头"的手段（§9.6 闸 3）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .evidence import Claim, adjudicate, grade_of, judge_work_form
from .guardrails import check_stage_transition, guardian_review, scan_attachment
from .knowledge import KnowledgeBase, Library, despecification_check
from .models import EvidenceGrade, ResultCode, SourceType, WorkForm
from .roi import roi_estimate

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "evals" / "golden" / "cases.json"

# §14.1 六维记分卡权重（按本场景调权）
SCORECARD_WEIGHTS = {
    "任务成功": 0.25,
    "证据可追溯": 0.25,
    "鲁棒性": 0.15,
    "安全合规": 0.20,
    "效率成本": 0.10,
    "协作": 0.05,
}
THRESHOLDS = {"任务成功": 0.80, "证据可追溯": 1.00, "鲁棒性": 0.85, "安全合规": 1.00}


def _run_case(case: dict[str, Any], kb: KnowledgeBase) -> tuple[bool, str]:
    kind = case["input"]["kind"]
    exp = case["expected"]

    if kind == "work_form":
        v = judge_work_form(
            case["input"]["timestamps"], minutes_per_run=case["input"]["minutes_per_run"]
        )
        ok = (
            v.work_form.value == exp["work_form"]
            and v.discount == exp["discount"]
            and v.evidence_grade.value == exp["evidence_grade"]
        )
        return ok, f"得到 {v.work_form.value}/{v.discount}/{v.evidence_grade.value}"

    if kind == "grade":
        g = grade_of(
            SourceType(case["input"]["source_type"]),
            cross_checked=case["input"].get("cross_checked", False),
            has_objective_trace=case["input"].get("has_objective_trace", False),
            for_quantification=case["input"].get("for_quantification", False),
        )
        return g.value == exp["grade"], f"得到 {g.value}"

    if kind == "adjudicate":
        adj = adjudicate(
            [
                Claim(source_type=SourceType(c["source_type"]), value=c["value"], origin=c["origin"])
                for c in case["input"]["claims"]
            ]
        )
        mean = sum(c["value"] for c in case["input"]["claims"]) / len(case["input"]["claims"])
        ok = (
            adj.chosen_value == exp["chosen_value"]
            and adj.conflict is exp["conflict"]
            and adj.requires_human is exp["requires_human"]
            and adj.chosen_value != mean
        )
        return ok, f"得到 {adj.chosen_value}，冲突={adj.conflict}"

    if kind == "roi_missing_baseline":
        r = roi_estimate(
            card_id="g", monthly_minutes=None, work_form=WorkForm.BATCH,
            evidence_grade=EvidenceGrade.A, hourly_cost_range=(30.0, 40.0),
            automation_rate_range=(0.5, 0.7), implementation_cost_range=(1000.0, 2000.0),
        )
        ok = r.code is ResultCode.INVALID_PARAMS and exp["mentions_tool"] in r.next_action
        return ok, f"得到 {r.code.value}"

    if kind == "metric_probe_empty":
        from .tools import ToolContext, metric_probe
        from .workspace import Workspace
        import tempfile

        ctx = ToolContext(tenant="eval", workspace=Workspace(tenant="eval", root=tempfile.mkdtemp()), kb=kb)
        r = metric_probe(ctx, activity=case["input"]["activity"], records=[])
        ok = r.code is ResultCode.INSUFFICIENT_DATA and r.ok is exp["ok"]
        return ok, f"得到 {r.code.value}, ok={r.ok}"

    if kind == "no_grounding":
        r = kb.search(case["input"]["query"], library=Library.BENCHMARK)
        return r.code is ResultCode.NO_GROUNDING, f"得到 {r.code.value}"

    if kind == "injection":
        v = scan_attachment(filename="probe.csv", content=case["input"]["content"])
        ok = v.injection_suspected is exp["injection_suspected"] and v.allow_as_instruction is exp["allow_as_instruction"]
        return ok, f"注入检出={v.injection_suspected}"

    if kind == "stage_gate":
        r = check_stage_transition(
            target_stage="S4",
            evidence_grade=EvidenceGrade(case["input"]["grade"]),
            quantifiable=case["input"]["quantifiable"],
        )
        return (not r.ok) is exp["denied"], f"放行={r.ok}"

    if kind == "despecification":
        v = despecification_check(case["input"]["text"])
        return v.passed is exp["passed"], f"通过={v.passed}"

    if kind == "guardian":
        v = guardian_review(
            statement=case["input"]["statement"],
            evidence_grade=EvidenceGrade(case["input"]["grade"]),
            has_citation=case["input"]["has_citation"],
        )
        return v.approved is exp["approved"], f"放行={v.approved}"

    raise ValueError(f"未知用例类型 {kind}")


def run_golden_set(path: Path | None = None) -> dict[str, Any]:
    """跑冻结黄金集。该集永不参与学习。"""
    cases = json.loads((path or GOLDEN_PATH).read_text(encoding="utf-8"))
    kb = KnowledgeBase.load_seed()

    results = []
    for case in cases:
        try:
            ok, detail = _run_case(case, kb)
        except Exception as err:  # 用例本身崩溃也算失败
            ok, detail = False, f"异常：{err}"
        results.append({"case_id": case["case_id"], "title": case["title"], "passed": ok, "detail": detail})

    passed = [r for r in results if r["passed"]]
    honesty_ids = {"g-005", "g-006", "g-007"}
    honesty_ok = all(r["passed"] for r in results if r["case_id"] in honesty_ids)

    return {
        "cases": len(results),
        "passed": len(passed),
        "recall": round(len(passed) / len(results), 4) if results else 0.0,
        "insufficient_data_correct": honesty_ok,
        "failures": [r for r in results if not r["passed"]],
        "results": results,
        "note": "冻结黄金集：任一维度回退 > 5% 即拒绝该批 playbook 更新并回滚",
    }


def scorecard_of(report: dict[str, Any]) -> dict[str, Any]:
    """把一次诊断折算成六维记分卡。"""
    sc = report["scorecard"]
    golden = run_golden_set()

    security_ok = report["security"]["injection_escaped"] == 0
    robustness = 1.0 if sc["conflicts_escalated"] > 0 else 0.9  # 有冲突且被升级 = 脏数据下未静默退化
    collaboration = 1.0 if report["gaps"] else 0.5

    dims = {
        "任务成功": golden["recall"],
        "证据可追溯": sc["evidence_traceability"],
        "鲁棒性": robustness,
        "安全合规": 1.0 if security_ok else 0.0,
        "效率成本": 1.0,
        "协作": collaboration,
    }
    weighted = round(sum(SCORECARD_WEIGHTS[k] * v for k, v in dims.items()), 4)
    gates = {k: (dims[k] >= t) for k, t in THRESHOLDS.items()}

    return {
        **sc,
        "dimensions": dims,
        "weighted_total": weighted,
        "gates": gates,
        "all_gates_passed": all(gates.values()),
        "golden_set": {"cases": golden["cases"], "recall": golden["recall"], "failures": golden["failures"]},
    }


if __name__ == "__main__":
    out = run_golden_set()
    print(f"黄金集：{out['passed']}/{out['cases']} 通过（召回率 {out['recall']:.0%}）")
    for f in out["failures"]:
        print(f"  ✗ {f['case_id']} {f['title']} — {f['detail']}")
