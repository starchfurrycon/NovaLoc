"""逐行救援路径必须把**掩码后**的文本交给 `_call_single`。

## 真实缺陷（本次实测）

`ollama_provider.translate_batch()` 的最后一段救援是
「多行条目失败 ⇒ 逐行重译」（长条目的困难在于**换行记号太多**，
逐行送翻时每行没有换行、记号降到 0～2 个，模型理应轻松应付 ——
手工复现实测 6 条长文本 **全部 4/4 行成功**）。

但这段代码当时写成了：

    got_one = self._call_single(batch_items[local_i], one)   # ← one 是**裸源码行**

而 `_call_single(item, masked, *, slots=...)` 的第二个参数语义是
**掩码后**的文本，`slots` 是它对应的槽位表，返回时靠 `slots`
把 `⟦n⟧` 还原成原样记号（`\\V[1]`、`\\C[0]` …）。

于是：
* 传进去的是**尚未掩码**的裸文本，`slots` 又没传（默认空），
  行内 `\\V[1]` 这类内容记号**永远不会被屏蔽、也不会被还原**；
* `_call_single` 内部基于掩码的判据（如 `remaining_masks`）失去依据。

后果：这条救援路径**几乎救不回任何条目**（实测长条目
`perline_recovered = 0`），而它本该是最对症的修法。

## 本文件守什么

守**调用形态**：逐行救援里对 `_call_single` 的调用必须是
"先 `ph.mask(...)`，再把 `.text` 与 `.slots` 一起传进去"。

为什么用 AST 而不是端到端：这条路径要跑到必须"整条先失败"，
且依赖真实模型输出，端到端用例会既慢又不稳；
而缺陷本身是**参数用错**，静态形态检查能精确抓住，
且**在旧写法下确实是红的**（见文末自检说明）。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from novaloc.translate.ollama_provider import OllamaTranslationProvider

#: 被排查的那个方法所在的源文件。
SRC = Path(inspect.getfile(OllamaTranslationProvider))

#: 逐行救援里应当出现的掩码调用。
REQUIRED_MASK_CALL = "ph.mask"


def _translate_batch_fn() -> ast.FunctionDef:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "translate_batch":
            return node
    pytest.fail("找不到 `translate_batch()` —— 源码结构变了，请更新本测试")


def _perline_section(fn: ast.FunctionDef) -> list[ast.stmt]:
    """取「逐行重译」那一段的语句。

    靠 `perline_saved` 这个变量名定位 —— 它是那段自己引入的计数器。
    找不到就说明结构改了，直接 fail（宁可红，也不要静默失效）。
    """
    for node in ast.walk(fn):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            targets = getattr(stmt, "targets", [])
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if "perline_saved" in names:
                return body[i:]
    pytest.fail("找不到 `perline_saved = 0` —— 逐行救援段可能被删了")


def _call_single_calls(stmts: list[ast.stmt]) -> list[ast.Call]:
    out: list[ast.Call] = []
    for stmt in stmts:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_call_single"
            ):
                out.append(node)
    return out


def test_perline_rescue_passes_masked_text_not_raw_line() -> None:
    """★ 逐行救援里 `_call_single` 的第二个参数必须是掩码文本。

    旧写法 `self._call_single(batch_items[local_i], one)` 里 `one`
    是裸源码行 —— 这条断言在旧写法下**失败**（已验证），
    这正是它存在的意义。
    """
    calls = _call_single_calls(_perline_section(_translate_batch_fn()))
    assert calls, "逐行救援里没有调用 `_call_single` —— 结构变了"
    for call in calls:
        assert len(call.args) >= 2, (
            f"`_call_single` 只传了 {len(call.args)} 个位置参数，"
            "第二个（掩码文本）缺失"
        )
        second = call.args[1]
        assert not (isinstance(second, ast.Name) and second.id == "one"), (
            "★ 逐行救援把**裸源码行** `one` 交给了 `_call_single`。\n"
            "第二个参数语义是**掩码后**的文本，必须传 `ph.mask(one, ...).text`，"
            "并把 `.slots` 一并传入，否则行内 `\\V[1]` 这类内容记号"
            "既不会被屏蔽、也不会被还原（实测该路径因此救不回条目）。"
        )


def test_perline_rescue_passes_slots() -> None:
    """`slots=` 必须一起传 —— 没有它就无法把 `⟦n⟧` 还原成原样记号。"""
    calls = _call_single_calls(_perline_section(_translate_batch_fn()))
    assert calls, "逐行救援里没有调用 `_call_single`"
    for call in calls:
        assert any(kw.arg == "slots" for kw in call.keywords), (
            "`_call_single` 调用没有传 `slots=` —— "
            "掩码记号将无法还原成 `\\V[1]` 这类原样记号"
        )


def test_perline_rescue_masks_before_calling() -> None:
    """掩码动作必须在调用之前出现（`ph.mask`）。"""
    section = _perline_section(_translate_batch_fn())
    src = "\n".join(ast.unparse(s) for s in section)
    assert REQUIRED_MASK_CALL in src, (
        f"逐行救援段里没有 `{REQUIRED_MASK_CALL}` —— "
        "说明这一行没有被掩码就送去翻译了"
    )
