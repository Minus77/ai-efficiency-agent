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


def test_readme_page_table_matches_actual_nav():
    """README 里的页面清单必须与实际导航条目数一致。

    本会话真实踩到的问题：新增「术语与标准」页之后，README 仍写着「9 个视图」，
    而报告页清单表里漏了「改造效果」——文档和界面对不上，新人照 README 找不到页。
    文档漂移没有任何自动信号，所以在这里钉住条目数。
    """
    readme = (WEB.parent / "README.md").read_text(encoding="utf-8")

    nav_views = re.findall(r'data-view="([a-z]+)"', INDEX_HTML)
    assert len(nav_views) == len(set(nav_views)), "导航里有重复的 data-view"

    # 「报告里有什么」那张表只讲交付物页面；工作台三页（建档/采集/连接器）
    # 在上面的工作流章节单独讲，不该重复列进这张表。
    WORKBENCH = {"clients", "intake", "connectors"}
    report_views = [v for v in nav_views if v not in WORKBENCH]

    # README 的页面清单表：以 "| 0X " 或 "| 附 " / "| 系 " 开头的行
    rows = re.findall(r"^\| (?:\d\d|附|系) [^|]+\|", readme, re.M)
    assert len(rows) == len(report_views), (
        f"README 页面清单 {len(rows)} 行，实际交付物页面 {len(report_views)} 个"
        f"（导航 {len(nav_views)} 个减去工作台 {len(WORKBENCH)} 页）——文档与界面不一致"
    )

    # 别再出现写死的旧数字
    for stale in ("9 个视图", "15 个条目"):
        assert stale not in readme, f"README 仍写着过时的「{stale}」"


# ---------------------------------------------------------------------------
# 色彩对比度：浏览器里逐个视图量过，这里做静态钉子防回归
# ---------------------------------------------------------------------------
def _srgb_luminance(hex_color: str) -> float:
    """WCAG 2.1 相对亮度。"""
    h = hex_color.lstrip("#")
    parts = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    chans = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


def _contrast(fg: str, bg: str) -> float:
    lo, hi = sorted((_srgb_luminance(fg), _srgb_luminance(bg)))
    return (hi + 0.05) / (lo + 0.05)


def _token(name: str) -> str:
    m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", STYLES)
    assert m, f"styles.css 里找不到 --{name}"
    return m.group(1)


def test_secondary_text_token_meets_wcag_aa():
    """次要文字色必须在它实际会落到的所有背景上达到 AA（小字 4.5:1）。

    原值 #8f959e 只有 3.02:1（白底），全站 55 处 `color: var(--n400)` 全部不达标——
    「浅灰=次要信息」的直觉很容易把对比度压到看不清，而这类问题肉眼审查极易放过：
    设计稿上"淡一点"看着更精致，实际用户在阳光下或低质量屏幕上就读不到了。

    背景取三种：白卡、浅灰画布、表头/徽标底。
    """
    fg = _token("n400")
    for bg_name in ("n0", "n50", "n75"):
        r = _contrast(fg, _token(bg_name))
        assert r >= 4.5, (
            f"--n400 ({fg}) 在 --{bg_name} ({_token(bg_name)}) 上只有 {r:.2f}:1，"
            f"低于 WCAG AA 要求的 4.5:1"
        )


def test_body_text_tokens_meet_wcag_aa():
    """正文与强调文字色同样要达标——它们比次要文字更该达标。"""
    for name in ("n500", "n600", "n700"):
        r = _contrast(_token(name), _token("n0"))
        assert r >= 4.5, f"--{name} 在白底上只有 {r:.2f}:1"


def test_semantic_text_colors_meet_wcag_aa():
    """语义色作文字时要用 -fg 变体。

    --danger (#f54a45) 作纯色文字只有 3.53:1。必填星号一度直接用了它——
    星号恰恰是最不能看不清的元素。
    """
    for name in ("success-fg", "warning-fg", "danger-fg"):
        r = _contrast(_token(name), _token("n0"))
        assert r >= 4.5, f"--{name} 在白底上只有 {r:.2f}:1"

    # 必填星号不得直接用高饱和的 --danger
    m = re.search(r"\.form label em\s*\{([^}]*)\}", STYLES)
    assert m, "找不到必填星号的样式"
    assert "var(--danger-fg)" in m.group(1), (
        "必填星号应使用 --danger-fg；--danger 作文字只有 3.53:1"
    )


def test_evidence_grade_colors_meet_wcag_aa():
    """A/B/C 徽标是判断结论强度的核心视觉元素，落在各自的浅色底上也要能读清。"""
    for fg, bg in (("ga", "ga-bg"), ("gb", "gb-bg"), ("gc", "gc-bg")):
        r = _contrast(_token(fg), _token(bg))
        assert r >= 4.5, f"--{fg} 在 --{bg} 上只有 {r:.2f}:1"


