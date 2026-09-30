"""Ren'Py 适配器。

Ren'Py 的文本在 ``game/**.rpy`` 脚本里，语法是：

.. code-block:: renpy

    label start:
        "这是一句对白。"                 # 无名对白
        e "旁白也可以带角色前缀。"        # 具名对白
        menu:
            "选项一":
                jump choice1
            "选项二":
                jump choice2
        define e = Character("Eileen")   # 角色名

要点与坑：

1. **只处理 ``.rpy``，不动 ``.rpyc``**。``.rpyc`` 是编译产物，
   改了游戏也不认；而只要 ``.rpy`` 存在，Ren'Py 启动时会重新编译它。
   如果游戏**只提供** ``.rpyc``（发行版常见），我们无法安全修改，
   必须在报告里明确说出来，而不是假装成功。

2. **``python:`` 块与 ``$`` 行绝不能翻** —— 那是可执行代码。

3. **字符串里含转义与插值**：``[variable]``（插值）、``{color=#fff}``
   （文本标签）、``\\n``。这些由占位符层加掩码保护。

4. **引号闭合并不能靠找最后一个引号**：``"她说：\\"好\\""`` 里有转义引号。
   必须按转义规则逐字符扫描。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..core.registry import register
from ..models import ExtractReport, FontCoverage, TextKind, TextLocation, TextUnit
from .base import ApplyResult, EngineAdapter, EngineInfo

log = logging.getLogger(__name__)

#: 对白行：``[缩进] [可选的 角色名 ] "文本" [with xxx]``
#: 角色名允许点号（``e``）、下划线、``store.var`` 形式
_DIALOGUE_RE = re.compile(
    r'^(?P<indent>[ \t]*)'
    r'(?P<who>[A-Za-z_][A-Za-z0-9_.]*[ \t]+)?'
    r'"(?P<text>(?:[^"\\]|\\.)*)"'
    r'(?P<tail>[ \t]*(?:with[ \t]+[A-Za-z0-9_.]+)?[ \t]*)$'
)

#: ``define e = Character("Eileen")`` / ``Character(_("Eileen"))``
_CHARACTER_RE = re.compile(
    r'^(?P<indent>[ \t]*)define[ \t]+(?P<var>[A-Za-z_][A-Za-z0-9_.]*)[ \t]*=[ \t]*'
    r'(?:_?\()?\s*Character\s*\(\s*'
    r'(?:_\()?\s*"(?P<name>(?:[^"\\]|\\.)*)"',
)

#: ``menu:`` 下面的选项行：``"选项文本":``
_MENU_ITEM_RE = re.compile(
    r'^(?P<indent>[ \t]+)"(?P<text>(?:[^"\\]|\\.)*)"[ \t]*:[ \t]*$'
)

#: 需要整段跳过的块起始
_CODE_BLOCK_RE = re.compile(r'^[ \t]*(python|init python|screen|transform)[ \t]*(:|[A-Za-z_])')

#: 可翻译的顶层语句：
#: ``define config.name = _("My Game")`` / ``old "..."`` / ``new "..."``
#: ``_()`` 是 gettext 包装，必须能穿透，否则配置里的游戏名抽不到。
_KEYWORD_STMT_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<kw>define|old|new|text|title|subtitle)[ \t]+'
    r'(?P<var>[A-Za-z_][A-Za-z0-9_.]*[ \t]*=[ \t]*)?'
    r'_?\(?\s*"(?P<text>(?:[^"\\]|\\.)*)"'
)

#: 纯符号/数字串不翻
_NOT_TEXT_RE = re.compile(r"^[\s\d\W_]+$", re.UNICODE)


@register("engine", "renpy")
class RenPyAdapter(EngineAdapter):
    """Ren'Py 适配器。"""

    id = "renpy"
    display_name = "Ren'Py"
    priority = 20

    # ------------------------------------------------------------------

    def detect(self, game_dir: Path) -> EngineInfo:
        info = EngineInfo(engine_id=self.id, display_name=self.display_name, root=game_dir)
        game = game_dir / "game"
        if not game.is_dir():
            # 有些打包版把 game/ 直接当成根
            game = game_dir
        rpy = list(game.glob("*.rpy")) + list(game.glob("**/*.rpy"))
        rpyc = list(game.glob("**/*.rpyc"))
        has_renpy_dir = (game / "renpy").is_dir()

        if not rpy and not rpyc and not has_renpy_dir:
            return info

        info.confidence = 0.3
        if has_renpy_dir:
            info.confidence += 0.35
            info.evidence.append("存在 renpy/ 运行时目录")
        if rpy:
            info.confidence = min(1.0, info.confidence + 0.35)
            info.evidence.append(f"发现 {len(rpy)} 个 .rpy 脚本")
        if rpyc:
            info.evidence.append(f"发现 {len(rpyc)} 个 .rpyc 编译脚本")
        if not rpy and rpyc:
            info.confidence *= 0.5
            info.evidence.append("⚠️ 只有 .rpyc 没有 .rpy，无法安全修改文本")

        # 找 options.rpy 之类的特征，进一步确认
        if (game / "options.rpy").is_file():
            info.confidence = min(1.0, info.confidence + 0.1)
            info.evidence.append("存在 options.rpy")
        try:
            import re as _re

            for f in (game / "options.rpy", game / "script.rpy"):
                if f.is_file():
                    head = f.read_text(encoding="utf-8", errors="replace")[:4000]
                    m = _re.search(r'config\.name\s*=\s*_?\("([^"]+)"', head)
                    if m:
                        info.evidence.append(f"游戏名：{m.group(1)!r}")
                    v = _re.search(r'config\.version\s*=\s*"([^"]+)"', head)
                    if v:
                        info.version = v.group(1)
                    break
        except Exception:  # noqa: BLE001
            pass

        return info

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------

    def extract_text(self, game_dir: Path) -> tuple[list[TextUnit], ExtractReport]:
        report = ExtractReport(adapter=self.id)
        units: list[TextUnit] = []
        game = game_dir / "game" if (game_dir / "game").is_dir() else game_dir

        files = sorted(p for p in game.rglob("*.rpy") if p.is_file())
        report.files_scanned = len(files)
        if not files:
            rpyc = list(game.rglob("*.rpyc"))
            if rpyc:
                report.errors.append(
                    f"只找到 {len(rpyc)} 个 .rpyc 编译脚本，没有 .rpy 源码。"
                    "Ren'Py 发行版常见此情况，无法安全地直接改写编译产物；"
                    "请向作者索取源码，或使用 Ren'Py 官方的翻译机制。"
                )
            else:
                report.errors.append("没有找到任何 .rpy 脚本")
            return units, report

        # ``define x = Character("Name")`` 是跨文件共享的角色名，
        # 单独先扫一遍并**去重**：同一个角色名在多个脚本里重复出现时，
        # 必须只翻一次、且全篇用同一个译名，否则会出现"同一个角色两种译名"。
        seen_names: set[str] = set()
        for f in files:
            try:
                rel = f.relative_to(game_dir).as_posix()
            except ValueError:
                rel = f.name
            for lineno, name in self._scan_characters(f):
                if name in seen_names:
                    continue
                seen_names.add(name)
                units.append(
                    TextUnit(
                        uid=f"{rel}:{lineno}:char",
                        source=name,
                        kind=TextKind.CHARACTER_NAME,
                        location=TextLocation(file=rel, pointer=f"L{lineno}", line=lineno),
                        tags=["renpy:character"],
                    )
                )

        for f in files:
            before = len(units)
            try:
                units.extend(self._extract_file(f, game, game_dir, skip_names=seen_names))
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{f.name} 解析失败：{exc}")
            if len(units) > before:
                report.files_matched += 1

        report.units = len(units)
        return units, report

    @staticmethod
    def _scan_characters(path: Path) -> list[tuple[int, str]]:
        """扫描 ``define x = Character("Name")``（含 ``_("Name")``）。"""
        out: list[tuple[int, str]] = []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return out
        for lineno, line in enumerate(text.splitlines(), 1):
            m = _CHARACTER_RE.match(line)
            if m:
                out.append((lineno, m.group("name")))
        return out

    def _extract_file(
        self, path: Path, game: Path, game_dir: Path, *, skip_names: set[str] | None = None
    ) -> list[TextUnit]:
        out: list[TextUnit] = []
        try:
            rel = path.relative_to(game_dir).as_posix()
        except ValueError:
            rel = path.name

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        in_code = False
        code_indent = 0

        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            indent = len(line) - len(line.lstrip(" \t"))

            # --- 代码块跟踪：python:/screen: 缩进块整段跳过 ---
            if _CODE_BLOCK_RE.match(line):
                in_code = True
                code_indent = indent
                continue
            if in_code:
                if stripped and indent <= code_indent:
                    in_code = False  # 块结束，继续按普通行处理
                else:
                    continue

            # ``$ xxx`` 单行 Python
            if stripped.startswith("$"):
                continue

            # 1) 角色名定义（已在 _scan_characters 统一处理过，这里跳过）
            if _CHARACTER_RE.match(line):
                continue

            # 2) menu 选项
            m = _MENU_ITEM_RE.match(line)
            if m:
                text = m.group("text")
                if self._ok(text):
                    out.append(
                        TextUnit(
                            uid=f"{rel}:{lineno}:menu",
                            source=text,
                            kind=TextKind.MENU,
                            location=TextLocation(file=rel, pointer=f"L{lineno}", line=lineno),
                            tags=["renpy:menu"],
                        )
                    )
                continue

            # 3) define/old/new/text 等带关键字的可翻译语句
            m = _KEYWORD_STMT_RE.match(line)
            if m:
                text = m.group("text")
                if self._ok(text):
                    out.append(
                        TextUnit(
                            uid=f"{rel}:{lineno}:{m.group('kw')}",
                            source=text,
                            kind=TextKind.DIALOGUE,
                            location=TextLocation(file=rel, pointer=f"L{lineno}", line=lineno),
                            tags=[f"renpy:{m.group('kw')}"],
                        )
                    )
                continue

            # 4) 对白
            m = _DIALOGUE_RE.match(line)
            if m:
                text = m.group("text")
                if self._ok(text):
                    who = (m.group("who") or "").strip()
                    out.append(
                        TextUnit(
                            uid=f"{rel}:{lineno}:say",
                            source=text,
                            kind=TextKind.DIALOGUE,
                            speaker=who or None,
                            location=TextLocation(file=rel, pointer=f"L{lineno}", line=lineno),
                            tags=["renpy:say"],
                        )
                    )

        return out

    @staticmethod
    def _ok(text: str) -> bool:
        """该串是否值得翻译（去掉转义/插值后还有自然语言）。"""
        if not text.strip():
            return False
        # 去掉 Ren'Py 插值与标签
        stripped = re.sub(r"\[[^\]]*\]", "", text)
        stripped = re.sub(r"\{[^}]*\}", "", stripped)
        stripped = stripped.replace("\\n", " ").replace("\\t", " ")
        stripped = stripped.strip()
        if not stripped or _NOT_TEXT_RE.match(stripped):
            return False
        return any(c.isalpha() or ord(c) > 0x2E80 for c in stripped)

    # ------------------------------------------------------------------
    # 字体
    # ------------------------------------------------------------------

    def discover_fonts(self, game_dir: Path) -> list[FontCoverage]:
        game = game_dir / "game" if (game_dir / "game").is_dir() else game_dir
        out: list[FontCoverage] = []
        for f in sorted(game.rglob("*")):
            if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                rel = self._rel(game_dir, f)
                out.append(FontCoverage(font_id=rel, path=rel, family=f.stem, is_game_font=True))
        return out

    def wire_fonts(self, out_dir: Path, installed: dict[str, str]) -> list[str]:
        """生成 ``game/novaloc_fonts.rpy``，把 Ren'Py 的字体指向补好的字体。

        Ren'Py 的字体由 ``gui.text_font`` / ``gui.name_text_font`` /
        ``gui.interface_text_font`` 等变量控制，定义在 ``game/gui.rpy``
        里。**光把 ttf 放进 game/ 不会生效**。

        这里不改用户的 ``gui.rpy``（那是人写的、还带主题注释，改了以后
        升级 Ren'Py 或用户手改会冲突），而是**新增一个 ``init 1`` 的
        覆盖文件**。Ren'Py 按 ``init`` 优先级执行，``init 1`` 晚于
        gui.rpy 的默认 ``init`` 但早于游戏启动，所以这是官方推荐的
        覆盖方式，也最容易卸载（删掉一个文件即可）。
        """
        notes: list[str] = []
        if not installed:
            return notes

        game = out_dir / "game" if (out_dir / "game").is_dir() else out_dir
        if not game.is_dir():
            return notes

        # 优先用已经存在的字体名（Ren'Py 里字体就是相对 game/ 的路径）
        target = ""
        for _orig, new in installed.items():
            target = new
            break
        if not target:
            return notes

        # installed 的 key 是相对 game_dir 的路径，Ren'Py 需要相对 game/ 的
        font_rel = target
        if font_rel.startswith("game/"):
            font_rel = font_rel[len("game/") :]

        out_file = game / "novaloc_fonts.rpy"
        body = "\n".join([
            "# 由 NovaLoc 新译自动生成 —— 让游戏使用补齐中文字形的字体。",
            "# 想还原原始字体：删掉本文件即可。",
            "init 1 python:",
            f"    _novaloc_font = {font_rel!r}",
            "    # 只覆盖由 gui 变量控制的字体。用户自定义的样式不动，",
            "    # 避免把主题改花。",
            "    for _v in (",
            '        "text_font", "name_text_font", "interface_text_font",',
            '        "button_text_font", "choice_button_text_font",',
            '        "label_text_font", "main_menu_text_font",',
            '        "game_menu_text_font", "history_text_font",',
            "    ):",
            '        _n = "gui." + _v',
            "        try:",
            "            if hasattr(store.gui, _v):",
            "                setattr(store.gui, _v, _novaloc_font)",
            "        except Exception:",
            "            pass",
            "",
        ])
        out_file.write_text(body, encoding="utf-8", newline="\n")
        notes.append(f"已生成 {self._rel(out_dir, out_file)}（指向 {font_rel}）")

        # 提示用户：如果 gui.rpy 里把字体写死在 style 里，覆盖不会生效
        gui_rpy = game / "gui.rpy"
        if gui_rpy.is_file():
            txt = gui_rpy.read_text(encoding="utf-8", errors="replace")
            hard = re.findall(r"style\s+\w+[^\n]*\n(?:[^\n]*\n){0,6}?[^\n]*font\s+", txt)
            if hard:
                notes.append(
                    f"⚠️ {gui_rpy.name} 里有直接写死的 font（{len(hard)} 处），"
                    "这些样式不会被 gui 变量覆盖，如仍有口口口请手动处理"
                )
        return notes

    # ------------------------------------------------------------------
    # 回写
    # ------------------------------------------------------------------

    def apply(
        self,
        game_dir: Path,
        out_dir: Path,
        units: list[TextUnit],
        translations: dict[str, str],
        *,
        font_patches: dict[str, Path] | None = None,
        rebuilt_images: dict[str, Path] | None = None,
    ) -> ApplyResult:
        res = ApplyResult(out_dir=out_dir)
        try:
            self.prepare_out(game_dir, out_dir)
        except Exception as exc:  # noqa: BLE001
            res.error = f"复制游戏目录失败：{exc}"
            return res

        by_file: dict[str, list[TextUnit]] = {}
        for u in units:
            if u.uid in translations and u.location.line:
                by_file.setdefault(u.location.file, []).append(u)

        for rel, us in by_file.items():
            target = out_dir / rel
            if not target.is_file():
                res.warnings.append(f"目标脚本不存在，跳过：{rel}")
                res.files_skipped += 1
                continue
            try:
                lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"{rel} 读取失败：{exc}")
                res.files_skipped += 1
                continue

            n = 0
            for u in us:
                ln = int(u.location.line or 0)
                if not (1 <= ln <= len(lines)):
                    res.warnings.append(f"行号越界：{rel} L{ln}")
                    continue
                new_line = self._replace_text(lines[ln - 1], u.source, translations[u.uid])
                if new_line is None:
                    res.warnings.append(f"定位失败（行内容不匹配）：{rel} L{ln}")
                    continue
                lines[ln - 1] = new_line
                n += 1

            if n:
                try:
                    target.write_text("".join(lines), encoding="utf-8", newline="")
                    res.files_written += 1
                except Exception as exc:  # noqa: BLE001
                    res.warnings.append(f"{rel} 写入失败：{exc}")
                    res.files_skipped += 1

        # 贴图与字体
        import shutil as _sh

        installed: dict[str, str] = {}
        for mapping, label in ((rebuilt_images or {}, "贴图"), (font_patches or {}, "字体")):
            for rel, src in mapping.items():
                dest = out_dir / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _sh.copy2(src, dest)
                    res.files_written += 1
                    if label == "字体":
                        installed[rel] = rel
                except Exception as exc:  # noqa: BLE001
                    res.warnings.append(f"{label}回写失败 {rel}：{exc}")

        # 字体文件到位还不够，必须让 Ren'Py 真的去加载它
        if installed:
            try:
                for note in self.wire_fonts(out_dir, installed):
                    res.warnings.append(f"[字体接线] {note}")
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"字体接线失败（字体文件已就位，可能需手动指向）：{exc}")

        res.ok = res.files_written > 0 or not by_file
        if not res.ok and not res.error:
            res.error = "没有任何脚本被写入"
        return res

    @staticmethod
    def _replace_text(line: str, old: str, new: str) -> str | None:
        """把这一行里**第一个**与 ``old`` 匹配的字符串字面量换成 ``new``。

        这里刻意按"字符串字面量"定位而不是简单的 ``str.replace``：
        简单替换会把恰好出现在代码里的同样文本也改掉。
        找不到就返回 ``None``（交给上层报警），绝不猜位置。
        """
        esc_old = RenPyAdapter._escape(new)
        i = 0
        n = len(line)
        while i < n:
            if line[i] == '"':
                j = i + 1
                buf: list[str] = []
                while j < n:
                    c = line[j]
                    if c == "\\" and j + 1 < n:
                        buf.append(line[j : j + 2])
                        j += 2
                        continue
                    if c == '"':
                        break
                    buf.append(c)
                    j += 1
                if j >= n:
                    return None  # 引号没闭合，这行有问题
                literal = "".join(buf)
                if literal == old:
                    return line[: i + 1] + esc_old + line[j:]
                i = j + 1
                continue
            i += 1
        return None

    @staticmethod
    def _escape(text: str) -> str:
        """转义成可以放进双引号字面量里的形式。

        只转义 ``"`` 和反斜杠 —— 换行等**必须保留原有的转义序列**，
        因为我们拿到的源串本身就已经是"文件里的字面量内容"了
        （例如 ``\\n`` 就是两个字符）。多转义一层会写出 ``\\\\n``。
        """
        out: list[str] = []
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                # 已经是转义序列，原样保留
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == '"':
                out.append('\\"')
            else:
                out.append(c)
            i += 1
        return "".join(out)


__all__ = ["RenPyAdapter"]
