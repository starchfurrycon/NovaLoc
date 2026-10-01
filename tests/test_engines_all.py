"""四个引擎适配器验证：识别优先级、抽取、回写、往返一致。

重点是**引擎识别不能抢**（Unity 目录不能被 RPG Maker 认领，
RPG Maker 目录不能被兜底适配器抢走），以及 Ren'Py 的回写要能
正确处理转义引号、插值、以及**绝不改动 python 代码块**。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from _fake_game import build_fake_game  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines import available_engines, detect_engine, get_adapter  # noqa: E402
from novaloc.engines.renpy import RenPyAdapter  # noqa: E402
from novaloc.engines.unity import UnityAdapter  # noqa: E402
from novaloc.images.io import imwrite_bgr  # noqa: E402

SB = FIXTURES


def _rpg_game() -> Path:
    """取合成 RPG Maker 工程；没有就自己造（原因见 ``_fake_game.py``）。

    以前这里直接指向 ``tests/fixtures/rpgmaker_game`` 并假设它存在 ——
    而它里面的 ``data/`` 是 git-ignored 的，只有
    ``test_engine_rpgmaker.py`` 先跑过才会有。全新 clone 出来单独跑本文件，
    "RPG Maker 被正确识别"这一条会因为 ``path.exists()`` 为假而**被静默跳过**，
    于是这条检查事实上从来没在本文件里真正执行过。
    """
    base = os.environ.get("NOVALOC_FAKE_GAME_DIR")
    dest = Path(base) / "rpgmaker_game" if base else SB / "rpgmaker_game"
    return build_fake_game(dest)


def build_renpy() -> Path:
    g = SB / "renpy_game"
    if g.exists():
        shutil.rmtree(g)
    (g / "game" / "renpy").mkdir(parents=True)
    (g / "game" / "options.rpy").write_text(
        'define config.name = _("My Visual Novel")\n'
        'define config.version = "1.2.3"\n',
        encoding="utf-8",
    )
    (g / "game" / "script.rpy").write_text(
        'define e = Character("Eileen")\n'
        'define m = Character("Mysterious Voice")\n'
        '\n'
        'label start:\n'
        '    "The rain had not stopped for three days."\n'
        '    e "Hello, [player_name]. Welcome to the {b}manor{/b}."\n'
        '    e "She said: \\"Do not go there.\\""\n'
        '    menu:\n'
        '        "Go inside":\n'
        '            jump inside\n'
        '        "Wait outside":\n'
        '            jump outside\n'
        '\n'
        '    python:\n'
        '        secret_code = "THIS_IS_CODE_NOT_TEXT"\n'
        '        x = 1 + 2\n'
        '    $ y = "ALSO_CODE"\n'
        '    return\n',
        encoding="utf-8",
    )
    return g


def build_unity() -> Path:
    g = SB / "unity_game"
    if g.exists():
        shutil.rmtree(g)
    d = g / "MyGame_Data"
    (d / "Managed").mkdir(parents=True)
    (d / "StreamingAssets").mkdir(parents=True)
    (g / "UnityPlayer.dll").write_bytes(b"MZ fake")
    (d / "globalgamemanagers").write_bytes(b"\x00" * 100 + b"2021.3.16f1" + b"\x00" * 20)
    (d / "Managed" / "Assembly-CSharp.dll").write_bytes(b"MZ")
    (d / "level0").write_bytes(b"\x00" * 4096)
    (d / "resources.assets").write_bytes(b"\x00" * 2048)
    (d / "StreamingAssets" / "strings.json").write_text(json.dumps({
        "menu": {"start": "Start Game", "options": "Options", "quit": "Quit"},
        "credits": ["Director: Jane Doe", "Music: John Smith"],
        "assetGuid": "a1b2c3d4e5f60718",
        "path": "Assets/Scenes/Main.unity",
    }, ensure_ascii=False), encoding="utf-8")
    (d / "StreamingAssets" / "dialog.csv").write_text(
        "id,text\n1,Welcome to the dungeon.\n2,You have died.\n",
        encoding="utf-8",
    )
    (d / "StreamingAssets" / "notes.txt").write_text(
        "This is a plain note.\n123\n===\nAnother readable line.\n", encoding="utf-8"
    )
    # 放一张图，验证 Unity 不会去抢贴图
    imwrite_bgr(g / "MyGame_Data" / "StreamingAssets" / "logo.png",
                np.full((100, 100, 3), 60, "uint8"))
    return g


def build_loose() -> Path:
    g = SB / "loose_pack"
    if g.exists():
        shutil.rmtree(g)
    (g / "images").mkdir(parents=True)
    imwrite_bgr(g / "images" / "title.png", np.full((300, 400, 3), 30, "uint8"))
    imwrite_bgr(g / "images" / "tiny.png", np.full((10, 10, 3), 30, "uint8"))
    (g / "text.json").write_text(
        json.dumps({"hello": "Hello there", "n": 5}, ensure_ascii=False), encoding="utf-8"
    )
    return g


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    ctx = Context(config=Config(), events=EventBus())

    print(f"已注册引擎适配器: {available_engines()}")

    renpy = build_renpy()
    unity = build_unity()
    loose = build_loose()
    rpg = _rpg_game()

    # ---------- 1. 识别优先级 ----------
    print("\n[1] 引擎识别")
    for name, path, want in [
        ("Ren'Py", renpy, "renpy"),
        ("Unity", unity, "unity"),
        ("RPG Maker", rpg, "rpgmaker"),
        ("散装", loose, "loose"),
    ]:
        if not path.exists():
            continue
        info = detect_engine(path, ctx)
        print(f"    {name:10} → {info.engine_id:10} conf={info.confidence:.2f} "
              f"{info.evidence[:2]}")
        check(f"{name} 被正确识别", info.engine_id == want,
              f"实际 {info.engine_id} (conf={info.confidence:.2f})")

    # 互不抢占
    check("Unity 目录不会被 RPG Maker 抢走",
          detect_engine(unity, ctx).engine_id != "rpgmaker")
    check("RPG Maker 目录不会被兜底适配器抢走",
          detect_engine(rpg, ctx).engine_id == "rpgmaker",
          detect_engine(rpg, ctx).engine_id)

    # ---------- 2. Ren'Py 抽取 ----------
    print("\n[2] Ren'Py 抽取")
    ad = get_adapter("renpy", ctx)
    units, rep = ad.extract_text(renpy)
    for u in units:
        print(f"    {u.kind.value:16} {u.location.pointer:8} {u.source[:46]!r}")
    srcs = {u.source for u in units}
    check("抽到对白", any("rain had not stopped" in s for s in srcs))
    check("抽到角色名", "Eileen" in srcs and "Mysterious Voice" in srcs)
    check("抽到菜单选项", "Go inside" in srcs and "Wait outside" in srcs)
    check("抽到转义引号的对白（引号闭合并未找错）",
          any("Do not go there" in s for s in srcs), str([s for s in srcs if "there" in s]))
    check("抽到含插值/标签的对白",
          any("[player_name]" in s and "{b}" in s for s in srcs))
    check("python 代码块内的字符串未被抽取", "THIS_IS_CODE_NOT_TEXT" not in srcs)
    check("$ 单行 Python 的字符串未被抽取", "ALSO_CODE" not in srcs)
    check("配置文件里的游戏名被抽取", "My Visual Novel" in srcs)
    info_r = detect_engine(renpy, ctx)
    check("读到了版本号", info_r.version == "1.2.3", info_r.version)

    # ---------- 3. Ren'Py 回写往返 ----------
    print("\n[3] Ren'Py 回写往返")
    tr = {u.uid: f"〔译〕{u.source}" for u in units}
    out = SB / "renpy_out"
    res = ad.apply(renpy, out, units, tr)
    print(f"    ok={res.ok} 写入 {res.files_written} 警告 {res.warnings[:2]}")
    check("Ren'Py 回写成功", res.ok, res.error)

    ad2 = RenPyAdapter(ctx)
    units2, _ = ad2.extract_text(out)
    got = {u.uid: u.source for u in units2}
    bad = [(u.uid, tr[u.uid], got.get(u.uid)) for u in units if got.get(u.uid) != tr[u.uid]]
    check("Ren'Py 译文往返一字不差", not bad, f"{len(bad)} 处，例如 {bad[:2]}")

    # 代码必须原封不动
    out_script = (out / "game" / "script.rpy").read_text(encoding="utf-8")
    check("python 代码块原封不动", "secret_code = \"THIS_IS_CODE_NOT_TEXT\"" in out_script)
    check("$ 行原封不动", '$ y = "ALSO_CODE"' in out_script)
    check("label/return 等语句未受影响",
          "label start:" in out_script and "return" in out_script)
    # 转义引号必须还能被 Ren'Py 正确解析（不能变成裸引号）
    check("转义引号未被破坏", '\\"Do not go there.\\"' in out_script
          or '\\"〔译〕She said' in out_script,
          out_script[out_script.find("She said") - 30: out_script.find("She said") + 80])

    # ---------- 3b. ★ 逐行验证：**只有文本单元内的行**允许变化 ----------
    #
    # 上面那些 `in` 断言只抽查了若干处。`.rpy` 是**可执行代码**，
    # 伤到缩进、`python:` 块、`$` 行或文档字符串会让游戏**直接起不来** ——
    # 而"起不来"和"翻译没做完"是两种完全不同的故障，必须能区分。
    #
    # 所以这里做**逐行**对比：除了被列为文本单元的行，
    # 其余每一行必须与原文**完全相等**（含缩进、空行、注释）。
    print("\n[3b] Ren'Py 逐行验证（越界改动必须为 0）")
    src_lines = (renpy / "game" / "script.rpy").read_text(encoding="utf-8").splitlines()
    out_lines = (out / "game" / "script.rpy").read_text(encoding="utf-8").splitlines()
    check("行数一致", len(src_lines) == len(out_lines),
          f"{len(src_lines)} vs {len(out_lines)}")

    # 哪些行**允许**变化：从抽取结果反查行号
    editable: set[int] = set()
    for u in units:
        loc = getattr(u, "location", None)
        for attr in ("line", "line_no", "lineno"):
            v = getattr(loc, attr, None) if loc is not None else None
            if isinstance(v, int):
                editable.add(v)
        # 指针形如 `/game/script.rpy:12:say`
        ptr = getattr(loc, "pointer", "") if loc is not None else ""
        for part in str(ptr).split(":"):
            if part.isdigit():
                editable.add(int(part))

    changed = [
        i
        for i, (a, b) in enumerate(zip(src_lines, out_lines, strict=False), 1)
        if a != b
    ]
    print(f"    文本单元行号: {sorted(editable)}")
    print(f"    实际变化行:   {changed}")
    if editable:
        out_of_range = [i for i in changed if i not in editable]
        check("★ 变化全部落在文本单元内（越界改动为 0）", not out_of_range,
              f"越界 {out_of_range}"
              + (f"  例如第 {out_of_range[0]} 行："
                 f"{src_lines[out_of_range[0]-1]!r} → {out_lines[out_of_range[0]-1]!r}"
                 if out_of_range else ""))
        check("★ 至少有一行真的被翻译了（否则上面那条是空转）",
              bool(changed), "一行都没变 —— 逐行断言没有意义")
    else:
        check("文本单元带行号（否则逐行验证退化成空转）", False,
              "抽取结果里没有行号信息")

    print("    回写后的对白行：")
    for ln in out_script.splitlines():
        if "〔译〕" in ln:
            print(f"      {ln}")

    # ---------- 4. Unity 抽取 ----------
    print("\n[4] Unity 抽取")
    ua = get_adapter("unity", ctx)
    uunits, urep = ua.extract_text(unity)
    for u in uunits:
        print(f"    {u.kind.value:14} {u.location.file}:{u.location.pointer:14} {u.source[:40]!r}")
    usrcs = {u.source for u in uunits}
    check("抽到 JSON 里的文本", "Start Game" in usrcs and "Options" in usrcs)
    check("抽到数组里的文本", "Director: Jane Doe" in usrcs)
    check("抽到 CSV 里的文本", any("Welcome to the dungeon" in s for s in usrcs))
    check("抽到纯文本文件的行", any("Another readable line" in s for s in usrcs))
    check("GUID 未被抽取", "a1b2c3d4e5f60718" not in usrcs)
    check("资源路径未被抽取", not any(s.startswith("Assets/") for s in usrcs))
    check("纯数字行未被抽取", "123" not in usrcs)
    # 必须如实报告未处理的序列化资源
    print(f"    报告的问题 {len(urep.errors)} 条：")
    for e in urep.errors:
        print(f"      · {e[:120]}")
    check("如实报告了未处理的序列化资源/AssetBundle",
          any("序列化资源" in e for e in urep.errors), str(urep.errors))
    check("说明了这些内容未被处理", any("未被处理" in e for e in urep.errors))
    check("给出了可行的替代方案",
          any("UABEA" in e or "AssetStudio" in e for e in urep.errors))

    # ---------- 5. Unity 回写 ----------
    print("\n[5] Unity 回写")
    utr = {u.uid: f"【译】{u.source}" for u in uunits}
    uout = SB / "unity_out"
    ures = ua.apply(unity, uout, uunits, utr)
    check("Unity 回写成功", ures.ok, ures.error)
    uu2, _ = UnityAdapter(ctx).extract_text(uout)
    ugot = {u.uid: u.source for u in uu2}
    ubad = [(u.uid, utr[u.uid], ugot.get(u.uid)) for u in uunits if ugot.get(u.uid) != utr[u.uid]]
    check("Unity 译文往返一字不差", not ubad, f"{len(ubad)} 处，例如 {ubad[:3]}")
    # 二进制资源必须原样复制而不是被改写
    check("序列化资源原样复制（未被改写）",
          (uout / "MyGame_Data" / "level0").read_bytes()
          == (unity / "MyGame_Data" / "level0").read_bytes())

    # ---------- 6. 散装 ----------
    print("\n[6] 散装文件")
    la = get_adapter("loose", ctx)
    limgs, lrep = la.extract_images(loose)
    print(f"    图片 {len(limgs)} 张：{[i.path for i in limgs]}")
    check("发现散装图片", any("title.png" in i.path for i in limgs))
    check("过小的图片被过滤", not any("tiny" in i.path for i in limgs))
    lunits, _ = la.extract_text(loose)
    check("抽到散装 JSON 文本", any(u.source == "Hello there" for u in lunits))

    # ---------- 7. 原目录只读 ----------
    check("Ren'Py 原目录未被修改",
          "〔译〕" not in (renpy / "game" / "script.rpy").read_text(encoding="utf-8"))
    check("Unity 原目录未被修改",
          "【译】" not in (unity / "MyGame_Data" / "StreamingAssets" / "strings.json")
          .read_text(encoding="utf-8"))

    print("\n" + "=" * 78)
    print("断言汇总")
    print("=" * 78)
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
