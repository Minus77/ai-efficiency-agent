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


# ---------------------------------------------------------------------------
# 无障碍与交互：都在浏览器里实测通过，这里做静态钉子防回归
# ---------------------------------------------------------------------------
def test_toast_is_a_live_region():
    """toast 是操作结果的唯一反馈。不声明 live region，读屏用户收不到任何结果。"""
    m = re.search(r'<div class="toast"[^>]*>', INDEX_HTML)
    assert m, "找不到 toast 元素"
    tag = m.group(0)
    assert 'role="status"' in tag, "toast 缺少 role=status"
    assert 'aria-live="polite"' in tag, (
        "toast 缺少 aria-live。用 polite 而非 assertive：这些是完成通知，不该打断阅读"
    )


def test_skip_to_content_link_exists():
    """左侧导航 15 个条目，键盘用户每换一页都要 Tab 穿过全部条目才能到正文。"""
    assert 'class="skip"' in INDEX_HTML, "缺少跳到主内容的链接"
    assert 'href="#stage"' in INDEX_HTML, "跳转链接必须指向主内容区"
    # 不能用 display:none —— 那样键盘也聚焦不到，等于没做
    m = re.search(r"\.skip\s*\{[^}]*\}", STYLES, re.S)
    assert m, "styles.css 缺少 .skip 样式"
    assert "display: none" not in m.group(0), (
        "跳转链接不能用 display:none 隐藏，否则键盘无法聚焦"
    )
    assert ".skip:focus" in STYLES, "跳转链接必须在聚焦时可见"


def test_wide_tables_have_sticky_headers():
    """ROI 与证据台账都是 8 列宽表，往下翻两屏就忘了这一列是什么。

    sticky 只相对最近的滚动祖先生效。`.tbl-scroll` 因 overflow 已成为滚动容器，
    所以它必须有高度上限，否则 top:0 永远不触发——这一条最容易写错还看不出来。
    """
    assert re.search(r"thead th\s*\{[^}]*position:\s*sticky", STYLES, re.S), "表头未吸顶"
    m = re.search(r"\.tbl-scroll\s*\{[^}]*\}", STYLES, re.S)
    assert m and "max-height" in m.group(0), (
        ".tbl-scroll 需要 max-height，否则它自己是滚动容器、sticky 不会触发"
    )
    # collapse 下 sticky 表头的边框会丢，因此必须改用 separate + 手动画线
    assert re.search(r"table\s*\{[^}]*border-collapse:\s*separate", STYLES, re.S)
    assert re.search(r"thead th\s*\{[^}]*box-shadow", STYLES, re.S), (
        "separate 模式下表头下边线要用 box-shadow 画，否则边框会丢"
    )


def test_modal_manages_focus():
    """模态对话框的焦点跑到背后页面上，就不再是"模态"了。"""
    assert "function openModal" in APP_JS, "缺少统一的 openModal（焦点需要在打开前记录）"
    assert "modalReturnFocus" in APP_JS, "关闭弹窗后必须把焦点还回触发按钮"
    # 焦点圈定
    assert re.search(r'e\.key !== "Tab"', APP_JS), "缺少 Tab 焦点圈定"
    assert "shiftKey" in APP_JS, "焦点圈定必须同时处理 Shift+Tab 反向循环"
    # 不允许再有绕过 openModal 的裸开法
    bare = re.findall(r"modal\.hidden = false", APP_JS)
    assert len(bare) == 1, (
        f"发现 {len(bare)} 处 modal.hidden = false；除 openModal 内部外都应改走 openModal，"
        "否则那些入口不会记录返回焦点"
    )


def test_reduced_motion_is_respected_globally():
    """逐个列举动画名漏得快（本轮新增的浮层就漏过），所以全局关。

    注意别把断言写成 `"*" in body`——`.skeleton > *` 里也有一个星号，
    那样即使退回逐个列举，测试依然会绿。必须匹配真正的通用选择器。
    """
    m = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", STYLES, re.S)
    assert m, "缺少 prefers-reduced-motion 处理"
    body = m.group(1)
    assert re.search(r"^\s*\*\s*,", body, re.M) or re.search(r"^\s*\*\s*\{", body, re.M), (
        "应以通用选择器全局关闭动画，而不是逐个列举 .view / .skeleton 这类具体类名"
    )
    assert "animation-duration" in body and "transition-duration" in body


def test_nav_marks_current_page_for_screen_readers():
    """只靠 class 变色，读屏用户不知道自己在哪一页。"""
    assert 'aria-current", "page"' in APP_JS or "aria-current=\"page\"" in APP_JS, (
        "导航缺少 aria-current"
    )


def test_interactive_controls_have_visible_focus():
    """键盘用户看不见焦点在哪，等于不能用键盘。"""
    for cls in (".term:focus-visible", ".bdg-btn:focus-visible", ".numref:focus-visible"):
        assert cls in STYLES, f"{cls} 缺少可见焦点样式"


def test_narrow_screen_offset_targets_the_element_that_owns_it():
    """窄屏下缩进的必须是真正持有左偏移的那个元素。

    这是本会话真实踩到的回归：为了让首访提示条与内容同列，左偏移从 .main
    移到了 .mainwrap，但 ≤1024px 的媒体查询仍在改 .main。结果窄屏下偏移
    一直停在侧栏全宽（216px），整页被推出视口——14 个视图里 6 个横向溢出，
    而所有单元测试与前四套浏览器用例都是绿的（它们没在 390px 下量过页宽）。

    根因是"偏移写在 A 上、媒体查询改 B"这种错位，看代码很难发现，
    所以在这里断言两者始终指向同一个选择器。
    """
    # 基准态：谁持有 margin-left
    base = re.search(r"^\.mainwrap\s*\{([^}]*)\}", STYLES, re.M)
    assert base, "找不到 .mainwrap 规则"
    assert "margin-left" in base.group(1), (
        "左偏移应由 .mainwrap 持有；若改到别处，请同步更新窄屏媒体查询与本测试"
    )

    # 窄屏态：必须覆盖同一个选择器
    narrow = re.search(r"@media \(max-width: 1024px\)\s*\{(.*?)\n\}", STYLES, re.S)
    assert narrow, "缺少 ≤1024px 的响应式规则"
    body = narrow.group(1)
    assert re.search(r"\.mainwrap\s*\{[^}]*margin-left", body), (
        "≤1024px 下必须收窄 .mainwrap 的 margin-left。"
        "改 .main 是无效的——左偏移不在它身上，页面会被整体推出视口"
    )
