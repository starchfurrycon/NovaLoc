r"""**判据只有一份定义** —— 三个执行点必须都走它。

## 事故：三个执行点各写各的，坏数据原地打转

"这条译文能不能写回游戏"这个判断，在代码里出现于**三处**：

1. `translate` 的强制重译判定（"已完成"也要能被推翻）；
2. `translate` 开头的重查入口（`revalidate_foreign_script`，作废旧译文）；
3. `apply` 的回写闸门（最后一道）。

`%n` 消息变量的判据上线时，**只加进了第 2 处**。后果：

* 第 2 处正确地把 120 条丢变量的译文作废（清空译文）；
* 重译后模型又给出同样丢变量的答案（模型能力问题，实测约 60% 能保住）；
* 这条答案因为**非空**被第 1、3 处放行，写成"已翻译"；
* 下一轮回到第 2 处 —— **每轮空跑一次作废，坏数据一条没少。**

## 为什么用源码扫描来钉，而不是只测行为

行为测试只能证明"我测的那条路径一致"。而这类 bug 的本质是
**又有人加判据时只改一处** —— 那需要检查"还有没有别的地方
在裸调这两个函数"。所以这里：

1. 行为测试：`is_unsafe_writeback` 对两类坏译文都返回 True；
2. **结构测试**：`stages.py` 里不许再出现裸的
   `check_foreign_script(` / `check_percent_vars(` 组合调用 ——
   出现就说明有人绕过了统一入口。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate.guards import (  # noqa: E402
    check_foreign_script,
    check_percent_vars,
    is_unsafe_writeback,
)

STAGES = ROOT / "src" / "novaloc" / "pipeline" / "stages.py"


# ---------------------------------------------------------------------------
# 一、行为：统一入口对两类坏译文都必须拦
# ---------------------------------------------------------------------------


def test_catches_missing_message_var() -> None:
    """丢 `%1` 必须拦（189/357 条真实事故的那一类）。"""
    assert is_unsafe_writeback("%1 attacks!", "攻击！")


def test_catches_foreign_script() -> None:
    """混进别的文字系统必须拦（此前 12 条乱码句的那一类）。"""
    # 阿拉伯文
    assert is_unsafe_writeback("Hello", "مرحبا")


def test_accepts_good_translation() -> None:
    """好译文必须放行 —— 否则这个门禁等于把流水线关掉。"""
    assert not is_unsafe_writeback("%1 attacks!", "%1 攻击！")
    assert not is_unsafe_writeback("New Game", "新游戏")
    assert not is_unsafe_writeback("Deals 100% damage", "造成 100% 伤害")


def test_agrees_with_the_two_underlying_criteria() -> None:
    """统一入口必须**等价于**那两个判据的"或"。

    这条防的是"有人往 `is_unsafe_writeback` 里加了新判据，
    但三个执行点里有两个还在用它旧的语义"——
    只要它是这两个判据的或，就不会出现分歧。
    """
    cases = [
        ("%1 attacks!", "攻击！"),
        ("%1 attacks!", "%1 攻击！"),
        ("%1 casts %2!", "%1 施法！"),
        ("Hello", "مرحبا"),
        ("New Game", "新游戏"),
        ("Deals 100% damage", "造成 100% 伤害"),
        ("%1 guards.", "守卫"),
    ]
    for src, tgt in cases:
        expected = bool(
            check_foreign_script(tgt, source=src) or check_percent_vars(src, tgt)
        )
        assert is_unsafe_writeback(src, tgt) == expected, f"{src!r} → {tgt!r} 不一致"


# ---------------------------------------------------------------------------
# 二、结构：不许有执行点绕过统一入口
# ---------------------------------------------------------------------------


def test_no_call_site_bypasses_the_single_definition() -> None:
    r"""★ 核心：`stages.py` 里不许再裸调那两个判据的组合。

    ## 怎么判"裸调"

    允许的形态只有三种：

    * `is_unsafe_writeback(...)` —— 统一入口；
    * 为了**分开计数**而单独调 `check_foreign_script(...)` 判断"是哪一类"
      （`revalidate_foreign_script` 里那个用法，出现在
      `is_unsafe_writeback` 判定为 True **之后**）；
    * import 行。

    不允许的是**把两者用 `or` 连起来**当门槛 —— 那正是三处分歧的来源。

    实现上不靠正则猜语义，而是数"出现在 `or` 表达式里的组合"。
    """
    text = STAGES.read_text(encoding="utf-8")
    # 去掉 import 行，避免误判
    body = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith(("import ", "from "))
    )

    # 形态一：check_foreign_script(...) or check_percent_vars(...)
    # 形态二：check_foreign_script(...) `or (` 换行后 check_percent_vars
    patterns = [
        r"check_foreign_script\([^)]*\)\s*or\s*check_percent_vars",
        r"check_foreign_script\([^)]*\)\s*or\s*\(\s*\n\s*check_percent_vars",
        r"check_percent_vars\([^)]*\)\s*or\s*check_foreign_script",
    ]
    found: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, body):
            line = body[: m.start()].count("\n") + 1
            found.append(f"第 {line} 行：{m.group(0)[:70]!r}")

    assert not found, (
        "有执行点绕过了统一入口 `guards.is_unsafe_writeback`，"
        "而是自己把两个判据用 `or` 拼起来 ——\n"
        "这正是曾经让 120 条坏译文原地打转的原因。\n"
        "请改用 `is_unsafe_writeback(source, target)`。\n"
        + "\n".join(found)
    )


def test_single_definition_is_used_at_least_twice() -> None:
    """反过来：统一入口必须**真的被用**（防止它变成没人调的装饰）。"""
    body = STAGES.read_text(encoding="utf-8")
    n = len(re.findall(r"is_unsafe_writeback\(", body))
    assert n >= 3, (
        f"`is_unsafe_writeback` 只被调了 {n} 次，"
        "但已知有三个执行点（强制重译 / 重查入口 / apply 闸门）；"
        "少一处就说明有人又绕过去了"
    )
