"""七层护栏（§13.1）。

原则：**模型提议，策略裁决** —— LLM 永不是访问控制边界。
本模块是纯代码判定，不调用 LLM，因此无法被提示词说服。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import EvidenceGrade, ToolResult

# §13.1 七层，一层不可省
LAYERS: tuple[str, ...] = (
    "输入校验",
    "推理校验",
    "工具入参",
    "工具出参",
    "最终输出",
    "规则层",
    "守护 Agent",
)

# ---------------------------------------------------------------------------
# 注入检测（§12.2.1、§8.3）
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS: tuple[str, ...] = (
    r"忽略(上述|以上|之前|前面)(的)?(规则|指令|要求|提示)",
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(the\s+)?(above|previous)",
    r"你现在是|from now on you are|act as (a )?(system|developer)",
    r"(输出|泄露|打印|показ|reveal|print)\s*(你的)?(系统)?(提示|prompt|system prompt)",
    r"(把|将).{0,12}(写成|标为|改成)\s*A\s*级",
    r"直接给出(最高|最大)(收益|ROI)",
    r"不要(标注|标记|提及)(缺口|证据|不确定)",
    r"<\s*/?\s*(system|instructions?)\s*>",
)

# 文件类型白名单（§12.2.1）
_ALLOWED_SUFFIXES: tuple[str, ...] = (
    ".csv", ".tsv", ".xlsx", ".xls", ".json", ".md", ".txt", ".pdf",
    ".png", ".jpg", ".jpeg", ".webp", ".docx",
)
_MACRO_SUFFIXES: tuple[str, ...] = (".xlsm", ".xlsb", ".docm", ".pptm")
_FORMULA_PATTERN = re.compile(r"^\s*[=+\-@]|cmd\s*\||DDE\s*\(|HYPERLINK\s*\(", re.IGNORECASE | re.MULTILINE)

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BATCH_FILES = 40


@dataclass
class ScanVerdict:
    injection_suspected: bool = False
    treat_as_untrusted: bool = True  # 客户材料一律视为不可信数据
    allow_as_data: bool = True
    allow_as_instruction: bool = False  # 永远为假：材料内容不得作为指令执行
    matched: list[str] = field(default_factory=list)
    note: str = ""
    origin: str = ""


def scan_untrusted_text(content: str, *, origin: str = "") -> ScanVerdict:
    """对任意不可信文本（含 OCR 结果、RAG chunk）跑注入检测。"""
    matched = [p for p in _INJECTION_PATTERNS if re.search(p, content, re.IGNORECASE)]
    suspected = bool(matched)
    note = (
        "检出指令样式文本，已降级为纯数据并记录事件；其中指令不执行，且不得触发任何记忆写入。"
        if suspected
        else "未检出注入特征；内容仍按不可信数据处理。"
    )
    return ScanVerdict(
        injection_suspected=suspected,
        treat_as_untrusted=True,
        allow_as_data=True,
        allow_as_instruction=False,
        matched=matched,
        note=note,
        origin=origin,
    )


def scan_attachment(*, filename: str, content: str, size_bytes: int | None = None) -> ScanVerdict:
    """附件安全边界：类型白名单 + 禁宏禁公式 + 注入检测（§12.2.1、G5）。"""
    lower = filename.lower()
    suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""

    if suffix in _MACRO_SUFFIXES:
        return ScanVerdict(
            allow_as_data=False,
            note="含宏的文件类型被拒：禁止执行宏与公式，请另存为 .xlsx 或 .csv 后重传。",
            origin=filename,
        )
    if suffix not in _ALLOWED_SUFFIXES:
        return ScanVerdict(
            allow_as_data=False,
            note=f"文件类型 {suffix or '未知'} 不在白名单内，已拒收；可接受 CSV/Excel/PDF/图片/文档。",
            origin=filename,
        )
    if size_bytes is not None and size_bytes > MAX_FILE_BYTES:
        return ScanVerdict(
            allow_as_data=False,
            note=f"单文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 上限，已拒收。",
            origin=filename,
        )
    if _FORMULA_PATTERN.search(content or ""):
        return ScanVerdict(
            allow_as_data=False,
            note="检出公式/DDE 样式内容：Excel 公式仅取值不求值，该文件已拒解析，请导出为纯值 CSV。",
            origin=filename,
        )

    verdict = scan_untrusted_text(content or "", origin=filename)
    return verdict


# ---------------------------------------------------------------------------
# PII 脱敏（§10.4、§13.3）
# ---------------------------------------------------------------------------
_PII_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "***@***"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "****身份证已脱敏****"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "****卡号已脱敏****"),
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "sk-***已脱敏***"),
)


def redact_pii(text: str) -> str:
    """日志与台账写入前的脱敏。密钥永不进上下文（§13.3）。"""
    out = text
    for pattern, repl in _PII_RULES:
        out = pattern.sub(repl, out)
    return out


# ---------------------------------------------------------------------------
# tenant 与只读边界（§4、§13.3）
# ---------------------------------------------------------------------------
def tenant_filter(*, requested_tenant: str, session_tenant: str) -> ToolResult:
    """每次检索强制带 tenant 过滤，无例外。"""
    if requested_tenant != session_tenant:
        return ToolResult.denied(
            f"检索目标 tenant={requested_tenant} 越出当前会话 tenant={session_tenant}",
            next_action="跨客户访问被硬隔离拦截；如需对照请使用公开基准库（L-公开）",
        )
    return ToolResult.success({"tenant": session_tenant}, note="tenant 校验通过")


_WRITE_OPS = {"write", "update", "insert", "delete", "patch", "post"}


def assert_readonly(*, operation: str, target: str) -> ToolResult:
    """只读断言：客户系统一律只读，写操作只允许落本地工作区。"""
    op = operation.lower()
    if op not in _WRITE_OPS:
        return ToolResult.success({"operation": op, "target": target}, note="只读操作，允许")
    if target.startswith("workspace/") or target.startswith("./workspace/"):
        return ToolResult.success({"operation": op, "target": target}, note="写入本地工作区，允许")
    return ToolResult.denied(
        f"拒绝对 {target} 执行 {op}：全部连接器只读，无一例外",
        next_action="诊断产出是报告，无任何业务理由写客户系统；请改为落盘 workspace/",
    )


# ---------------------------------------------------------------------------
# 阶段跃迁校验（§13.1 推理校验层）
# ---------------------------------------------------------------------------
def check_stage_transition(*, target_stage: str, evidence_grade: EvidenceGrade, quantifiable: bool) -> ToolResult:
    """证据不足不许进 S4 ROI 估算。"""
    if target_stage.upper().startswith("S4"):
        if not quantifiable:
            return ToolResult.denied(
                "该场景为真碎片或不可量化，禁止进入 S4 ROI 估算",
                next_action="转为定性描述，并在报告说明若为批量作业收益可上调",
            )
        if evidence_grade is EvidenceGrade.C:
            return ToolResult.denied(
                "C 级证据禁止进入 S4 ROI 估算",
                next_action="先补 R1 时间戳导出或 R2 补数表；确无材料则该场景仅给方向性判断",
            )
    return ToolResult.success({"stage": target_stage}, note="阶段跃迁校验通过")


# ---------------------------------------------------------------------------
# 守护 Agent（§13.1 第七层、§15.3 措辞纪律）
# ---------------------------------------------------------------------------
_PROMISSORY = ("将节省", "将省", "必将", "保证", "一定能", "承诺", "确保实现", "应当实施")
_MONEY = re.compile(r"(¥|￥|\$)\s*[\d,]+(\.\d+)?|[\d,]{2,}\s*(元|万元|万)")
_QUANT = re.compile(r"\d+(\.\d+)?\s*(%|小时|分钟|个月|天|次|条|万|元)")
_PAYBACK = re.compile(r"\d+(\.\d+)?\s*个月\s*回本|回本周期")


@dataclass
class GuardianVerdict:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    action: str = ""


def guardian_review(*, statement: str, evidence_grade: EvidenceGrade, has_citation: bool) -> GuardianVerdict:
    """独立观察者：对"结论超出证据强度"有否决权。"""
    reasons: list[str] = []

    has_money = bool(_MONEY.search(statement))
    has_quant = bool(_QUANT.search(statement))

    if evidence_grade is EvidenceGrade.C and (has_money or _PAYBACK.search(statement)):
        reasons.append("C 级证据不得出现金额或回本周期，结论强度超出证据强度")
    if any(word in statement for word in _PROMISSORY):
        reasons.append("使用了承诺性动词，违反措辞纪律：应为『预计可省』『建议评估』")
    if (has_money or has_quant) and not has_citation:
        reasons.append("量化声明未回指证据台账，最终输出层拦截")

    if reasons:
        return GuardianVerdict(
            approved=False,
            reasons=reasons,
            action="该句已标灰，不入正文；请补证据引用或改写为方向性表述",
        )
    return GuardianVerdict(approved=True, reasons=[], action="通过")
