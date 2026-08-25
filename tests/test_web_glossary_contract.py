"""前端与术语表的契约：界面用到的每个术语键必须在后端术语表中存在。

为什么要有这个测试：术语解释是"两处各写一份"的典型场景——
app.js 里写 term("折现")，glossary.py 里定义"折现"。
一旦有人改了键名或删了词条，界面上那个词就会静默退化成纯文本，
不再可点、不再有解释，而且没有任何报错。这种退化只有真人点开才会发现。

这里用静态扫描把两边锁死：拼错的键、未登记的词，测试直接红。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from aiea.glossary import GLOSSARY, INTERNAL_ONLY

WEB = Path(__file__).resolve().parents[1] / "web"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
STYLES = (WEB / "styles.css").read_text(encoding="utf-8")

# term("键") / term("键", "显示名")
TERM_CALLS = re.findall(r"""\bterm\(\s*["']([^"']+)["']""", APP_JS)


def test_frontend_actually_uses_terms():
    """光有术语表不算完——前端必须真的把它用起来。"""
    assert len(TERM_CALLS) >= 20, (
        f"前端只用了 {len(TERM_CALLS)} 处术语解释，术语表形同虚设"
    )


@pytest.mark.parametrize("key", sorted(set(TERM_CALLS)))
def test_every_term_call_resolves(key: str):
    assert key in GLOSSARY, f"app.js 里 term(\"{key}\") 在术语表中查不到"


def test_internal_terms_never_reach_the_ui():
    """内部实现词不上界面。

    只查"会被用户读到的文案"，不查代码标识符：
    `m.insufficient_data_count` 是读服务端字段名，用户看不到；
    而 `<p>insufficient_data</p>` 就是泄漏。二者必须区分开，
    否则测试会逼着代码把正常的字段名改成拼音，反而更糟。
    """
    visible = APP_JS
    visible = re.sub(r"data-[a-z-]+=\"[^\"]*\"", "", visible)   # data-* 属性
    visible = re.sub(r"\.[A-Za-z_$][\w$]*", "", visible)         # 属性访问 m.xxx
    visible = re.sub(r"^\s*//.*$", "", visible, flags=re.M)      # 行注释
    for bad in ("insufficient_data", "no_grounding", "taskcard_upsert", "metric_probe", "rubric"):
        assert bad not in visible, f"内部术语 {bad} 出现在界面文案里"


def test_grade_badges_are_explainable():
    """A/B/C 徽标必须能点开看判定标准，否则"B 级"对用户是天书。"""
    assert "data-scale=\"grade\"" in APP_JS
    assert "gradeBadge(" in APP_JS


def test_glossary_reference_page_exists():
    """要有一个集中查词的地方，不能只靠散落的浮层。"""
    assert "viewGlossary" in APP_JS, "缺少术语与标准参考页"
    assert 'data-view="glossary"' in INDEX_HTML, "侧栏缺少术语页入口"


def test_term_control_is_keyboard_reachable():
    """术语用 <button> 而非 <span>：键盘可 Tab、读屏可识别。"""
    m = re.search(r"function term\(key, display\)\s*\{.*?\n\}", APP_JS, re.S)
    assert m, "找不到 term() 定义"
    body = m.group(0)
    assert "<button" in body, "术语控件必须是 button，span 无法键盘聚焦"
    assert "aria-label" in body or "aria-describedby" in body


def test_difficulty_values_are_explained_not_bare():
    """难度是个 1-5 的加权分。裸着显示"2.02"没人看得懂，必须带标尺说明。"""
    assert "data-scale=\"difficulty\"" in APP_JS, "难度值缺少可点开的分级标准"


def test_scale_popover_has_styles():
    """有交互就得有样式，否则浮层会变成裸文本堆在页面上。"""
    for cls in (".term", ".pop", ".scale-tbl"):
        assert cls in STYLES, f"styles.css 缺少 {cls} 的样式"


def test_hidden_attribute_is_globally_enforced():
    """[hidden] 必须被全局强制 display:none。

    这个坑本项目踩过三次：隐形遮罩吞掉全页点击、连接器空组留下空标题、
    首访提示条点了"知道了"关不掉。根因都一样——元素上任何 display 声明
    都会盖掉 hidden 属性自带的 display:none。逐个元素补 `.x[hidden]`
    是打地鼠，所以在全局兜一次，并用这个测试钉住它别被"整理样式"时删掉。
    """
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", STYLES), (
        "styles.css 缺少全局 [hidden] { display: none !important }"
    )
