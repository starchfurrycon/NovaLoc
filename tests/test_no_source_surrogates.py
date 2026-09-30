"""守卫：仓库里**任何** Python 文件都不得含真代理项。

## 这个测试为什么存在

写"代理项净化"的测试时，我在**测试文件的 docstring 里**写了一个
`\\uXXXX` 转义（单反斜杠）。后果非常反直觉：

* 文件本身是**合法 UTF-8**（字节里当然没有代理项，代理项在 UTF-8 里
  本来就没有合法编码）；
* `ast.parse()` 也**通过**；
* 但 CPython 的**词法分析**会把这个转义**在编译期**解码成一个真的
  代理项，放进 code object 的字符串常量里；
* 于是 `py_compile` / `import` 写 `.pyc` 时炸：

      UnicodeEncodeError: 'utf-8' codec can't encode character '\\uddd1'
      in position 752: surrogates not allowed

**这个测试文件自己都 import 不进来**，pytest 收集阶段就报错。
排查花了不少时间，因为所有"看字节""看字符"的检查都说没问题 ——
问题出在**编译产物**里，不在源码字节里。

所以要有这条守卫：它直接检查 **AST 里的字符串常量**，
这正是 `.pyc` 会写的东西。任何新增文件里再出现这种写法都会被拦下。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = ("src", "tests")


def _iter_py_files():
    for d in SCAN_DIRS:
        base = ROOT / d
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))


def _surrogate_lines(path: Path) -> list[tuple[int, str]]:
    """返回 ``[(行号, 该常量里的代理项码点)]``。

    注意用 `ast.parse` 之后再看 `ast.Constant` —— 这才是编译器实际
    放进 code object 的东西。直接扫源码字符是**查不出来**的。
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            hits = sorted({ord(c) for c in node.value if 0xD800 <= ord(c) <= 0xDFFF})
            if hits:
                out.append((node.lineno, ", ".join(hex(h) for h in hits)))
    return out


def test_no_python_file_contains_a_real_surrogate() -> None:
    problems: list[str] = []
    for p in _iter_py_files():
        for lineno, hits in _surrogate_lines(p):
            problems.append(f"{p.relative_to(ROOT)}:{lineno} 含 {hits}")

    assert not problems, (
        "以下文件在**编译产物**里含真代理项，会导致自身无法 import"
        "（写 .pyc 时 UnicodeEncodeError）：\n  "
        + "\n  ".join(problems)
        + "\n\n修法：把字符串里的 `\\uXXXX` 转义改成 `chr(0xXXXX)`，"
        "或在文档里写成双反斜杠 `\\\\uXXXX`。代理项只能存在于运行期字符串里。"
    )


def test_the_scanner_actually_detects_a_planted_surrogate(tmp_path: Path) -> None:
    """**守卫本身必须有效**：塞一个真代理项进去，扫描器要能发现。

    没有这条，"扫不出问题"可能只是扫描器写错了（本项目里已经有过
    "指标写错比没有指标更糟"的教训）。
    """
    bad = tmp_path / "planted.py"
    # 用 chr() 之外的办法种进去：这里直接构造 source 文本，
    # 其中 `\u` 转义会被 tokenizer 解码成真代理项。
    bad.write_text('X = "' + chr(0x5C) + 'uddd1"\n', encoding="utf-8")
    hits = _surrogate_lines(bad)
    assert hits, "扫描器没能发现植入的代理项 —— 守卫本身失效了"

    clean = tmp_path / "clean.py"
    clean.write_text('X = chr(0xDDD1)\n', encoding="utf-8")
    assert not _surrogate_lines(clean), "扫描器对 chr() 写法误报"


def test_real_sanitize_module_imports_cleanly() -> None:
    """净化模块本身必须能 import（写 .pyc 不炸）。"""
    sys.path.insert(0, str(ROOT / "src"))
    from novaloc.core.sanitize import (  # noqa: F401
        has_lone_surrogate,
        sanitize_for_json,
        sanitize_tree,
    )

    assert sanitize_for_json("ok") == "ok"
