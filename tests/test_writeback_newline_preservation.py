r"""★ 回归：写回**不得**改变原文的行尾（CRLF 不能变成 LF）。

## 现场（`.scratch/_ini_change_probe.py` 实测）

GameMaker 游戏 `Sloppy_Fields_0.5.1.3_-_Subscribestar`，跑一次 `auto`：

    跑之前  b'[Windows]\r\nSleepMargin=10\r\nUsex64=True\r\n'   40 字节
    跑之后  b'[Windows]\nSleepMargin=10\nUsex64=True\n'        37 字节
                            ↑ 三个 `\r` 全没了

**文件内容一字未改，字节数却变了。**

## 根因：`read_text()` 默认做**通用换行翻译**

`Path.read_text()` 不传 `newline=` 时，底层 `open()` 用 `newline=None`
⇒ **读进来时 `\r\n` 已被翻译成 `\n`**。之后
`splitlines(keepends=True)` 拿到的行尾已经是 `\n`，
最后 `write_text(newline="")`（正确地）不做翻译地写出 ——
于是 `\r` **在读取那一步就已经丢了**。

最小复现（与项目代码无关）：

    p.write_bytes(b'a\r\nb\r\n')
    p.read_text().splitlines(keepends=True)   # ['a\n', 'b\n']  ← 注意不是 'a\r\n'

## 为什么必须修（三条，都不是洁癖）

1. **与翻译无关的改动**：`.ini`/`.cfg` 里可能一条译文都没有，文件却被改了
   ⇒ 用户拿到的是"没汉化、配置还被动了"；
2. **可能破坏依赖 CRLF 的程序**：尤其 Windows 原生 / C# 写的启动器；
3. **制造"假阳性改动"**：哈希变了但内容没变，
   排查"文件到底改没改"时极易被带偏（我就差点被带偏）。

## 影响面（**不止 `loose`**）

`loose` / `unity`（3 处）/ `renpy` 的写回都走"按行替换"这条路，
全部有同一个缺陷。所以本模块**同时做行为测试与源码级守卫** ——
行为测试只能覆盖我构造的那几条路径，源码级守卫才能保证
"以后新加的写回路径也别忘了 `newline=\"\"`"。
"""

from __future__ import annotations

import inspect

#: ★ 见 `tests/conftest.py` 的说明：这些标记是**实际探测**而非仅描述，
#: 免得忘了打 `needs_fonts` 就照常跑然后失败（CI 上实测过）。
from types import SimpleNamespace

import pytest
from conftest import windows_only  # noqa: E402

from novaloc.engines.loose import LooseFilesAdapter
from novaloc.models import TextKind, TextLocation, TextUnit

pytestmark = windows_only


CRLF = b"[Windows]\r\nSleepMargin=10\r\nUsex64=True\r\n"
LF = b"[Windows]\nSleepMargin=10\nUsex64=True\n"


def _ctx(tmp_path: object) -> SimpleNamespace:
    """最小 Context 替身（`BaseAdapter.__init__` 会读 `ctx.config`）。"""
    return SimpleNamespace(config=None, events=None, logger=None, root=tmp_path)


def _unit(source: str, line: int, fname: str = "options.ini") -> TextUnit:
    return TextUnit(
        uid=f"{fname}:L{line}",
        source=source,
        kind=TextKind.SYSTEM,
        location=TextLocation(file=fname, pointer=f"line:{line}", line=line),
    )


def _apply(tmp_path, raw: bytes, units, translations, fname="options.ini"):
    """跑一遍 `LooseFilesAdapter.apply`，返回写回后的字节。"""
    game = tmp_path / "game"
    out = tmp_path / "out"
    game.mkdir(parents=True, exist_ok=True)
    (game / fname).write_bytes(raw)
    ad = LooseFilesAdapter(_ctx(tmp_path))
    res = ad.apply(game, out, units, translations)
    return (out / fname).read_bytes(), res


# ---------------------------------------------------------------------------
# 1) 行为：原文行尾必须原样保留
# ---------------------------------------------------------------------------
def test_crlf_is_preserved_when_a_line_is_translated(tmp_path) -> None:
    """★★ 本条是本次修复的核心：替换一行之后，其余行尾仍是 CRLF。

    判据是"写回后的字节里**仍然**有 `\\r\\n`"，而不是"字节数相同" ——
    字节数会因为译文长短变化，不能当判据。
    """
    units = [_unit("SleepMargin=10", 2)]
    got, _res = _apply(tmp_path, CRLF, units, {units[0].uid: "SleepMargin=99"})
    assert b"\r\n" in got, f"CRLF 必须保留，实际 {got!r}"
    assert got.count(b"\r\n") == 3, f"三行都该是 CRLF，实际 {got!r}"
    assert b"\n" not in got.replace(b"\r\n", b""), "不该出现裸 LF"
    assert b"SleepMargin=99" in got, "译文必须写进去"


def test_lf_input_stays_lf(tmp_path) -> None:
    """反向：**本来**是 LF 的文件不该被"修"成 CRLF。"""
    units = [_unit("SleepMargin=10", 2)]
    got, _res = _apply(tmp_path, LF, units, {units[0].uid: "SleepMargin=99"})
    assert b"\r" not in got, f"LF 输入不该冒出 CR，实际 {got!r}"


def test_no_translation_means_no_write(tmp_path) -> None:
    """没有可用译文时**一个字节都不该动**。

    这一条同样重要：`options.ini` 里那三条是引擎配置，
    `_is_engine_config_line` 会把它们过滤掉 ⇒ 没有译文 ⇒ 不该有写回。
    """
    units = [_unit("SleepMargin=10", 2)]
    got, res = _apply(tmp_path, CRLF, units, {})  # 空译文表
    assert got == CRLF, f"没有译文就不该改文件，实际 {got!r}"
    assert res.files_written == 0


