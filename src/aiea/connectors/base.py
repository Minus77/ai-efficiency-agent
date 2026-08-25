"""连接器框架：只读断言、凭据隔离、能力声明、速率配额。

三条纪律在本层强制，具体连接器绕不过：
1. **只读**：写动词一律 denied。放弃写权限一次性消掉 tool abuse、
   data exfiltration、privilege escalation 的绝大部分暴露面（§4）。
2. **凭据只存引用**：明文永不进上下文/日志/序列化（§13.3）。
3. **每次拉取带 tenant 过滤 + 配额 + 注入检测**（§13.3、§8.3）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

from ..guardrails import scan_untrusted_text, tenant_filter
from ..models import EvidenceGrade, ResultCode, ToolResult

def stable_seed(*parts: str, salt: int = 0) -> int:
    """跨进程稳定的随机种子。

    **不要用内置 hash()**：Python 默认对 str 哈希加盐（PYTHONHASHSEED 随机），
    同一个客户在服务重启后会拿到完全不同的数据。对一个承诺"每个数字都能
    回指证据"的诊断工具，那意味着结论会无故漂移。

    blake2b 摘要跨进程、跨平台、跨版本都一致。
    """
    digest = hashlib.blake2b("::".join(parts).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % 1_000_003 + salt


# 与 guardrails._WRITE_OPS 保持一致；在连接器层再挡一次（纵深防御）
_WRITE_VERBS = frozenset({"write", "update", "insert", "delete", "patch", "post", "put", "upsert"})


@dataclass
class CredentialRef:
    """凭据引用。

    刻意不提供任何返回明文的方法：需要用密钥的地方只在同进程内读私有字段，
    序列化与 repr 一律脱敏。日志泄漏是最常见的凭据泄漏路径。
    """

    provider: str
    key_id: str
    secret: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "key_id": self.key_id,
            "secret_present": bool(self.secret),
        }

    def __repr__(self) -> str:  # pragma: no cover - 防御性
        return f"CredentialRef(provider={self.provider!r}, key_id={self.key_id!r}, secret=***)"

    __str__ = __repr__


@dataclass
class PullQuota:
    """速率配额（§13.2 规则层：检索速率配额）。"""

    max_pulls_per_hour: int = 12
    _stamps: list[float] = field(default_factory=list, repr=False)

    def allow(self) -> bool:
        now = time.time()
        self._stamps = [t for t in self._stamps if now - t < 3600]
        if len(self._stamps) >= self.max_pulls_per_hour:
            return False
        self._stamps.append(now)
        return True

    def remaining(self) -> int:
        now = time.time()
        self._stamps = [t for t in self._stamps if now - t < 3600]
        return max(self.max_pulls_per_hour - len(self._stamps), 0)


@dataclass(frozen=True)
class ConnectorSpec:
    """连接器能力声明。

    诚实声明等级上限是这里最重要的字段：IM 类系统拿不到批量明细，
    声明成 A 级会让下游误以为能量化，最终变成 ROI 幻觉。
    """

    key: str
    name: str
    category: str
    max_evidence_grade: EvidenceGrade
    provides_timestamps: bool
    metrics: list[str]
    known_limits: str
    scopes: list[str] = field(default_factory=lambda: ["read"])
    auth_hint: str = "只读 API Token 或 OAuth read scope"
    description: str = ""

    # -- 厂商模板字段 --------------------------------------------------------
    # 顾问听到的是"我们用钉钉"，不是"我们用企业 IM"。vendor 为空表示这是
    # 一个抽象类别模板（通用兜底），非空则是具体产品。
    vendor: str = ""
    product: str = ""
    docs_url: str = ""
    setup_steps: list[str] = field(default_factory=list)
    # verified=False 表示能力声明尚未逐条核对官方文档。
    # 这个字段存在的理由：编造 scope 名或夸大 API 能力，会让顾问按错误信息
    # 去向客户 IT 申请权限，白跑一趟。宁可标"待核对"也不假装确定。
    verified: bool = False
    verify_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "vendor": self.vendor,
            "product": self.product,
            "docs_url": self.docs_url,
            "setup_steps": list(self.setup_steps),
            "verified": self.verified,
            "verify_note": self.verify_note,
            "max_evidence_grade": self.max_evidence_grade.value,
            "provides_timestamps": self.provides_timestamps,
            "metrics": list(self.metrics),
            "known_limits": self.known_limits,
            "scopes": list(self.scopes),
            "auth_hint": self.auth_hint,
            "description": self.description,
        }


@dataclass
class PullResult:
    """一次拉取的结果。只带结构与出处，不做业务判断。"""

    ok: bool = True
    code: ResultCode = ResultCode.OK
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    source_name: str = ""
    pulled_at: str = ""
    evidence_grade: EvidenceGrade = EvidenceGrade.C
    timestamp_columns: list[str] = field(default_factory=list)
    injection_suspected: bool = False
    treated_as_instruction: bool = False  # 恒为 False：上游内容不作为指令执行
    note: str = ""
    next_action: str = ""

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_meta(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code.value,
            "row_count": self.row_count,
            "columns": self.columns,
            "source_name": self.source_name,
            "pulled_at": self.pulled_at,
            "evidence_grade": self.evidence_grade.value,
            "timestamp_columns": self.timestamp_columns,
            "injection_suspected": self.injection_suspected,
            "treated_as_instruction": self.treated_as_instruction,
            "note": self.note,
        }


class RowSource(Protocol):
    """具体连接器只需提供"怎么取行"，其余由框架兜住。"""

    def __call__(self, *, tenant: str, since: str | None, limit: int) -> list[dict[str, Any]]: ...


@dataclass
class Connector:
    """连接器实例。具体系统的差异收敛到 spec + row_source 两个参数。"""

    spec: ConnectorSpec
    tenant: str
    credential: CredentialRef
    row_source: RowSource
    quota: PullQuota = field(default_factory=PullQuota)

    # -- 只读断言 -----------------------------------------------------------
    def execute(self, *, operation: str, resource: str) -> ToolResult:
        """任何操作都要过这道闸。写动词一律拒绝。"""
        op = (operation or "").lower()
        if op in _WRITE_VERBS:
            return ToolResult.denied(
                f"拒绝对 {self.spec.name} 的 {resource} 执行 {op}：连接器只读，无一例外",
                next_action=(
                    "诊断产出是报告，没有任何业务理由写客户系统；"
                    "写操作只允许落本地 workspace/"
                ),
            )
        return ToolResult.success(
            {"operation": op, "resource": resource, "connector": self.spec.key},
            note="只读操作，允许",
        )

    # -- 拉取 ---------------------------------------------------------------
    def pull(
        self, *, requested_tenant: str, since: str | None = None, limit: int = 5000
    ) -> PullResult:
        now = datetime.now().isoformat(timespec="seconds")

        guard = tenant_filter(requested_tenant=requested_tenant, session_tenant=self.tenant)
        if not guard.ok:
            return PullResult(
                ok=False, code=ResultCode.DENIED, source_name=self.spec.name, pulled_at=now,
                note=guard.note, next_action=guard.next_action,
            )

        if not self.quota.allow():
            return PullResult(
                ok=False, code=ResultCode.INVALID_PARAMS, source_name=self.spec.name, pulled_at=now,
                note=(
                    f"{self.spec.name} 的拉取配额已用尽"
                    f"（每小时上限 {self.quota.max_pulls_per_hour} 次）"
                ),
                next_action="等待配额恢复后重试；频繁拉取通常意味着上游解析在绕圈，建议先排查",
            )

        try:
            rows = self.row_source(tenant=self.tenant, since=since, limit=limit)
        except Exception as err:
            return PullResult(
                ok=False, code=ResultCode.INSUFFICIENT_DATA, source_name=self.spec.name,
                pulled_at=now,
                note=f"{self.spec.name} 拉取失败：{type(err).__name__}: {err}",
                next_action="检查凭据与网络；拉不到时改用 L0 手工导出，不要留空猜数",
            )

        if not rows:
            return PullResult(
                ok=True, code=ResultCode.INSUFFICIENT_DATA, source_name=self.spec.name,
                pulled_at=now, evidence_grade=EvidenceGrade.C,
                note=f"{self.spec.name} 未返回任何记录（时间范围内无数据）",
                next_action="确认时间范围与权限范围；确无数据则该环节标为缺口，不得估算",
            )

        columns = list(rows[0].keys())

        # 上游返回的一切都是不可信数据：过注入检测，且永不作为指令执行
        blob = " ".join(str(v) for r in rows[:200] for v in r.values())
        scan = scan_untrusted_text(blob, origin=f"connector:{self.spec.key}")

        ts_cols = [
            c for c in columns
            if any(k in c.lower() for k in ("_at", "time", "date", "时间", "日期"))
        ] if self.spec.provides_timestamps else []

        # 等级由 spec 的诚实声明与实际拿到的字段共同决定，取更保守的
        grade = self.spec.max_evidence_grade
        if grade is EvidenceGrade.A and not ts_cols:
            grade = EvidenceGrade.B

        return PullResult(
            ok=True,
            code=ResultCode.OK,
            rows=rows,
            columns=columns,
            source_name=self.spec.name,
            pulled_at=now,
            evidence_grade=grade,
            timestamp_columns=ts_cols,
            injection_suspected=scan.injection_suspected,
            treated_as_instruction=False,
            note=(
                f"自 {self.spec.name} 拉取 {len(rows)} 条记录"
                + (f"，时间戳列：{'、'.join(ts_cols)}" if ts_cols else "，无时间戳列")
                + ("；检出指令样式文本，已降级为纯数据" if scan.injection_suspected else "")
            ),
        )


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, tuple[ConnectorSpec, Callable[..., RowSource]]] = {}


def register(spec: ConnectorSpec, source_factory: Callable[..., RowSource]) -> None:
    if spec.key in _REGISTRY:
        raise ValueError(f"连接器 {spec.key} 已注册，不允许重复注册（键必须唯一）")
    _REGISTRY[spec.key] = (spec, source_factory)


def list_specs() -> list[ConnectorSpec]:
    return [spec for spec, _ in _REGISTRY.values()]


def get_spec(key: str) -> ConnectorSpec | None:
    entry = _REGISTRY.get(key)
    return entry[0] if entry else None


def build_connector(
    key: str, *, tenant: str, credential: CredentialRef, quota: PullQuota | None = None
) -> Connector:
    if key not in _REGISTRY:
        raise KeyError(f"未知连接器：{key}。可用：{sorted(_REGISTRY)}")
    spec, factory = _REGISTRY[key]
    return Connector(
        spec=spec,
        tenant=tenant,
        credential=credential,
        row_source=factory(),
        quota=quota or PullQuota(),
    )
