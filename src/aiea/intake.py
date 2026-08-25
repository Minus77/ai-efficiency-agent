"""受理探测与材料上传解析（§17.1.1 探测、§12.2 采集、§12.2.1 附件安全）。

两个职责：
1. **探测**：一份样本导出就能判定可达证据级别与交付形态，把"事后发现产出平庸"
   变成"事前约定交付边界"。
2. **解析**：只抽结构信号（列名、行数、时间戳分布），不把明细灌进上下文——
   客户原始数据只在这一层活一次（§8.1）。

安全边界：文件名消毒（防路径穿越）+ 类型白名单 + 禁宏禁公式 + 注入检测。
附件内容一律不可信，解析结果不作为指令执行。
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .evidence import probe_material_reachability
from .guardrails import scan_attachment

MAX_EXCERPT_CHARS = 3000
MAX_TIMESTAMPS_KEPT = 4000

# 时间戳列名的常见写法；同时用值本身做二次确认，避免只靠列名猜
_TS_NAME_HINT = re.compile(
    r"(time|date|_at$|_at[^a-z]|created|updated|edited|modified|响应|时间|日期)", re.IGNORECASE
)
_TS_VALUE = re.compile(
    r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?"
)

# 汇总类表头特征：出现这些词且行数很少 → 只有汇总没有明细
_SUMMARY_HINT = re.compile(r"(总量|合计|平均|总计|占比|汇总|total|avg|average|sum)", re.IGNORECASE)


def safe_filename(name: str) -> str:
    """消毒上传文件名。只保留 basename，去掉路径分隔符与前导点。"""
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = base.strip().lstrip(".")
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)
    base = base[:120]
    return base or "unnamed"


def _parse_ts(value: str) -> datetime | None:
    raw = (value or "").strip().replace("/", "-")
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return None


@dataclass
class ParsedMaterial:
    """解析结果：只含结构信号，不含明细。"""

    kind: str  # csv | text | binary
    filename: str
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    timestamp_columns: list[str] = field(default_factory=list)
    timestamps: dict[str, list[str]] = field(default_factory=dict)
    numeric_columns: list[str] = field(default_factory=list)
    categorical_samples: dict[str, list[str]] = field(default_factory=dict)
    # 精确计数：按取值分摊记录数会让频次系统性失真，因此必须全表计数
    categorical_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    # 每个分类取值对应的时间戳，用于按取值分别判定作业形态
    timestamps_by: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    excerpt: str = ""
    summary_only: bool = False
    injection_suspected: bool = False
    note: str = ""

    def to_meta(self) -> dict[str, Any]:
        """给编排层与 API 用的紧凑摘要（不含时间戳明细）。"""
        return {
            "kind": self.kind,
            "filename": self.filename,
            "columns": self.columns,
            "row_count": self.row_count,
            "timestamp_columns": self.timestamp_columns,
            "numeric_columns": self.numeric_columns,
            "summary_only": self.summary_only,
            "injection_suspected": self.injection_suspected,
            "note": self.note,
        }


def _decode(content: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def parse_bytes(content: bytes, *, filename: str) -> ParsedMaterial:
    """解析上传内容。CSV/TSV 抽结构，其余按文本抽摘要。"""
    fname = safe_filename(filename)
    suffix = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
    text = _decode(content)
    scan = scan_attachment(filename=fname, content=text[:20000], size_bytes=len(content))

    if suffix not in ("csv", "tsv"):
        return ParsedMaterial(
            kind="text",
            filename=fname,
            excerpt=text[:MAX_EXCERPT_CHARS],
            injection_suspected=scan.injection_suspected,
            note="文本类材料：仅用于定位痛点，不得用于量化（纪要记录决策而非操作机制）",
        )

    delimiter = "\t" if suffix == "tsv" else ","
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        columns = list(reader.fieldnames or [])
    except Exception:
        return ParsedMaterial(
            kind="text", filename=fname, excerpt=text[:MAX_EXCERPT_CHARS],
            injection_suspected=scan.injection_suspected,
            note="按 CSV 解析失败，已降级为文本处理；解析失败不猜测内容",
        )

    if not columns:
        return ParsedMaterial(
            kind="text", filename=fname, excerpt=text[:MAX_EXCERPT_CHARS],
            injection_suspected=scan.injection_suspected, note="未识别到表头",
        )

    ts_cols: list[str] = []
    ts_values: dict[str, list[str]] = {}
    numeric_cols: list[str] = []
    cat_samples: dict[str, list[str]] = {}

    sample = rows[: min(len(rows), 200)]
    for col in columns:
        vals = [(r.get(col) or "").strip() for r in sample]
        nonempty = [v for v in vals if v]
        if not nonempty:
            continue
        # 时间戳：值本身能解析才算，列名只作加分项
        parsed_ok = sum(1 for v in nonempty[:60] if _TS_VALUE.match(v))
        if parsed_ok >= max(3, len(nonempty[:60]) * 0.6):
            ts_cols.append(col)
            keep = [(r.get(col) or "").strip() for r in rows if (r.get(col) or "").strip()]
            ts_values[col] = keep[:MAX_TIMESTAMPS_KEPT]
            continue
        numeric_ok = sum(1 for v in nonempty[:60] if re.fullmatch(r"-?[\d,]+(\.\d+)?", v))
        if numeric_ok >= max(3, len(nonempty[:60]) * 0.8):
            numeric_cols.append(col)
            continue
        uniq = list(dict.fromkeys(nonempty))
        if len(uniq) <= 24:
            cat_samples[col] = uniq[:12]

    # 分类列的精确计数 + 按取值分组时间戳（只对基数合理的列做，避免 O(列 x 行) 爆炸）
    cat_counts: dict[str, dict[str, int]] = {}
    ts_by: dict[str, dict[str, list[str]]] = {}
    primary_ts = ts_cols[0] if ts_cols else ""
    for col in cat_samples:
        counter: dict[str, int] = {}
        grouped: dict[str, list[str]] = {}
        for r in rows:
            val = (r.get(col) or "").strip()
            if not val:
                continue
            counter[val] = counter.get(val, 0) + 1
            if primary_ts:
                tv = (r.get(primary_ts) or "").strip()
                if tv:
                    grouped.setdefault(val, []).append(tv)
        if counter:
            cat_counts[col] = counter
            if grouped:
                ts_by[col] = grouped

    # 汇总判定：表头含汇总词且行数很少 → 拿不到明细
    header_blob = " ".join(columns)
    summary_only = bool(_SUMMARY_HINT.search(header_blob)) and len(rows) <= 24 and not ts_cols

    return ParsedMaterial(
        kind="csv",
        filename=fname,
        columns=columns,
        row_count=len(rows),
        timestamp_columns=ts_cols,
        timestamps=ts_values,
        numeric_columns=numeric_cols,
        categorical_samples=cat_samples,
        categorical_counts=cat_counts,
        timestamps_by=ts_by,
        excerpt=text[:600],
        summary_only=summary_only,
        injection_suspected=scan.injection_suspected,
        note=(
            f"识别到 {len(rows)} 条记录、{len(columns)} 个字段"
            + (f"，其中时间戳列：{'、'.join(ts_cols)}" if ts_cols else "，未发现时间戳列")
        ),
    )


def probe_bytes(content: bytes, *, filename: str) -> dict[str, Any]:
    """受理前探测：判定可达证据级别与交付形态（§17.1.1）。"""
    fname = safe_filename(filename)
    text_head = _decode(content)[:20000]
    scan = scan_attachment(filename=fname, content=text_head, size_bytes=len(content))
    if not scan.allow_as_data:
        return {
            "accepted": False,
            "filename": fname,
            "reason": scan.note,
            "reachable_grade": "",
            "delivery_form": "",
            "injection_suspected": scan.injection_suspected,
            "treated_as_instruction": False,
            "row_count": 0,
            "columns": [],
            "timestamp_columns": [],
        }

    parsed = parse_bytes(content, filename=fname)
    has_records = parsed.kind == "csv" and parsed.row_count > 0 and not parsed.summary_only
    has_ts = bool(parsed.timestamp_columns)
    structured = parsed.kind == "csv" and bool(parsed.columns)

    verdict = probe_material_reachability(
        has_records=has_records, has_timestamps=has_ts, structured=structured
    )

    return {
        "accepted": verdict.accepted,
        "filename": fname,
        "kind": parsed.kind,
        "reachable_grade": verdict.grade.value,
        "delivery_form": verdict.delivery_form.value,
        "reason": verdict.note,
        "row_count": parsed.row_count,
        "columns": parsed.columns,
        "timestamp_columns": parsed.timestamp_columns,
        "numeric_columns": parsed.numeric_columns,
        "summary_only": parsed.summary_only,
        "injection_suspected": parsed.injection_suspected,
        # 附件内容一律不作为指令执行（§12.2.1）
        "treated_as_instruction": False,
        "parse_note": parsed.note,
    }


# 材料在取证体系中的角色（§3）
EVIDENCE_ROLES = {
    "R1": "时间戳导出（A 级主力：频次从记录条数直接数，耗时从时间戳间隔推算）",
    "R2": "补数表（B 级兜底：纯数字填空）",
    "R3": "多方交叉材料（B 级：需至少一路有客观痕迹）",
    "R4": "工时记录（A 级：客户配合记录 3–5 个工作日）",
    "R5": "纪要类文档（C 级：仅用于定位痛点，不得用于量化）",
}


def save_material(
    *, root: Path | str, slug: str, filename: str, content: bytes, evidence_role: str = "R1"
) -> dict[str, Any]:
    """落盘一份材料并登记元数据。不通过安全检查的一律不落盘。"""
    from .clients import safe_slug

    checked = safe_slug(slug)
    if checked is None:
        return {"accepted": False, "reason": "客户标识不合法", "filename": safe_filename(filename)}

    base = Path(root) / checked
    if not (base / "client.json").exists():
        return {"accepted": False, "reason": "客户不存在", "filename": safe_filename(filename)}

    probe = probe_bytes(content, filename=filename)
    if not probe["accepted"] and probe.get("kind") != "text":
        # 拒收的文件不落盘（含类型白名单外、含宏、公式注入）
        if not probe.get("columns") and probe.get("row_count", 0) == 0:
            return {**probe, "stored_as": None}

    materials = base / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    stored = probe["filename"]
    target = materials / stored
    n = 2
    while target.exists():
        stem, _, ext = stored.rpartition(".")
        stored = f"{stem}-{n}.{ext}" if ext else f"{stored}-{n}"
        target = materials / stored
        n += 1
    target.write_bytes(content)

    record = {
        **probe,
        "stored_as": stored,
        "evidence_role": evidence_role,
        "evidence_role_desc": EVIDENCE_ROLES.get(evidence_role, ""),
        "size_bytes": len(content),
    }

    meta_path = base / "materials.json"
    existing = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else []
    existing.append(record)
    meta_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def list_materials(*, root: Path | str, slug: str) -> list[dict[str, Any]]:
    from .clients import safe_slug

    checked = safe_slug(slug)
    if checked is None:
        return []
    meta = Path(root) / checked / "materials.json"
    if not meta.exists():
        return []
    return json.loads(meta.read_text(encoding="utf-8"))