def test_crlf_preserved_with_multiple_edits(tmp_path) -> None:
    """多行同时替换时也要保住 CRLF（别只修了单行那条路径）。"""
    units = [_unit("[Windows]", 1), _unit("Usex64=True", 3)]
    got, _res = _apply(
        tmp_path, CRLF, units,
        {units[0].uid: "[视窗]", units[1].uid: "Usex64=真"},
    )
    assert got.count(b"\r\n") == 3, f"实际 {got!r}"
    assert "[视窗]".encode() in got
    assert "Usex64=真".encode() in got


def test_bytes_only_differ_in_translated_segment(tmp_path) -> None:
    """★ 除被替换的那一段外，其余字节**逐一相同**。

    这是最贴切的判据：它同时排除"行尾被改"和"别的字节被动过"。
    """
    units = [_unit("SleepMargin=10", 2)]
    got, _res = _apply(tmp_path, CRLF, units, {units[0].uid: "SleepMargin=99"})
    before = CRLF.replace(b"10", b"99", 1)
    assert got == before, f"期望 {before!r}\n实际 {got!r}"


# ---------------------------------------------------------------------------
# 2) 源码级守卫：所有"按行替换"的写回都必须 newline="" 成对出现
# ---------------------------------------------------------------------------
def _strip_comments(src: str) -> str:
    """去掉注释与字符串**之前**先用它做匹配的辅助。

    ⚠️ 必须去注释：我在 `loose.py` 里写了一段**演示这个 bug 的注释**，
    里面就有裸的 `read_text()` —— 第一版守卫把它当成了真代码而误报。
    （另一处误报：`renpy.py` 的 `read_text(...).splitlines()` 是**只读**用法，
    不写回，本来就不该被这条判据管。）
    """
    import io
    import tokenize

    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                # 注释保留一个占位，避免把两行粘在一起
                out.append("\n")
            else:
                out.append(tok.string)
    except tokenize.TokenError:
        return src
    return "".join(out)


def _read_text_calls_without_newline(src: str) -> list[str]:
    """找出"读了又写"、但读取时没传 `newline=` 的可疑调用。

    判据收窄到**真正会丢 CRLF 的形态**：

    * `read_text(...)` 后面紧跟 `.splitlines(keepends=True)`
      —— 这是"按行替换后整文件写回"的写法（`keepends=True` 是关键：
      保留行尾才有"把行尾原样写回"的意图）；
    * 且参数里没有 `newline=`。

    ⚠️ 不把 `splitlines()`（无 `keepends`）算进来：那种用法拿到的行
    **本来就不带行尾**，是纯读不写，不该被这条判据管
    （`renpy.py` L221 就是这种，第一版误报了它）。
    """
    src = _strip_comments(src)
    bad: list[str] = []
    idx = 0
    while True:
        i = src.find("read_text(", idx)
        if i < 0:
            break
        depth = 0
        j = i + len("read_text(") - 1
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        call = src[i : j + 1]
        after = src[j + 1 : j + 60]
        if "splitlines(keepends=True)" in after and "newline=" not in call:
            bad.append(call)
        idx = j + 1
    return bad


@pytest.mark.parametrize("module", ["loose", "unity", "renpy"])
def test_line_based_writeback_reads_with_newline_empty(module: str) -> None:
    """★★ 源码级守卫：按行替换的写回，读取必须传 `newline=""`。

    ⚠️ 为什么光有行为测试不够：行为测试只能覆盖我**构造**出来的路径。
    这个缺陷的本质是"少写一个参数"，它**没有任何运行时报错**，
    只会静默改字节 —— 新增一个引擎适配器时极容易再犯。
    源码级守卫能把"以后新加的写回路径"也一起管住。
    """
    import importlib

    mod = importlib.import_module(f"novaloc.engines.{module}")
    src = inspect.getsource(mod)
    bad = _read_text_calls_without_newline(src)
    assert not bad, (
        f"novaloc/engines/{module}.py 里有按行替换的写回没传 newline=\"\"：\n"
        + "\n".join(f"  {b}" for b in bad)
        + "\n⇒ 会把原文的 CRLF 静默改成 LF（实测 40 → 37 字节）"
    )


def test_loose_apply_writes_with_newline_empty() -> None:
    """写回时也必须 `newline=""`（读、写必须**成对**）。"""
    from novaloc.engines.loose import LooseFilesAdapter as A

    src = inspect.getsource(A.apply)
    assert 'newline=""' in src, "写回必须 newline=\"\"，否则写出去的行尾会被再翻译一次"


def test_unity_kv_preserves_original_line_ending() -> None:
    """★ `unity._apply_kv` 不能把行尾硬写成 `\\n`。

    原实现是 `tail = "\\n" if lines[ln-1].endswith("\\n") else ""` ——
    即便读取那步修好了，这里仍会把 `\\r\\n` 换成 `\\n`
    （`"x\\r\\n".endswith("\\n")` 为真，于是只补 `\\n`）。

    ⚠️ 必须**先剥注释**：我在 `_apply_kv` 里写了一段注释**引用**了那行旧代码，
    第一版守卫把注释当代码而误报。
    """
    from novaloc.engines.unity import UnityAdapter

    src = _strip_comments(inspect.getsource(UnityAdapter._apply_kv))
    assert 'tail = "\\n" if' not in src, (
        "行尾不能硬写成 \\n —— 必须从原行里切出真实的 \\r\\n"
    )
    assert 'rstrip("\\r\\n")' in src, "应当用真实行尾长度来切"
