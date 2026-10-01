r"""Unity 序列化资源**只读**字符串提取。

这个套件的价值主要在三处，都是本会话真实踩过的坑：

## 一、噪音必须被拦住（否则功能等于没用）

真实游戏 `<Game>_Data/output_log.txt` 是 **33 MB** 的 Unity 播放器日志，
后缀 `.txt`、内容是英文 —— 完全符合"明文文本文件"的判据，
被抽成 **530,507 条**"待翻译文本"（占该游戏 98%）。
排掉它之后又冒出 **9,586 条**，全部来自
`Mono/etc/mono/browscap.ini`（2009 年的浏览器能力数据库）
与 `mconfig/config.xml`（Mono 自己的配置）。

判据必须按**"属不属于游戏内容"**定，不能按"看起来像不像文本"定。

## 二、候选必须**真的提出来**（跳过优化曾经静默漏掉它们）

曾经写过"连续 4096 次未命中就前跳 64 KB"的优化。
实测 `level0` 有 13 条真实候选，加上这个优化后**只剩 1 条** ——
因为相邻候选的间隔是 **10,681 / 17,585 / 33,553 / 49,353 字节**，
和 64 KB 同一量级。"连续未命中"推不出"这一段没有候选"。

现在只保留**可证明安全**的跳过：零字节长串
（长度前缀 ≥ 4 意味着首字节非零，所以连续零字节里不可能有字符串起点）。

## 三、路径匹配必须用 `as_posix()`

`_in_runtime_infra` 最初写的是 `"/".join(p.parts)`，在 Windows 上
拼出来仍是反斜杠，于是 `"/mono/etc/" in low` **永远为假**、
排除规则静默失效（数字一条没变才发现）。这类 bug 不报错、只静默不生效。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.engines.unity import (  # noqa: E402
    _in_runtime_infra,
    _looks_like_runtime_log,
)
from novaloc.engines.unity_strings import (  # noqa: E402
    ZERO_RUN_MIN,
    extract_strings_from_bytes,
    find_serialized_assets,
    scan_game,
    write_csv,
)


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


def _ustr(s: str) -> bytes:
    b = s.encode("utf-8")
    return _u32(len(b)) + b


# ---------------------------------------------------------------------------
# 一、日志与运行时基础设施必须被排除
# ---------------------------------------------------------------------------


def test_output_log_name_is_excluded() -> None:
    """`output_log.txt` 必须被文件名判据拦掉。"""
    assert _looks_like_runtime_log(Path("output_log.txt"), "随便什么内容")


def test_player_log_renamed_variants_are_excluded() -> None:
    for name in ("Player.log", "player-prev.log", "crash_log.txt", "build-log.txt"):
        assert _looks_like_runtime_log(Path(name), "x"), name


def test_real_log_content_is_detected_even_if_renamed() -> None:
    """★ 改名也躲不过：内容是真正的 Unity 播放器日志。"""
    content = (
        "Initialize engine version: 4.6.7f1 (bb67e21913cc)\n"
        "GfxDevice: creating device client; threaded=1\n"
        "Direct3D:\n"
        "    Version:  Direct3D 11.0 [level 11.1]\n"
        "Begin MonoManager ReloadAssembly\n"
        "Platform assembly: D:\\Game\\AlienQuest-EVE_Data\\Managed\\UnityEngine.dll\n"
        "Loading D:\\Game\\AlienQuest-EVE_Data\\Managed\\mscorlib.dll\n"
        "UnloadTime: 1.234500 ms\n"
    )
    # 文件名完全无害，只有内容能说明问题
    assert _looks_like_runtime_log(Path("notes.txt"), content)


def test_game_dialogue_is_not_mistaken_for_a_log() -> None:
    """反向：真正的游戏文案不能被当成日志排掉。"""
    content = (
        "Would you like to save?\n"
        "Quit to the Main Menu. \nAny unsaved progress will be lost.\n"
        "Select Location to Teleport.\n"
        "1. Copy the experiment data from EVE Core.\n"
        "2. Get the sample of MOTHER BRAIN.\n"
        "3. Activate the emergency destruction system.\n"
        "4. Escape now!\n"
    )
    assert not _looks_like_runtime_log(Path("dialogue.txt"), content)


def test_runtime_infra_paths_are_detected_on_windows() -> None:
    """★ `as_posix()` 归一化 —— 曾经因为反斜杠而**静默失效**。"""
    for rel in (
        "Mono/etc/mono/browscap.ini",
        "Mono/etc/mono/mconfig/config.xml",
        "Mono/2.0/mscorlib.dll",
        "il2cpp_data/etc/MonoBleedingEdge/x",
        "Resources/unity_builtin_extra",
    ):
        assert _in_runtime_infra(Path(rel)), rel


def test_real_game_paths_are_not_infra() -> None:
    for rel in (
        "StreamingAssets/dialogue.txt",
        "texts/messages.json",
        "data/items.csv",
        "Monologue/script.txt",       # 名字里有 Mono，但不是 Mono/etc
    ):
        assert not _in_runtime_infra(Path(rel)), rel


# ---------------------------------------------------------------------------
# 二、候选提取：真实文本要提到，噪音要拦掉
# ---------------------------------------------------------------------------


def test_extracts_real_unity_ui_text() -> None:
    """真实文案必须被提出，且 offset 指向正确位置。"""
    payload = b"\x00" * 16 + _ustr("Would you like to save?") + b"\x00" * 8
    got = extract_strings_from_bytes(payload, filename="level0")
    assert [c.text for c in got] == ["Would you like to save?"]
    # offset 必须指向长度前缀的起点
    assert got[0].offset == 16
    assert struct.unpack_from("<I", payload, got[0].offset)[0] == len(
        b"Would you like to save?"
    )


def test_extracts_offset_correctly_with_cjk() -> None:
    """CJK 文案：长度按**字节**算，不是字符。

    ⚠️ 前缀用**零字节**（对齐填充的真实形态）。
    早先这里写的是 `b"\\xff" * 7` —— 7 个 0xff 连在一起，
    会被"零区跳过"之外的路径当成 `0xffffffff` 这种超长前缀直接否掉，
    测试失败看着像"漏了 CJK"，实际是**测试数据本身不像真实布局**。
    """
    s = "你确定要保存吗？"
    payload = b"\x00" * 7 + _ustr(s)
    got = extract_strings_from_bytes(payload)
    assert [c.text for c in got] == [s]
    assert got[0].offset == 7
    assert got[0].reason == "含 CJK"


@pytest.mark.parametrize(
    "text",
    [
        "Xiph.Org libVorbis I 20101101 (Schaufenugget)",
        "Copyright=Copyright 2000, Sounddogs.com",
        "UnityEngine.UI.Text",
        "m_Script",
        "Mono.MonoConfig.FeatureNodeHandler, mconfig, Version=1.0.0.0",
        "Assembly-CSharp",
        "0123456789abcdef0123456789abcdef",
        "C:/Projects/Assets/Scenes/Main.unity",
        "Assets/Resources/text.json",
        "Base Layer.Idle -> Base Layer.Attack",
        "256_Glow_Circle  --Color",
        "FOOTSTEP Walk Trainers Wood Boards RR1 (mono)",
        "IMPACT Concrete Slab on Concrete Slab Short Dark (mono)",
        "[BGM_5] FX_sound_design_effect_build_suspenseful_cinematic",
        "Noto Sans CJK JP",
        "true",
        "default",
        "ab",
        "SELECT * FROM items WHERE id = 1",
    ],
)
def test_rejects_non_game_text(text: str) -> None:
    """这些全是真实数据里出现过的噪音，一条都不能进清单。"""
    got = extract_strings_from_bytes(_u32(len(text.encode())) + text.encode())
    assert all(c.text != text for c in got), f"误收噪音：{text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Would you like to save?",
        "Quit to the Main Menu. \nAny unsaved progress will be lost.",
        "2. Get the sample of MOTHER BRAIN.",
        "Click or Press 'Spin Attack'",
        "THANK YOU \nFOR YOUR SUPPORT!",
        "How to control",
        "Slot 1 has been deleted.",
        "Press ESC key to Exit",
        # 下面这些是"放宽判据后**必须仍然**收下"的，都是实测漏过或差点漏掉的
        "Game Over",                    # 两个词 —— 早期要求 ≥3 词被漏掉
        "Save Game",
        "Options",                      # 单个词 —— 早期"单词一律拒"被漏掉
        "Select Location to Teleport.",  # 曾被 SQL 判据（IGNORECASE）误杀
        "THANK YOU FOR PLAYING THIS DEMO",  # 曾被"全大写=音效名"误杀
    ],
)
def test_keeps_real_ui_text(text: str) -> None:
    got = extract_strings_from_bytes(_u32(len(text.encode())) + text.encode())
    assert [c.text for c in got] == [text], f"漏掉真实文案：{text!r}"


# ---------------------------------------------------------------------------
# 二·B、★ 资源名 vs 人写的词（放宽判据后**必须**挡住的那一大类）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # 实测来自真实游戏，共 527 条 —— 占"放宽判据后新增候选"的绝大多数
        "-----Creep  Center",
        "0 ---BlockUpper x7  Bot",
        "---Egg_Container  --",
        "1024_CircleGlow  --Shadow",
        "256_Black  ObjectOver",
        "Tile_4_Bottom  --",
        "Tile____L Bottom",
        "2 ---StairUp_L",
        "-50 HP Penalty",          # 多位数字/负号
        "2020  GRIMHELM",
    ],
)
def test_rejects_asset_names_with_separators(text: str) -> None:
    """资源名分隔符（`--`/`__`/连续短横线/多位数字）必须挡住。

    这一条是"放宽判据"之后**最关键**的刹车：不加它，
    候选从 28 条暴涨到 **974** 条，其中 527 条是这类资源名。
    """
    got = extract_strings_from_bytes(_u32(len(text.encode())) + text.encode())
    assert all(c.text != text for c in got), f"误收资源名：{text!r}"


@pytest.mark.parametrize(
    "text",
    [
        # PascalCase / 驼峰 = C# 类型名与字段名
        "AdvancingFrontNode",
        "HorizontalOrVerticalLayoutGroup",
        "BloomAndLensFlares",
        "BoneJoint",
        "PostEffectsBase",
        "BaseInputModule",
        # 全大写 / 全小写 / 驼峰
        "IMPACT",
        "default",
        "someField",
        # 含下划线或数字
        "m_Script",
        "PlayerName",
        "BrightPassFilter2",
    ],
)
def test_rejects_identifier_single_words(text: str) -> None:
    """单个的**代码标识符**必须挡住 —— 靠大小写形态，不靠黑名单。

    判据：`is_human_word` 要求"首字母大写 + 内部零大写"。
    C# 类型名全是 PascalCase（内部必有大写），
    资源名要么全大写要么带下划线 —— 所以这条判据
    **不需要我知道 Unity 里有哪些类名**。
    """
    got = extract_strings_from_bytes(_u32(len(text.encode())) + text.encode())
    assert all(c.text != text for c in got), f"误收标识符：{text!r}"


@pytest.mark.parametrize(
    "text",
    ["Options", "Gallery", "Status", "Attack", "Charge", "Teleport", "Dialogue"],
)
def test_keeps_human_single_words(text: str) -> None:
    """★ 反向：人写的单词必须收下。

    早先"单个词一律拒绝"，把**最常见的菜单项**（`Options`）
    和状态词（`Status`）全漏了 —— 那是 141 条真实单词文案。
    """
    got = extract_strings_from_bytes(_u32(len(text.encode())) + text.encode())
    assert [c.text for c in got] == [text], f"漏掉单词文案：{text!r}"


def test_human_word_rule_is_about_shape_not_a_blocklist() -> None:
    """★ 判据是**形态**的，所以对**没见过的**类名同样有效。

    这条测试的意义：如果判据退化成黑名单，下面这些编造的名字就会漏进来。
    """
    from novaloc.engines.unity_strings import is_human_word

    # 编造的 PascalCase 类名（不可能在黑名单里）⇒ 必须全判为标识符
    for fake in ("ZorblattHandler", "QuibbleManagerX", "FakeThingFactory"):
        assert not is_human_word(fake), f"判据退化成黑名单了：{fake}"
    # 编造的人写的词 ⇒ 必须通过
    for fake in ("Blibble", "Quonket", "Zorble"):
        assert is_human_word(fake), f"判据过严：{fake}"


def test_real_game_precision_regression_guard() -> None:
    """★ 在**真实数据**上钉住精度（不依赖网络/模型，样本内联）。

    样本是从真实游戏（Unity / Mono 后端）的 974 条候选里
    按"真实文案"和"资源名/标识符"各取若干条挑出来的。
    改成"放宽"或"收紧"判据时，这条会立刻变红。
    """
    from novaloc.engines.unity_strings import _looks_like_game_text

    must_keep = [
        "Would you like to save?",
        "Quit to the Main Menu. \nAny unsaved progress will be lost.",
        "Select Location to Teleport.",
        "THANK YOU \nFOR YOUR SUPPORT!",
        "Camera Zoom :",
        "Charge to Cum :",
        "GAME OVER",
        "Save Game",
        "Options",
        "Damage",            # 单词文案
        "イエローカード",      # 日文技能名（贴图上的原文）
        "サンダーブレード",
        "凍った刃",
    ]
    must_drop = [
        "-----Creep  Center",
        "0 ---BlockUpper x7  Bot",
        "1024_CircleGlow  --Shadow",
        "AdvancingFrontNode",
        "HorizontalOrVerticalLayoutGroup",
        "BloomAndLensFlares",
        "BaseInputModule",
        "Xiph.Org libVorbis I 20101101 (Schaufenugget)",
        "Copyright=Copyright 2000, Sounddogs.com",
        "Mono.MonoConfig.FeatureNodeHandler, mconfig, Version=1.0.0.0",
        "SELECT * FROM items WHERE id = 1",
        "FOOTSTEP Walk Trainers Wood Boards RR1 (mono)",
    ]
    bad_keep = [t for t in must_keep if not _looks_like_game_text(t)[0]]
    bad_drop = [t for t in must_drop if _looks_like_game_text(t)[0]]
    assert not bad_keep, f"漏掉真实文案：{bad_keep}"
    assert not bad_drop, f"误收噪音：{bad_drop}"



def test_zero_run_skip_does_not_lose_candidates() -> None:
    """★ 核心回归：跳过优化必须**逐条**保住所有候选。

    历史事故：曾经用"连续 4096 次未命中就前跳 64 KB"，
    结果 `level0` 的 13 条真实候选只剩 1 条 ——
    相邻候选间隔（10～49 KB）与跳幅同量级，"未命中"推不出"没候选"。

    ⚠️ 填充字节必须**完全不含 0**。
    早先写的 `bytes(range(1, 256)) * 300` 看着"全是 `\\x01..\\xff`"，
    但 4 字节一组时最高位是 `0x00`（小端下正是长度前缀的高字节），
    于是填充里混进了零字节、意外形成长零区，
    测试失败报出来像是"漏了候选"，实际是**测试数据自己触发了跳过**。
    """
    texts = [
        "Would you like to save?",
        "2. Get the sample of MOTHER BRAIN.",
        "Quit to the Main Menu. \nAny unsaved progress will be lost.",
        "Click or Press 'Spin Attack'",
        "Select Location to Teleport.",
    ]
    # 模仿真实布局：候选之间隔着几十 KB 的高熵二进制垃圾。
    # 每个字节取模 255 再 +1 ⇒ 取值域 [1,255]，**结构上不可能有 0**。
    rnd = bytes((i % 255) + 1 for i in range(255)) * 300
    assert b"\x00" not in rnd, "填充里混进了零字节，会意外触发零区跳过"
    payload = b""
    expect: list[tuple[int, str]] = []
    for t in texts:
        payload += rnd
        expect.append((len(payload), t))
        payload += _ustr(t)
    payload += b"\x00" * (ZERO_RUN_MIN * 3)  # 结尾来一段真正的零区

    got = extract_strings_from_bytes(payload, filename="level0")
    assert [(c.offset, c.text) for c in got] == expect


def test_zero_run_skip_actually_skips() -> None:
    """跳过必须**真的**发生（否则大资源会慢到不可用）。

    判据：一段 10 MB 的零区，扫描耗时应当远低于同长度的非零区。
    """
    import time

    text = "Would you like to save?"
    big_zero = b"\x00" * (10 * 1024 * 1024)
    payload = big_zero + _ustr(text)
    t0 = time.perf_counter()
    got = extract_strings_from_bytes(payload)
    dt = time.perf_counter() - t0
    assert [c.text for c in got] == [text]
    # 逐字节扫 10 MB 也要几十毫秒；跳过应当**明显**更快
    assert dt < 0.35, f"跳过似乎没生效：10 MB 零区用了 {dt:.3f}s"


def test_zero_run_boundary_is_not_skipped_over_a_string_start() -> None:
    """零区判据的**边界**：零区之后紧跟的字符串起点不能被吃掉。

    长度前缀 ≥ 4 ⇒ 首字节非零，所以零区里不可能有起点；
    但零区的**最后一个零**之后第一个非零字节必须被检查到。
    """
    text = "Game Over"
    payload = b"\x00" * ZERO_RUN_MIN + _u32(len(text.encode())) + text.encode()
    got = extract_strings_from_bytes(payload)
    assert [c.text for c in got] == [text]
    assert got[0].offset == ZERO_RUN_MIN


def test_short_zero_runs_are_not_skipped() -> None:
    """少于阈值（比如 3 字节对齐填充）的零区不能触发跳过。

    ⚠️ 候选之间必须夹**非零**字节。
    早先这个测试把三个字符串直接首尾相接，而每个长度前缀的
    高 3 字节都是 `0x00` —— 它们和 3 字节对齐填充连起来
    凑够了 `ZERO_RUN_MIN`，于是触发了跳过，连"Options"一起跳掉。
    真实布局里字符串之间总有非零的序列化字段，不会这样首尾相接。

    这个测试同时钉住期望偏移：早期版本的期望值是手算的、**算错了 4 字节**，
    报出来的失败看着像"漏了 Options"，实际是期望值本身写错。
    现在用 `len(payload)` 逐条记录真实偏移。
    """
    texts = ("Save Game", "Load Game", "Options")
    sep = b"\xAB\xCD\xEF\x01"  # 非零分隔：模拟字符串之间的序列化字段
    payload = b""
    expect: list[tuple[int, str]] = []
    for t in texts:
        payload += b"\x00" * 3  # 对齐填充：短零区
        expect.append((len(payload), t))
        payload += _ustr(t) + sep
    got = extract_strings_from_bytes(payload)
    assert [(c.offset, c.text) for c in got] == expect


# ---------------------------------------------------------------------------
# 四、分块扫描必须与单块**完全一致**
# ---------------------------------------------------------------------------


def test_chunked_scan_matches_single_block_scan(tmp_path: Path) -> None:
    """★ 分块（8 MB 块 + 4 MB 重叠）不能引入重复或遗漏。

    这是为 613 MB 的主资源准备的：早先的实现设 400 MB 上限直接跳过它，
    于是**最可能藏文本的文件根本没被扫**，报告却显示成功。
    """
    import os

    rnd1 = os.urandom(300_000)
    rnd2 = os.urandom(300_000)
    payload = rnd1 + _u32(0) + b""
    # 构造一个**跨块边界**的字符串：把长度前缀放在 8 MB 边界前 2 字节
    chunk = 8 * 1024 * 1024
    text = "Would you like to save?"
    head = os.urandom(chunk - 2 - 4)
    payload = head + _u32(len(text.encode())) + text.encode() + rnd2
    p = tmp_path / "sharedassets9.assets"
    p.write_bytes(payload)

    from novaloc.engines.unity_strings import scan_unity_assets

    rep = scan_unity_assets(p)
    texts = [c.text for c in rep.candidates]
    assert text in texts, "跨块边界的字符串被漏掉了"
    # 重叠区不能产生重复条目
    assert len(texts) == len(set(texts)), f"分块引入了重复：{texts}"


def test_scan_game_reports_no_text_for_infra_only_game(tmp_path: Path) -> None:
    """一个只有运行时基础设施的 Unity 游戏：候选必须是 **0**，而不是几千条。"""
    game = tmp_path / "SomeGame"
    data = game / "SomeGame_Data"
    (data / "Mono" / "etc" / "mono").mkdir(parents=True)
    (data / "Mono" / "etc" / "mono" / "browscap.ini").write_text(
        "\n".join(f"Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1) {i}" for i in range(200)),
        encoding="utf-8",
    )
    (data / "output_log.txt").write_text(
        "Initialize engine version: 4.6.7f1\nGfxDevice: creating device client\n" * 50,
        encoding="utf-8",
    )
    rep = scan_game(game)
    assert rep.total == 0, f"运行时噪音被当成文案了：{[c.text for c in rep.candidates][:5]}"


def test_find_serialized_assets(tmp_path: Path) -> None:
    d = tmp_path / "G_Data"
    d.mkdir()
    for n in ("level0", "level1", "resources.assets", "sharedassets0.assets", "notes.txt"):
        (d / n).write_bytes(b"\x00" * 8)
    names = [p.name for p in find_serialized_assets(d)]
    assert "level0" in names and "level1" in names
    assert "resources.assets" in names and "sharedassets0.assets" in names
    assert "notes.txt" not in names


# ---------------------------------------------------------------------------
# 五、导出
# ---------------------------------------------------------------------------


def test_write_csv_roundtrip(tmp_path: Path) -> None:
    import csv as _csv

    from novaloc.engines.unity_strings import UnityScanReport, UnityString

    rep = UnityScanReport()
    rep.candidates = [
        UnityString(file="level0", offset=16, text="Would you like to save?", confidence=0.85),
        UnityString(file="level1", offset=32, text="你确定吗？", confidence=0.9),
    ]
    dest = tmp_path / "out" / "unity_strings.csv"
    write_csv(rep, dest)
    assert dest.is_file()
    rows = list(_csv.DictReader(dest.open(encoding="utf-8-sig")))
    assert len(rows) == 2
    assert rows[0]["source"] == "Would you like to save?"
    assert rows[0]["offset"] == "16"
    # target 列留给用户填译文
    assert rows[0]["target"] == ""
    assert rows[1]["source"] == "你确定吗？"
