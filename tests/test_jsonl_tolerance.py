"""``_read_jsonl`` 的容错行为。

## 这个函数要同时做到两件看起来矛盾的事

1. **单行坏了不能毁掉整个列表** —— `entries.jsonl` 可能有几万行，
   坏一行就把整批译文丢掉，用户会以为翻译全没了。所以**跳过**。
2. **但必须留下痕迹** —— 这些文件是给用户手改的（审校页和脚本都写它）。
   改坏一行后静默跳过 = "用户改的译文凭空消失"，而报告里一切正常。
   这正是本项目反复栽跟头的那一类：**静默部分丢弃**。

所以判据是：**返回值照旧，同时 `log.warning` 必须报出行号与原因**。
只测第 1 条会放过最危险的半边。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.workspace import _read_jsonl  # noqa: E402
from novaloc.models import TranslationEntry  # noqa: E402


def _line(uid: str, target: str = "你好") -> str:
    return TranslationEntry(uid=uid, source="Hello", target=target).model_dump_json()


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "entries.jsonl"
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert _read_jsonl(tmp_path / "nope.jsonl", TranslationEntry) == []


def test_happy_path(tmp_path: Path) -> None:
    p = _write(tmp_path, "\n".join([_line("a"), _line("b")]) + "\n")
    out = _read_jsonl(p, TranslationEntry)
    assert [e.uid for e in out] == ["a", "b"]


def test_blank_lines_are_skipped_silently(tmp_path: Path) -> None:
    """空行是**正常**的（文件末尾换行），不该报警。"""
    p = _write(tmp_path, _line("a") + "\n\n\n" + _line("b") + "\n\n")
    out = _read_jsonl(p, TranslationEntry)
    assert len(out) == 2


def test_bad_line_is_skipped(tmp_path: Path) -> None:
    """★ 坏行不能毁掉整个列表。"""
    p = _write(tmp_path, "\n".join([_line("a"), "{不是合法 json", _line("b")]) + "\n")
    out = _read_jsonl(p, TranslationEntry)
    assert [e.uid for e in out] == ["a", "b"], "坏行必须只影响它自己"


def test_bad_line_is_reported(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """★ 跳过必须留下痕迹 —— 这是本文件存在的理由。

    只测 `test_bad_line_is_skipped` 会放过最危险的半边：
    用户手改的文件里坏了一行，译文静默消失而报告一切正常。
    """
    p = _write(tmp_path, "\n".join([_line("a"), "{不是合法 json", _line("b")]) + "\n")
    with caplog.at_level(logging.WARNING, logger="novaloc.core.workspace"):
        _read_jsonl(p, TranslationEntry)
    text = caplog.text
    assert "跳过 1 行" in text, f"必须报出跳过了几行，实际日志：{text!r}"
    assert "第 2 行" in text, "必须报出**行号**，否则用户无从下手"
    assert "entries.jsonl" in text, "必须报出**文件名**"
    assert "已有 2 条正常读入" in text, "要说明还剩多少条，否则用户以为全丢了"


def test_valid_json_but_wrong_shape_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """JSON 合法但**字段不对**（比如手工删了一个字段）也要报。

    只认 `JSONDecodeError` 是不够的：用户最容易犯的错是
    "复制一条改内容"，很可能把 `source` 删掉。
    """
    p = _write(tmp_path, '{"uid":"a","target":"你好"}\n')
    with caplog.at_level(logging.WARNING, logger="novaloc.core.workspace"):
        out = _read_jsonl(p, TranslationEntry)
    assert out == []
    assert "跳过 1 行" in caplog.text


def test_many_bad_lines_summarised(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """坏行很多时不刷屏 —— 只列前 3 条 + 总数。"""
    lines = [_line("a")] + ["{坏"] * 10 + [_line("b")]
    p = _write(tmp_path, "\n".join(lines) + "\n")
    with caplog.at_level(logging.WARNING, logger="novaloc.core.workspace"):
        out = _read_jsonl(p, TranslationEntry)
    assert len(out) == 2
    assert "跳过 10 行" in caplog.text
    assert "另有 7 行" in caplog.text
    # 日志总长要可控：不能把 10 条错误全打出来
    assert caplog.text.count("第 ") <= 4


def test_no_warning_on_clean_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """干净文件不该有任何警告（否则用户会习惯性忽略警告）。"""
    p = _write(tmp_path, _line("a") + "\n" + _line("b") + "\n")
    with caplog.at_level(logging.WARNING, logger="novaloc.core.workspace"):
        _read_jsonl(p, TranslationEntry)
    assert caplog.text == "", f"干净文件不该报警，实际：{caplog.text!r}"
