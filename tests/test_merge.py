"""验证重写后的字体合并。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.fonts import coverage as cov  # noqa: E402
from novaloc.fonts import merge as mg  # noqa: E402

FONTS = Path(r"C:\Windows\Fonts")
OUT = FIXTURES / "out"
OUT.mkdir(parents=True, exist_ok=True)

SAMPLE = (
    "勇者啊，你终于醒了。这里是艾尔登村，魔王军三天前攻陷了北面的要塞。"
    "HP 1200 / MP 340，攻击力 +15%，防御力 -8%。"
    "「你确定要使用『圣光斩』吗？」是／否（注意：此操作不可撤销）"
    "物品：回复药水 ×3、魔力结晶、古代符文碎片、贤者之石。"
    "系统提示：存档失败，请检查磁盘空间。"
)


def trial(base_name: str, cjk_name: str, tag: str) -> bool:
    b, c = FONTS / base_name, FONTS / cjk_name
    print(f"\n{'-' * 74}")
    print(f"[{tag}]  {base_name}  <-  {cjk_name}")
    if not b.exists() or not c.exists():
        print("  跳过：字体不存在")
        return False

    out = OUT / f"merged_{tag}.ttf"
    t0 = time.time()
    try:
        rep = mg.merge_fonts(b, c, out, SAMPLE)
    except mg.MergeError as exc:
        print(f"  ❌ {exc}")
        return False

    print(f"  ✅ {time.time() - t0:.2f}s   {rep.summary}")
    print(f"     产物 {out.stat().st_size / 1024:.0f} KB")
    for w in rep.warnings:
        print(f"     ⚠️  {w}")

    ai = cov.load_font_info(out)
    if ai:
        miss = ai.missing(SAMPLE)
        latin_ok = all(ai.has_char(ch) for ch in "ABCabc0123.,!?%-+/")
        print(f"     仍缺字 = {''.join(miss) or '（无）'}   拉丁保留 = {latin_ok}")

    # 真的用 PIL 渲染，确认产物可用且字形没有互相覆盖
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

        f = ImageFont.truetype(str(out), 26)
        img = Image.new("L", (1180, 140), 0)
        ImageDraw.Draw(img).text((8, 8), SAMPLE[:34], font=f, fill=255)
        ImageDraw.Draw(img).text((8, 48), SAMPLE[34:68], font=f, fill=255)
        ImageDraw.Draw(img).text((8, 88), SAMPLE[68:], font=f, fill=255)
        ink = sum(1 for p in img.getdata() if p > 40)
        img.save(OUT / f"preview_{tag}.png")
        print(f"     PIL 渲染 OK  ink={ink}px  预览 {OUT / f'preview_{tag}.png'}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"     ❌ PIL 渲染失败：{exc}")
        return False


def main() -> int:
    """跑完所有组合，返回 0 表示全部通过。

    注意 ``trial`` 在"字体不存在"时也返回 False —— 在非 Windows 上
    这些系统字体确实没有，于是整轮会报失败。这是**故意保留**的行为：
    真正完整的字体合并验证在 ``test_font_real.py``（用产品真实的候选池，
    且会把缺字形判为失败）。这个文件现在只作为"直接调底层 merge_fonts
    的多组合冒烟"，是本机 Windows 上的额外检查。
    """
    results = [
        trial("arial.ttf", "simhei.ttf", "arial+simhei"),
        trial("segoeui.ttf", "simhei.ttf", "segoeui+simhei"),
        trial("msyh.ttc", "simhei.ttf", "msyh+simhei"),
        trial("consola.ttf", "simsun.ttc", "consola+simsun"),
        trial("impact.ttf", "simhei.ttf", "impact+simhei"),
    ]
    print(f"\n{'=' * 74}\n通过 {sum(results)}/{len(results)}")
    return 0 if all(results) else 1


def test_suite() -> None:
    """pytest 入口。

    非 Windows（没有这些系统字体）直接 skip，不要报一个"环境不具备"
    的假失败。
    """
    if not FONTS.is_dir():
        import pytest

        pytest.skip(f"没有系统字体目录 {FONTS}（该套件依赖 Windows 自带字体）")
    assert main() == 0

pytestmark = [
    pytest.mark.needs_fonts,
]


if __name__ == "__main__":
    raise SystemExit(main())
