r"""★ 回归：逐行救援路径必须**补回被丢掉的样式记号**（颜色等）。

## 缺陷现场（实测 `.scratch/_single_vs_batch_color.py`，3/3 轮一致）

同一个输入、同一个模型，**两条路径结果不同**：

    输入            '\c[0]Suck it.'      （掩码后 '⟦0⟧Suck it.'）
    模型原样返回    '{"0": "吃不了就别来!"}'   ← 压根没回 ⟦0⟧

    _call_single    → '吃不了就别过来。'        ← 颜色码**丢了**
    translate_batch → '\c[0]吃不了就别过来。'   ← 颜色码**保住了**

### 根因

`repair_dropped_masks` 只在**整条路径**里被调用
（`ollama_provider.py` §3 的 `if ph_check.fatal` 分支）；
`_call_single` **不做**这一步。于是逐行救援救回的译文**丢配色**。

它按"记号在原文里的左邻字符 / 相对位置"补回记号，**位置确定**时才算成功
（内部保证，否则返回 `None`），所以不是猜。

### 影响面

逐行救援是现在多行对话的主力路径（实测救回率 90%），
而多行对话几乎**每行都带颜色码** ⇒ 这是"救回来的译文质量"问题，
不是"能不能救回"的问题。

## 判据

* 生产路径跑出来的译文里，**原文有的样式记号必须还在**；
* 补回**不能**凭空制造记号（数量仍由 `guard` / `verify_restored` 复查）。
"""

from __future__ import annotations

import inspect

from novaloc.translate import placeholders as ph


# ---------------------------------------------------------------------------
# 1) 机制层：repair_dropped_masks 的行为（逐行路径依赖它）
# ---------------------------------------------------------------------------
def test_repair_dropped_masks_restores_leading_colour_code() -> None:
    """模型整段丢掉记号时，`repair_dropped_masks` 必须能补回并还原。"""
    m = ph.mask(r"\c[0]Suck it.", newlines=False)
    assert m.slots == [r"\c[0]"]
    # 模拟模型返回：没有记号，只有译文
    repaired = ph.repair_dropped_masks(m.text, "吃干榨尽。", m.slots)
    assert repaired is not None, "开头就是记号的，位置是确定的，必须能补"
    restored = ph.unmask(repaired, m.slots)
    assert r"\c[0]" in restored, f"必须还原出颜色码：{restored!r}"
    assert restored == r"\c[0]吃干榨尽。"


def test_repair_dropped_masks_is_noop_when_nothing_missing() -> None:
    """记号齐全时返回 `None`（调用方据此知道"没什么可补"）。"""
    m = ph.mask(r"\c[0]Suck it.", newlines=False)
    assert ph.repair_dropped_masks(m.text, m.text, m.slots) is None


def test_repair_dropped_masks_never_invents_extra_codes() -> None:
    """★ 补回**不能**凭空造出原文没有的颜色码。"""
    m = ph.mask(r"\c[0]A", newlines=False)
    repaired = ph.repair_dropped_masks(m.text, "甲", m.slots) or "甲"
    restored = ph.unmask(repaired, m.slots)
    # 原文只有一个 \c[0]，结果里也只能有一个
    assert restored.count(r"\c[0]") == 1, f"不能多出颜色码：{restored!r}"


# ---------------------------------------------------------------------------
# 2) 源码级：逐行救援路径必须调用它（这是本次真正修的地方）
# ---------------------------------------------------------------------------
def _code_only(fn: object) -> str:
    """只保留 NAME/OP 之外的**代码** token（剥掉注释与字符串）。

    与 `test_source_keyed_raw_form.py` 的同类辅助函数一致。
    注意它用空格连接 token，所以判据要写成 token 序列的样子。
    """
    import io
    import tokenize

    src = inspect.getsource(fn)
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            continue
        if tok.string.strip():
            out.append(tok.string)
    return " ".join(out)


def test_perline_rescue_calls_repair_dropped_masks() -> None:
    r"""★★ 逐行救援路径必须补回丢掉的记号。

    断点就是"少调一个函数"——它**没有任何运行时报错**，只会让救回的
    译文静默丢配色（`\c[0]` 消失），而玩家看到的是"颜色不对"，
    根本不会联想到"翻译程序漏了一步"。
    """
    from novaloc.translate import ollama_provider as op

    src = inspect.getsource(op.OllamaTranslationProvider.translate_batch)
    # 逐行救援段落里必须出现 repair_dropped_masks
    assert "repair_dropped_masks" in src, (
        "translate_batch 里必须调用 repair_dropped_masks（逐行救援路径）"
    )
    # 至少有**两处**调用：整条路径 + 逐行路径
    assert src.count("repair_dropped_masks") >= 2, (
        "整条路径与逐行路径**都**要补回记号码，"
        f"实测只找到 {src.count('repair_dropped_masks')} 处"
    )
    # 必须有一个专门的统计键，否则"有没有生效"看不出来
    assert "perline_style_repaired" in src, (
        "必须记录 perline_style_repaired —— 否则无法判断这条修复是否真的生效"
    )


def test_perline_rescue_uses_masked_form_for_repair() -> None:
    """★ 补回时的三个参数必须成体系：`line_mask.text` / `got_one` / `line_mask.slots`。

    ⚠️ 关键陷阱：`_call_single` 返回的是**掩码形态**（带 `⟦i⟧`），
    不是还原后的文本。传错形态会让 `repair_dropped_masks` 找不到记号。
    这一点已从 `_call_single_once` 的返回路径确认。
    """
    from novaloc.translate import ollama_provider as op

    code = _code_only(op.OllamaTranslationProvider.translate_batch)
    # token 序列形态（_code_only 用空格连接）
    assert "repair_dropped_masks ( line_mask . text , got_one , line_mask . slots )" in code, (
        "必须用 line_mask.text / got_one / line_mask.slots 三件套调用"
    )


def test_guard_receives_unmasked_text() -> None:
    """★ 过守卫前必须**还原**成原文记号形态。

    `guard` 比的是"原文 vs 译文"的占位符，原文是 `\\c[0]` 形态，
    所以译文也必须是 `\\c[0]` 形态；拿掩码形态 `⟦0⟧` 去比会全部不匹配。
    """
    from novaloc.translate import ollama_provider as op

    code = _code_only(op.OllamaTranslationProvider.translate_batch)
    assert "ph . unmask ( got_one , line_mask . slots )" in code, (
        "过守卫前必须 ph.unmask(got_one, line_mask.slots)"
    )
