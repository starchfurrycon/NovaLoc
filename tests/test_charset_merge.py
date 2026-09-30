"""字符集规划 + 多字体合并 的端到端验证。

这是"防口口口"契约的验收测试：
  1) 规划器必须给出完整的覆盖链，否则硬失败；
  2) 按链合并后，产物必须通过不变量质检（无缺码点/无空白/无豆腐块）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.fonts import charset as cs  # noqa: E402
from novaloc.fonts import merge as mg  # noqa: E402
from novaloc.fonts import qa  # noqa: E402

FONTS = Path(r"C:\Windows\Fonts")
DL = FIXTURES / "fonts"
OUT = FIXTURES / "out3"
OUT.mkdir(parents=True, exist_ok=True)

# 模拟一份真实译文（含 UI 标签、对白、符号、数值）
TRANSLATED = [
    "勇者啊，你终于醒了。这里是艾尔登村，魔王军三天前攻陷了北面的要塞。",
    "你确定要使用「圣光斩」吗？",
    "物品：回复药水 ×3、魔力结晶、古代符文碎片、贤者之石。",
    "攻击力 +15%　防御力 -8%　暴击率 5.5%　移动速度 120",
    "系统提示：存档失败，请检查磁盘空间。",
    "★传说级★　♪背景音乐已关闭♪　任务完成 ✓　失败 ✖　生命值 ♥♥♥",
    "①②③ Ⅰ Ⅱ Ⅲ 　← 返回　→ 确认　↑↓ 选择",
    "■ 已装备　□ 未装备　▲ 上等　▼ 下等　◆ 稀有",
]


def main() -> int:
    print("=" * 80)
    print("步骤 1：构建项目字符集")
    print("=" * 80)
    required = cs.build_required_charset(TRANSLATED)
    print(f"  译文 {len(TRANSLATED)} 条 → 需要渲染 {len(required)} 个不同字符")
    print(f"  其中界面安全字符 {len(set(cs.UI_SAFE_CHARS))} 个")
    print()

    print("=" * 80)
    print("步骤 2：为「Arial + 中文」规划覆盖链（基座 = 游戏原字体 arial.ttf）")
    print("=" * 80)
    base = FONTS / "arial.ttf"
    candidates = cs.default_supplement_candidates(
        bundled_dir=DL, extra_dirs=[FONTS]
    )
    # 把 OFL 可分发字体排在前面（风格统一 + 许可干净），系统字体兜底
    prio = [
        "LXGWNeoXiHei.ttf",
        "LXGWWenKaiLite-Regular.ttf",
        "simhei.ttf",
        "Deng.ttf",
        "msyh.ttc",
        "simsun.ttc",
        "seguisym.ttf",
    ]
    plan = cs.plan_charset(required, base, candidates, prioritize=prio)
    print(plan.summary())
    print()
    print(f"  规划覆盖率 = {plan.coverage:.4%}   可完整覆盖 = {plan.ok}")
    print()

    if not plan.ok:
        print("  ⚠️ 规划阶段就缺字，硬失败逻辑会被触发：")
        try:
            cs.assert_plannable(plan)
        except cs.CharsetCoverageError as exc:
            print(f"     {str(exc).splitlines()[0][:160]}")

    print()
    print("=" * 80)
    print("步骤 3：按规划链执行多字体合并")
    print("=" * 80)
    out = OUT / "arial_cn.ttf"
    rep = mg.merge_fonts_multi(base, plan.source_paths, out, required)
    print(f"  {rep.summary}")
    for s in rep.sources_used:
        print(f"    + {Path(str(s['font'])).name:<28} {s['glyphs']:>5} 字")
    print(f"  产物：{rep.out_path}  ({Path(str(rep.out_path)).stat().st_size / 1024:.0f} KB)")
    for w in rep.warnings:
        print(f"    ⚠️  {w}")
    print()

    print("=" * 80)
    print("步骤 4：不变量质检（缺码点 / 空白 / 豆腐块 / 字形互不相同）")
    print("=" * 80)
    if rep.out_path:
        r = qa.verify_font(rep.out_path, required)
        print(f"  {r.summary}")
        print(f"    检查字符数 {r.checked}")
        if r.missing:
            print(f"    ❌ 缺码点({len(r.missing)})：{''.join(r.missing[:80])}")
        if r.blank:
            print(f"    ❌ 渲染空白({len(r.blank)})：{''.join(r.blank[:80])}")
        if r.tofu:
            print(f"    ❌ 豆腐块({len(r.tofu)})：{''.join(r.tofu[:80])}")
        for w in r.warnings:
            print(f"    ⚠️  {w}")
        print()
        print(f"    垂直度量：{r.metrics}")
        print()
        print("  用产物实际渲染一段中文（确认能出图）：")
        try:
            from PIL import Image, ImageDraw, ImageFont

            img = Image.new("RGB", (900, 220), (18, 22, 30))
            d = ImageDraw.Draw(img)
            f1 = ImageFont.truetype(str(rep.out_path), 22)
            f2 = ImageFont.truetype(str(rep.out_path), 16)
            for i, t in enumerate(TRANSLATED[:3]):
                d.text((12, 10 + i * 28), t, font=f1, fill=(230, 240, 255))
            for i, t in enumerate(TRANSLATED[5:8]):
                d.text((12, 110 + i * 24), t, font=f2, fill=(160, 220, 255))
            p = OUT / "preview_full.png"
            img.save(p)
            ink = sum(1 for px in img.getdata() if px[0] > 60 or px[1] > 60)
            print(f"    ✅ 渲染成功 → {p}  ({ink} 个非背景像素)")
        except Exception as exc:  # noqa: BLE001
            print(f"    ❌ 渲染失败：{exc}")
            return 1

        ok = r.ok and bool(rep.ok)
    else:
        ok = False

    print()
    print("=" * 80)
    print("步骤 5：验证「硬失败」逻辑")
    print("=" * 80)
    # 先确认候选里到底覆盖哪些 emoji，再用一个真正没人覆盖的码点
    probe = "\U0001F600\U0001F3AE\u16A0\u16A1\u16A2"  # emoji + 卢恩字母
    uncovered = []
    for ch in probe:
        covered = any(
            (lambda i: i is not None and i.has_char(ch))(__import__(
                "novaloc.fonts.coverage", fromlist=["load_font_info"]
            ).load_font_info(p))
            for p in candidates
        )
        if not covered:
            uncovered.append(ch)
    print(f"  候选字体覆盖情况：{[(c, c not in uncovered) for c in probe]}")
    if not uncovered:
        print("  候选字体意外覆盖了全部探测字符，改用合成码点 U+E0100")
        uncovered = ["\U000E0100"]

    bogus = required + "".join(uncovered)
    p2 = cs.plan_charset(bogus, base, candidates, prioritize=prio)
    print(f"  仍缺 {len(p2.still_missing)} 字：{''.join(p2.still_missing[:20])!r}")
    try:
        cs.assert_plannable(p2)
        print("  ❌ 不该通过")
        ok = False
    except cs.CharsetCoverageError as exc:
        print(f"  ✅ 正确硬失败：{str(exc).splitlines()[0][:130]}")

    print()
    print("=" * 80)
    print("结论：" + ("✅ 字符集规划 + 多字体合并 + 质检 全部通过" if ok else "❌ 存在问题"))
    return 0 if ok else 1


def test_suite() -> None:
    """pytest 入口：跑一遍完整报告并断言全通过。"""
    assert main() == 0

pytestmark = [
    pytest.mark.needs_fonts,
]


if __name__ == "__main__":
    raise SystemExit(main())
