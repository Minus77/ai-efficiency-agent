"""交付物 API（多客户）。

路由分两层：
- `/api/clients/...`：生产路径。客户建档 → 上传材料 → 跑诊断 → 按租户取交付物。
- `/api/overview` 等旧路径：继续指向预置演示客户，避免破坏已有链接与截图。

视图组装抽成以 report 为入参的纯函数，两层路由共用同一套代码，不写两份。

分层可见性（§10.6、§12.3）：
- 完整 trace / 指标 / replay 属内部后台，不在此暴露
- 专家判断分区在 schema 层就不含任何金额字段（§7、§11.6）——模板层无从渲染
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .clients import ClientRegistry, safe_slug
from .config import default_workspace_root
from .connector_intake import list_bindings, save_binding, sync_connector
from .connectors import list_specs
from .connectors.base import CredentialRef
from .diagnose import DiagnosisNotReady, load_report, run_diagnosis
from .glossary import (
    all_terms,
    delivery_scale,
    difficulty_scale,
    explain,
    grade_scale,
    grouped_terms,
    severity_scale,
    tier_scale,
    work_form_scale,
)
from .intake import EVIDENCE_ROLES, list_materials, save_material
from .knowledge import KnowledgeBase
from .measure import capture_baseline, effect_summary, measure_effect
from .seed import TENANT, run_seed_diagnosis
from .tools import TOOL_REGISTRY, ToolContext
from .workspace import Workspace

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


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


class ClientIn(BaseModel):
    name: str
    industry: str = ""
    headcount: int | None = None
    departments: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    background: str = ""
    as_of: str | None = None

    @field_validator("name")
    @classmethod
    def _name_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("客户名称必填")
        return v.strip()


class ConnectorBindIn(BaseModel):
    """绑定连接器。secret 只入不出——响应与列表一律不回显。"""

    key: str
    key_id: str = ""
    secret: str = ""


class MetricIn(BaseModel):
    """基线/后测的录入。metric 白名单校验在 measure.py，这里只做形状校验。"""

    card_id: str
    metric: str
    value: float | None = None
    timestamps: list[str] | None = None
    sample_size: int | None = None
    source: str
    note: str = ""

    @field_validator("source")
    @classmethod
    def _source_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("必须写明数据来源，否则无法复议")
        return v.strip()


# ===========================================================================
# 视图组装：纯函数，入参是 report，出参是前端要的结构
# ===========================================================================
def _card_of(report: dict[str, Any], cid: str) -> dict[str, Any]:
    return next(c for c in report["cards"] if c["card_id"] == cid)


def build_overview(r: dict[str, Any]) -> dict[str, Any]:
    h_quantified = r["scorecard"]["scenarios_quantified"]
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
            "quantified": h_quantified,
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


def build_scenarios(r: dict[str, Any]) -> dict[str, Any]:
    parents = []
    for p in r["parents"]:
        children = []
        for cid in p["child_ids"]:
            card = _card_of(r, cid)
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


def build_matrix(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": r["matrix"],
        "thresholds": r["matrix_thresholds"],
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


def build_roi(r: dict[str, Any]) -> dict[str, Any]:
    items = []
    for cid, roi_data in r["roi"].items():
        card = _card_of(r, cid)
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


def build_evidence(r: dict[str, Any]) -> dict[str, Any]:
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


def build_insights(r: dict[str, Any]) -> dict[str, Any]:
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


def build_gaps(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": r["gaps"],
        "closure_rate": r["scorecard"]["evidence_closure_rate"],
        "rule": "收口时必须显式列出未获取的材料清单及其影响，不隐藏缺口。",
    }


def build_counter_review(r: dict[str, Any]) -> dict[str, Any]:
    sources = {i.get("source", "curated") for i in r["counter_review"]}
    return {
        "items": r["counter_review"],
        "generated_by": "模型现场生成" if "llm" in sources else "定稿内容（未启用模型生成）",
        "isolation": r["counter_review_isolation"],
        "known_limit": "反评审只能审内部一致性，审不了真伪，不能替代人工终审。",
    }


def build_observability(r: dict[str, Any]) -> dict[str, Any]:
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


VIEW_BUILDERS = {
    "overview": build_overview,
    "scenarios": build_scenarios,
    "matrix": build_matrix,
    "roi": build_roi,
    "roadmap": lambda r: {"batches": r["roadmap"]},
    "evidence": build_evidence,
    "insights": build_insights,
    "gaps": build_gaps,
    "counter-review": build_counter_review,
    "observability": build_observability,
}


# ===========================================================================
# 应用
# ===========================================================================
def create_app(*, root: Path | str | None = None, use_llm: bool = False) -> FastAPI:
    """构建应用。

    use_llm=False（默认）：S5 反评审与洞察用确定性内容，完全离线、可复现。
    use_llm=True：改由 judge 档模型现场生成，需要 AIEA_API_KEY。
    """
    root = root if root is not None else default_workspace_root()
    app = FastAPI(title="中小企业 AI 提效场景识别 Agent", version="2.0")
    registry = ClientRegistry(root=root)

    def _llm():
        if not use_llm:
            return None
        from .llm import LLMClient

        return LLMClient()

    # ---- 预置演示客户：懒加载并缓存，旧路径与客户列表都用它 ----
    @lru_cache(maxsize=1)
    def preset_report() -> dict[str, Any]:
        return run_seed_diagnosis(root=root, llm=_llm())

    def ensure_preset() -> None:
        """把预置客户登记进注册表，让它在客户列表里可见。"""
        if registry.get(TENANT) is not None:
            return
        preset_report()  # 先跑出报告与工作区
        base = Path(root) / TENANT
        if not base.exists():
            return
        import json as _json

        from .seed_materials import CLIENT_PROFILE

        profile = {
            "slug": TENANT,
            "name": CLIENT_PROFILE["short_name"],
            "industry": CLIENT_PROFILE["industry"],
            "headcount": CLIENT_PROFILE["headcount"],
            "departments": CLIENT_PROFILE["departments"],
            "excluded": CLIENT_PROFILE["excluded"],
            "background": CLIENT_PROFILE["background"],
            "as_of": CLIENT_PROFILE["as_of"],
            "created_at": CLIENT_PROFILE["as_of"],
            "status": "diagnosed",
            "reachable_grade": "A",
            "delivery_form": "完整诊断",
            "is_preset": True,
        }
        (base / "client.json").write_text(
            _json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def resolve_slug(slug: str) -> str:
        checked = safe_slug(slug)
        if checked is None:
            raise HTTPException(status_code=404, detail="客户不存在")
        ensure_preset()
        if registry.get(checked) is None:
            raise HTTPException(status_code=404, detail="客户不存在")
        return checked

    def report_of(slug: str) -> dict[str, Any]:
        """取某客户的报告。预置客户走 seed，其余读盘。"""
        if slug == TENANT:
            return preset_report()
        r = load_report(tenant=slug, root=root)
        if r is None:
            raise HTTPException(
                status_code=409,
                detail="该客户还没有诊断报告。请先上传材料，再点『开始诊断』。",
            )
        return r

    # ======================= 客户管理 =======================
    @app.get("/api/clients")
    def list_clients() -> dict[str, Any]:
        ensure_preset()
        return {
            "items": [c.to_dict() for c in registry.list()],
            "evidence_roles": EVIDENCE_ROLES,
        }

    @app.post("/api/clients")
    def create_client(payload: ClientIn) -> dict[str, Any]:
        profile = registry.create(
            name=payload.name,
            industry=payload.industry,
            headcount=payload.headcount,
            departments=payload.departments,
            excluded=payload.excluded,
            background=payload.background,
            as_of=payload.as_of,
        )
        return profile.to_dict()

    @app.get("/api/clients/{slug}")
    def get_client(slug: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        profile = registry.get(checked)
        assert profile is not None
        return profile.to_dict()

    @app.delete("/api/clients/{slug}")
    def delete_client(slug: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        profile = registry.get(checked)
        if profile is not None and profile.is_preset:
            raise HTTPException(status_code=400, detail="预置演示客户不可删除")
        ok = registry.delete(checked)
        if not ok:
            raise HTTPException(status_code=400, detail="删除失败")
        return {"deleted": checked}

    # ======================= 材料 =======================
    @app.get("/api/clients/{slug}/materials")
    def get_materials(slug: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        return {"items": list_materials(root=root, slug=checked), "evidence_roles": EVIDENCE_ROLES}

    @app.post("/api/clients/{slug}/materials")
    async def upload_material(
        slug: str,
        file: UploadFile = File(...),
        evidence_role: str = Form("R1"),
    ) -> dict[str, Any]:
        checked = resolve_slug(slug)
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"单个文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限",
            )
        record = save_material(
            root=root,
            slug=checked,
            filename=file.filename or "unnamed",
            content=content,
            evidence_role=evidence_role if evidence_role in EVIDENCE_ROLES else "R1",
        )
        if record.get("accepted") or record.get("stored_as"):
            profile = registry.get(checked)
            if profile is not None and profile.status == "draft":
                registry.update(checked, status="materials")
        return record

    # ======================= 诊断 =======================
    @app.post("/api/clients/{slug}/diagnose")
    def diagnose(slug: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        if checked == TENANT:
            r = preset_report()
        else:
            try:
                r = run_diagnosis(tenant=checked, root=root, llm=_llm())
            except DiagnosisNotReady as err:
                raise HTTPException(status_code=409, detail=str(err)) from err
        return {
            "ok": True,
            "scenarios": len(r["cards"]),
            "parents": len(r["parents"]),
            "delivery_form": r["delivery_form"],
            "traceability": r["scorecard"]["evidence_traceability"],
            "quantified": r["scorecard"]["scenarios_quantified"],
            "direction_only": r["scorecard"]["scenarios_direction_only"],
            "naming_source": r.get("naming_source", "curated"),
        }

    # ======================= 按客户取交付物 =======================
    def _view(slug: str, name: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        return VIEW_BUILDERS[name](report_of(checked))

    @app.get("/api/clients/{slug}/overview")
    def client_overview(slug: str) -> dict[str, Any]:
        return _view(slug, "overview")

    @app.get("/api/clients/{slug}/scenarios")
    def client_scenarios(slug: str) -> dict[str, Any]:
        return _view(slug, "scenarios")

    @app.get("/api/clients/{slug}/matrix")
    def client_matrix(slug: str) -> dict[str, Any]:
        return _view(slug, "matrix")

    @app.get("/api/clients/{slug}/roi")
    def client_roi(slug: str) -> dict[str, Any]:
        return _view(slug, "roi")

    @app.get("/api/clients/{slug}/roadmap")
    def client_roadmap(slug: str) -> dict[str, Any]:
        return _view(slug, "roadmap")

    @app.get("/api/clients/{slug}/evidence")
    def client_evidence(slug: str) -> dict[str, Any]:
        return _view(slug, "evidence")

    @app.get("/api/clients/{slug}/insights")
    def client_insights(slug: str) -> dict[str, Any]:
        return _view(slug, "insights")

    @app.get("/api/clients/{slug}/gaps")
    def client_gaps(slug: str) -> dict[str, Any]:
        return _view(slug, "gaps")

    @app.get("/api/clients/{slug}/counter-review")
    def client_counter_review(slug: str) -> dict[str, Any]:
        return _view(slug, "counter-review")

    @app.get("/api/clients/{slug}/observability")
    def client_observability(slug: str) -> dict[str, Any]:
        return _view(slug, "observability")

    # ======================= 术语表与分级标准 =======================
    # 不分租户：这是全局参考信息，任何客户下都一样
    @app.get("/api/glossary")
    def glossary() -> dict[str, Any]:
        return {
            "terms": all_terms(),
            "groups": grouped_terms(),
            "grade_scale": grade_scale(),
            "work_form_scale": work_form_scale(),
            "difficulty_scale": difficulty_scale(),
            "delivery_scale": delivery_scale(),
            "tier_scale": tier_scale(),
            "severity_scale": severity_scale(),
            "note": "报告里出现的每个专有名词都能在这里查到，包含判定标准与为什么值得看。",
        }

    @app.get("/api/glossary/{word}")
    def glossary_term(word: str) -> dict[str, Any]:
        d = explain(word)
        if d is None:
            raise HTTPException(status_code=404, detail=f"术语表中没有「{word}」")
        return d

    # ======================= 连接器（L1 只读双轨） =======================
    # 测试专用连接器不进客户可见目录
    _HIDDEN_CONNECTORS = {"demo_ticketing", "demo_injection"}

    @app.get("/api/connectors")
    def connector_catalog() -> dict[str, Any]:
        return {
            "items": [
                s.to_dict() for s in list_specs() if s.key not in _HIDDEN_CONNECTORS
            ],
            "note": (
                "全部连接器只读，无一例外。等级上限是诚实声明的实际能力边界——"
                "声明 C 级意味着该系统拿不到可量化的明细。"
            ),
        }

    @app.get("/api/clients/{slug}/connectors")
    def client_connectors(slug: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        return {"items": list_bindings(root=root, slug=checked)}

    @app.post("/api/clients/{slug}/connectors")
    def bind_connector(slug: str, payload: ConnectorBindIn) -> dict[str, Any]:
        checked = resolve_slug(slug)
        try:
            record = save_binding(
                root=root,
                slug=checked,
                key=payload.key,
                credential=CredentialRef(
                    provider=payload.key, key_id=payload.key_id, secret=payload.secret
                ),
            )
        except KeyError as err:
            raise HTTPException(status_code=400, detail=f"未知连接器：{payload.key}") from err
        return record

    @app.post("/api/clients/{slug}/connectors/{key}/sync")
    def sync(slug: str, key: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        result = sync_connector(root=root, slug=checked, key=key)
        if not result.get("ok"):
            detail = result.get("note", "同步失败")
            if result.get("next_action"):
                detail = f"{detail}｜下一步：{result['next_action']}"
            raise HTTPException(status_code=409, detail=detail)
        return result

    # ======================= 效果衡量 =======================
    @app.get("/api/clients/{slug}/effect")
    def effect(slug: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        return effect_summary(root=root, slug=checked)

    @app.post("/api/clients/{slug}/baselines")
    def add_baseline(slug: str, payload: MetricIn) -> dict[str, Any]:
        checked = resolve_slug(slug)
        r = capture_baseline(
            root=root, slug=checked, card_id=payload.card_id, metric=payload.metric,
            value=payload.value, timestamps=payload.timestamps,
            sample_size=payload.sample_size, source=payload.source, note=payload.note,
        )
        if not r.get("ok"):
            detail = r.get("note", "记录失败")
            if r.get("next_action"):
                detail = f"{detail}｜{r['next_action']}"
            raise HTTPException(status_code=422, detail=detail)
        return r

    @app.post("/api/clients/{slug}/measurements")
    def add_measurement(slug: str, payload: MetricIn) -> dict[str, Any]:
        checked = resolve_slug(slug)
        r = measure_effect(
            root=root, slug=checked, card_id=payload.card_id, metric=payload.metric,
            value=payload.value, timestamps=payload.timestamps,
            sample_size=payload.sample_size, source=payload.source, note=payload.note,
        )
        if r.get("code") != "ok":
            detail = r.get("note", "无法衡量")
            if r.get("next_action"):
                detail = f"{detail}｜{r['next_action']}"
            raise HTTPException(status_code=422, detail=detail)
        return r

    # ======================= 反馈（按客户） =======================
    @app.get("/api/clients/{slug}/feedback")
    def client_feedback(slug: str) -> dict[str, Any]:
        checked = resolve_slug(slug)
        return {"items": Workspace(tenant=checked, root=root).list_feedback()}

    @app.post("/api/clients/{slug}/feedback")
    def add_client_feedback(slug: str, payload: FeedbackIn) -> dict[str, Any]:
        checked = resolve_slug(slug)
        ws = Workspace(tenant=checked, root=root)
        ctx = ToolContext(tenant=checked, workspace=ws, kb=KnowledgeBase.load_seed())
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

    # ======================= 旧路径：继续指向预置客户 =======================
    def _legacy(name: str):
        def handler() -> dict[str, Any]:
            return VIEW_BUILDERS[name](preset_report())

        return handler

    for _name, _path in (
        ("overview", "/api/overview"),
        ("scenarios", "/api/scenarios"),
        ("matrix", "/api/matrix"),
        ("roi", "/api/roi"),
        ("roadmap", "/api/roadmap"),
        ("evidence", "/api/evidence"),
        ("insights", "/api/insights"),
        ("gaps", "/api/gaps"),
        ("counter-review", "/api/counter-review"),
        ("observability", "/api/observability"),
    ):
        app.add_api_route(_path, _legacy(_name), methods=["GET"], name=f"legacy_{_name}")

    @app.get("/api/feedback")
    def legacy_list_feedback() -> dict[str, Any]:
        return {"items": Workspace(tenant=TENANT, root=root).list_feedback()}

    @app.post("/api/feedback")
    def legacy_add_feedback(payload: FeedbackIn) -> dict[str, Any]:
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

    # ======================= 前端 =======================
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app(use_llm=os.getenv("AIEA_USE_LLM", "").lower() in ("1", "true", "yes"))
