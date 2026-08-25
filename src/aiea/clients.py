"""客户注册表：多租户建档与切换。

安全要点：slug 直接参与文件系统路径，因此必须消毒。
路径穿越（`../../etc`）是这里最现实的攻击面，比注入更容易被忽略。
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .config import default_workspace_root

# §17.2 规模范围：20–200 人。超范围不拒接，但标注"范围外，基准参考有限"且不计入自学习样本
SCOPE_MIN, SCOPE_MAX = 20, 200

_SLUG_OK = re.compile(r"[a-z0-9-]+")


def slugify(name: str) -> str:
    """把客户名转成安全的目录名。

    中文没有可用的 ASCII 转写时退化为稳定哈希，保证：
    - 结果只含 [a-z0-9-]
    - 绝不产生 ""、"."、".." 或含 "/" 的值（否则就是路径穿越）
    """
    normalized = unicodedata.normalize("NFKD", name or "")
    ascii_part = normalized.encode("ascii", "ignore").decode("ascii").lower()
    ascii_part = re.sub(r"[^a-z0-9]+", "-", ascii_part).strip("-")

    if not ascii_part or set(ascii_part) <= {"-"}:
        # 中文名等无 ASCII 可用：用稳定短哈希，保证可复现
        import hashlib

        digest = hashlib.sha256((name or "client").encode("utf-8")).hexdigest()[:10]
        return f"c-{digest}"

    # 再兜一层：确保没有 . 与 /
    safe = "".join(_SLUG_OK.findall(ascii_part))
    return safe or "client"


def safe_slug(raw: str) -> str | None:
    """校验外部传入的 slug。不合法返回 None，调用方一律当作"不存在"。"""
    if not raw or len(raw) > 64:
        return None
    if raw in (".", "..") or "/" in raw or "\\" in raw or ".." in raw:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", raw):
        return None
    return raw


@dataclass
class ClientProfile:
    slug: str
    name: str
    industry: str = ""
    headcount: int | None = None
    departments: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    background: str = ""
    as_of: str = field(default_factory=lambda: date.today().isoformat())
    created_at: str = field(default_factory=lambda: date.today().isoformat())

    # 流程状态：draft → materials → diagnosed
    status: str = "draft"
    reachable_grade: str = ""
    delivery_form: str = ""

    # 派生字段（读盘时计算，不落库）
    material_count: int = 0
    has_report: bool = False
    is_preset: bool = False

    @property
    def out_of_scope(self) -> bool:
        if self.headcount is None:
            return False
        return not (SCOPE_MIN <= self.headcount <= SCOPE_MAX)

    @property
    def scope_note(self) -> str:
        if not self.out_of_scope:
            return ""
        if (self.headcount or 0) < SCOPE_MIN:
            return (
                f"范围外（{self.headcount} 人 < {SCOPE_MIN} 人）：流程通常尚未成型、缺少可自动化的重复量，"
                "基准参考有限，且不计入自学习样本。"
            )
        return (
            f"范围外（{self.headcount} 人 > {SCOPE_MAX} 人）：建议改做部门级诊断而非全公司，"
            "基准参考有限，且不计入自学习样本。"
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["out_of_scope"] = self.out_of_scope
        d["scope_note"] = self.scope_note
        return d


@dataclass
class ClientRegistry:
    """以工作区目录为唯一真相源，不引数据库——与 §5 的落盘原则一致。"""

    root: Path | str | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root if self.root is not None else default_workspace_root())
        self.root.mkdir(parents=True, exist_ok=True)

    # -- 内部 ---------------------------------------------------------------
    def _dir(self, slug: str) -> Path | None:
        checked = safe_slug(slug)
        if checked is None:
            return None
        return self.root / checked

    def _load(self, path: Path) -> ClientProfile | None:
        meta = path / "client.json"
        if not meta.exists():
            return None
        raw = json.loads(meta.read_text(encoding="utf-8"))
        known = {f for f in ClientProfile.__dataclass_fields__}
        profile = ClientProfile(**{k: v for k, v in raw.items() if k in known})
        # 派生字段每次读盘重算，避免与磁盘状态不一致
        materials = path / "materials"
        profile.material_count = len(list(materials.glob("*"))) if materials.exists() else 0
        profile.has_report = (path / "REPORT.json").exists()
        return profile

    # -- 对外 ---------------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        industry: str = "",
        headcount: int | None = None,
        departments: list[str] | None = None,
        excluded: list[str] | None = None,
        background: str = "",
        as_of: str | None = None,
    ) -> ClientProfile:
        base = slugify(name)
        slug = base
        n = 2
        while (self.root / slug / "client.json").exists():
            slug = f"{base}-{n}"
            n += 1

        profile = ClientProfile(
            slug=slug,
            name=name,
            industry=industry,
            headcount=headcount,
            departments=departments or [],
            excluded=excluded or [],
            background=background,
            as_of=as_of or date.today().isoformat(),
        )
        target = self.root / slug
        for sub in ("materials", "task-cards", "evidence", "trace", "feedback"):
            (target / sub).mkdir(parents=True, exist_ok=True)
        (target / "client.json").write_text(
            json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return profile

    def list(self) -> list[ClientProfile]:
        out: list[ClientProfile] = []
        for path in sorted(self.root.iterdir()):
            if not path.is_dir():
                continue
            profile = self._load(path)
            if profile is not None:
                out.append(profile)
        out.sort(key=lambda p: (not p.is_preset, p.created_at, p.name))
        return out

    def get(self, slug: str) -> ClientProfile | None:
        path = self._dir(slug)
        if path is None or not path.exists():
            return None
        return self._load(path)

    def update(self, slug: str, **changes) -> ClientProfile | None:
        path = self._dir(slug)
        if path is None:
            return None
        profile = self._load(path)
        if profile is None:
            return None
        allowed = {
            "name", "industry", "headcount", "departments", "excluded", "background",
            "as_of", "status", "reachable_grade", "delivery_form",
        }
        for key, value in changes.items():
            if key in allowed and value is not None:
                setattr(profile, key, value)
        (path / "client.json").write_text(
            json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self._load(path)

    def delete(self, slug: str) -> bool:
        path = self._dir(slug)
        if path is None or not path.exists():
            return False
        if not (path / "client.json").exists():
            # 不是客户目录，拒绝删——防止误删 workspace 下的其他内容
            return False
        shutil.rmtree(path)
        return True
