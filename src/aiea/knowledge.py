"""三库物理隔离检索（§8.1、§8.2、§8.3、§15.2）。

两条检索路线的分工：
- 客户本次素材 → 即时检索（process_search / document_forensics），**不进任何索引**
- 方法论 / 基准 / 案例 / 能力 → 本模块，跨诊断复用才值得建索引

本实现用关键词 + 元数据打分替代向量库（接口保留可替换），
因为防幻觉的关键不在召回算法，而在 no_grounding、元数据强制、库间隔离这三条约束。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from .models import ResultCode, ToolResult

SEED_DIR = Path(__file__).resolve().parents[2] / "seed" / "knowledge"

# 相关性阈值：低于此值一律视为未召回 → no_grounding（§8.3）
RELEVANCE_THRESHOLD = 0.18
PROBATION_PENALTY = 0.5  # probation 分区检索时降权


class Library(str, Enum):
    METHODOLOGY = "methodology"  # 方法论库（L-抽象，客户侧唯一出口）
    BENCHMARK = "benchmark"  # 基准库（L-公开）
    CASE = "case"  # 案例库（L-公开，主来源）
    CAPABILITY = "capability"  # AI 能力边界（L-公开，版本化）


_FILES = {
    Library.METHODOLOGY: "methodology.json",
    Library.BENCHMARK: "benchmark.json",
    Library.CASE: "case.json",
    Library.CAPABILITY: "capability.json",
}


# ---------------------------------------------------------------------------
# 去具体化检验（§15.2.1）——L-抽象 入库硬门槛
# ---------------------------------------------------------------------------
_INDUSTRY_WORDS = (
    "零售", "建材", "家居", "餐饮", "制造", "电商", "物流", "医药", "教育", "服装", "分销",
)
_SCALE_PATTERN = re.compile(r"\d+\s*[–\-~到]\s*\d+\s*人|\d+\s*人(公司|企业|规模|团队)|\d+\s*人以下")
_NUMERIC_PATTERN = re.compile(r"\d+(\.\d+)?\s*(小时|分钟|万元|元|%|次|条|天|个月|倍)")
_SYSTEM_NAMES = ("钉钉", "企微", "企业微信", "飞书", "金蝶", "用友", "旺铺", "有赞", "微信")


@dataclass
class DespecVerdict:
    passed: bool
    hits: list[str] = field(default_factory=list)
    note: str = ""


def despecification_check(text: str) -> DespecVerdict:
    """删除全部行业、规模、系统名、数值后仍成立且仍有价值，才允许入库。"""
    hits: list[str] = []
    found_industry = [w for w in _INDUSTRY_WORDS if w in text]
    if found_industry:
        hits.append(f"行业限定词：{'、'.join(found_industry)}")
    if _SCALE_PATTERN.search(text):
        hits.append("规模限定（人数区间）")
    if _NUMERIC_PATTERN.search(text):
        hits.append("量化数值")
    found_systems = [s for s in _SYSTEM_NAMES if s in text]
    if found_systems:
        hits.append(f"系统名：{'、'.join(found_systems)}")

    if hits:
        return DespecVerdict(
            passed=False,
            hits=hits,
            note=(
                "未通过去具体化检验：条目含可识别维度（"
                + "；".join(hits)
                + "）。这是客户业务事实的聚合，不得入库；请改写为与行业、规模、数值无关的方法论表述。"
            ),
        )
    return DespecVerdict(passed=True, hits=[], note="通过去具体化检验：删除行业/规模/系统/数值后仍成立且仍有价值。")


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    """中英混合的轻量切词：英文按词，中文按 2-gram。"""
    tokens = re.findall(r"[a-zA-Z]{2,}", text.lower())
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", text)
    tokens += [cjk[i : i + 2] for i in range(len(cjk) - 1)]
    return tokens


@dataclass
class KnowledgeBase:
    entries: dict[Library, list[dict[str, Any]]] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # 检索内容永不触发记忆写入（§9.7）：该列表必须恒为空，作为断言锚点
    pending_writes: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load_seed(cls, seed_dir: Path | None = None) -> KnowledgeBase:
        base = seed_dir or SEED_DIR
        entries: dict[Library, list[dict[str, Any]]] = {}
        for lib, filename in _FILES.items():
            path = base / filename
            entries[lib] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return cls(entries=entries)

    # -- 检索 ---------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        library: Library,
        top_k: int = 4,
        as_of: str | None = None,
        include_probation: bool = False,
    ) -> ToolResult:
        """单库检索，永不混检。召回为空或全低于阈值 → no_grounding。"""
        pool = list(self.entries.get(library, []))
        if include_probation:
            pool += [c for c in self.candidates if c.get("library") == library.value]

        q_tokens = set(_tokenize(query))
        scored: list[dict[str, Any]] = []
        today = date.fromisoformat(as_of) if as_of else date.today()

        for entry in pool:
            haystack = " ".join(
                [entry.get("text", ""), " ".join(entry.get("tags", [])), entry.get("capability", "")]
            )
            e_tokens = set(_tokenize(haystack))
            if not e_tokens:
                continue
            overlap = len(q_tokens & e_tokens)
            score = overlap / max(len(q_tokens), 1)
            if entry.get("status") == "probation":
                score *= PROBATION_PENALTY
            if score < RELEVANCE_THRESHOLD:
                continue

            # 时效性：超 validity_days 自动降置信度并标 stale（§8.3）
            stale = False
            published = entry.get("published_at")
            validity = entry.get("validity_days")
            if published and validity:
                age_days = (today - date.fromisoformat(published)).days
                stale = age_days > int(validity)
            elif published:
                stale = (today - date.fromisoformat(published)).days > 900

            source_layer = entry.get("source_layer", "L-公开")
            scored.append(
                {
                    "entry_id": entry.get("entry_id"),
                    "text": entry.get("text", ""),
                    "library": library.value,
                    "source_layer": source_layer,
                    "origin": entry.get("origin", ""),
                    "published_at": published or "",
                    "version": entry.get("version", ""),
                    "status": entry.get("status", "active"),
                    "score": round(score, 4),
                    "stale": stale,
                    # §15.2.2：L-抽象 条目不得作为"行业基准"被引用
                    "citable_as_benchmark": source_layer == "L-公开" and library is Library.BENCHMARK,
                    "capability": entry.get("capability"),
                    "known_limits": entry.get("known_limits"),
                    "selection_criteria": entry.get("selection_criteria"),
                    "automation_rate_range": entry.get("automation_rate_range"),
                }
            )

        if not scored:
            return ToolResult.no_grounding(
                f"在 {library.value} 库中未检索到与「{query}」相关且高于阈值的条目",
                next_action=(
                    "不得用训练知识补齐；对应数字标为缺口或降为 C 级方向性判断，"
                    "并在报告的未获取材料清单中列出"
                ),
            )

        scored.sort(key=lambda h: h["score"], reverse=True)
        hits = scored[:top_k]
        return ToolResult.success(
            {"hits": hits, "library": library.value},
            source=[f"{h['origin']}（{h['published_at']}）" for h in hits],
            sample_size=len(hits),
            note="每条带出处与时效；stale 条目已标注并应降置信度使用",
        )

    def index_customer_material(self, *, tenant: str, content: str) -> ToolResult:
        """客户原始数据不进任何向量库（§8.1）。此入口永远拒绝。"""
        return ToolResult.denied(
            f"拒绝为 tenant={tenant} 的客户原始素材建索引",
            next_action=(
                "客户素材只在采集子 Agent 上下文活一次即丢；"
                "如需复用请走 playbook_propose 提交去具体化后的方法论候选"
            ),
        )


def playbook_propose(kb: KnowledgeBase, *, statement: str, source_tenant: str) -> ToolResult:
    """只写候选区（§9.7）。playbook_commit 是人工动作，不暴露给 Agent。"""
    verdict = despecification_check(statement)
    if not verdict.passed:
        return ToolResult(
            ok=False,
            code=ResultCode.DENIED,
            data={"hits": verdict.hits},
            next_action="改写为行业无关的方法论表述后重提；客户业务事实一律不入库",
            note=verdict.note,
        )

    entry = {
        "entry_id": f"cand-{len(kb.candidates) + 1:03d}",
        "text": statement,
        "tags": _tokenize(statement)[:8],
        "library": Library.METHODOLOGY.value,
        "source_layer": "L-抽象",
        "origin": f"客户诊断沉淀（匿名映射：{source_tenant}）",
        "published_at": date.today().isoformat(),
        "version": "candidate",
        "status": "probation",
        # §15.2.2 可级联删除：映射必须从第一天就建
        "source_tenant_mapping": source_tenant,
    }
    kb.candidates.append(entry)
    return ToolResult.success(
        {"entry_id": entry["entry_id"], "status": "probation", "area": "candidate"},
        note=(
            "已写入候选区并置为 probation：仅在人工抽检的诊断中生效，"
            "累计 5 次无反例才可由人工转 active"
        ),
    )
