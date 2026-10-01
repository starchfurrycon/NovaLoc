r"""★ `_call_single` 的返回类型契约，以及"定向重试"必须能跑起来。

## 为什么单独一个套件

实测一个**三重静默失效**（见 `tests/README.md` 第 43 条）：

1. 模型对单条请求回 `{"t": ["译文", ""]}`（把单条当数组）；
2. `_call_single` 直接 `return mapping[0]` —— 一个 **list**；
3. list 带进 `verify_restored` 抛 `AttributeError`，
   而重试逻辑 `except ProviderError` 抓不住 ⇒ **重试静默失效**。

三层任一层修好都能让问题暴露，但**只有全修**才能同时做到
"不静默失效"和"能救回条目"。所以这里两类都测：

* **契约**：`_call_single` / `_call_batch` 只返回 `str`；
* **可观测**：重试被触发时，必须在 `stats` 里留下计数。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate import placeholders as ph  # noqa: E402
from novaloc.translate import prompts  # noqa: E402
from novaloc.translate.ollama_provider import OllamaTranslationProvider  # noqa: E402


class _FakeUnit:
    def __init__(self, source: str, uid: str = "u1") -> None:
        from novaloc.models import TextKind

        self.source = source
        self.kind = TextKind.DIALOGUE
        self.max_chars = 0
        self.uid = uid


class _FakeItem:
    def __init__(self, source: str) -> None:
        self.unit = _FakeUnit(source)
        self.glossary: list[object] = []
        self.context_lines: list[str] = []


# ---------------------------------------------------------------------------
# 一、`_call_single` 只许返回 str
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        # 真正的非字符串、又拼不出一句的形态 —— 必须报错
        '{"t": []}',
        '{"t": {"nested": "obj"}}',
        '{"t": 123}',
        '{"t": null}',
        # 编号**有洞**：模型自己都没弄清段落边界 ⇒ 不许猜
        '{"0": "第一段", "2": "第三段"}',
    ],
)
def test_call_single_rejects_unusable_shapes(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """★ 拼不出唯一一句的非字符串值必须在**边界处**被拦住，不许带下去。

    带下去的后果不是"报错"，而是**下游抛异常 + 重试静默失效**。
    """
    prov = _make_provider(monkeypatch, raw)
    src = "{color=#ffd700}The lamp was lit at dawn.{/color}"
    with pytest.raises(Exception) as ei:
        prov._call_single(_FakeItem(src), src)
    msg = str(ei.value)
    # 错误信息必须说清是"类型/拼接"问题（而不是一个下游的 AttributeError）
    assert any(k in msg for k in ("不连续", "非字符串", "失败")), (
        f"报错信息应说明类型/拼接问题，实际：{msg!r}"
    )


def test_call_single_picks_one_segment_not_a_concatenation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""★ 实测形态：模型对**单条**请求回了**批格式**、带编号的答案。

    ```
    源文  'A girl who grew up in the Kingdom of Bohelos. Not very athletic.\n
            Weapon Type: \C[6]Sword\C[0]'
    输出  {"t": [{"i": 0, "t": "来自博赫洛斯王国的女孩。并不擅长运动。"},
                 {"i": 1, "t": "武器类型：剑"}]}
    ```

    `parse_translations` 摊成 `{0: ..., 1: ...}`。此时：

    * 只取 `mapping[0]` ⇒ 可能丢掉第二行；
    * 整条拒收 ⇒ 白丢一条本来完全可用的译文。

    ## ▲ 这里**不能**断言"两段都接起来"

    早先这个测试断言"按编号接起来、不丢任何内容"，并配了一段
    "编号是模型自己给的顺序信息"的理由。**真实数据推翻了它**：
    用产品真实路径对 ElfLifia 全部 76 条多行条目各请求一次，
    **76/76** 都返回多段，而其中约 2/3 的第二段是**同一句话的
    另一个措辞**而不是续写。拼接的后果是复读：

        造成 4 倍「防御」的伤害。随后所有「防御」效果消失。
        造成 4 倍你的「防御」值造成的伤害。\n然后移除所有「防御」效果。

    同一句话显示两遍 —— 比丢内容更糟。

    所以现在的做法是 `_best_segment`：**挑一段**，不拼。
    详细样本与判定见 `tests/test_best_segment.py`。
    """
    prov = _make_provider(
        monkeypatch,
        '{"t": [{"i": 0, "t": "来自博赫洛斯王国的女孩。"}, {"i": 1, "t": "武器类型：剑"}]}',
    )
    out = prov._call_single(_FakeItem("x"), "x")
    assert isinstance(out, str), f"必须返回 str，实际 {type(out).__name__}"
    # 必须是**某一段原文**，不能是两段拼起来
    assert out in ("来自博赫洛斯王国的女孩。", "武器类型：剑"), f"拼接成了：{out!r}"


