r"""★★★ `note` 字段里的 `<JS …>` 代码**绝不能翻** —— 翻了游戏启动即崩。

## 实测事故（`Beyond the Portal`，游戏打不开）

从游戏 stderr 抓到的完整调用栈：

```
SyntaxError: Unexpected number
    at new Function (<anonymous>)
    at Object.VisuMZ...Parse_Notetags_State_ApplyRemoveLeaveJS
       (js/plugins/VisuMZ_1_SkillsStatesCore.js:2913)
    at Object.VisuMZ.ParseStateNotetags (VisuMZ_3_StateTooltips.js)
    at Object.VisuMZ.ParseAllNotetags (VisuMZ_0_CoreEngine.js)
    at Scene_Boot.<computed> (VisuMZ_0_CoreEngine.js)
```

**机理**：VisuMZ（VisuStella MZ）插件把 `States.json` 等条目的
**`note` 字段**当配置区解析 —— `<JS …>` 区块里的内容会被
**`new Function()` 执行**。

而 NovaLoc 把 `note` 当文本翻了：

```
备份: '<JS On Expire State>\ntarget.addState(80);\n</JS On Expire State>'
当前: '确认<JS On Expire State>\n目标生命值恢复至 80。\n</JS On Expire State>'
                                            ^^^^^^^^^^^^^^^^^^^^ JS 变成了中文
```

⇒ `new Function('目标生命值恢复至 80。')` ⇒ `SyntaxError: Unexpected number`。

## 根因：守卫**只看语法特征**，漏掉了纯 API 调用式代码

原 `NOTE_JS_RE`：

```python
r"\$game[A-Za-z]+|\barguments\s*\[|\bfunction\b|=>|\bvar\s+\w|\blet\s+\w"
r"|\bnew\s+[A-Z]|\.setValue\s*\(|\.value\s*\("
```

而 `target.addState(80);` **一条都不匹配** ⇒ 被当"人话"送去翻译。

## 修法（两条判据）

1. **`<JS …>` / `</JS>` 区块本身**就是铁证 ——
   凡是在这种区块里，内容**必然会被 eval**，无论像不像代码；
2. **常见 API 调用形态**：`<对象>.<方法>(...)`，覆盖
   `target.addState(80)` / `user.gainHp(-1)` / `this.setState(5)`；
3. 补 `x++` / `x--` 这类赋值语句。

## 教训

**"是不是代码"不能只看语法特征。** `<JS …>` 区块是**语义标记**，
比语法特征硬得多 —— 这与本项目反复学到的同一条教训一致：
**用结构证据，不要用形态猜测。**

## 本文件测什么

1. `<JS …>` 区块里的内容**一律**判为不可翻（含纯 API 调用）；
2. **不误伤**真正的帮助文本（`<Help Description>+5% Hp…` 仍该翻）；
3. `States.json` 的 `note` 在字段白名单里（所以守卫是**唯一**防线）；
4. 结构性守卫：`<JS` 模式仍在 `NOTE_JS_RE` 里。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.engines.rpgmaker import (  # noqa: E402
    DATABASE_FIELDS,
    NOTE_JS_RE,
)

RPG_SRC = ROOT / "src" / "novaloc" / "engines" / "rpgmaker.py"


# ----------------------------------------------------------------------
# 1. ★★ `<JS …>` 区块里的内容一律判为代码
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "note",
    [
        # ★ 实测让游戏崩掉的那两条（纯 API 调用，无语法关键字）
        "<JS On Expire State>\ntarget.addState(80);\n</JS On Expire State>",
        "<JS On Expire State>\ntarget.addState(98);\n</JS On Expire State>",
        # 其它常见形态
        "<JS On Apply State>\nthis.setState(5);\n</JS On Apply State>",
        "<JS On Add State>\nuser.gainHp(-1);\n</JS On Add State>",
        "<JS On Battle Start>\n$gameParty.members()[0].gainHp(10);\n</JS On Battle Start>",
        # 只有开标签（插件也可能这么写）
        "<JS On Expire State>\ntarget.removeState(3);\n",
    ],
)
def test_js_blocks_are_never_translatable(note: str) -> None:
    r"""★★ `<JS …>` 区块里的内容**一律**判为代码。

    这里尤其要覆盖"没有任何语法关键字、只有一次 API 调用"的形态 ——
    那正是原守卫漏掉、导致游戏崩掉的那一类。
    """
    assert NOTE_JS_RE.search(note), f"漏判了会被 eval 的 JS：{note[:60]!r}"


def test_plain_api_call_is_caught() -> None:
    """★ 单独一条 `target.addState(80);`（没有 `<JS>` 标签）也要拦下。"""
    assert NOTE_JS_RE.search("target.addState(80);")


# ----------------------------------------------------------------------
# 2. ★ 不误伤真正的帮助文本
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "note",
    [
        "<Positive State>\n<Help Description>\n+5% Hp, Resist. Poison\n</Help Description>",
        "Charging Power: gain 20% damage for 3 turns.",
        "<Negative State>\n<Help Description>\n-2% Hp, Resist. Certain grips\n</Help Description>",
    ],
)
def test_help_text_is_not_mistaken_for_code(note: str) -> None:
    r"""★★ 真正的帮助文本**必须仍然可翻**。

    过度拦下会让游戏里的状态说明、技能描述**永远不被翻译** ——
    那是用户能看见的功能缺失。
    """
    assert not NOTE_JS_RE.search(note), f"误把帮助文本当代码：{note[:60]!r}"


# ----------------------------------------------------------------------
# 3. ★★ 为什么守卫是唯一防线
# ----------------------------------------------------------------------
def test_note_is_in_the_translation_whitelist() -> None:
    r"""★★ `States.json`/`Skills.json` 的 `note` **在**翻译白名单里。

    这一点很重要：它说明**不能靠"别抽 note"来解决** ——
    那会连带丢掉 note 里真正的 `Help Description` 文本。
    所以 `NOTE_JS_RE` 守卫是**唯一**的防线。
    """
    for f in ("States.json", "Skills.json", "Items.json", "Armors.json", "Weapons.json"):
        assert "note" in DATABASE_FIELDS.get(f, set()), f"{f} 的 note 不在白名单里"


# ----------------------------------------------------------------------
# 4. 结构性守卫
# ----------------------------------------------------------------------
def test_js_tag_pattern_is_present() -> None:
    r"""★★ `NOTE_JS_RE` 必须能匹配 `<JS …>` —— 防止被人无意删掉。

    删掉的症状是"某些游戏启动即崩，且报错里没有文件名行号"
    （`new Function` 抛的 SyntaxError 就是这样），**极难归因**。
    """
    assert NOTE_JS_RE.search("<JS X>\ncode();\n</JS X>")
    src = RPG_SRC.read_text(encoding="utf-8")
    assert "<JS" in src, "`<JS` 判据不在源码里了 ⇒ 游戏又会崩"
    assert "new Function" in src, "没记录事故机理（`new Function` 会 eval note）"


def test_js_api_pattern_documented() -> None:
    """源码里要记着实测事故（含 VisuMZ 与那个游戏名）。"""
    src = RPG_SRC.read_text(encoding="utf-8")
    assert "VisuMZ" in src, "没记录是谁 eval 了 note"
    assert "Beyond the Portal" in src, "没记录触发事故的游戏"
