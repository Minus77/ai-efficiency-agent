"""交付物 API（只读为主 + 反馈写入）。

分层可见性（§10.6、§12.3）：
- 完整 trace / 指标 / replay 属内部后台，不在此暴露
- 专家判断分区在 schema 层就不含任何金额字段（§7、§11.6）——模板层无从渲染
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .knowledge import KnowledgeBase
from .seed import TENANT, run_seed_diagnosis
from .tools import TOOL_REGISTRY, ToolContext
from .workspace import Workspace

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


class FeedbackIn(BaseModel):
    """§19.2/19.3：角色必填；不问"准不准"，只问偏高/偏低与排序。"""

    card_id: str
    role: str
    direction: Literal["偏高", "偏低", "基本相符", "没说到点上"]
    reason: str = ""
    would_do_first: str | None = None
    would_not_do: str | None = None

    @field_validator("role")
    @classmethod
    def _role_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("角色必填：不同角色的偏差方向已知，可反向校正")
        return v


def create_app(*, root: Path | str = "workspace", use_llm: bool = False) -> FastAPI:
    """构建应用。

    use_llm=False（默认）：S5 反评审与洞察用定稿内容，完全离线、可复现。
    use_llm=True：改由 judge 档模型现场生成，需要 AIEA_API_KEY。
    """
    app = FastAPI(title="中小企业 AI 提效场景识别 Agent", version="1.0")

    @lru_cache(maxsize=1)
    def report() -> dict[str, Any]:
        llm = None
        if use_llm:
            from .llm import LLMClient

            llm = LLMClient()
        return run_seed_diagnosis(root=root, llm=llm)

    def card_of(cid: str) -> dict[str, Any]:
        return next(c for c in report()["cards"] if c["card_id"] == cid)

    # -- 概览 ---------------------------------------------------------------
    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        r = report()
        quantified = r["scorecard"]["scenarios_quantified"]
        total_low = sum(
            (roi["tiers"][0]["monthly_saving_low"] or 0) for roi in r["roi"].values() if roi["tiers"]
        )
        total_high = sum(
            (roi["tiers"][-1]["monthly_saving_high"] or 0) for roi in r["roi"].values() if roi["tiers"]
        )
        return {
            "client": r["client"],
            "delivery_form": r["delivery_form"],
            "admission_probe": r["admission_probe"],
            "scope": r["scope"],
            "assumptions": r["assumptions"],
            "scorecard": r["scorecard"],
            "headline": {
                "parents": len(r["parents"]),
                "children": len(r["cards"]),
                "quantified": quantified,
                "direction_only": r["scorecard"]["scenarios_direction_only"],
                "monthly_saving_low": round(total_low, 2),
                "monthly_saving_high": round(total_high, 2),
                "deduped_sum": r["aggregate"]["deduped_sum"],
                "naive_sum": r["aggregate"]["naive_sum"],
                "dedup_delta": r["aggregate"]["delta"],
            },
            "customer_progress": r["observability"]["customer_progress"],
            "disclaimer": "本报告为决策参考，非投资承诺或收益保证。",
        }

    # -- 场景清单 -----------------------------------------------------------
    @app.get("/api/scenarios")
    def scenarios() -> dict[str, Any]:
        r = report()
        parents = []
        for p in r["parents"]:
            children = []
            for cid in p["child_ids"]:
                card = card_of(cid)
                roi = r["roi"][cid]
                children.append(
                    {
                        **card,
                        "capability": (r["capabilities"].get(cid) or [{}])[0],
                        "roi_summary": {
                            "amount": roi["amount"],
                            "direction_only": roi["direction_only"],
                            "low": roi["tiers"][0]["monthly_saving_low"] if roi["tiers"] else None,
                            "high": roi["tiers"][-1]["monthly_saving_high"] if roi["tiers"] else None,
                        },
                        "in_body": cid in r["render_gate"]["body_ids"],
                    }
                )
            parents.append({**p, "children": children})
        return {"parents": parents, "render_gate": r["render_gate"]}

    # -- 优先级矩阵 ---------------------------------------------------------
    @app.get("/api/matrix")
    def matrix() -> dict[str, Any]:
        r = report()
        return {
            "items": r["matrix"],
            "axes": {
                "benefit": "月度可省工时 × 岗位成本系数（来自 ROI 估算）",
                "difficulty": "七维加权：数据可得性、系统集成度、流程标准化、人员接受度、AI 能力匹配、合规风险、维护成本",
            },
            "quadrant_semantics": {
                "先做": "进 90 天路线图第一批",
                "规划": "拆成阶段，先做可验证的一小步",
                "顺手做": "有余力时做，不占主资源",
                "不做": "明确不建议做，并写出理由",
            },
            "note": "『不做』象限必须保留——顾问的价值一半在于告诉客户哪些别碰。",
        }

    # -- 分级 ROI -----------------------------------------------------------
    @app.get("/api/roi")
    def roi() -> dict[str, Any]:
        r = report()
        items = []
        for cid, roi_data in r["roi"].items():
            card = card_of(cid)
            items.append(
                {
                    "card_id": cid,
                    "name": card["name"],
                    "evidence_grade": card["evidence_grade"],
                    "work_form": card["work_form"],
                    "discount_factor": roi_data["discount_factor"],
                    "monthly_minutes": card["monthly_minutes"],
                    "discounted_monthly_minutes": roi_data["discounted_monthly_minutes"],
                    "tiers": roi_data["tiers"],
                    "amount": roi_data["amount"],
                    "direction_only": roi_data["direction_only"],
                    "implementation_cost_low": roi_data["implementation_cost_low"],
                    "implementation_cost_high": roi_data["implementation_cost_high"],
                    "payback_months_conservative": roi_data["payback_months_conservative"],
                    "payback_months_neutral": roi_data["payback_months_neutral"],
                    "dependency": card["dependency"],
                    "dependency_released_saving": roi_data["dependency_released_saving"],
                    "benefit_composition": card["benefit_composition"],
                    "calculation_trace": roi_data["calculation_trace"],
                    "assumptions": roi_data["assumptions"],
                    "evidence_refs": card["evidence_refs"],
                }
            )
        return {
            "items": items,
            "aggregate": r["aggregate"],
            "presentation_rule": "A 级给点估 + 区间；B 级仅区间；C 级不给数字，只给方向。",
            "benchmarks": r["benchmarks"],
        }

    # -- 90 天路线图 --------------------------------------------------------
    @app.get("/api/roadmap")
    def roadmap() -> dict[str, Any]:
        return {"batches": report()["roadmap"]}

    # -- 证据台账 -----------------------------------------------------------
    @app.get("/api/evidence")
    def evidence() -> dict[str, Any]:
        r = report()
        return {
            "items": r["evidence"],
            "grading_rule": {
                "A": "单据痕迹或系统数据支撑 → 点估 + 区间",
                "B": "多方交叉且至少一路有客观痕迹 → 仅区间",
                "C": "单方陈述或基准外推 → 仅方向性判断",
            },
            "adjudication_order": "时间戳导出 > 工时记录 > 补数表 > 多方交叉 > 单方自述/纪要",
            "conflict_rule": "偏差超过 30% 标注冲突并转人工，不取均值掩盖分歧。",
        }

    # -- 专家判断（schema 层无金额字段） --------------------------------------
    @app.get("/api/insights")
    def insights() -> dict[str, Any]:
        r = report()
        return {
            "title": "基于经验的判断（无数据支撑）",
            "notice": "本区为顾问经验判断，无数据支撑，且按设计不含任何金额或回本周期。",
            "items": [
                {
                    "insight_id": i["insight_id"],
                    "statement": i["statement"],
                    "basis": i["basis"],
                    "verification_suggestion": i["verification_suggestion"],
                    "label": i["label"],
                }
                for i in r["insights"]
            ],
        }

    # -- 缺口 ---------------------------------------------------------------
    @app.get("/api/gaps")
    def gaps() -> dict[str, Any]:
        r = report()
        return {
            "items": r["gaps"],
            "closure_rate": r["scorecard"]["evidence_closure_rate"],
            "rule": "收口时必须显式列出未获取的材料清单及其影响，不隐藏缺口。",
        }

    # -- 反评审 -------------------------------------------------------------
    @app.get("/api/counter-review")
    def counter_review() -> dict[str, Any]:
        r = report()
        sources = {i.get("source", "curated") for i in r["counter_review"]}
        return {
            "items": r["counter_review"],
            "generated_by": "模型现场生成" if "llm" in sources else "定稿内容（未启用模型生成）",
            "isolation": r["counter_review_isolation"],
            "known_limit": "反评审只能审内部一致性，审不了真伪，不能替代人工终审。",
        }

    # -- 可观测性（自然语言） -------------------------------------------------
    @app.get("/api/observability")
    def observability() -> dict[str, Any]:
        r = report()
        obs = r["observability"]
        return {
            "daily_brief": obs["daily_brief"],
            "customer_progress": obs["customer_progress"],
            "metrics": obs["metrics"],
            "security": r["security"],
            "guardian_checks": r["guardian_checks"],
            "playbook_candidate": obs["playbook_candidate"],
            "material_checklist": r["material_checklist"],
        }

    # -- 反馈写入 -----------------------------------------------------------
    @app.get("/api/feedback")
    def list_feedback() -> dict[str, Any]:
        ws = Workspace(tenant=TENANT, root=root)
        return {"items": ws.list_feedback()}

    @app.post("/api/feedback")
    def add_feedback(payload: FeedbackIn) -> dict[str, Any]:
        ws = Workspace(tenant=TENANT, root=root)
        ctx = ToolContext(tenant=TENANT, workspace=ws, kb=KnowledgeBase.load_seed())
        result = TOOL_REGISTRY["outcome_record"](
            ctx,
            card_id=payload.card_id,
            role=payload.role,
            direction=payload.direction,
            reason=payload.reason,
            would_do_first=payload.would_do_first,
            would_not_do=payload.would_not_do,
        )
        if not result.ok:
            raise HTTPException(status_code=422, detail=result.note)
        return {"feedback_id": result.data["feedback_id"], "note": result.note}

    # -- 前端 ---------------------------------------------------------------
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


import os

app = create_app(use_llm=os.getenv("AIEA_USE_LLM", "").lower() in ("1", "true", "yes"))
