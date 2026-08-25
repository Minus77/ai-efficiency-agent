"""L1 连接器接入诊断（§4 双轨并行、§3.1 冲突裁决）。

定位：把连接器拉到的行转成与 L0 手工导入**同一形态**（ParsedMaterial + 落盘 CSV），
因此下游 derive / diagnose 完全不需要区分数据是人工上传还是 API 拉取。

三条纪律：
1. 双轨并行而非二选一：同一客户可 L0 打底、L1 补强。
2. L1 拿到的客观计数用于**校验** L0 自述——这是 R1 单据考古的自动化版本。
3. L1 拉取失败绝不阻断诊断，降级为显式缺口（拿不到就说拿不到）。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .clients import safe_slug
from .config import default_workspace_root
from .connectors import build_connector, get_spec
from .connectors.base import CredentialRef, PullQuota, PullResult
from .evidence import Claim, adjudicate
from .intake import ParsedMaterial, parse_bytes, save_material
from .models import EvidenceGrade, ResultCode, SourceType

BINDINGS_FILE = "connectors.json"


@dataclass
class ConnectorBinding:
    """客户与连接器的绑定。凭据只存引用，明文不落盘。"""

    key: str
    credential: CredentialRef
    bound_at: str = ""
    last_sync_at: str = ""
    last_row_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "credential": self.credential.to_dict(),
            "bound_at": self.bound_at,
            "last_sync_at": self.last_sync_at,
            "last_row_count": self.last_row_count,
        }


def _base(root: Path | str | None, slug: str) -> Path | None:
    checked = safe_slug(slug)
    if checked is None:
        return None
    base = Path(root if root is not None else default_workspace_root()) / checked
    return base if (base / "client.json").exists() else None


# ---------------------------------------------------------------------------
# 绑定管理
# ---------------------------------------------------------------------------
def save_binding(
    *, root: Path | str | None = None, slug: str, key: str, credential: CredentialRef
) -> dict[str, Any]:
    """绑定一个连接器。未知 key 直接抛错，避免把拼错的名字静默存下来。"""
    if get_spec(key) is None:
        raise KeyError(f"未知连接器：{key}")
    base = _base(root, slug)
    if base is None:
        raise KeyError(f"客户不存在：{slug}")

    path = base / BINDINGS_FILE
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing = [b for b in existing if b.get("key") != key]

    binding = ConnectorBinding(
        key=key, credential=credential, bound_at=date.today().isoformat()
    )
    record = binding.to_dict()
    # 明文密钥单独存到受限文件，不进 connectors.json（后者会被 API 回显）
    existing.append(record)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    secrets_path = base / ".connector-secrets.json"
    secrets = json.loads(secrets_path.read_text(encoding="utf-8")) if secrets_path.exists() else {}
    secrets[key] = {"provider": credential.provider, "key_id": credential.key_id,
                    "secret": credential.secret}
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def list_bindings(*, root: Path | str | None = None, slug: str) -> list[dict[str, Any]]:
    base = _base(root, slug)
    if base is None:
        return []
    path = base / BINDINGS_FILE
    if not path.exists():
        return []
    items = json.loads(path.read_text(encoding="utf-8"))
    # 补上 spec 信息，方便前端直接渲染能力与边界
    for item in items:
        spec = get_spec(item.get("key", ""))
        item["spec"] = spec.to_dict() if spec else None
    return items


def _load_credential(base: Path, key: str) -> CredentialRef | None:
    path = base / ".connector-secrets.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8")).get(key)
    if not data:
        return None
    return CredentialRef(
        provider=data.get("provider", key),
        key_id=data.get("key_id", ""),
        secret=data.get("secret", ""),
    )


# ---------------------------------------------------------------------------
# 拉取 → ParsedMaterial
# ---------------------------------------------------------------------------
def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def pull_to_material(
    *,
    key: str,
    tenant: str,
    credential: CredentialRef,
    since: str | None = None,
    limit: int = 5000,
) -> tuple[ParsedMaterial | None, dict[str, Any]]:
    """拉取并转成 ParsedMaterial。返回 (material, meta)。"""
    spec = get_spec(key)
    if spec is None:
        return None, {"ok": False, "note": f"未知连接器：{key}", "source_kind": "L1"}

    connector = build_connector(key, tenant=tenant, credential=credential)
    result: PullResult = connector.pull(requested_tenant=tenant, since=since, limit=limit)

    meta: dict[str, Any] = {
        **result.to_meta(),
        "connector_key": key,
        "source_kind": "L1",
        "spec": spec.to_dict(),
    }
    if not result.ok or not result.rows:
        meta["next_action"] = result.next_action or (
            "该连接器暂时取不到数据；改用 L0 手工导出补上，不要留空猜数"
        )
        return None, meta

    csv_text = _rows_to_csv(result.rows)
    filename = f"{key}-{date.today().isoformat()}.csv"
    material = parse_bytes(csv_text.encode("utf-8"), filename=filename)

    # 连接器声明的等级是上限：实际等级取两者更保守的那个
    meta["evidence_grade"] = result.evidence_grade.value
    meta["csv_text"] = csv_text
    meta["filename"] = filename
    return material, meta


def sync_connector(
    *,
    root: Path | str | None = None,
    slug: str,
    key: str,
    since: str | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    """同步一次：拉取 → 落盘为材料 → 登记元数据。"""
    base = _base(root, slug)
    if base is None:
        return {"ok": False, "note": f"客户不存在：{slug}", "next_action": "请先建档"}

    credential = _load_credential(base, key)
    if credential is None:
        return {
            "ok": False,
            "note": f"连接器 {key} 未绑定到该客户",
            "next_action": "先在连接器页面完成绑定（填入只读凭据）后再同步",
        }

    try:
        material, meta = pull_to_material(
            key=key, tenant=slug, credential=credential, since=since, limit=limit
        )
    except Exception as err:
        return {
            "ok": False,
            "note": f"同步失败：{type(err).__name__}: {err}",
            "next_action": "检查凭据与网络连通性；拉不到时改用 L0 手工导出补齐，不要留空猜数",
        }

    if material is None:
        return {
            "ok": False,
            "note": meta.get("note", "未取到数据"),
            "next_action": meta.get("next_action", "改用 L0 手工导出补齐"),
            "injection_suspected": meta.get("injection_suspected", False),
            "treated_as_instruction": False,
        }

    record = save_material(
        root=root, slug=slug, filename=meta["filename"],
        content=meta["csv_text"].encode("utf-8"),
        evidence_role="R1",
    )

    # 更新绑定的同步状态
    path = base / BINDINGS_FILE
    if path.exists():
        items = json.loads(path.read_text(encoding="utf-8"))
        for item in items:
            if item.get("key") == key:
                item["last_sync_at"] = meta.get("pulled_at", "")
                item["last_row_count"] = material.row_count
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "connector_key": key,
        "source_name": meta.get("source_name", ""),
        "row_count": material.row_count,
        "columns": material.columns,
        "timestamp_columns": material.timestamp_columns,
        "evidence_grade": meta["evidence_grade"],
        "stored_as": record.get("stored_as"),
        "pulled_at": meta.get("pulled_at", ""),
        "injection_suspected": meta.get("injection_suspected", False),
        "treated_as_instruction": False,
        "note": meta.get("note", ""),
        "source_kind": "L1",
    }


# ---------------------------------------------------------------------------
# L0 / L1 交叉互校（§3.1）
# ---------------------------------------------------------------------------
def cross_check_l0_l1(
    *,
    activity: str,
    l0_value: float | None,
    l0_source: SourceType | None,
    l0_origin: str,
    l1_value: float | None,
    l1_origin: str,
) -> dict[str, Any]:
    """用 L1 的客观计数校验 L0 自述。

    这是 §3 R1「单据考古」的自动化版本：同一活动两路取证，
    按固定裁决序择一，偏差过大转人工——**不取均值掩盖分歧**。
    """
    if l0_value is None or l1_value is None or l0_source is None:
        return {
            "code": ResultCode.INSUFFICIENT_DATA.value,
            "activity": activity,
            "chosen_value": l1_value if l1_value is not None else l0_value,
            "chosen_source": (
                SourceType.SYSTEM_DATA.value if l1_value is not None
                else (l0_source.value if l0_source else "")
            ),
            "evidence_grade": (
                EvidenceGrade.A.value if l1_value is not None else EvidenceGrade.C.value
            ),
            "conflict": False,
            "requires_human": False,
            "divergence": 0.0,
            "note": (
                "只有单路取证，无法交叉互校。"
                "L1 单路可给 A 级；仅 L0 自述则止步 C 级，需补客观痕迹。"
            ),
        }

    adj = adjudicate([
        Claim(source_type=l0_source, value=float(l0_value), origin=l0_origin),
        Claim(source_type=SourceType.SYSTEM_DATA, value=float(l1_value), origin=l1_origin),
    ])
    return {
        "code": ResultCode.OK.value,
        "activity": activity,
        "chosen_value": adj.chosen_value,
        "chosen_source": adj.chosen_source.value,
        "chosen_origin": adj.chosen_origin,
        "evidence_grade": adj.grade.value,
        "conflict": adj.conflict,
        "requires_human": adj.requires_human,
        "divergence": adj.divergence,
        "note": adj.note,
        "considered": adj.considered,
    }
