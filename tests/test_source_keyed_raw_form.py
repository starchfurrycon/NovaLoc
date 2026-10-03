r"""回归：模型以**原文作键**回答时，单条/批量都必须能取出译文。

## 钉住的缺陷

真实游戏库 `auto` 的 stderr（工作区 `f4b03ca791a9`，单条重试路径）：

```
批 0（1 条）第 1/2/3 次失败：单条翻译失败（无法解析为 JSON）
    ：解析得到空映射：'{"火山を主な生息地とする竜種。\n首の長さで…": "火山是主要栖息地…"}'
```

**译文明明就在值里**（完整、正确），却三次重试全败 ⇒ 该条内容丢失。

## 两层原因（第二层是这次修的）

1. 原来 `_call_single` **没传** `sources` ⇒ 无法把"原文键"反查成编号
   （上一轮已修）。
2. ★ 但传了 `masked` **还不够**：配置 `mask_newlines=True` 时
   `masked` 里换行是记号 ``⟦0⟧``，而模型回吐的是**字面 ``\n``** 形态。
   两者不完全相等 ⇒ 精确匹配失败 ⇒ 依然空映射。

实测判据（`.scratch/_reject_probe.py`）：

| sources | 结果 |
| --- | --- |
| `None` | `keys=None` ❌ |
| `[掩码形态]` | `keys=None` ❌（记号形态≠字面 `\n`） |
| `[原文形态]` | `keys=[0]` ✅ 取出 58 字符 |

⇒ 修法是**两个候选都传**。多传不会误配：`to_translation_map` 是精确匹配，
键必须恰好等于某候选才会被认成那一编号。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.translate import json_parse as jp  # noqa: E402
from novaloc.translate import placeholders as ph  # noqa: E402
from novaloc.translate.ollama_provider import (  # noqa: E402
    OllamaTranslationProvider,
)

JA = "火山を主な生息地とする竜種。\n首の長さでオスの優劣が決まる。"
ZH = "火山是主要栖息地。根据领头的长度来区分雄性和雌性。"
#: 真实日志里模型回吐的键：**字面 `\n`**、不是掩码记号。
RAW_KEYED = '{"' + JA.replace("\n", "\\n") + '": "' + ZH + '"}'


def _masked() -> ph.MaskResult:
    return ph.mask(JA, newlines=True)


def test_original_repro_shape_is_as_described() -> None:
    """前提固定：掩码形态与原文形态**确实不同**。"""
    mr = _masked()
    assert "\u27e60\u27e7" in mr.text, "掩码里应当是记号形态"
    assert "\n" in JA
    assert mr.text != JA, "前提：两者不同，否则这条缺陷不存在"


def test_masked_only_cannot_resolve_literal_newline_key() -> None:
    """★ 只传掩码形态 ⇒ 查不到（这正是修好第一层后**仍然**失败的原因）。"""
    mr = _masked()
    mapping, res = jp.parse_translations(
        RAW_KEYED, expect_indices=[0], sources=[mr.text]
    )
    assert res.ok, "解析本身应当成功（JSON 合法）"
    assert not mapping, "只传掩码形态时应当查不到 —— 若查到了说明前提变了"


def test_raw_form_resolves_the_key() -> None:
    """★ 传原文形态 ⇒ 能取出译文（证明"两个候选都传"是对的）。"""
    mapping, res = jp.parse_translations(
        RAW_KEYED, expect_indices=[0], sources=[JA]
    )
    assert res.ok
    assert mapping.get(0) == ZH, f"应当取出完整译文：{mapping}"


def test_both_candidates_together_WOULD_misnumber() -> None:
    """★★ 反例：把两种形态塞进**同一个** `sources` 会**编号错位**。

    这是修法的关键约束，必须钉住 —— 否则下一个人"顺手合并两个候选"
    就会重新引入这个缺陷：

        sources=[掩码, 原文]  ⇒  `_build_source_index` 按位置分配编号
                                掩码→0、原文→1
        ⇒ 模型回"原文作键"时反查得到 **编号 1**，而单条只有编号 0
        ⇒ `mapping.get(0)` 取不到 ⇒ **仍然失败**

    实测（`.scratch/_reject_probe.py` 的延伸）：

        sources=[掩码形态, 原文形态]  →  {1: '火山是主要栖息地…'}  ❌
        sources=[原文形态]            →  {0: '火山是主要栖息地…'}  ✅

    ⇒ 正确做法是**两次独立解析**，而不是把候选并成一个列表。
    """
    mr = _masked()
    mapping, _res = jp.parse_translations(
        RAW_KEYED, expect_indices=[0], sources=[mr.text, JA]
    )
    assert mapping.get(0) is None, (
        "若这里取到了 0，说明 `_build_source_index` 的行为变了 —— "
        "那么 `_call_single` 里的两次解析可以简化，但必须先改这条断言"
    )
    assert mapping.get(1) == ZH, f"错位后会落在编号 1：{mapping}"


def test_sequential_two_pass_resolves_to_index_zero() -> None:
    """★★ 修法本身：**两次独立解析**，两次都能正确落在编号 0。"""
    mr = _masked()
    mapping, _res = jp.parse_translations(
        RAW_KEYED, expect_indices=[0], sources=[mr.text]
    )
    assert not mapping, "第一遍（掩码形态）应当取不到"
    mapping2, _res2 = jp.parse_translations(
        RAW_KEYED, expect_indices=[0], sources=[JA]
    )
    assert mapping2.get(0) == ZH, f"第二遍（原文形态）必须落在编号 0：{mapping2}"


def test_batch_multi_source_indices_stay_parallel() -> None:
    """★ 多条目批次：两次解析的编号都必须**与 expect 平行**。"""
    m1 = ph.mask("First line.\nSecond line.", newlines=True)
    p1, p2 = "First line.\nSecond line.", "Another one."
    raw = '{"' + p1.replace("\n", "\\n") + '": "第一行。\\n第二行。"}'
    mapping, _res = jp.parse_translations(
        raw, expect_indices=[0, 1], sources=[p1, p2]
    )
    assert mapping.get(0), f"第 0 条应当取到：{mapping}"
    assert mapping.get(1) is None, f"不该给出第 1 条：{mapping}"
    assert m1.text != p1


def test_extra_candidates_do_not_mis_map() -> None:
    """★ 多传候选**不许**造成误配（这是"两个都传"安全的依据）。"""
    other = "完全不同的另一句原文。"
    mr = _masked()
    mapping, _res = jp.parse_translations(
        '{"0": "编号形态译文"}',
        expect_indices=[0, 1],
        sources=[mr.text, JA, other],
    )
    assert mapping.get(0) == "编号形态译文", f"编号形态不该被候选干扰：{mapping}"
    assert 1 not in mapping, "不该凭空造出第 1 条"


def _code_only(fn: object) -> str:
    """函数源码，**去掉注释与字符串字面量**。

    ★ 为什么不能直接数原始源码：`_call_single` 的**文档字符串**里
      就写着 `sources=[掩码, 原文]` 这种反例说明（正是为了警示后人
      不要合并），直接 `count("sources=[")` 会把注释里的例子也数进去
      —— 实测得到 7 而不是 2，于是断言无效。
    """
    import io
    import tokenize

    src = inspect.getsource(fn)  # type: ignore[arg-type]
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_call_single_retries_with_raw_source() -> None:
    """★ 源码级断言：`_call_single` 必须在**取不到**时用原文再解析一次。

    这里用源码断言而不是跑一遍 provider —— 真正要钉住的是"第二遍解析
    存在、且用的是 `item.unit.source`"这个**结构**，跑一遍会引入
    模型/网络依赖。项目里对 `_call_single` 已有同类断言
    （见 `test_source_keyed_translations.py`）。
    """
    code = _code_only(OllamaTranslationProvider._call_single)
    assert "if not mapping" in code, "必须只在第一遍取不到时才走第二遍"
    assert "item . unit . source" in code, "第二遍必须用未掩码原文"
    # 两次**单元素** sources 解析；合并成一个列表会编号错位
    assert code.count("sources = [ masked ]") == 1, "第一遍用掩码形态"
    assert code.count("sources = [ item . unit . source ]") == 1, (
        "第二遍用原文形态（单元素，不能与掩码合并）"
    )


def test_call_batch_retries_with_raw_sources() -> None:
    """★ 批量路径同样要求"取不到时用原文再解析一次"。"""
    code = _code_only(OllamaTranslationProvider._call_batch)
    assert "if not mapping" in code, "必须只在第一遍取不到时才走第二遍"
    assert "raw_sources" in code, "第二遍必须用未掩码原文列表"
    assert "sources = raw_sources" in code, "把原文列表交给 parse_translations"


def test_empty_value_key_is_still_rejected() -> None:
    """★ 安全边界：值为空的那次重试（日志第 3 次）**不该**被当成译文。

    日志第 3 次是 `{"t": {"<译文>": ""}}` —— 键是**译文**、值是空串。
    那种形态没有可用内容，必须继续判失败（否则会把空串写回游戏）。
    """
    bad = '{"t": {"' + ZH + '": ""}}'
    mapping, _res = jp.parse_translations(
        bad, expect_indices=[0], sources=[JA, ZH]
    )
    assert not mapping.get(0), f"空值不能当成译文：{mapping}"