# ---------------------------------------------------------------------------
# 不可逆操作的确认：原生 confirm 说不清后果，且默认按钮是「确定」
# ---------------------------------------------------------------------------
def test_no_native_confirm_for_destructive_actions():
    """原生 confirm 不该再出现在代码里。

    它有三个问题：样式不可控、说不清「到底会删掉什么」、
    而且默认按钮是确定——不可逆操作最忌讳顺手回车。
    """
    assert "window.confirm" not in APP_JS, (
        "删除类操作不得用原生 confirm，应走 confirmDanger()"
    )
    assert "confirmDanger" in APP_JS, "缺少应用内的危险操作确认框"


def test_danger_confirm_lists_consequences_and_defaults_to_cancel():
    """确认框必须列出后果，且默认焦点在取消。"""
    m = re.search(r"function confirmDanger\(\{.*?\n\}", APP_JS, re.S)
    assert m, "找不到 confirmDanger 定义"
    body = m.group(0)

    assert "willDelete" in body, "必须逐条列出会被删除的内容"
    # 默认焦点给取消：不可逆操作不该让回车直接生效
    assert re.search(r'getElementById\("dgCancel"\)\.focus\(\)', body), (
        "默认焦点必须在取消按钮上"
    )
    # 三条关闭途径（取消键 / 遮罩 / Esc）都要回一个"取消"
    assert "modalOnClose" in body, (
        "遮罩点击与 Esc 也要 resolve，否则 await 会永远挂着"
    )


def test_delete_client_spells_out_what_is_lost():
    """删客户会连带删掉材料、台账、基线、凭据——必须写清楚，不能只说「确定吗」。"""
    m = re.search(r"confirmDanger\(\{(.*?)\}\);", APP_JS, re.S)
    assert m, "找不到删除客户的确认调用"
    call = m.group(1)
    for kw in ("材料", "台账", "基线", "凭据"):
        assert kw in call, f"删除确认没有说明「{kw}」会被删除"


def test_danger_button_keeps_white_text_readable():
    """危险按钮是实心红底白字，底色必须够深。"""
    m = re.search(r"\.btn-danger\s*\{([^}]*)\}", STYLES)
    assert m, "缺少 .btn-danger 样式"
    assert "var(--danger-fg)" in m.group(1), (
        "实心危险按钮的底色应用 --danger-fg；用 --danger 时白字只有 3.53:1"
    )
    r = _contrast(_token("n0"), _token("danger-fg"))
    assert r >= 4.5, f"危险按钮白字只有 {r:.2f}:1"


# ---------------------------------------------------------------------------
# 换页加载态：既要有反馈，又不能因为反馈太急而闪
# ---------------------------------------------------------------------------
def test_pending_state_is_delayed_not_immediate():
    """立刻显示骨架屏会在缓存命中时闪一下，那种闪动比没有反馈更让人不安。"""
    assert "PENDING_DELAY_MS" in APP_JS, "缺少延迟显示的加载态"
    m = re.search(r"const PENDING_DELAY_MS = (\d+)", APP_JS)
    assert m, "找不到 PENDING_DELAY_MS 取值"
    delay = int(m.group(1))
    assert 150 <= delay <= 500, (
        f"延迟 {delay}ms 不合适：太短会闪，太长会让人以为点击没生效"
    )
    assert re.search(r"function startPending\(\)", APP_JS)
    assert re.search(r"function endPending\(\)", APP_JS)


def test_pending_state_is_announced_to_screen_readers():
    """读屏用户面对一片沉默无法判断是在加载还是卡住了。"""
    m = re.search(r"function startPending\(\).*?\n\}", APP_JS, re.S)
    assert m, "找不到 startPending"
    body = m.group(0)
    assert 'aria-busy' in body, "加载中必须标记 aria-busy"
    assert 'role="status"' in body, "骨架屏要对读屏可见"

    end = re.search(r"function endPending\(\).*?\n\}", APP_JS, re.S)
    assert end and "removeAttribute" in end.group(0), (
        "加载结束必须撤下 aria-busy，否则读屏会一直播报"
    )


def test_stale_response_does_not_overwrite_current_view():
    """慢请求后到会把用户已经离开的页面盖回来——这是异步渲染最典型的竞态。"""
    m = re.search(r"async function render\(name\)\s*\{.*?\n\}", APP_JS, re.S)
    assert m, "找不到 render()"
    body = m.group(0)
    guards = len(re.findall(r"currentView !== name", body))
    assert guards >= 2, (
        f"render() 只有 {guards} 处新旧视图校验；成功与失败两条路径都要有，"
        "否则慢请求或慢失败都会覆盖当前页面"
    )


