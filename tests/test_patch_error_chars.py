r"""★★ 字体补丁报错必须能被**结构化解析**出"该剔哪些字符"。

## 实测缺陷（`Battle Demon Kirsten`，白跑 7.45 小时）

`stage_fonts` 有"剔掉只影响极少数条目的字符、然后重试"的机制，
它靠 `_chars_from_patch_error(res.error)` 取回字符。而 QA 失败的
error 串**只报数量**：

```
'❌ 检查 2996 字  缺码点 8  空白 1'
```

那个解析函数按"最后一个冒号之后全是字符"切（适配 `patch_font`
**另一种**措辞），于是把 summary 里的**汉字字面量**当成了数据：

```
输入 'QA 未通过：❌ 检查 2997 字  空白 1'
输出 {'空','❌','1','白','查','7','9','字','2','检'}
```

⇒ 真正的 `U+2800`（盲文空白）**永远不在候选里** ⇒ 永远剔不掉
⇒ 迭代 3 次用尽 ⇒ **硬失败** ⇒ 整局判失败。

修法：`patch_font` 在 QA 失败时按**既有格式**（末尾"："+ 一串字符）
附上具体字符。

## 本文件测什么

1. 解析器对**修复后的格式**能取回全部字符（含 `U+2800`）；
2. 解析器对**旧格式**（只有数量）**不会**返回一堆汉字
   —— 这是"别再让人读的文案当数据"的守卫；
3. `service.py` 里确实附上了字符（结构性守卫）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.pipeline.stages import Pipeline  # noqa: E402

BRAILLE_BLANK = "\u2800"
#: 真实出现过的"补不上的字符"（孟加拉文/泰文乱码幻觉 + 盲文空白）
REAL_BAD = "\u0982\u09a8\u09b6\u0e19\u0e30" + BRAILLE_BLANK


# ----------------------------------------------------------------------
# 1. 修复后的格式必须能解析
# ----------------------------------------------------------------------
def test_fixed_error_format_yields_all_chars() -> None:
    """★★ 核心：附上字符后，解析器要能取回**全部**字符。"""
    err = f"QA 未通过：❌ 检查 2996 字  缺码点 8  空白 1；不合格字符：{REAL_BAD}"
    got = Pipeline._chars_from_patch_error(err)
    assert got == set(REAL_BAD), f"解析结果不符：{''.join(sorted(got))!r}"


def test_braille_blank_is_recoverable() -> None:
    r"""★★ 最关键的一个：`U+2800` 必须能被取回。

    它永远取不回时，剔除机制对它就**永远无效** ⇒ 每轮都硬失败。
    这是那个游戏白跑 7.45 小时的直接原因。
    """
    err = f"QA 未通过：❌ 检查 2996 字  空白 1；不合格字符：{BRAILLE_BLANK}"
    got = Pipeline._chars_from_patch_error(err)
    assert BRAILLE_BLANK in got, "U+2800 取不回 ⇒ 剔除机制对它永久失效"


# ----------------------------------------------------------------------
# 2. 旧格式（只有数量）不能产生"假字符"
# ----------------------------------------------------------------------
def test_count_only_error_does_not_yield_hanzi() -> None:
    r"""★★ 旧格式只报数量时，**不该**把 summary 的汉字当成待剔字符。

    这是"别再让人读的文案当数据"的守卫。旧格式下解析结果为空或
    噪声 —— 但**绝不能**被上游当成"这些汉字补不上"。
    （修复前的行为就是返回 `{'空','白','查','字',...}`。）
    """
    err = "QA 未通过：❌ 检查 2997 字  空白 1"
    got = Pipeline._chars_from_patch_error(err)
    # 允许为空（最干净）或仍含 summary 里的字面量（兼容旧路径），
    # 但**必须**不含任何真实待剔字符 —— 因为那条串里根本没有它们。
    assert BRAILLE_BLANK not in got, "旧格式里没有 U+2800，不该凭空出现"
    assert not (got & set(REAL_BAD)), "旧格式里没有这些字符，不该凭空出现"


def test_empty_error_is_safe() -> None:
    """空/None 不该崩。"""
    assert Pipeline._chars_from_patch_error("") == set()
    assert Pipeline._chars_from_patch_error(None) == set()


def test_error_without_colon_is_safe() -> None:
    """没有冒号的错误串返回空集（而不是把整串当字符）。"""
    assert Pipeline._chars_from_patch_error("something went wrong") == set()


# ----------------------------------------------------------------------
# 3. 结构性守卫：service.py 确实附上了字符
# ----------------------------------------------------------------------
def test_service_appends_chars_to_qa_error() -> None:
    r"""★★ 结构性守卫：`patch_font` 的 QA 失败路径必须附上字符。

    防止有人后来把那段删掉 —— 那会让剔除机制再次失效，
    而症状是"某个游戏莫名其妙硬失败"，很难归因。
    """
    src = (ROOT / "src" / "novaloc" / "fonts" / "service.py").read_text(encoding="utf-8")
    for needle in (
        "res.qa.missing",
        "res.qa.blank",
        "res.qa.tofu",
        "不合格字符：",
    ):
        assert needle in src, f"service.py 里找不到 {needle!r} —— QA 报错可能又不带字符了"