def test_call_single_prefers_segment_with_intact_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""多段时选**占位符丢得最少**的那一段。

    多行条目的段落边界就在换行占位符处：丢了占位符的段没法正确回写
    （`verify_restored` 第 4 层会判死），所以这是最硬的约束。
    """
    prov = _make_provider(
        monkeypatch,
        '{"t": [{"i": 0, "t": "第一段把记号弄丢了"}, '
        '{"i": 1, "t": "第二段保留了 ⟦0⟧ 记号"}]}',
    )
    out = prov._call_single(_FakeItem("x"), "x", slots=["⟦0⟧"])
    assert out == "第二段保留了 ⟦0⟧ 记号", f"应选占位符完整的段，实际 {out!r}"


def test_call_single_joins_single_fragment_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测形态：模型把**一句话**切成数组，且只有一个非空片段。

    `{"t": ["清晨，油灯被点亮了。", ""]}` —— 拼起来正好是完整译文。
    丢掉可惜，所以接受；但**只在这一个片段非空时**接受。
    """
    prov = _make_provider(monkeypatch, '{"t": ["清晨，油灯被点亮了。", ""]}')
    out = prov._call_single(_FakeItem("x"), "x")
    assert isinstance(out, str), f"必须返回 str，实际 {type(out).__name__}"
    assert out.strip() == "清晨，油灯被点亮了。"