# ---------------------------------------------------------------------------
# 服务端成文段落的术语自动接入
# ---------------------------------------------------------------------------
def test_autoterm_escapes_before_injecting():
    """先转义再注入。顺序颠倒就等于把客户材料里的内容当 HTML 执行。

    这是安全属性，不只是样式问题：关键假设、裁决规则这些段落里含客户名与
    文件名，都是外部输入。必须先 esc() 得到纯文本，再往里插术语按钮。
    """
    m = re.search(r"function autoTerm\(text\)\s*\{.*?\n\}", APP_JS, re.S)
    assert m, "找不到 autoTerm()"
    body = m.group(0)

    # 起初这条断言写成"esc(text) 的位置早于 .replace() 的位置"，是个装饰品：
    # 把 `const safe = text` 改坏之后，函数里别处仍有 esc(text)，位置检查照样通过。
    # 真正要钉住的是**被替换的那个值本身是转义结果**，所以直接断言这两处绑定。
    assert re.search(r"const safe = esc\(text\);", body), (
        "被替换的值必须是 esc(text) 的结果；绑定成原始 text 就等于把标签当 HTML 执行"
    )
    assert re.search(r"return safe\.replace\(autoTermRe", body), (
        "替换必须作用在已转义的 safe 上，不能作用在原始文本上"
    )


def test_autoterm_prefers_longer_terms():
    """「连续作业」必须先于「作业形态」匹配，否则长词会被短词切碎。"""
    m = re.search(r"function buildAutoTermRe\(\)\s*\{.*?\n\}", APP_JS, re.S)
    assert m, "找不到 buildAutoTermRe()"
    body = m.group(0)
    assert re.search(r"sort\(\(a, b\) => b\.length - a\.length\)", body), (
        "术语必须按长度降序排列，否则短词会先命中、把长词切碎"
    )
    # 单字词误伤概率过高
    assert "length >= 2" in body, "应排除长度 1 的词，避免嵌在别的词里误标"


def test_autoterm_marks_each_term_once_per_paragraph():
    """同一段里同个词标三遍是噪音，反而更难读。"""
    m = re.search(r"function autoTerm\(text\)\s*\{.*?\n\}", APP_JS, re.S)
    body = m.group(0)
    assert "used" in body and "Set" in body, "需要用集合记录已标过的词"


def test_autoterm_degrades_when_glossary_missing():
    """术语表拉不到时必须退回纯文本，不能连报告一起挂掉。"""
    m = re.search(r"function autoTerm\(text\)\s*\{.*?\n\}", APP_JS, re.S)
    body = m.group(0)
    assert "glossary.loaded" in body, "必须检查术语表是否可用"
    assert re.search(r"if \(!glossary\.loaded\) return safe", body), (
        "术语表不可用时应直接返回已转义的纯文本"
    )


def test_server_prose_is_wired_to_autoterm():
    """服务端下发的成文段落必须走 autoTerm，否则里面的术语点不开。

    这些字段是"整段中文"，不是标签或数值，用户最需要在这里查词。
    """
    # 关键假设是逐条 map 出来的，写法是 `autoTerm(a)` 而不是 `autoTerm(d.assumptions…)`，
    # 所以单独按渲染片段匹配，不能跟其他字段套同一个模式。
    # 注意 `${` 里的 `$` 在正则中是行尾锚点，必须转义成 `\$\{`，
    # 否则这条断言永远匹配不上（写错过一次）。
    assert re.search(r"d\.assumptions\.map\(\(a\) => `<li>\$\{autoTerm\(a\)\}", APP_JS), (
        "关键假设未接入 autoTerm，其中的折现/真碎片/补数表等术语点不开"
    )

    for field in (
        "d.admission_probe.explanation",     # 受理探测说明
        "d.render_gate.grey_reason",         # 标灰原因
        "d.adjudication_order",              # 裁决优先级
        "d.conflict_rule",                   # 冲突处理规则
        "r.isolation",                       # 反评审隔离说明
        "r.known_limit",                     # 已知局限
        "g.rule",                            # 缺口规则
    ):
        pat = re.compile(r"autoTerm\(" + re.escape(field))
        assert pat.search(APP_JS), f"{field} 未接入 autoTerm，其中的术语点不开"

    # 反过来：这些字段不该还留着裸 esc()
    for field in ("d.adjudication_order", "d.conflict_rule", "r.known_limit"):
        assert f"esc({field})" not in APP_JS, f"{field} 仍是裸 esc()，术语无法点开"


def test_readme_term_count_matches_glossary():
    """README 里写死的术语数必须与 glossary 实际条数一致。

    本会话踩到过：新增 6 个术语后，README 四处仍写着「38 个术语」。
    写死的数字必然漂移，而文档漂移没有任何自动信号——新人照 README
    对不上界面，就会怀疑是不是自己看错了版本。
    """
    from aiea.glossary import GLOSSARY

    readme = (WEB.parent / "README.md").read_text(encoding="utf-8")
    actual = len(GLOSSARY)

    # README 中所有形如「NN 个术语」「NN 个词」的写法都必须等于实际条数
    claims = re.findall(r"(\d+)\s*个(?:术语|词)", readme)
    assert claims, "README 应至少写明一次术语总数"
    wrong = sorted({c for c in claims if int(c) != actual})
    assert not wrong, (
        f"README 写着 {wrong} 个术语，实际是 {actual} 个——文档与代码不一致"
    )
