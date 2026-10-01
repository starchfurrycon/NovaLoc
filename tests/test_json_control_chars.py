"""脱缰控制字符的修补（`_escape_raw_control_chars`）。

## 为什么需要这一级

JSON 规范不允许字符串字面量里出现裸控制字符，而模型经常吐出来。
实测（ElfLifia 批 28）模型**回吐提示词**时的形态：

    {"0": "获得 1 个『额外抽牌』。\\n『额外抽牌』：…」} ⟦0⟧"<裸换行>
        <裸换行>    <裸换行>    …确保输出的"}

## 这一级的收益要说清楚

它**不是**为了救回那条译文 —— 那条内容是回吐的提示词，
护栏（`hint_echo`）本来就该拦掉。收益是让**解析能走完**，
于是护栏拿到文本、给出真正的原因（`hint_echo`），
而不是一个"解析不了"的笼统错误，让人查错方向被带偏。

## 判据必须两边都测

* 字符串**内**的裸换行 → 必须转义（否则解析失败）；
* 结构性的换行（对象/数组之间）→ 必须**保持原样**
  （它是合法空白，动了会把结构改坏）。

只测前者会放过"把结构换行也转义掉"的坏实现。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate.json_parse import (  # noqa: E402
    _escape_raw_control_chars,
    parse_json_loose,
    parse_translations,
)


def test_raw_newline_inside_string_is_escaped() -> None:
    """字符串里的裸换行必须转义，转义后能解析出完整内容。"""
    raw = '{"0": "第一行\n第二行"}'
    fixed = _escape_raw_control_chars(raw)
    assert "\n" not in fixed, f"还有裸换行：{fixed!r}"
    assert "\\n" in fixed
    res = parse_json_loose(raw)
    assert res.ok, f"修补后应能解析：{res.notes}"
    assert res.value == {"0": "第一行\n第二行"}, res.value


def test_structural_newlines_are_preserved() -> None:
    """★ 结构换行是合法空白，**不许**动。"""
    raw = '{\n  "0": "甲",\n  "1": "乙"\n}'
    assert _escape_raw_control_chars(raw) == raw, "结构换行被改坏了"


def test_tab_and_cr_inside_string() -> None:
    raw = '{"0": "甲\t乙\r丙"}'
    fixed = _escape_raw_control_chars(raw)
    assert "\t" not in fixed and "\r" not in fixed
    assert parse_json_loose(raw).ok


def test_escaped_quote_does_not_end_the_string() -> None:
    r"""`\"` 是转义引号，**不算**字符串结束。

    这条是状态机的关键分支：漏了它，`"a\"b\nc"` 里的换行
    会被当成结构换行而漏掉转义。
    """
    raw = '{"0": "a\\"b\nc"}'
    fixed = _escape_raw_control_chars(raw)
    assert "\n" not in fixed, f"漏掉了转义引号之后的裸换行：{fixed!r}"


def test_trailing_backslash_does_not_crash() -> None:
    """结尾处有个孤立反斜杠时不许炸（模型截断很常见）。"""
    for raw in ('{"0": "甲\\', '{"0": "甲', '"'):
        _escape_raw_control_chars(raw)  # 不抛异常即通过


def test_normal_json_is_untouched() -> None:
    raw = '{"0": "甲", "1": "乙"}'
    assert _escape_raw_control_chars(raw) == raw


def test_echoed_prompt_is_still_rejected_not_silently_accepted() -> None:
    """★ 修补**不能**把回吐的提示词变成"可用译文"。

    真实形态：译文后面接了一串提示词规则。修补后解析可能成功，
    但内容里含大量提示词措辞 —— 这一步只保证"能被看到"，
    拦截由护栏负责（`tests/test_hint_echo.py`）。
    这里断言的是：**不会**把它当成一条干净译文返回。
    """
    raw = (
        '{"0": "获得 1 个『额外抽牌』。』} ⟦0⟧ 保持原文格式和数值。'
        ' 调整句子结构以更符合中文表达习惯。 确保所有信息都包含在译文中。'
        ' 仅输出 JSON 格式。 避免任何额外的解释或说明。"}'
    )
    m, res = parse_translations(raw, expect_indices=[0])
    # 解析成功也行、失败也行，但**绝不能**把这段提示词当正常译文
    if res.ok and m:
        text = m.get(0) or ""
        assert "确保所有信息都包含在译文中" in text  # 内容确实是回吐的
        # 交给护栏：只要它含提示词措辞，就必须被拦
        from novaloc.translate.guards import guard

        g = guard(
            "Gain 1 「Extra Draw」.",
            text,
            max_chars=None,
            length_ratio=3.0,
            allow_untranslated=False,
            target_lang="zh-Hans",
            hints=None,
            check_repeat=False,
        )
        assert g.fatal or g.warnings, "回吐提示词的内容必须被护栏标记"
