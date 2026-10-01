r"""下载到的**不是字体**时必须拒绝 —— 否则一次失败会让它**永久坏掉**。

## 事故：一个 HTML 错误页被当成字体永久缓存

实测发现 `D:\NovaLoc\fonts\cache\lxgw-wenkai-screen.ttf` 的开头是
`<b'<!DO'` —— 一个 HTML 错误页，157 KB。

它**轻松通过了**下载器那条 `min_bytes=10240` 的检查，
于是被当成"已缓存的字体"写进磁盘。后果不是"某个字体用不了"：

* 它对**每一轮** `fonts` 阶段都是"本机已有这个字体"
  （缓存命中路径只看扩展名 `.ttf`），于是**永远不再重新下载**；
* **一次下载失败让这个字体永久坏掉**，而且没有任何东西会报错；
* 直到**合并阶段**才炸（`Not a TrueType or OpenType font`）——
  报错点离病因隔了好几个阶段。

这个 bug 是被**测试套件**抓到的：两个字体测试突然变红，
而代码其实没动过。

## 教训

**只检查文件大小的校验挡不住 HTML 错误页。**
`min_bytes` 的注释里写的就是"用来挡住下载到一个 HTML 错误页"——
但它挡的是**小**错误页（404 页面常是 1~2 KB），
而带 CSS/JS 的错误页、登录跳转页、反爬页轻松超过 10 KB。

内容类型必须**按内容本身判**，不能按长度判。
这在别处也一样：本项目前面已经栽过一次同类问题
（`os.kill(pid, 0)` 在 Windows 上不是存活性测试 ——
API 的名字暗示了错误的语义）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.fonts.service import _looks_like_font_file  # noqa: E402

#: 真实字体文件的魔数（一个都不能漏）
VALID_HEADS = {
    "truetype": b"\x00\x01\x00\x00",
    "opentype_cff": b"OTTO",
    "mac_truetype": b"true",
    "collection": b"ttcf",
    "woff": b"wOFF",
    "woff2": b"wOF2",
}

#: 会伪装成字体的**非字体**内容 —— 全部来自真实事故或常见错误页形态
INVALID_HEADS = {
    "真实事故：HTML 错误页": b"<!DOCTYPE html><html><head><title>404</title>",
    "带 BOM 的 HTML": b"\xef\xbb\xbf<!DOCTYPE html>",
    "JSON 错误响应": b'{"error":"not found","code":404}',
    "纯文本 404": b"Not Found\nThe requested URL was not found",
    "XML 错误页": b"<?xml version=\"1.0\"?><Error><Code>NoSuchKey</Code>",
    "GitHub 限流页": b"<html><body>Rate limit exceeded</body></html>",
    "空文件": b"",
    "太短": b"ab",
    "gzip 压缩流（没解压）": b"\x1f\x8b\x08\x00\x00\x00\x00\x00",
    "PNG（不是字体）": b"\x89PNG\r\n\x1a\n",
    "ZIP": b"PK\x03\x04",
}


def _write(tmp_path: Path, name: str, head: bytes, size: int = 0) -> Path:
    """写一个以 ``head`` 开头、总长 ``size`` 的文件。

    ``size`` 可以超过 `min_bytes` —— 用来证明**大小检查挡不住错误页**。
    """
    p = tmp_path / name
    body = head + b"\x00" * max(0, size - len(head))
    p.write_bytes(body)
    return p


def test_html_error_page_is_rejected(tmp_path: Path) -> None:
    """★ 核心：真实事故里那个 HTML 错误页必须被认出来。"""
    p = _write(tmp_path, "lxgw-wenkai-screen.ttf", INVALID_HEADS["真实事故：HTML 错误页"])
    assert not _looks_like_font_file(p), "HTML 错误页被当成了字体"


def test_size_check_alone_would_have_passed(tmp_path: Path) -> None:
    """★ 证明"只查大小"是不够的：这个错误页有 157 KB。

    这条是整份修复的**理由** —— 下载器那条 `min_bytes=10240`
    对它是完全无效的。
    """
    p = _write(
        tmp_path, "big-error-page.ttf",
        INVALID_HEADS["真实事故：HTML 错误页"], size=157 * 1024,
    )
    assert p.stat().st_size > 10240, "前提不成立：这个错误页应该超过 min_bytes"
    assert not _looks_like_font_file(p), "超过 10 KB 的错误页仍然必须是非法字体"


def test_all_valid_font_magics_accepted(tmp_path: Path) -> None:
    """6 种真实字体魔数一个都不能漏（漏了会误删好字体）。"""
    for name, magic in VALID_HEADS.items():
        p = _write(tmp_path, f"{name}.ttf", magic, size=2048)
        assert _looks_like_font_file(p), f"{name}（{magic!r}）被误判成非字体"


def test_all_invalid_content_rejected(tmp_path: Path) -> None:
    """各种"伪装成字体的非字体"内容全部拒绝。"""
    for i, (why, head) in enumerate(INVALID_HEADS.items()):
        p = _write(tmp_path, f"bad{i}.ttf", head)
        assert not _looks_like_font_file(p), f"{why} 被误判成字体"


def test_missing_file_is_not_a_font(tmp_path: Path) -> None:
    """文件不存在 → False（不能抛异常，调用方在上传/缓存路径上）。"""
    assert not _looks_like_font_file(tmp_path / "does-not-exist.ttf")


def test_directory_is_not_a_font(tmp_path: Path) -> None:
    """路径是目录 → False（不能抛 `IsADirectoryError`）。"""
    d = tmp_path / "a-dir.ttf"
    d.mkdir()
    assert not _looks_like_font_file(d)
