r"""★ 幂等回写：区分"真的写不出去"和"**早就写过了**"。

## 这是什么回归（我自己引入的）

我给 `out/` 加了"写回成功后回收"（省下 11.5 GB，ROADMAP §22）。
但回收之后，下一轮 `auto` 会这样走：

1. `prepare_out` 从**现盘**重新复制 `out/` —— 而现盘**已经是译文了**；
2. 于是 `changed_files()` 发现**零差异**；
3. `files_written == 0` 且 `by_file` 非空
   ⇒ `res.ok = False` ⇒ 报"没有任何文件被写入" ⇒ **整局被判 `failed`**。

实测受害者：`AliQ`（473 条全译完、8 个文件已写回），
下一轮变成 `✗ failed 条目 0 用时 10.7 s`，日志：

    PipelineError: [回写产物] 没有任何文件被写入

## 修法的判据

**游戏现盘的那个文件里是不是已经含有这条译文**。这是内容层面的证据，
比"比较 mtime""看备份在不在"都可靠（ROADMAP §13 的精神）。

要求**抽查全部命中**才算幂等 —— 只要有一条不在现盘里，
就说明不是"已应用"，仍按失败处理。
宁可报失败，也不能把"没写进去"说成"已经好了"。

## 本文件测 `_already_applied` 这个判据本身

它是 `stage_apply` 的私有方法，所以这里用一个最小的假 Pipeline 实例
来调它（不需要跑整条流水线）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.models import TextKind, TextLocation, TextUnit  # noqa: E402
from novaloc.pipeline.stages import Pipeline  # noqa: E402


class _FakeWs:
    """只有 `effective_source` 就够了 —— `_already_applied` 只用它。"""

    def __init__(self, src: Path) -> None:
        self.effective_source = src


def _mk_pipeline(src: Path) -> Pipeline:
    p = Pipeline.__new__(Pipeline)  # 不跑 __init__，避免需要完整 Context
    p.ws = _FakeWs(src)  # type: ignore[assignment]
    return p


def _unit(uid: str, rel: str) -> TextUnit:
    return TextUnit(
        uid=uid,
        source="Hello world, this is a line.",
        kind=TextKind.DIALOGUE,
        location=TextLocation(file=rel, pointer=uid),
    )


def _game(tmp_path: Path, rel: str, body: str) -> Path:
    src = tmp_path / "game"
    p = src / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return src


# ----------------------------------------------------------------------
# 命中：译文已经在现盘里
# ----------------------------------------------------------------------
def test_all_hits_when_translation_present(tmp_path: Path) -> None:
    """现盘文件里已含译文 ⇒ 全部命中。"""
    rel = "www/data/Map001.json"
    src = _game(tmp_path, rel, '{"a": "你好，旅行者。", "b": "欢迎来到村庄。"}')
    p = _mk_pipeline(src)
    units = [_unit("u1", rel), _unit("u2", rel)]
    tr = {"u1": "你好，旅行者。", "u2": "欢迎来到村庄。"}
    hit, checked = p._already_applied(units, tr)
    assert (hit, checked) == (2, 2)


def test_partial_hits_are_not_fully_applied(tmp_path: Path) -> None:
    r"""★ 只要有一条没命中 ⇒ 不算已应用（仍按失败处理）。

    这是**安全的一侧**：宁可报失败让用户看到，
    也不能把"没写进去"说成"已经好了"。
    """
    rel = "www/data/Map001.json"
    src = _game(tmp_path, rel, '{"a": "你好，旅行者。"}')
    p = _mk_pipeline(src)
    units = [_unit("u1", rel), _unit("u2", rel)]
    tr = {"u1": "你好，旅行者。", "u2": "这句根本不在文件里。"}
    hit, checked = p._already_applied(units, tr)
    assert checked == 2
    assert hit == 1, "只有一条命中，不该算全部已应用"


def test_no_hits_when_nothing_applied(tmp_path: Path) -> None:
    """译文一条都不在现盘 ⇒ 0 命中（真正的失败情形）。"""
    rel = "www/data/Map001.json"
    src = _game(tmp_path, rel, '{"a": "Hello world"}')
    p = _mk_pipeline(src)
    units = [_unit("u1", rel)]
    tr = {"u1": "你好，旅行者。"}
    hit, checked = p._already_applied(units, tr)
    assert (hit, checked) == (0, 1)


# ----------------------------------------------------------------------
# 边界：不能误判
# ----------------------------------------------------------------------
def test_missing_source_file_is_skipped(tmp_path: Path) -> None:
    """源文件不存在 ⇒ 这条无法判定，既不计命中也不计 checked。

    否则"找不到文件"会被当成"没写进去"，把幂等误判成失败。
    """
    src = tmp_path / "game"
    src.mkdir()
    p = _mk_pipeline(src)
    units = [_unit("u1", "nope.json")]
    hit, checked = p._already_applied(units, {"u1": "你好，旅行者。"})
    assert (hit, checked) == (0, 0)


def test_empty_translation_is_skipped(tmp_path: Path) -> None:
    """空译文/极短译文不参与判定（那是"没译出来"，不是"已应用"）。"""
    rel = "a.json"
    src = _game(tmp_path, rel, "x")
    p = _mk_pipeline(src)
    units = [_unit("u1", rel), _unit("u2", rel)]
    tr = {"u1": "", "u2": " x "}
    _hit, checked = p._already_applied(units, tr)
    assert checked == 0


def test_sample_limit_is_respected(tmp_path: Path) -> None:
    """抽查条数受 `sample` 限制（大游戏上不能全量读）。"""
    rel = "a.json"
    src = _game(tmp_path, rel, "你好，旅行者。")
    p = _mk_pipeline(src)
    units = [_unit(f"u{i}", rel) for i in range(100)]
    tr = {f"u{i}": "你好，旅行者。" for i in range(100)}
    _hit, checked = p._already_applied(units, tr, sample=7)
    assert checked == 7


def test_multiline_translation_matches_on_first_line(tmp_path: Path) -> None:
    r"""多行译文用**第一行的前若干字符**比对。

    RPG Maker 的 401 消息框天然是多行的，而源文件里换行是 `\n` 转义，
    直接整串匹配会全部失配 ⇒ 幂等判别会永远返回"没应用"。
    """
    rel = "www/data/CommonEvents.json"
    tgt = "马希罗：\n我真的听不懂你在说什么。\n等一下。"
    src = _game(tmp_path, rel, '{"x": "马希罗：\\n我真的听不懂你在说什么。\\n等一下。"}')
    p = _mk_pipeline(src)
    hit, checked = p._already_applied([_unit("u1", rel)], {"u1": tgt})
    assert (hit, checked) == (1, 1)
