"""RPG Maker 的 `note` 字段是**插件配置区**，不是给人读的注释。

## 真实事故

BeyondPortal 的 265 条 `note` 里：

==================== ======
含 JS 代码            27
含 ``<插件标签>``      263
两者都有              27
**纯散文（该翻译）**   **2**
==================== ======

也就是 **263/265 根本不该翻**。不判断的后果实测有三层：

1. **会把插件改坏**：``<Crafting Ingredients>`` 被翻成 ``<制作材料>``，
   插件再也匹配不到这个标签，制作系统直接失效。
   而这些标签里的内容**会被插件读取、甚至 eval 执行**。
2. **翻译必然失败**：一条 note 里塞着
   ``$gameVariables.setValue(77, ...)`` 和十几个标签，占位符掩码后
   模型完全读不懂 —— 实测 45 条空译文里 28 条是 note，
   报 ``placeholder_broken: 丢失占位符：['$gameVariables', ...]``。
3. **模型会泄漏掩码标记**：真实记录里模型吐出
   ``'啊…乌鲁拉，别急，我还没瞄准呢！”} ⟦0⟧'`` ——
   连**正常对话**都被这种批次带坏了。

## 这里钉住的第二个坑：同一个标签，两种含义

``<right>`` 在 ``name`` 字段里是**排版指令**（说话人名要靠右显示，
正文得翻），在 ``note`` 字段里却是**插件参数**（翻了插件就废了）。

第一版修法把"插件配置"规则用到了**所有字段**上，结果说话人名从
**7694 条掉到 660 条** —— RichText 标签 ``<right>  Ulula  </right>``
被当成插件标签，去掉标签后只剩 ``Ulula``（没有句号），
被判成"标签名本身就是内容"。真实游戏里 7034 个说话人
**一句话都翻不了，而且不报错**。

所以 `_is_translatable` 必须带上 `field`。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.engines.rpgmaker import PLUGIN_TAG_RE, RpgMakerAdapter  # noqa: E402

# ----------------------------------------------------------------------
# 一、标签正则本身：**标签名里可以带空格**
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "tag",
    [
        "<Crafting Ingredients>",
        "<JS Crafting Effect>",
        "<Crafting Show All Switches: 127,152>",
        "<PassiveSkill:5>",
        "<Max: 1>",
        "<onEquip: 95>",
    ],
)
def test_tag_regex_matches_tags_with_spaces(tag: str) -> None:
    """真实插件写了带空格的标签名。

    原先的正则 ``<[A-Za-z_]…>`` 不允许空格，于是这些标签
    **一个都没匹配上**，整个标签连同配置都被当成正文送进翻译 ——
    这正是插件配置被翻坏、模型吐出 ``⟦0⟧`` 的直接原因。
    """
    assert PLUGIN_TAG_RE.search(tag), f"{tag!r} 没匹配上"


def test_regex_matches_inside_a_full_note() -> None:
    note = (
        "<Crafting Ingredients>\n Item 60: 3\n"
        "<Crafting Show All Switches: 127,152>\n</Crafting Ingredients>\n"
        "<JS Crafting Effect>\n$gameVariables.setValue(77);\n</JS Crafting Effect>"
    )
    found = [m.group(0) for m in PLUGIN_TAG_RE.finditer(note)]
    assert "<Crafting Ingredients>" in found
    assert "<JS Crafting Effect>" in found
    assert "<Crafting Show All Switches: 127,152>" in found


# ----------------------------------------------------------------------
# 二、note 里的插件配置必须被拒
# ----------------------------------------------------------------------

REAL_PLUGIN_NOTES = [
    # 真实数据（BeyondPortal），带 JS 和一堆标签
    "<Crafting Ingredients>\n Item 60: 3\n Item 52: 2\n"
    "<Crafting Show All Switches: 127,152>\n</Crafting Ingredients>\n"
    "<JS Crafting Effect>\n"
    "$gameVariables.setValue(77, $gameVariables.value(77) - 3*arguments[1]);\n"
    "</JS Crafting Effect>\n<Max: 1>\n<Crafting Turn Off Switch: 152>",
    # 只有标签
    "<PassiveSkill:5>",
    "<CustomEffect:heal:500>",
    "<Crafting Ingredients>",
    "<Max: 1>\n<Crafting Turn Off Switch: 152>",
    # 标签 + 配置行
    "<Crafting Ingredients>\n Item 49: 1",
    # JS 但没标签
    "<JS X>\nfunction foo(){ return 1; }\n</JS X>",
]


@pytest.mark.parametrize("note", REAL_PLUGIN_NOTES)
def test_plugin_config_notes_are_not_translatable(note: str) -> None:
    assert not RpgMakerAdapter._is_translatable(note, "note"), (
        f"插件配置被当成正文了：{note[:60]!r}"
    )


# ----------------------------------------------------------------------
# 三、note 里真正的散文要**保留**
# ----------------------------------------------------------------------

REAL_PROSE_NOTES = [
    "Skill #1 corresponds to the Attack command.\n",
    "Skill #2 corresponds to the Guard command.\n",
    "State #1 will be added when HP hits 0.",
    "Below are spaces for when you would like",
    # 短句也是句子：2 个词但有句号。用"词数"判会把它误杀
    "\\C[29]Recupera salud.\\C[0] <Max: 1>",
]


@pytest.mark.parametrize("note", REAL_PROSE_NOTES)
def test_prose_notes_are_still_translatable(note: str) -> None:
    assert RpgMakerAdapter._is_translatable(note, "note"), (
        f"正常的开发者注释被误杀了：{note!r}"
    )


def test_short_sentence_with_period_is_not_config() -> None:
    """**回归**：用"≥4 个词"判句子时，`'Recupera salud.'`（2 个词）
    被误判成配置行。而 `'Crafting Ingredients'`（同样 2 个词）
    确实只是标签名 —— 词数分不开，**标点可以**。"""
    assert RpgMakerAdapter._is_translatable("\\C[29]Recupera salud.\\C[0] <Max: 1>", "note")
    assert not RpgMakerAdapter._is_translatable("<Crafting Ingredients>", "note")


# ----------------------------------------------------------------------
# 四、**字段语义**：同一个标签在 name 里是排版指令，在 note 里是配置
# ----------------------------------------------------------------------

RICH_TEXT_NAMES = [
    "<right>  Ulula  </right>",
    "<right>Naho</right>",
    "<left>  Ulula  </left>",
    "<center>Ulula</center>",
]


@pytest.mark.parametrize("name", RICH_TEXT_NAMES)
def test_richtext_names_are_translatable(name: str) -> None:
    """**最重要的回归 —— 7034 个说话人一句话都翻不了的那次。**

    `<right>` 是**排版指令**，名字本身必须翻。
    把"插件配置"规则用到所有字段上就会把它们全杀掉。
    """
    assert RpgMakerAdapter._is_translatable(name, "name"), (
        f"RichText 说话人名被误杀了：{name!r}"
    )


def test_richtext_name_without_field_arg_is_still_ok() -> None:
    """不传 field 时默认按"非 note"处理，不能顺手杀名字。"""
    assert RpgMakerAdapter._is_translatable("<right>  Ulula  </right>")


def test_field_scoping_is_the_actual_mechanism() -> None:
    """同一个字符串，两种字段，两种结论 —— 这是本 bug 的核心。

    如果哪天有人把 `field == "note"` 这个限定条件去掉，
    这个测试会立刻失败。
    """
    rich = "<right>  Ulula  </right>"
    assert RpgMakerAdapter._is_translatable(rich, "name"), "name 字段必须翻"
    # 而 note 字段里这个形状（标签 + 无标点短内容）会被判成配置
    assert not RpgMakerAdapter._is_translatable(rich, "note"), (
        "note 字段里的标签+无标点内容应视为插件参数"
    )


# ----------------------------------------------------------------------
# 五、别把原有规则弄坏
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["<PassiveSkill:5>", "<CustomEffect:heal:500>", "", "   ", "12345", "...", "\\C[1]"],
)
def test_still_rejects_non_text(text: str) -> None:
    assert not RpgMakerAdapter._is_translatable(text, "name")


@pytest.mark.parametrize(
    "text",
    [
        "Attack",
        "恢复生命值",
        "A long developer note explaining how this state works.",
        "<PassiveSkill:5> Increases attack by 50%.",
        "\\C[29]Recupera salud.\\C[0]",
    ],
)
def test_still_accepts_real_text(text: str) -> None:
    assert RpgMakerAdapter._is_translatable(text, "name") or RpgMakerAdapter._is_translatable(
        text, "note"
    )