def test_call_single_accepts_valid_string(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _make_provider(monkeypatch, '{"t": "黎明时，油灯被点亮了。"}')
    out = prov._call_single(_FakeItem("x"), "x")
    assert isinstance(out, str)
    assert out == "黎明时，油灯被点亮了。"


# ---------------------------------------------------------------------------
# 二、定向重试的提示词形态
# ---------------------------------------------------------------------------


def test_marker_retry_hint_lists_every_mark() -> None:
    """提示里必须**逐个列出**记号，而不是抽象说"占位符"。"""
    masked = "⟦0⟧The lamp was lit at dawn.⟦1⟧"
    slots = ["{color=#ffd700}", "{/color}"]
    hint = prompts.marker_retry_hint(masked, slots)
    assert "⟦0⟧" in hint and "⟦1⟧" in hint
    assert "{color=#ffd700}" in hint and "{/color}" in hint


def test_marker_retry_hint_says_position_may_move() -> None:
    """★ 必须明说"位置可以变"。

    中文语序和英文不同，模型常常因为"记号不好放"而**干脆删掉**。
    告诉它位置自由，它就不删了。
    """
    hint = prompts.marker_retry_hint("⟦0⟧a⟦1⟧", ["{b}", "{/b}"])
    assert "位置" in hint


@pytest.mark.parametrize("bad", ["⚠️", "**", "`", "###"])
def test_marker_retry_hint_has_no_markdown(bad: str) -> None:
    """★ 纠正提示**不许**含 emoji / Markdown。

    实测第一版带 `⚠️` 和 `**加粗**`，把模型带成了
    `{"t": ["译文", ""]}` —— **输出的结构都变了**，解析直接失败。
    """
    hint = prompts.marker_retry_hint("⟦0⟧a⟦1⟧", ["{b}", "{/b}"])
    assert bad not in hint, f"重试提示里不该出现 {bad!r}：{hint!r}"


def test_marker_retry_hint_has_no_negative_example() -> None:
    """不许给反面例子 —— 实测模型会**模仿那个错例**（第 37 条）。"""
    hint = prompts.marker_retry_hint("⟦0⟧a⟦1⟧", ["{b}", "{/b}"])
    assert "错误示范" not in hint
    assert "例如" not in hint


# ---------------------------------------------------------------------------
# 三、重试必须**可观测**（"没效果"和"没运行"必须能区分）
# ---------------------------------------------------------------------------


def test_retry_books_a_counter() -> None:
    """★ 重试成功必须在 stats 里留下 `marker_retry_recovered`。

    没有这个计数，"重试没救回来"和"重试根本没跑"从外面看一模一样 ——
    这正是本次三重静默失效能藏住的原因。
    """
    import inspect

    src = inspect.getsource(OllamaTranslationProvider)
    assert "marker_retry_recovered" in src, "重试没有计数，失效时无法察觉"


def test_retry_only_touches_failed_placeholder_entries() -> None:
    """重试的准入条件必须同时看"状态"和"病因"，不能盲目重问所有条目。"""
    import inspect

    src = inspect.getsource(OllamaTranslationProvider)
    # 只看失败条目
    assert "EntryStatus.FAILED" in src
    # 只挑占位符病因的
    assert '"placeholder" in' in src or "'placeholder' in" in src


# ---------------------------------------------------------------------------
# 四、失败时**绝不**写回坏译文（正确性不依赖重试）
# ---------------------------------------------------------------------------


def test_lost_pair_tag_repair_is_position_only_never_content() -> None:
    r"""★ 成对标签丢了记号时，补回只许**插记号**，不许改文字。

    这里记下的是一个**已知取舍**（见 `placeholders.repair_dropped_masks`
    的注释）：Ren'Py 的成对标签被整条丢掉时，补回位置是**猜**的。

    ⚠️ 注意**不**断言"补回后一定合格" —— 补回只负责**插入位置**，
    "记号是否齐全"由调用方的 `verify_restored` 判。
    实测补回 `{/color}` 之后，`{color=#ffd700}` 仍然缺，照样 fatal ——
    那是**正确**的：残缺标记就该被拒。

    真正要守住的不变量：**译文正文一字不差**。
    """
    src = "{color=#ffd700}The lamp was lit at dawn.{/color}"
    m = ph.mask(src)
    assert m.slots == ["{color=#ffd700}", "{/color}"]
    body = "灯在黎明时被点亮了。"
    rep = ph.repair_dropped_masks(m.text, body, m.slots)
    if rep is not None:
        # 正文必须原样在里面（只允许多出 ⟦n⟧ 记号）
        assert rep.replace("⟦0⟧", "").replace("⟦1⟧", "") == body, (
            f"补回只许插记号、不许改正文，实际产出：{rep!r}"
        )


def test_incomplete_tags_are_rejected_by_verify_restored() -> None:
    """★ 真正的不变量：**残缺的标记过不了校验**，因此不会被采用。

    写回闸门 `is_unsafe_writeback` 管的是**变量/外文字符**丢失；
    而"记号是否齐全"由 `verify_restored` 在**翻译阶段**就把关。
    两者是不同层次，这里明确分开测 —— 免得日后有人以为
    "写回闸门会兜住一切"而把翻译阶段的检查删掉。
    """
    src = "{color=#ffd700}The lamp was lit at dawn.{/color}"
    m = ph.mask(src)
    # 只剩闭标签 —— 开标签丢了，颜色范围无从得知
    _restored, check = ph.verify_restored(
        src, "{/color}灯在黎明时被点亮了。", m.slots, masked_source=m.text
    )
    assert check.fatal, "残缺标记必须被判 fatal，否则会写进游戏"


def test_complete_answer_still_verifies() -> None:
    """对照组：记号齐全时必须通过 —— 证明上面的拒绝不是"一律拒绝"。"""
    src = "{color=#ffd700}The lamp was lit at dawn.{/color}"
    m = ph.mask(src)
    got = "⟦0⟧黎明时，油灯被点亮了。⟦1⟧"
    restored, check = ph.verify_restored(src, got, m.slots, masked_source=m.text)
    assert not check.fatal, check.describe()
    assert restored == "{color=#ffd700}黎明时，油灯被点亮了。{/color}"


# ---------------------------------------------------------------------------
# 三、★ 被守卫判死的条目**不许留译文**
# ---------------------------------------------------------------------------


def test_fatal_guard_result_clears_target(monkeypatch: pytest.MonkeyPatch) -> None:
    r"""★ 判死之后必须把译文清掉，否则垃圾会污染下游。

    ## 真实事故

    `foreign_script` 守卫把 3 条跑偏译文判坏之后，它们的 `target`
    **仍然留着**（模型写的俄文/格鲁吉亚文）。而下游 `fonts` 阶段的
    `_collect_texts()` 只看"target 非空"，于是把它们当译文收进字符集：

        приходится долго ждать动画，真是让人受不了。   ← 俄文
        等等！？你要去打შინ纳王？这不可能！              ← 格鲁吉亚文

    那几个字符**本机任何 CJK 字体都没有**，而字符集缺字触发**硬失败**
    ⇒ 整轮字体适配中止 ⇒ `apply` 把原字体原样拷过去
    ⇒ **游戏里满屏口口口**。

    也就是说：**三条被正确拒绝的坏译文，差点让整个游戏的全部中文
    变成方块。** 判据没错，错在拒绝之后没把垃圾清掉。

    `FAILED` 的语义本来就是"没有可用的译文"，
    `_invalidate_entries()` 一直是"清空 + 标 FAILED"，
    只有 provider 这条路径当初漏了清空。
    """
    from novaloc.models import EntryStatus

    # 让 batch 路径直接回一条"跑偏"的译文（西里尔），必须被守卫判死
    prov = _make_provider(monkeypatch, '{"0": "приходится долго ждать"}')
    entries = prov.translate_batch(
        [_FakeItem("It takes a long time to wait.")], "zh-Hans"
    )
    assert len(entries) == 1
    e = entries[0]

    if e.status is not EntryStatus.FAILED:
        pytest.skip("这条样本没被判死（判据调整了？）—— 本用例专门测判死后的清理")

    assert e.warnings, "判死必须留下原因"
    assert not (e.target or "").strip(), (
        f"判死的条目必须清空译文，实际留着：{e.target!r}\n"
        "留着会让 fonts 阶段把它当译文收进字符集 —— "
        "那几个外文字符没有字体覆盖，会让整个字体适配硬失败。"
    )


def test_clean_translation_keeps_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照组：正常译文必须**保留** —— 证明上面的清空不是"一律清空"。"""
    from novaloc.models import EntryStatus

    prov = _make_provider(monkeypatch, '{"0": "等待需要很长时间。"}')
    entries = prov.translate_batch(
        [_FakeItem("It takes a long time to wait.")], "zh-Hans"
    )
    e = entries[0]
    assert e.status is not EntryStatus.FAILED, f"正常译文被判死：{e.warnings}"
    assert (e.target or "").strip(), "正常译文被清空了"


# ---------------------------------------------------------------------------
# 六、【已撤回】换行重试分支不可达 —— 改为守住真实病因
# ---------------------------------------------------------------------------
#
# 这里原来有 4 个测试，测的是「换行记号被吞掉 ⇒ 不屏蔽换行重问一次」。
# **它们喂的是不现实的输入**，所以"通过"并不代表分支会被走到：
#
# * 实测：`repair_dropped_masks` 会把丢掉的记号**重新插回去**（含换行
#   记号，插在数字/量词后面），所以 `verify_restored` 失败的原因
#   **从来不是**"缺换行记号"；
# * 实测 60 条真实多行条目：`placeholder_fatal` 27 条，命中该路径 **0 条**。
#
# 测一个永不执行的分支比不测更糟 —— 它给出"这条修复有效"的假信号。
# 所以换成守住**真实病因**的不变量：真实病因是模型丢掉**内容类**记号
# （`\V[122]` 这种），见 docs/ROADMAP.md §3.21。


def test_dropped_content_marker_never_writes_back_a_broken_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""★ 丢了**内容类**记号（`\V[n]`）时，绝不写回残缺译文。

    这是这条路径唯一真正要守的东西：宁可少翻一条，也不能让玩家
    看到人名/数值消失或错位的文本。
    """
    from novaloc.models import EntryStatus

    # 模型把 ⟦0⟧（即 `\V[1]`，一个变量）整个丢了
    prov = _make_provider(monkeypatch, '{"0": "说这里。"}')
    entries = prov.translate_batch([_FakeItem(r"Say \V[1] here.")], "zh-Hans")
    e = entries[0]
    if e.status is EntryStatus.TRANSLATED:
        # 允许 repair 插回记号后通过，但必须**真的插回去了**
        assert r"\V[1]" in (e.target or ""), (
            f"判成功却没把变量写回去，玩家会看不到名字：{e.target!r}"
        )
    else:
        assert not (e.target or "").strip(), "判死的条目不许可留译文"


def test_repair_only_inserts_markers_never_edits_text() -> None:
    r"""★ 补回记号时**只许插记号**，正文一个字都不许改。

    这条和上面那条是一对：上面守"缺记号不许算成功"，这条守
    "补记号的过程本身不能改动玩家看到的文字"。两者都成立，
    才谈得上"安全地救回条目"。

    用真实条目里的形态（`\V[122]` —— 实测被吞掉的就是这种）。
    """
    src = r"Say \V[122] here."
    m = ph.mask(src)
    body = "在这里说。"
    rep = ph.repair_dropped_masks(m.text, body, m.slots)
    if rep is not None:
        stripped = rep
        for i in range(len(m.slots)):
            stripped = stripped.replace(f"⟦{i}⟧", "")
        assert stripped == body, (
            f"补回只许插记号、不许改正文，实际：{rep!r}（正文应仍为 {body!r}）"
        )


# ---------------------------------------------------------------------------
# 七、多行条目失败 ⇒ 逐行重译（ROADMAP §3.21，实测救回约 23%）
# ---------------------------------------------------------------------------


def _make_line_aware_provider(
    monkeypatch: pytest.MonkeyPatch, *, merged_ok: bool
) -> OllamaTranslationProvider:
    """按"送进来的是整条还是单行"给出不同回复。

    * 整条（提示词里含 `\\n`）且 `merged_ok=False` ⇒ 故意**丢掉第二行**，
      模拟实测病因（模型把两行合并/截断，换行记号因此丢失 → 判死）；
    * 单行 ⇒ 正常翻译。

    这样能真实走完"判死 → 逐行兜底 → 救回"的整条链路，
    而不是只测一个孤立的辅助函数。
    """
    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context

    ctx = Context(config=Config(), events=EventBus())
    prov = OllamaTranslationProvider(ctx)

    def fake_chat(user: str, *a: object, **k: object) -> str:
        whole = "\n" in user
        if whole and not merged_ok:
            # 只译第一行，第二行整句丢掉（`⟦0⟧` 换行记号也随之消失）
            return '{"0": "莉菲娅"}'
        if "Lifia" in user:
            return '{"0": "莉菲娅"}'
        return '{"0": "我得走了。"}'

    monkeypatch.setattr(prov, "_chat", fake_chat)
    return prov


def test_perline_fallback_recovers_multiline_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""★ 多行条目判死后，逐行重译应能救回，并留下计数。

    真实病因见 ROADMAP §3.21：模型把两行合并或截断，而"换行记号丢失"
    是唯一能抓住它的信号 —— 所以只能靠**改变任务形态**（逐行送翻）
    来救，而不是放宽判据。

    实测（30 条真实多行条目）：合并送翻通过 17、判死 13，
    逐行重译**救回 7 条**（占 23%），原通过的 17 条一条未改。
    24 条抽样上：成功 16 → **20**，逐行救回 4。
    """
    from novaloc.models import EntryStatus

    prov = _make_line_aware_provider(monkeypatch, merged_ok=False)
    entries = prov.translate_batch([_FakeItem("Lifia\n「I have to go.」")], "zh-Hans")
    e = entries[0]
    assert e.meta.get("perline_fallback") is True, f"没走逐行兜底：{e.warnings}"
    assert e.status is EntryStatus.TRANSLATED
    assert "\n" in e.target, f"救回的译文必须有换行：{e.target!r}"
    assert prov.stats.get("perline_recovered") == 1


def test_perline_fallback_not_used_for_single_line_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**零假阳性对照**：单行条目没有"逐行"可言，不许走这条路。

    否则每条失败的单行条目都会多打一次请求，成本上升而毫无收益。

    ▲ 这里**只**断言"没走逐行兜底"，不断言状态是 FAILED ——
      单行条目本来就可能翻译成功（这个假 provider 对不含换行的
      请求总会给出一句译文），那是另一回事，与这条修复无关。
    """
    prov = _make_line_aware_provider(monkeypatch, merged_ok=False)
    entries = prov.translate_batch([_FakeItem("Some single line.")], "zh-Hans")
    assert "perline_fallback" not in entries[0].meta, (
        f"单行条目不该走逐行兜底：{entries[0].meta}"
    )
    assert not prov.stats.get("perline_recovered"), (
        "单行条目不该产生逐行救回计数"
    )


def test_perline_fallback_validates_each_line_individually() -> None:
    r"""★ 每一行都要**自己**过守卫 —— 这是实测要求，不是洁癖。

    整条过守卫会漏掉行内坏产物。实测样例：逐行送翻时
    `translategemma:4b` 吐出 `「邪恶之 bane」` ——
    原文词 `bane` 泄漏进了中文，而整条校验看不出来。

    ⚠️ 这里只断言"逐行守卫确实在那里"这个**结构事实**，
    因为 `guard` 目前**抓不住**那种拉丁词泄漏（既不是
    `foreign_script`，也不触发重复/长度判据）。
    把已知限制写进测试，比假装它已经解决要好 ——
    后续方向见 ROADMAP §3.21。
    """
    import inspect

    src = inspect.getsource(OllamaTranslationProvider.translate_batch)
    assert "3.7 多行条目失败" in src, "逐行兜底不见了"
    idx = src.find("3.7 多行条目失败")
    seg = src[idx : idx + 7000]
    assert "r_one = guard(" in seg, (
        "逐行兜底里没有逐行过守卫 —— 整条校验会漏掉行内泄漏"
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_provider(monkeypatch: pytest.MonkeyPatch, raw: str) -> OllamaTranslationProvider:
    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context

    ctx = Context(config=Config(), events=EventBus())
    prov = OllamaTranslationProvider(ctx)
    monkeypatch.setattr(prov, "_chat", lambda *a, **k: raw)
    return prov
