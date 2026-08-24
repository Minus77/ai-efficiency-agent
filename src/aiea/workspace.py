"""工作区：落盘即唯一真相源（§5 Write 支柱、长周期会话模型）。

会话上下文可随时丢弃重建——每次恢复先读盘，不依赖历史上下文。
写操作只允许落这里（§4 只读边界）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .guardrails import assert_readonly, redact_pii

# 单工具返回 > 2K token 一律落盘，上下文只留路径（§5）
SPILL_THRESHOLD_CHARS = 6000


@dataclass
class Workspace:
    tenant: str
    root: Path | str = "workspace"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        for sub in ("task-cards", "evidence", "materials", "trace", "feedback"):
            (self.path / sub).mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return Path(self.root) / self.tenant

    # -- 写 -----------------------------------------------------------------
    def _guarded(self, relative: str) -> Path:
        target = self.path / relative
        verdict = assert_readonly(operation="write", target=f"workspace/{self.tenant}/{relative}")
        if not verdict.ok:  # pragma: no cover - 结构性保险
            raise PermissionError(verdict.note)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_text(self, relative: str, content: str) -> Path:
        target = self._guarded(relative)
        target.write_text(content, encoding="utf-8")
        return target

    def write_json(self, relative: str, payload: Any) -> Path:
        target = self._guarded(relative)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return target

    def spill(self, name: str, content: str) -> str:
        """大返回落盘，上下文只留路径。"""
        p = self.write_text(f"materials/{name}", redact_pii(content))
        return str(p.relative_to(self.path))

    # -- 读（状态重建） -------------------------------------------------------
    def read_json(self, relative: str, default: Any = None) -> Any:
        target = self.path / relative
        if not target.exists():
            return default
        return json.loads(target.read_text(encoding="utf-8"))

    def list_cards(self) -> list[dict[str, Any]]:
        cards = []
        for p in sorted((self.path / "task-cards").glob("*.json")):
            cards.append(json.loads(p.read_text(encoding="utf-8")))
        return cards

    def list_evidence(self) -> list[dict[str, Any]]:
        items = []
        for p in sorted((self.path / "evidence").glob("*.json")):
            items.append(json.loads(p.read_text(encoding="utf-8")))
        return items

    def list_feedback(self) -> list[dict[str, Any]]:
        items = []
        for p in sorted((self.path / "feedback").glob("*.json")):
            items.append(json.loads(p.read_text(encoding="utf-8")))
        return items

    def state(self) -> dict[str, Any]:
        """读盘重建状态：SCOPE + 任务卡 + 台账 + 阶段。"""
        return {
            "tenant": self.tenant,
            "scope": self.read_json("scope.json", {}),
            "stage": (self.read_json("state.json", {}) or {}).get("stage", "S0_立项与口径"),
            "cards": self.list_cards(),
            "evidence": self.list_evidence(),
            "findings_exists": (self.path / "FINDINGS.md").exists(),
        }
