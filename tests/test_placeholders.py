"""验证占位符屏蔽：必须覆盖真实游戏文本里出现的各种记号。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate import placeholders as ph  # noqa: E402

CASES = [
    # (说明, 原文, 模拟模型返回的"坏"译文)
    ("RPG Maker 转义", r"勇者\V[1]获得了\C[3]圣剑\C[0]！", r"勇者⟦0⟧获得了⟦1⟧圣剑⟦2⟧！"),
    ("RPG Maker 换行", r"第一行\n第二行", r"第一行⟦0⟧第二行"),
    ("Ren'Py 标签", "Hello {color=#ff0000}world{/color}!", "你好{color=#ff0000}世界{/color}！"),
    ("Ren'Py 变量", "You have [gold] coins.", "你有 [gold] 枚金币。"),
    ("富文本", "<b>Warning</b>: <size=20>low HP</size>", "<b>警告</b>：<size=20>血量低</size>"),
    ("printf", "Dealt %d damage to %s.", "对 %s 造成了 %d 点伤害。"),
    ("printf 位置", "%1$s attacked %2$s.", "%1$s 攻击了 %2$s。"),
    ("C# 格式化", "Level {0}: {1} points", "等级 {0}：{1} 点"),
    ("Python 模板", "Hello ${user_name}, you have {count} items.",
     "你好 ${user_name}，你有 {count} 件物品。"),
    ("HTML 实体", "Tom &amp; Jerry&nbsp;Show", "汤姆 &amp; 杰瑞&nbsp;节目"),
    ("混合", r"\V[1]：<color=#0f0>HP %d</color>\nNext: {0}",
     r"\V[1]：<color=#0f0>生命 %d</color>\n下一项：{0}"),
    ("纯自然语言（不该屏蔽）", "Hello world, how are you?", "你好世界，你好吗？"),
    ("单个百分号（不该屏蔽）", "100% complete", "完成度 100%"),
    ("百分号后带空格（不该屏蔽）", "Discount 50% off today", "今日五折 50% off"),
    ("浮点格式（该屏蔽）", "Accuracy: %.2f%%", "命中率：⟦0⟧⟦1⟧"),
    ("代码反引号", "Press `F1` for help.", "按 `F1` 查看帮助。"),
]

# 阶段 2 的用例：模型改坏占位符的各种方式
BROKEN = [
    ("正常：模型原样保留记号", "勇者⟦0⟧获得圣剑⟦1⟧", True),
    ("模型吞掉一个记号", "勇者获得圣剑⟦1⟧", False),
    ("模型改写了记号内部数字", "勇者⟦X⟧获得圣剑⟦1⟧", False),
    ("模型凭空多造一个记号", "勇者⟦0⟧获得⟦1⟧圣剑⟦9⟧", False),
    # 注意：模型把记号还原成了**正确**的原始占位符，这实际上是"帮了忙"，
    # 多重集一致，不应判为致命。真正致命的是数量/内容不符。
    ("模型自己写回了原始占位符（数量正确）", r"勇者\V[1]获得圣剑\C[3]", True),
    ("模型只写回一个原始占位符", r"勇者\V[1]获得圣剑", False),
]

SRC2 = r"勇者\V[1]获得圣剑\C[3]"

# 阶段 3b：配对标签顺序
SWAP_CASES = [
    ("正常", "<color=#f00>Warning!</color>", "⟦0⟧警告！⟦1⟧"),
    ("标签顺序被交换", "<color=#f00>Warning!</color>", "⟦1⟧警告！⟦0⟧"),
    ("printf 变量被交换", "%1$s attacked %2$s.", "⟦1⟧ 攻击了 ⟦0⟧"),
]

# 阶段 4：全角化与原文泄漏
PUNCT_CASES = [
    ("Hello, world!", "你好, 世界!"),      # 半角标点
    ("Hello, world!", "你好，世界！"),      # 正确全角
    ("Press the START button", "Press the START button"),  # 完全没翻译
    ("Press the START button", "按下 START 按钮"),  # 局部借用（正常）
]


def main() -> int:
    print("=" * 82)
    print("阶段 1：屏蔽正确性（占位符必须全部被 ⟦i⟧ 取代，自然语言不受影响）")
    print("=" * 82)
    allok = True
    for label, src, _fake in CASES:
        r = ph.mask(src)
        print(f"\n[{label}]")
        print(f"  原文   {src!r}")
        print(f"  屏蔽后 {r.text!r}")
        print(f"  槽位   {r.slots}")
        # 还原必须幂等于原文
        back = ph.unmask(r.text, r.slots)
        same = back == src
        allok &= same
        print(f"  往返还原 {'✅ 一致' if same else '❌ 不一致 → ' + repr(back)}")

    print()
    print("=" * 82)
    print("阶段 2：还原 + 校验（模拟模型改坏占位符的几种方式）")
    print("=" * 82)

    src = SRC2
    r = ph.mask(src)
    print(f"\n原文 {src!r} → 屏蔽 {r.text!r}  槽位 {r.slots}\n")
    for label, model_out, expect_ok in BROKEN:
        restored, check = ph.verify_restored(src, model_out, r.slots, masked_source=r.text)
        got_ok = not check.fatal
        flag = "✅" if got_ok == expect_ok else "❌ 判定错误"
        allok &= got_ok == expect_ok
        print(f"  {flag} [{label}]")
        print(f"       模型输出 {model_out!r}")
        print(f"       还原后   {restored!r}   → {'通过' if got_ok else '判为致命'}")
        print(f"       {check.describe()}")

    print()
    print("=" * 82)
    print("阶段 3：跨条目索引泄漏（每条目的占位符编号是**局部**的）")
    print("=" * 82)
    batch = ["Alpha ⟦0⟧ beta", "Gamma"]
    masked, slots_per = ph.mask_batch(batch)
    print(f"  批量屏蔽：{masked}")
    print(f"  每条槽位数：{[len(s) for s in slots_per]}")
    # 第 2 条只应有 ⟦0⟧；这里故意喂一个 ⟦3⟧（属于别的条目）
    leaked = ["⟦0⟧ 阿尔法", "⟦3⟧ 伽马"]
    bad = ph._detect_cross_item_leak(masked, leaked)
    print(f"    检出越界记号：{bad}" + ("  ✅" if bad else "  ❌ 应当检出"))
    for i, (orig, m_, s, out) in enumerate(
        zip(batch, masked, slots_per, leaked, strict=False)
    ):
        restored, check = ph.verify_restored(orig, out, s, masked_source=m_)
        status = "✅ 通过" if not check.fatal else f"❌ 致命：{check.describe()}"
        print(f"    [{i}] {status}  还原={restored!r}")

    print()
    print("=" * 82)
    print("阶段 3b：配对标签顺序被打乱（沉默损坏）必须拦掉")
    print("=" * 82)
    for label, src_, out_ in SWAP_CASES:
        m_ = ph.mask(src_)
        restored, check = ph.verify_restored(src_, out_, m_.slots, masked_source=m_.text)
        want_fatal = label != "正常"
        ok = check.fatal == want_fatal
        allok &= ok
        print(f"  {'✅' if ok else '❌'} [{label}] 还原={restored!r}")
        print(f"        {check.describe()}")

    print()
    print("=" * 82)
    print("阶段 4：全角化与原文泄漏检测")
    print("=" * 82)
    for src_, tgt_ in PUNCT_CASES:
        issues = ph.check_punctuation_style(tgt_)
        leaks = ph.has_source_leak(src_, tgt_)
        print(f"  {src_!r} → {tgt_!r}")
        print(f"     标点问题 {issues or '无'}   原文泄漏 {leaks or '无'}")

    print()
    print("=" * 82)
    print("结论：" + ("✅ 占位符子系统全部通过" if allok else "❌ 存在问题"))
    return 0 if allok else 1


def test_suite() -> None:
    """pytest 入口：跑一遍完整报告并断言全通过。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
