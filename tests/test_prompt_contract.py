r"""提示词里那几条"**说不出口就会出事故**"的规则。

## 为什么提示词也值得写测试

提示词不是"文案"，它是**程序的一部分** —— 每一条规则都对应一个
真实踩过的坑。删掉一条规则，本机上什么也不会报错，
只有跑到真实游戏上才会出现"某类文本又开始坏了"。

而且提示词很容易在"顺手精简一下"的时候被删掉：
它看起来像散文，diff 里人眼很容易滑过去。

所以这里把"每条规则对应哪个坑"钉下来。
下面的断言全部**针对语义**（关键词必须出现），
不比对整段文本 —— 那样每改一次措辞都要改测试，反而会让人
把测试删掉。这样测：
**规则被删会红，措辞调整不会红。**

## 各条规则对应的真实事故

* 规则 2（`⟦n⟧` 原样保留）→ 变量/颜色被模型"翻译"掉，
  游戏里显示错的名字或直接丢变量。
* 规则 4（全角标点）→ 玩家看到中英标点混排。
* 规则 7（不要漏译）→ 单个词/标点的条目被跳过。
* 规则 8（**逐行保留换行**）→ 见下。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate import prompts  # noqa: E402


def test_system_prompt_requires_line_preservation() -> None:
    r"""**核心**：提示词必须明确要求"逐行保留换行 + 插件标签原样保留"。

    ## 事故：模型把多行原文截断成一行

    实测（可稳定复现）对**多行**原文，模型会只翻第一行就把后面全丢掉：

        源: 'Un escudo básico… <Max: 1>\nArte: Bloqueo Débil, \C[29]…'
        出: '一件基本的、由坚韧木材制成的轻型盾牌。'      ← 第二行没了

        '<Custom Action Sequence>\n<Cooldown: 5>\n<All AI Conditions>\n…'
        出: '目标生命值低于 30%'                          ← 只剩最后一行

    这不是"占位符丢了"，而是**整行内容丢了**。
    占位符校验只是**碰巧**报出来的症状（被丢掉的行里正好有
    `\C[n]` / `<Tag>`）。真正的损失是"玩家看到的物品说明少了一半"。

    ## 为什么靠"补记号的机制"救不回来

    `repair_dropped_masks` 能把**记号**补回去，但**补不回丢掉的文字** ——
    被丢的那行里往往还有实义内容（`Arte: Bloqueo Débil`）。
    所以这必须从**提示词**这一层解决：把"换行是有意义的结构"说出口。

    加上规则 8 之后实测：9 条多行失败里 2 条被完整救回，
    其余条目的**内容完整度也明显提高**（行数从 1 行涨到 3～6 行）。
    """
    p = prompts.SYSTEM_PROMPT
    # 必须点明"换行是有意义的结构"，而不是只说"不要漏译"
    assert "换行" in p, "提示词里必须提到换行"
    assert "逐行" in p or "几个换行" in p, "必须要求逐行对应"
    # 必须点名插件标签/脚本行这类"看着像噪音、其实不能动"的内容
    assert "<" in p and "插件" in p or "配置" in p, (
        "必须说明 <...> 插件标签/配置行不能翻译、不能省略"
    )
    # 必须明确禁止"只翻第一行"这个具体行为
    assert "第一行" in p, "必须明确禁止'只翻第一行'"


def test_system_prompt_keeps_the_older_hard_won_rules() -> None:
    """早先"用真实事故换来的"规则不能被顺手删掉。

    每一条都有对应的历史事故，删掉就会以另一种形式复发。
    """
    p = prompts.SYSTEM_PROMPT
    checks = [
        ("⟦", "占位符记号原样保留（规则 2）"),
        ("JSON", "只输出 JSON（规则 1）"),
        ("术语表", "术语表优先（规则 3）"),
        ("全角", "中文标点用全角（规则 4）"),
        ("复读", "不要复读（规则 5）"),
        ("不要漏译", "不要漏译（规则 7）"),
    ]
    missing = [why for token, why in checks if token not in p]
    assert not missing, f"这些规则不见了：{missing}"


def test_single_user_prompt_passes_multiline_through_untouched() -> None:
    r"""单条提示词必须把多行原文**原样**塞进去。

    如果构造提示词时把换行折叠成空格，规则 8 就变成了一句空话 ——
    模型根本看不到行结构。这条测的是"规则 8 有没有可执行的前提"。
    """
    src = "第一行\n<Cooldown: 5>\ntarget hp% <= 0.30"
    u = prompts.build_single_user_prompt(src, kind="note")
    assert u.count("\n") >= 3, f"换行被吃掉了：{u!r}"
    assert "<Cooldown: 5>" in u
    assert "target hp% <= 0.30" in u


@pytest.mark.parametrize("kind", ["dialogue", "item_desc", "note", "menu", None])
def test_single_user_prompt_survives_all_kinds(kind: str | None) -> None:
    """各种 `kind` 都不能把原文弄坏（`kind=None` 是真实存在的取值）。"""
    src = "Hola\n\\C[29]mundo\\C[0]"
    u = prompts.build_single_user_prompt(src, kind=kind)
    assert "Hola" in u and "\\C[29]" in u and "mundo" in u
