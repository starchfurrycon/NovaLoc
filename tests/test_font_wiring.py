"""验证"字体接线"：补好的字体必须真的被引擎加载。

这是整个项目里**最容易假成功**的一环：把 ttf 复制进 fonts/ 目录，
文件确实换了，日志显示"已写入 N 个文件"，但游戏里仍然是口口口 ——
因为没人告诉引擎去用这个字体。

所以本测试不看"文件是否被复制"，只看**引擎的字体指向是否真的变了**：

* RPG Maker：``gamefont.css`` 的 ``@font-face src`` 指向新字体，
  且 ``font-family`` 名字保持不变（改了名字 rpg_core.js 就找不到）；
* Ren'Py：生成 ``novaloc_fonts.rpy``，内容能被 Python 语法解析，
  且确实设置了 ``gui.text_font``。

顺带验证真实字体文件能被打包/解析，避免"接线对了但字体本身是坏的"。
"""

from __future__ import annotations

import ast
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines import get_adapter  # noqa: E402

SB = FIXTURES
checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))


def make_fake_ttf(path: Path, family: str = "TestFont") -> Path:
    """造一个**结构合法**的最小 TTF（fontTools 能解析）。"""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(1000, isTTF=True)
    # 字形名必须能放进 post 表的 format 2.0（latin-1 编码），
    # 所以汉字要用 uniXXXX 形式，不能直接叫 "中"。cmap 仍然指向它。
    fb.setupGlyphOrder([".notdef", "A", "uni4E2D"])
    fb.setupCharacterMap({0x41: "A", 0x4E2D: "uni4E2D"})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((0, 700))
    pen.lineTo((600, 700))
    pen.lineTo((600, 0))
    pen.closePath()
    glyf = {".notdef": pen.glyph(), "A": pen.glyph(), "uni4E2D": pen.glyph()}
    fb.setupGlyf(glyf)
    fb.setupHorizontalMetrics({".notdef": (700, 0), "A": (700, 0), "uni4E2D": (1000, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Regular", "psName": family})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    fb.save(str(path))
    return path


def ctx() -> Context:
    return Context(config=Config(), events=EventBus())


def test_rpgmaker_wiring() -> None:
    print("\n[1] RPG Maker MV：gamefont.css 的 @font-face 必须改指向")
    game = SB / "wire_mv"
    if game.exists():
        shutil.rmtree(game)
    (game / "data").mkdir(parents=True)
    (game / "js").mkdir(parents=True)
    (game / "fonts").mkdir(parents=True)
    (game / "img" / "system").mkdir(parents=True)
    (game / "data" / "System.json").write_text("{}", encoding="utf-8")
    (game / "data" / "MapInfos.json").write_text("[]", encoding="utf-8")

    # 原引擎用的字体族名（rpg_core.js 里引用的）
    (game / "js" / "rpg_core.js").write_text(
        'Graphics._createFontLoader = function(){\n'
        '  var fontFamily = "GameFont";\n'
        '};\n',
        encoding="utf-8",
    )
    (game / "fonts" / "gamefont.css").write_text(
        '@font-face {\n'
        '  font-family: "GameFont";\n'
        '  src: url("mplus-1m-regular.ttf");\n'
        '}\n',
        encoding="utf-8",
        newline="\n",
    )
    (game / "fonts" / "mplus-1m-regular.ttf").write_bytes(b"FAKE-OLD-FONT")

    # 补好的字体
    patched = make_fake_ttf(SB / "_wire" / "GameFont_cn.ttf", "GameFontCN")

    c = ctx()
    ad = get_adapter("rpgmaker", c)
    out = SB / "wire_mv_out"
    res = ad.apply(
        game, out, [], {},
        font_patches={"fonts/GameFont_cn.ttf": patched},
    )
    print(f"    回写结果：ok={res.ok} written={res.files_written} "
          f"warnings={len(res.warnings)}")
    for w in res.warnings:
        print(f"      · {w}")

    check("回写成功", res.ok, res.error)

    css = (out / "fonts" / "gamefont.css").read_text(encoding="utf-8")
    print(
        "    新的 gamefont.css：\n"
        + chr(10).join("      " + line for line in css.splitlines())
    )

    check("CSS 的 src 指向了新字体",
          "GameFont_cn.ttf" in css, css.replace("\n", " "))
    check("CSS 不再指向旧字体",
          "mplus-1m-regular.ttf" not in css, css.replace("\n", " "))
    # 这条最关键：改了 font-family 名字，rpg_core.js 里的 "GameFont" 就找不到了
    check("font-family 名字保持不变（仍是 GameFont）",
          re.search(r'font-family:\s*["\']?GameFont["\']?\s*;', css) is not None,
          css.replace("\n", " "))
    check("字体文件确实在产物目录里", (out / "fonts" / "GameFont_cn.ttf").is_file())
    check("接线说明写进了 warnings（用户能看到）",
          any("字体接线" in w for w in res.warnings), str(res.warnings))

    # 幂等：再跑一次不能把 src 改坏
    out2 = SB / "wire_mv_out2"
    res2 = ad.apply(game, out2, [], {}, font_patches={"fonts/GameFont_cn.ttf": patched})
    check("幂等重跑本身成功", res2.ok, res2.error)
    css2 = (out2 / "fonts" / "gamefont.css").read_text(encoding="utf-8")
    check("重跑后 CSS 仍然正确（幂等）",
          "GameFont_cn.ttf" in css2 and "mplus" not in css2, css2.replace("\n", " "))

    # 没有 @font-face 的情况：应该生成一份
    game3 = SB / "wire_mv3"
    if game3.exists():
        shutil.rmtree(game3)
    shutil.copytree(game, game3)
    (game3 / "fonts" / "gamefont.css").unlink()
    out3 = SB / "wire_mv3_out"
    ad.apply(game3, out3, [], {}, font_patches={"fonts/GameFont_cn.ttf": patched})
    css3 = (out3 / "fonts" / "gamefont.css")
    check("没有 gamefont.css 时会新建一份", css3.is_file())
    if css3.is_file():
        t = css3.read_text(encoding="utf-8")
        print(
            "    新建的 CSS：\n"
            + chr(10).join("      " + line for line in t.splitlines())
        )
        check("新建 CSS 用了从 rpg_core.js 读到的族名 GameFont",
              "GameFont" in t, t.replace("\n", " "))
        check("新建 CSS 指向新字体", "GameFont_cn.ttf" in t, t.replace("\n", " "))


def test_renpy_wiring() -> None:
    print("\n[2] Ren'Py：必须生成覆盖 gui 字体变量的 .rpy")
    game = SB / "wire_renpy"
    if game.exists():
        shutil.rmtree(game)
    (game / "game").mkdir(parents=True)
    (game / "game" / "gui.rpy").write_text(
        "init offset = -2\n"
        "define gui.text_font = \"DejaVuSans.ttf\"\n"
        "define gui.name_text_font = \"DejaVuSans.ttf\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (game / "game" / "script.rpy").write_text(
        'label start:\n    "Hello."\n', encoding="utf-8", newline="\n"
    )
    (game / "game" / "DejaVuSans.ttf").write_bytes(b"FAKE")

    patched = make_fake_ttf(SB / "_wire" / "SourceHanSans_cn.ttf", "SourceHanSansCN")

    c = ctx()
    ad = get_adapter("renpy", c)
    out = SB / "wire_renpy_out"
    res = ad.apply(
        game, out, [], {},
        font_patches={"game/SourceHanSans_cn.ttf": patched},
    )
    print(f"    回写结果：ok={res.ok} written={res.files_written}")
    for w in res.warnings:
        print(f"      · {w}")
    check("Ren'Py 回写成功", res.ok, res.error)

    gen = out / "game" / "novaloc_fonts.rpy"
    check("生成了字体接线文件", gen.is_file(), str(gen))
    if not gen.is_file():
        return

    text = gen.read_text(encoding="utf-8")
    print(f"    生成的文件（{len(text)} 字符）：")
    for line in text.splitlines():
        print(f"      {line}")

    check("指向了补好的字体", "SourceHanSans_cn.ttf" in text, text[:200])
    check("设置的是 gui 变量（Ren'Py 约定的覆盖点）",
          "gui" in text and "text_font" in text, text[:200])
    check("用 init 1 覆盖（晚于 gui.rpy 的默认值）",
          "init 1" in text, text[:200])
    check("提示了如何还原", "删掉" in text, text[:120])

    # 生成的 python 块必须是合法 Python（Ren'Py 会真的执行它）。
    # 块内是 4 空格缩进的，ast.parse 前要统一 dedent。
    import textwrap

    py_lines: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.strip().startswith("init 1 python:"):
            in_block = True
            continue
        if in_block:
            if line.startswith("    "):
                py_lines.append(line)
            elif line.strip() == "":
                py_lines.append("")
            else:
                break
    src = textwrap.dedent("\n".join(py_lines))
    try:
        tree = ast.parse(src)
        check("生成的 python 块语法合法", True)
        # 再确认它真的干了该干的事，而不是一个空块
        assigned = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "_novaloc_font" for t in n.targets)
        ]
        check("python 块里真的赋值了字体变量", len(assigned) == 1, str(len(assigned)))
        check("python 块里调用了 setattr 覆盖 gui",
              any(isinstance(n, ast.Call)
                  and getattr(n.func, "id", "") == "setattr" for n in ast.walk(tree)))
    except SyntaxError as exc:
        check("生成的 python 块语法合法", False, str(exc))

    # gui.rpy 不能被改动（那是用户/主题的文件）
    gui_after = (out / "game" / "gui.rpy").read_text(encoding="utf-8")
    check("没有改动 gui.rpy（用户主题文件保持原样）",
          "DejaVuSans.ttf" in gui_after and "SourceHanSans" not in gui_after,
          gui_after.replace("\n", " ")[:120])

    # 字体文件到位
    check("字体文件已在 game/ 下", (out / "game" / "SourceHanSans_cn.ttf").is_file())

    # game/ 不在子目录时（散装 Ren'Py）也要能用
    print("\n[3] Ren'Py 指纹：写死的 style font 会被警告")
    game2 = SB / "wire_renpy2"
    if game2.exists():
        shutil.rmtree(game2)
    shutil.copytree(game, game2)
    (game2 / "game" / "gui.rpy").write_text(
        "init offset = -2\n"
        'define gui.text_font = "DejaVuSans.ttf"\n'
        "style say_dialogue:\n"
        '    font "SomeHardcodedFont.ttf"\n',
        encoding="utf-8",
        newline="\n",
    )
    out2 = SB / "wire_renpy2_out"
    res2 = ad.apply(game2, out2, [], {},
                    font_patches={"game/SourceHanSans_cn.ttf": patched})
    warned = any("写死" in w for w in res2.warnings)
    check("写死的 style font 被警告（否则用户查不出口口口原因）",
          warned, str(res2.warnings))


def test_font_file_valid() -> None:
    print("\n[4] 补好的字体文件本身必须合法")
    p = make_fake_ttf(SB / "_wire" / "check.ttf", "CheckFont")
    from fontTools.ttLib import TTFont

    f = TTFont(str(p))
    check("fontTools 能解析", True)
    check("cmap 里有 '中'", 0x4E2D in f.getBestCmap(), str(list(f.getBestCmap())[:5]))
    check("有 glyf 表", "glyf" in f)
    # f["name"] 是 name 表对象，必须用 getName 取实际字符串
    fam = f["name"].getDebugName(1)
    check("字体名正确", fam == "CheckFont", f"实际={fam!r}")
    f.close()


def main() -> int:
    test_rpgmaker_wiring()
    test_renpy_wiring()
    test_font_file_valid()

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


if __name__ == "__main__":
    raise SystemExit(main())
