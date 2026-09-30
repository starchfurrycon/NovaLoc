"""回归：RPG Maker 的 **NW.js 打包布局**（资源在 `www/` 下）必须能用。

## 这个 bug 是拿真实游戏跑出来的

`E:\\lush\\1\\newlytransport` 里一个 558 MB 的 RPG Maker MV 游戏
（`Elf Lifia and the Labyrinth of Everdream`）资源全在 `www/` 下：

    ElfLifia/
      Game.exe, nw.dll, ...
      www/
        data/*.json      ← 文本
        img/             ← 贴图
        js/rpg_core.js   ← 引擎脚本
        fonts/           ← 字体

修复前：

* `detect` 找 `game_dir/data` → 不存在 → **置信度 0**，
  被 `loose` 兜底适配器抢占，报"散装文件（图片/文本），置信度 25%"，
  还建议用户"先用 AssetStudio/UAE 导出资源"；
* `extract_text` 报"找不到数据目录"；
* `extract_images` 找到 `img` 后返回的路径**相对 `www/`**，而各阶段用
  `effective_source / path` 解析（`effective_source` 是游戏根）——
  于是 11 张贴图全部报"源文件不存在"、`analyzed` 永远是 false，
  **而阶段本身报 ok**（"0/11 张贴图已汉化"看起来像"这些图本来没字"）。

这里用**最小的合成工程**把这个布局固定下来，避免依赖外部 E 盘数据。
真实游戏上的实测结果放在文件末尾注释里。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines import detect_engine, get_adapter  # noqa: E402
from novaloc.engines.rpgmaker import _content_root  # noqa: E402


def _ctx() -> Context:
    return Context(config=Config(), events=EventBus())


def _make_www_game(root: Path) -> Path:
    """造一个 NW.js 布局（资源在 www/）的最小 MV 工程。"""
    www = root / "www"
    (www / "data").mkdir(parents=True)
    (www / "js").mkdir(parents=True)
    (www / "img" / "system").mkdir(parents=True)
    (www / "img" / "pictures").mkdir(parents=True)
    (www / "fonts").mkdir(parents=True)

    # 根目录放 Exe 之类，让"资源不在根"这一点更真实
    (root / "Game.exe").write_bytes(b"MZ fake")
    (www / "index.html").write_text("<html></html>", encoding="utf-8")

    # 引擎脚本（wire_fonts 会从这里读 fontFamily）
    (www / "js" / "rpg_core.js").write_text(
        "Graphics.fontFace = 'GameFont';\nfontFamily: 'GameFont',\n", encoding="utf-8"
    )
    (www / "fonts" / "gamefont.css").write_text(
        '@font-face {\n  font-family: "GameFont";\n  src: url("mplus.ttf");\n}\n',
        encoding="utf-8",
    )

    for name in ("System.json", "MapInfos.json", "CommonEvents.json", "Tilesets.json"):
        if name == "System.json":
            (www / "data" / name).write_text(
                json.dumps(
                    {"gameTitle": "WWW Layout Test", "switches": ["", "S1"]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        else:
            (www / "data" / name).write_text("[]", encoding="utf-8")
    (www / "data" / "Map001.json").write_text(
        json.dumps(
            {
                "displayName": "Test Map",
                "events": [
                    None,
                    {
                        "id": 1,
                        "name": "Door",
                        "pages": [
                            {
                                # RPG Maker 的指令是**二维数组** [code, indent, [params]]，
                                # 不是 dict（第一版写成 dict，抽取器正确地什么都没抽到）
                                "list": [
                                    [101, 0, ["", 0, 0, 0, "Elf"]],
                                    [401, 0, ["Hello there."]],
                                    [102, 0, ["Yes", "No", 1]],
                                    [0, 0, []],
                                ]
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 贴图：一张"有文字"的 loading 条 + 一张立绘
    Image.new("RGB", (200, 60), (20, 20, 30)).save(www / "img" / "system" / "Loading.png")
    Image.new("RGB", (120, 200), (200, 180, 170)).save(
        www / "img" / "pictures" / "hero.png"
    )
    return root


# ----------------------------------------------------------------------
# 1. 探测
# ----------------------------------------------------------------------

def test_content_root_finds_www() -> None:
    import tempfile

    d = Path(tempfile.mkdtemp())
    _make_www_game(d)
    assert _content_root(d) == d / "www"


def test_detect_recognizes_www_layout(tmp_path: Path) -> None:
    game = _make_www_game(tmp_path / "game")
    info = detect_engine(game, _ctx())
    assert info.engine_id == "rpgmaker", (
        f"NW.js 布局（资源在 www/）没被认出来，实际判成 {info.engine_id!r}。"
        "修复前这里会是 'loose'（散装兜底），置信度 25%。"
    )
    assert info.confidence >= 0.8, f"置信度只有 {info.confidence}"
    assert any("www" in e for e in info.evidence), (
        f"证据里没说清资源在 www/ 下：{info.evidence}"
    )


def test_extract_text_from_www_layout(tmp_path: Path) -> None:
    game = _make_www_game(tmp_path / "game")
    ad = get_adapter("rpgmaker", _ctx())
    units, report = ad.extract_text(game)
    assert not report.errors, f"抽取报错：{report.errors}"
    srcs = {u.source for u in units}
    assert "Hello there." in srcs, f"没抽到对话文本，实际抽到 {srcs}"


# ----------------------------------------------------------------------
# 2. 路径必须是"相对游戏根"的 —— 这是最阴的一处
# ----------------------------------------------------------------------

def test_image_paths_are_relative_to_game_root(tmp_path: Path) -> None:
    """各阶段用 `effective_source / asset.path` 解析，所以必须相对游戏根。

    修复前这里返回 `img/system/Loading.png`（相对 www/），
    阶段就会去找 `<游戏根>/img/system/Loading.png` —— 不存在。
    结果是**所有贴图被静默跳过，阶段还报 ok**。
    """
    game = _make_www_game(tmp_path / "game")
    ad = get_adapter("rpgmaker", _ctx())
    assets, report = ad.extract_images(game)
    assert assets, f"一张贴图都没抽到：{report.errors}"

    missing = [a.path for a in assets if not (game / a.path).is_file()]
    assert not missing, (
        f"这些贴图的 path 无法用 `<游戏根>/path` 定位到：{missing}。\n"
        "path 必须相对游戏根（含 www/ 前缀）。"
    )
    assert all(a.path.startswith("www/") for a in assets), [a.path for a in assets]


def test_font_paths_are_relative_to_game_root(tmp_path: Path) -> None:
    game = _make_www_game(tmp_path / "game")
    ad = get_adapter("rpgmaker", _ctx())
    fonts = ad.discover_fonts(game)
    assert all((game / f.path).is_file() for f in fonts), [f.path for f in fonts]


def test_apply_writes_into_www(tmp_path: Path) -> None:
    """回写必须落到 out/www/data，而不是 out/data。

    `units` 是从资源根抽出来的，写入却写死了 `out_dir/"data"` ——
    两边不一致时每个文件都"目标不存在，跳过"，回写计数为 0 而阶段报 ok。
    """
    game = _make_www_game(tmp_path / "game")
    ctx = _ctx()
    ad = get_adapter("rpgmaker", ctx)
    units, _ = ad.extract_text(game)
    unit = next(u for u in units if u.source == "Hello there.")

    out = tmp_path / "out"
    res = ad.apply(game, out, units, {unit.uid: "你好。"})
    assert not res.error, res.error
    assert res.files_written >= 1, f"没有文件被写入：{res.warnings}"

    written = out / "www" / "data" / "Map001.json"
    assert written.is_file(), f"应该写到 {written}，实际 out 下是：{list(out.rglob('*.json'))}"
    blob = written.read_text(encoding="utf-8")
    assert "你好。" in blob, blob
    # 不该在旁边多造一个错的 data/
    assert not (out / "data").exists(), "回写路径算错了，多出 out/data"


def test_apply_and_wire_fonts_land_in_www(tmp_path: Path) -> None:
    """字体接线：CSS 与 rpg_core.js 都要在 www/ 下找，不能新建到 out/fonts。

    ## 这里刻意用真实流水线那一对命名

    fonts 阶段把补好的字体写到 `<工作区>/fonts/patched/<原字体名>.zh.ttf`
    （即 `stage_fonts` 里那句 `f"{fp.stem}.zh{fp.suffix}"`），
    而 `font_id` 是原字体相对游戏根的路径 `www/fonts/mplus.ttf`。
    两者**不同名** —— `apply` 早先把 `installed[rel] = rel` 写成了原文件名，
    于是 `wire_fonts` 把 CSS "改写成原来的名字"：`fixed == old_css`，
    看起来流程走完了，实际上新字体从未被引用，游戏继续加载旧字体。

    所以测试必须用不同名的补丁字体，否则这个 bug 会被同名覆盖掩盖掉
    （第一版测试用的就是同名，白白放过了一个真 bug）。
    """
    game = _make_www_game(tmp_path / "game")
    ctx = _ctx()
    ad = get_adapter("rpgmaker", ctx)
    units, _ = ad.extract_text(game)

    # 与 stage_fonts 一致：patched/<原字体名>.zh<后缀>
    patched = tmp_path / "patched" / "mplus.zh.ttf"
    patched.parent.mkdir(parents=True, exist_ok=True)
    patched.write_bytes(b"\x00\x01\x00\x00")  # 内容不重要，接线看路径
    out = tmp_path / "out"
    res = ad.apply(
        game, out, units, {},
        font_patches={"www/fonts/mplus.ttf": patched},
    )

    assert (out / "www" / "fonts" / "mplus.zh.ttf").is_file(), (
        f"补好的字体没落到 out/www/fonts/mplus.zh.ttf。实际 out 下："
        f"{[p.name for p in (out / 'www' / 'fonts').glob('*')]}"
    )
    css = out / "www" / "fonts" / "gamefont.css"
    assert css.is_file()
    text = css.read_text(encoding="utf-8")
    assert "mplus.zh.ttf" in text, (
        f"gamefont.css 的 src 没指向补好的字体，游戏会继续加载旧字体。\n实际内容：{text}\n"
        f"接线记录：{res.warnings}"
    )
    # 不该因为"改不动"而静默放行
    assert not any("没找到可改写的 src" in w for w in res.warnings), res.warnings
    assert not (out / "fonts").exists(), "字体被写到 out/fonts（错的一层）"


def test_root_layout_still_works(tmp_path: Path) -> None:
    """**不能把 MZ / 新 MV 的根布局弄坏**：资源直接在游戏根下。"""
    root = tmp_path / "mz"
    (root / "data").mkdir(parents=True)
    (root / "js").mkdir()
    (root / "img" / "system").mkdir(parents=True)
    (root / "fonts").mkdir()
    for name in ("System.json", "MapInfos.json", "CommonEvents.json", "Tilesets.json"):
        (root / "data" / name).write_text(
            json.dumps({"gameTitle": "Root Layout"}) if name == "System.json" else "[]",
            encoding="utf-8",
        )
    Image.new("RGB", (10, 10)).save(root / "img" / "system" / "Loading.png")

    assert _content_root(root) == root
    info = detect_engine(root, _ctx())
    assert info.engine_id == "rpgmaker", info.engine_id
    ad = get_adapter("rpgmaker", _ctx())
    assets, _ = ad.extract_images(root)
    assert all((root / a.path).is_file() for a in assets)
    assert all(not a.path.startswith("www/") for a in assets), [a.path for a in assets]


# ----------------------------------------------------------------------
# 真实游戏实测记录（人工验证，不在这里自动跑）
# ----------------------------------------------------------------------
# 游戏：E:\lush\1\newlytransport\Elf Lifia and the Labyrinth of Everdream
#       558.6 MB / 1608 文件 / RPG Maker MV / locale=ja_JP
#
# 修复前 → 修复后：
#   detect     loose 25%（"散装文件"）      → rpgmaker MV 100%
#   extract    "找不到数据目录"              → 1343 条文本（14 个文件）
#   images_scan 11 张候选（path 无法定位）    → 11 张，全部可定位
#   localize   0/11（全部"源文件不存在"）     → 1/11 真文字被汉化
#                                              Now Loading... → 正在加载...
#   fonts      0 个                          → 发现 www/fonts/mplus-1m-regular.ttf
#
# 另外从这次实测里发现并修掉的**第二个**问题（单字符幻觉）见
# `tests/test_texture_block_filter.py`：立绘上读出的 '2'/'S'/'0'
# 曾被翻译并重绘，其中 '0' 被翻成 'VS'。
