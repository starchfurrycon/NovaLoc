"""用**真实中文字体**验证"游戏内不出口口口"的硬保证。

前面的字体测试都用 fontTools 合成的迷你字体，只能验证流程通不通。
这个测试用真实的微软雅黑/黑体 + 霞鹜新晰黑，验证三件真事：

1. **单一字体确实覆盖不全** —— 实测系统字体缺哪些字符，证明
   多字体贪心合并是必需的而不是过度设计；
2. **合并后的字体逐个字符都可渲染** —— 这是"不出口口口"的定义，
   判据不能用 IoU（对合并字体无意义），必须是确定性不变量：
   cmap 里有这个码位、能渲染出墨迹、不是 .notdef、无重复 glyph id、
   FreeType 能加载；
3. **覆盖不了时硬失败** —— 绝不能交付一个缺字形的字体。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.fonts.service import FontService  # noqa: E402

SB = FIXTURES
checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))


# 项目真实需要渲染的字符集：简体常用汉字 + 中文标点 + 游戏常见符号。
# 这些符号是实测下来最容易缺的（↔↕♪♫✓✔✕✖❤ 没有一个系统字体全覆盖）。
REQUIRED = (
    "你好世界游戏开始设置退出继续加载存档读取"
    "攻击防御生命魔法金币道具装备技能任务地图"
    "勇者公主魔王村民商人铁匠药师士兵骑士法师"
    "确定取消返回上一页下一页是是否否保存删除"
    "，。！？：；、（）「」『』【】《》〈〉“”‘’…—～·"
    "←↑→↓↔↕♪♫✓✔✕✖❤°±×÷≤≥≠∞√∑∏∫∴∵≈①②③④⑤"
    "壹贰叁肆伍陆柒捌玖拾佰仟万亿"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    " !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
)


def ctx() -> Context:
    return Context(config=Config(), events=EventBus())


def font_paths() -> dict[str, Path]:
    out = {}
    for name, p in (
        ("simhei", Path(r"C:\Windows\Fonts\simhei.ttf")),
        ("msyh", Path(r"C:\Windows\Fonts\msyh.ttc")),
        ("deng", Path(r"C:\Windows\Fonts\Deng.ttf")),
        ("lxgw_neo", Path(r"D:\NovaLoc\fonts\cache\lxgw-neo-xihei.ttf")),
        ("lxgw_screen", Path(r"D:\NovaLoc\fonts\cache\lxgw-wenkai-screen.ttf")),
    ):
        if p.is_file():
            out[name] = p
    return out


def main() -> int:
    svc = FontService(ctx())
    fonts = font_paths()
    print(f"可用中文字体：{list(fonts)}\n")

    # ---------- 1. 单字体覆盖不全（这是整个字体子系统的存在理由） ----------
    print("=" * 78)
    print("[1] 单一字体的覆盖缺口（证明多字体合并是必需的）")
    print("=" * 78)
    audits = {}
    for name, p in fonts.items():
        a = svc.audit(p, REQUIRED)
        audits[name] = a
        missing = "".join(a.missing[:40])
        print(f"  {name:<12} 覆盖 {a.coverage * 100:6.2f}%  缺 {len(a.missing):>3} 字  {missing}")

    best_name = max(audits, key=lambda n: audits[n].coverage)
    best = audits[best_name]
    print(f"\n  最好的单字体：{best_name}（{best.coverage * 100:.2f}%）")

    check("存在覆盖不足的单字体（说明必须有合并机制）",
          any(a.coverage < 1.0 for a in audits.values()),
          str({n: round(a.coverage, 4) for n, a in audits.items()}))
    check("最好的单字体也覆盖不满（多字体合并是必需的，不是过度设计）",
          best.coverage < 1.0, f"{best_name}={best.coverage:.4f}")

    # 特定符号的缺口：这是实测结论，写死在测试里防止回归
    hard_symbols = "↔↕♪♫✓✔✕✖❤"
    for name, a in audits.items():
        miss = "".join(c for c in hard_symbols if c in a.missing)
        if miss:
            print(f"    {name} 缺这些难缠符号：{miss}")
    check("确实存在系统字体覆盖不到的游戏符号",
          any(any(c in a.missing for c in hard_symbols) for a in audits.values()),
          "没有任何字体缺符号，实测结论可能已过时")

    # ---------- 2. 字符集规划 + 合并 ----------
    print("\n" + "=" * 78)
    print("[2] 贪心合并：用最少字体补齐全部字符")
    print("=" * 78)
    base = fonts.get("simhei") or next(iter(fonts.values()))
    # 用**产品真实的候选池**，而不是自己挑几个字体。
    # 第一版手工传了 candidates=[其它中文字体]，把 seguisym.ttf 排除了，
    # 于是合并必然缺 ✔✕✖❤ —— 那是测试的错，不是产品的错。
    #
    # 但要**排除基准字体自身**：否则规划器会用基准字体"自己补自己"，
    # 规划器阶段就判定 100% 覆盖、直接早返回，根本不会走合并路径，
    # 也就测不到"合并后度量被写坏"这个真 bug。刻意制造一次真实合并。
    cands = [c for c in svc.supplement_candidates(base) if c != base]
    print(f"  候选池（{len(cands)} 个）：{[c.name for c in cands]}")
    has_sym = any(c.name.lower() == "seguisym.ttf" for c in cands)
    check("候选池里包含系统符号字体 seguisym.ttf（✔✕✖❤ 的唯一来源）",
          has_sym, str([c.name for c in cands]))
    check("候选池排除了基准字体自身（保证真的走合并路径）",
          all(c != base for c in cands), str([c.name for c in cands if c == base]))

    plan = svc.patch_font(base, REQUIRED, candidates=cands)
    print(f"  基准字体：{base.name}")
    print(f"  结果：{plan.summary}")
    if plan.error:
        print(f"  ⚠️ 合并报错：{plan.error}")
    check("合并成功（没有缺字）", plan.ok, plan.error)
    check("合并后覆盖率达标",
          plan.audit_after is not None and plan.audit_after.coverage >= 0.999,
          str(plan.audit_after.coverage if plan.audit_after else None))
    check("真的用到了补充字体（证明贪心合并起作用）",
          plan.plan is not None and len(plan.plan.sources) >= 1,
          str(len(plan.plan.sources) if plan.plan else None))
    if plan.plan:
        for s in plan.plan.sources:
            print(f"    + {s.path.name:<28} 补 {len(s.covers):>4} 字")
        # 补的应该是那些难缠符号，不该是汉字（汉字基准字体自己有）
        sym_sources = [s for s in plan.plan.sources
                       if s.path.name.lower() == "seguisym.ttf"]
        if sym_sources:
            covered = "".join(sym_sources[0].covers)
            print(f"    seguisym 补的字符：{covered}")

    out_font = plan.out_path
    check("产物字体文件已生成",
          out_font is not None and Path(out_font).is_file(), str(out_font))
    if out_font is None or not Path(out_font).is_file():
        print("\n❌ 没有产物字体，后续验证无法进行")
        return _report()

    # 关键回归：合并后的垂直度量必须还在**基准字体的坐标系**里。
    # 曾经的 bug：把补充字体（UPEM 2048）的 ascender 原样写进合并字体
    # （UPEM 256），比值 asc/upem 变成 8.63，Pillow 把文字排到 y=399，
    # 在 48px 的画布里完全画不出来 —— 游戏里表现为**文字彻底不可见**，
    # 比缺字形的口口口更难排查。
    _check_metrics(out_font, base)

    # ---------- 3. 确定性不变量：逐个字符都可渲染 ----------
    print("\n" + "=" * 78)
    print("[3] 合并字体的确定性验证（不是 IoU —— 那对合并字体无意义）")
    print("=" * 78)
    verify = _verify_font(out_font, REQUIRED)
    for item in verify:
        label, ok = item[0], item[1]
        detail = item[2] if len(item) > 2 else ""
        print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail and not ok else ""))
    check("合并字体通过全部确定性不变量", all(i[1] for i in verify),
          str([i[0] for i in verify if not i[1]]))

    # ---------- 4. 覆盖不了必须硬失败 ----------
    print("\n" + "=" * 78)
    print("[4] 覆盖不了时必须硬失败（绝不交付会出口口口的字体）")
    print("=" * 78)
    # 只给一个拉丁字体当候选，汉字必然补不上
    latin = Path(r"C:\Windows\Fonts\seguisym.ttf")
    impossible = REQUIRED + "𠀀𠀁𠀂𠀃"  # 扩展 B 区罕见字，任何常见字体都没有
    plan2 = svc.patch_font(base, impossible, candidates=[base] + ([latin] if latin.is_file() else []))
    print(f"  用不可能满足的字符集：{'ok' if plan2.ok else '拒绝'}")
    print(f"    error={plan2.error}")
    if plan2.audit_after:
        print(f"    覆盖 {plan2.audit_after.coverage * 100:.3f}%，"
              f"仍缺 {len(plan2.audit_after.missing)} 字")
        print(f"    {' '.join(plan2.audit_after.missing[:12])}")
    check("不可能满足时必须硬失败（不交付缺字体的字体）", not plan2.ok, str(plan2.ok))
    check("失败原因说清了缺哪些字符",
          "𠀀" in plan2.error or "扩展" in plan2.error or "无法" in plan2.error,
          plan2.error[:120])
    check("失败时不产出可用字体",
          not (plan2.out_path and Path(plan2.out_path).is_file()),
          str(plan2.out_path))
    # 回归：一次失败的合并**绝不能破坏**上一次成功的产物。
    # （产品行为正确：失败时 patch_font 不写 out_path。这里守住它。）
    still_ink = _render_check(out_font, "你好世界")
    print(f"    失败请求之后，上一次成功产物的墨迹：{still_ink:.4f}")
    check("失败的合并不会破坏上一次成功的产物", still_ink > 0.02, f"ink={still_ink:.4f}")

    # ---------- 5. 合并结果必须真的能渲染出汉字 ----------
    print("\n" + "=" * 78)
    print("[5] 渲染实拍：合并字体画出'你好世界'并检查墨迹")
    print("=" * 78)
    # 先确认文件本身是完好的（大小/可解析/字形数），把"文件坏了"和
    # "渲染调用方式不对"两种情况区分开
    st = out_font.stat()
    print(f"  产物：{out_font}")
    print(f"    大小={st.st_size} 字节  修改时间={st.st_mtime}")
    from fontTools.ttLib import TTFont as _TT

    _f = _TT(str(out_font))
    _cmap = _f.getBestCmap()
    print(f"    字形数={_f['maxp'].numGlyphs}  '你'={_cmap.get(0x4F60)!r}  "
          f"'好'={_cmap.get(0x597D)!r}")
    _g = _f["glyf"][_cmap[0x4F60]]
    print(f"    '你'的轮廓数={_g.numberOfContours} 度量={_f['hmtx'][_cmap[0x4F60]]}")
    _f.close()

    ink = _render_check(out_font, "你好世界")
    print(f"  用合并字体渲染'你好世界'：墨迹占比 {ink:.4f}")
    check("合并字体能渲染出真实墨迹（不是空白/方块）", ink > 0.02, f"ink={ink:.4f}")

    # 对比：原始基准字体渲染这几个字的样子（应该也有墨迹，作为对照）
    ink_base = _render_check(base, "你好世界")
    print(f"  用基准字体渲染'你好世界'：墨迹占比 {ink_base:.4f}（对照）")
    check("基准字体同样能渲染（对照组有效）", ink_base > 0.02, f"ink={ink_base:.4f}")

    return _report()


def _check_metrics(merged: Path, base: Path) -> None:
    """合并字体的垂直度量必须与基准字体处在同一 UPEM 坐标系。

    这是"文字可见"的前提。判据取 ``ascender / unitsPerEm`` 的比值 ——
    它必须接近基准字体，而不是接近那个 UPEM 大 8 倍的补充字体。
    """
    from fontTools.ttLib import TTFont

    b = TTFont(str(base))
    m = TTFont(str(merged))
    try:
        b_upem, m_upem = b["head"].unitsPerEm, m["head"].unitsPerEm
        b_asc, m_asc = b["hhea"].ascender, m["hhea"].ascender
        b_ratio = b_asc / b_upem
        m_ratio = m_asc / m_upem
        print(f"    基准度量：UPEM={b_upem} asc={b_asc} 比值={b_ratio:.3f}")
        print(f"    合并度量：UPEM={m_upem} asc={m_asc} 比值={m_ratio:.3f}")
        check("合并字体 UPEM 与基准一致", m_upem == b_upem, f"{m_upem} vs {b_upem}")
        check("合并字体 ascender/UPEM 与基准同量级（不再被补充字体带偏）",
              abs(m_ratio / b_ratio - 1.0) < 0.5,
              f"合并 {m_ratio:.3f} vs 基准 {b_ratio:.3f}")
        check("合并字体 ascender 合理（未超出 2 倍 UPEM）",
              m_asc <= 2 * m_upem, f"asc={m_asc} upem={m_upem}")
        # descent 类必须是负的 / 是正数，符号不能乱
        check("hhea.descent 为负", m["hhea"].descender < 0, str(m["hhea"].descender))
        check("OS/2.usWinDescent 为正", m["OS/2"].usWinDescent > 0,
              str(m["OS/2"].usWinDescent))
    finally:
        b.close()
        m.close()


def _verify_font(font: Path, required: str) -> list[tuple[str, bool, str]]:
    """确定性不变量。

    **为什么不用 IoU**：把字形从 A 字体合并进 B 字体后，"合并结果和
    原字体逐像素有多像"这个指标没有意义 —— 我们要的恰恰是"不一样"
    （用 A 的字形补 B 缺的字）。IoU 只会把正确的合并判成失败。
    真正该验证的是"这个码位在这个字体里到底能不能正常显示"。
    """
    from fontTools.ttLib import TTFont

    out: list[tuple[str, bool, str]] = []
    f = TTFont(str(font))
    try:
        cmap = f.getBestCmap()
        check_set = sorted(set(required))

        # 1) 每个码位都在 cmap 里
        absent = [c for c in check_set if ord(c) not in cmap]
        out.append((f"全部 {len(check_set)} 个码位都在 cmap 里", not absent,
                    f"缺 {len(absent)}：{''.join(absent[:20])}"))

        # 2) 都不是 .notdef（在 cmap 里但指向 .notdef 等于没字形）
        notdef = [c for c in check_set
                  if ord(c) in cmap and cmap[ord(c)] == ".notdef"]
        out.append(("没有码位被映射到 .notdef", not notdef,
                    f"{len(notdef)} 个：{''.join(notdef[:20])}"))

        # 3) 没有重复 glyph id（合并时最容易出的错：两个码位共用一个字形）
        from collections import Counter

        used = Counter(cmap[ord(c)] for c in check_set if ord(c) in cmap)
        dupes = {g: n for g, n in used.items() if n > 1}
        out.append(("没有两个码位共用同一个字形", not dupes,
                    str(list(dupes.items())[:5])))

        # 4) 字形表结构完整（glyf 或 CFF 二选一）
        has_glyf = "glyf" in f
        has_cff = "CFF " in f or "CFF2" in f
        out.append(("字形表存在（glyf 或 CFF）", has_glyf or has_cff,
                    f"glyf={has_glyf} CFF={has_cff}"))

        # 5) 每个字形都有非空轮廓（空轮廓 = 渲染成空白）
        #    注意例外：空格、不换行空格等**本来就该是空的**字符。
        empty: list[str] = []
        blanks = " \u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u3000\ufeff"
        if has_glyf:
            glyf = f["glyf"]
            for c in check_set:
                if c in blanks:
                    continue  # 空白字符空轮廓是正确行为
                gn = cmap.get(ord(c))
                if not gn:
                    continue
                try:
                    g = glyf[gn]
                    if g.numberOfContours == 0:
                        empty.append(c if c.strip() else f"U+{ord(c):04X}")
                except Exception:  # noqa: BLE001
                    empty.append(c if c.strip() else f"U+{ord(c):04X}")
        out.append((f"没有空轮廓字形（{len(check_set)} 个码位，空白字符已排除）",
                    not empty, f"{len(empty)} 个：{''.join(empty[:20])}"))

        # 6) 关键度量存在且合理（否则排版会错乱）
        hmtx = f.get("hmtx")
        upem = f["head"].unitsPerEm
        out.append(("unitsPerEm 合理", 16 <= upem <= 16384, str(upem)))
        missing_metrics = [c for c in check_set
                           if ord(c) in cmap and cmap[ord(c)] not in (hmtx.metrics if hmtx else {})]
        out.append(("每个码位都有水平度量", not missing_metrics,
                    f"{len(missing_metrics)} 个"))

        # 7) FreeType 能加载（真实渲染器的最终判据）
        try:
            from PIL import ImageFont

            fnt = ImageFont.truetype(str(font), 24)
            out.append(("FreeType 能加载该字体", True))
            del fnt
        except Exception as exc:  # noqa: BLE001
            out.append(("FreeType 能加载该字体", False, str(exc)))
    finally:
        f.close()
    return out


def _render_check(font: Path, text: str, *, size: int = 48) -> float:
    """渲染文字，返回墨迹占比。

    判据用"有没有墨迹"而不是"像不像"：合并字体的字形本来就来自别的
    字体，和基准字体不一致是**预期行为**。

    画布必须按**实测文本宽度**来开 —— 第一版按 ``size * len(text)``
    估算，CJK 在 48px 下每个字宽正好约 48px，加上边距后文字被裁掉，
    得到 ink=0 的假失败。这里用 ``getlength`` 直接量，并留足余量。
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    fnt = ImageFont.truetype(str(font), size)
    pad = 24
    try:
        tw = float(fnt.getlength(text))
    except Exception:  # noqa: BLE001
        tw = float(size * len(text))
    w = int(tw + pad * 2)
    h = int(size * 2.2) + pad * 2
    img = Image.new("L", (max(w, size * 3), h), 0)
    d = ImageDraw.Draw(img)
    d.text((pad, pad), text, fill=255, font=fnt)
    arr = np.asarray(img)
    ink = float((arr > 32).mean())
    if ink == 0.0:
        # 渲染不出来时把关键量打出来，别只说"没墨迹"
        import sys as _s

        print(f"    [诊断] size={size} 画布={img.size} "
              f"getlength={tw:.1f} bbox={d.textbbox((pad, pad), text, font=fnt)} "
              f"max={arr.max()} 非零={(arr > 0).sum()} 字体={font.name}",
              file=_s.stderr)
    return ink


def _report() -> int:
    print("\n" + "=" * 78)
    n = 0
    for label, ok, detail in checks:
        n += ok
        print(f"  {'✅' if ok else '❌'} {label}" + (f"   ({detail})" if detail and not ok else ""))
    print(f"\n结论：{n}/{len(checks)} 通过" + ("  ✅" if n == len(checks) else "  ❌"))
    return 0 if n == len(checks) else 1


def test_suite() -> None:
    """pytest 入口：跑一遍完整报告并断言全通过。"""
    assert main() == 0

pytestmark = [
    pytest.mark.needs_fonts,
    pytest.mark.slow,
]


if __name__ == "__main__":
    raise SystemExit(main())
