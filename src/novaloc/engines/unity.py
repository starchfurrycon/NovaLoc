"""Unity 适配器。

**必须先把话说清楚**：Unity 的文本没有统一的存放位置。
它可能在任何地方：

* ``*_Data/StreamingAssets/`` 下的 txt/json/csv；
* ``*_Data/level*``、``resources.assets``、``sharedassets*.assets``
  里的 MonoBehaviour 字段（**需要序列化格式知识**）；
* ``.unity3d`` / AssetBundle 里的 TextAsset；
* 甚至编译进 ``Assembly-CSharp.dll`` 的字符串常量里。

本适配器做**能力之内且可靠**的那部分：

1. 识别 Unity 工程（``*_Data/`` + ``Managed/`` + ``UnityPlayer.dll``）；
2. 抽取 ``StreamingAssets`` 与 ``*_Data`` 下明文的 txt/json/csv/xml/po 文本；
3. 报告**哪些东西没被处理**（序列化资源、AssetBundle），
   并给出可行的替代方案（让用户用 UABEA/AssetStudio 导出后再走"散装文件"模式）。

**不做**的事情：不去猜 ``.assets`` 的二进制布局然后盲写 —— 那几乎必然
破坏资源文件，而且失败时表现为"游戏打不开"，用户根本查不出原因。
宁可不支持，也不能把游戏改坏。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from ..core.registry import register
from ..models import ExtractReport, FontCoverage, TextKind, TextLocation, TextUnit
from .base import ApplyResult, EngineAdapter, EngineInfo

log = logging.getLogger(__name__)

#: 明文的、可以直接安全改写的文本文件后缀
TEXT_SUFFIXES = (".txt", ".json", ".csv", ".tsv", ".xml", ".po", ".properties", ".ini", ".lang")

#: 明显不该翻的键名（Unity 工程里常见的配置键）
_SKIP_KEYS = {
    "id", "uuid", "guid", "path", "file", "url", "key", "type", "class",
    "version", "hash", "md5", "sha", "assetbundle", "prefab", "scene",
}
_NOT_TEXT_RE = re.compile(r"^[\s\d\W_]+$", re.UNICODE)


@register("engine", "unity")
class UnityAdapter(EngineAdapter):
    """Unity 适配器（仅处理明文文本资源）。"""

    id = "unity"
    display_name = "Unity"
    priority = 30

    # ------------------------------------------------------------------

    def detect(self, game_dir: Path) -> EngineInfo:
        info = EngineInfo(engine_id=self.id, display_name=self.display_name, root=game_dir)
        data_dirs = [d for d in game_dir.iterdir() if d.is_dir() and d.name.endswith("_Data")]
        if not data_dirs:
            return info

        info.confidence = 0.5
        info.evidence.append(f"发现 {len(data_dirs)} 个 *_Data 目录：{[d.name for d in data_dirs]}")

        d = data_dirs[0]
        if (d / "Managed").is_dir():
            info.confidence += 0.25
            info.evidence.append("存在 Managed/（.NET 程序集）")
        if (d / "globalgamemanagers").is_file():
            info.confidence += 0.1
            info.evidence.append("存在 globalgamemanagers")
        if (game_dir / "UnityPlayer.dll").is_file():
            info.confidence += 0.15
            info.evidence.append("存在 UnityPlayer.dll")

        # 判断是 Mono 还是 IL2CPP
        if (d / "Managed" / "Assembly-CSharp.dll").is_file():
            info.evidence.append("Mono 后端（有 Assembly-CSharp.dll）")
        elif (d / "il2cpp_data").is_dir() or (game_dir / "GameAssembly.dll").is_file():
            info.evidence.append("IL2CPP 后端（文本多编译进二进制，抽取能力有限）")

        # Unity 版本
        try:
            gg = d / "globalgamemanagers"
            if gg.is_file():
                head = gg.read_bytes()[:256]
                m = re.search(rb"(20\d\d\.\d+\.\d+[a-z]\d+)", head)
                if m:
                    info.version = m.group(1).decode("ascii", "ignore")
        except Exception:  # noqa: BLE001
            pass

        return info

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------

    def extract_text(self, game_dir: Path) -> tuple[list[TextUnit], ExtractReport]:
        report = ExtractReport(adapter=self.id)
        units: list[TextUnit] = []

        # 收集扫描根。**必须去重**：*_Data 已经包含 StreamingAssets，
        # 再单独加一次会让每个文件被抽两遍，于是同一个 uid 出现两条 TextUnit，
        # 回写时译文被叠加成"【译】【译】xxx"。
        # 早先就是这么错的，所以这里用 resolve() 后按路径去重，
        # 并且剔除"已被其它根包含"的子目录。
        raw_roots: list[Path] = []
        for d in game_dir.iterdir():
            if not d.is_dir():
                continue
            if d.name.endswith("_Data") or d.name == "StreamingAssets":
                raw_roots.append(d)
        resolved = sorted({r.resolve() for r in raw_roots})
        roots: list[Path] = []
        for r in resolved:
            if any(r != o and o in r.parents for o in resolved):
                continue  # 已被更上层的根覆盖
            roots.append(r)

        files: list[Path] = []
        for r in roots:
            for p in r.rglob("*"):
                if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
                    files.append(p)
        report.files_scanned = len(files)

        for f in files:
            before = len(units)
            try:
                units.extend(self._extract_file(f, game_dir))
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{f.name} 解析失败：{exc}")
            if len(units) > before:
                report.files_matched += 1

        # 明确报告**没处理**的部分，避免用户误以为全都翻好了
        serialized = []
        for d in roots:
            serialized.extend(
                p.name for p in d.glob("level*") if p.is_file()
            )
            for name in ("resources.assets", "sharedassets0.assets", "globalgamemanagers"):
                if (d / name).is_file():
                    serialized.append(name)
            if list(d.glob("*.assets")):
                serialized.extend(p.name for p in d.glob("*.assets") if p.name not in serialized)
        bundles = [p.name for r in roots for p in r.rglob("*.unity3d")]
        bundles += [p.name for p in game_dir.rglob("*.bundle")]

        if serialized or bundles:
            note = (
                f"⚠️ 检测到 {len(set(serialized))} 个序列化资源文件"
                + (f"与 {len(bundles)} 个 AssetBundle" if bundles else "")
                + "，其中的文本**未被处理**。这些文件是二进制序列化格式，"
                "盲写极易破坏资源导致游戏无法启动。建议先用 UABEA / AssetStudio "
                "导出其中的 TextAsset，再用本工具的「散装文件」模式处理。"
            )
            report.errors.append(note)
            report.skipped["serialized_assets"] = len(set(serialized))
            if bundles:
                report.skipped["asset_bundles"] = len(bundles)

        report.units = len(units)
        return units, report

    def _extract_file(self, path: Path, game_dir: Path) -> list[TextUnit]:
        rel = self._rel(game_dir, path)
        suffix = path.suffix.lower()
        text = path.read_text(encoding="utf-8", errors="replace")

        if suffix == ".json":
            try:
                obj = json.loads(text)
            except Exception:  # noqa: BLE001
                return []
            return self._walk_json(obj, rel, "")
        if suffix in (".csv", ".tsv"):
            return self._walk_csv(text, rel, "\t" if suffix == ".tsv" else ",")
        if suffix == ".po":
            return self._walk_po(text, rel)
        if suffix in (".properties", ".ini", ".lang"):
            return self._walk_kv(text, rel)

        # 纯文本：整行成块（跳过短行与纯符号行）
        out: list[TextUnit] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if len(s) < 3 or _NOT_TEXT_RE.match(s):
                continue
            out.append(
                TextUnit(
                    uid=f"{rel}:L{lineno}",
                    source=s,
                    kind=TextKind.SYSTEM,
                    location=TextLocation(file=rel, pointer=f"L{lineno}", line=lineno),
                    tags=["unity:plaintext"],
                )
            )
        return out

    def _walk_json(self, obj: Any, rel: str, pointer: str) -> list[TextUnit]:
        """递归找出 JSON 里所有值得翻译的字符串。

        注意**数组里直接放字符串**的情况（``"credits": ["Director: ...", ...]``）：
        递归到数组元素时对方是 ``str``，既不是 dict 也不是 list，
        早先的实现会在这里**静默丢掉**整段内容。所以要有显式的 str 分支。
        """
        out: list[TextUnit] = []

        # 顶层/元素本身就是字符串（数组元素、或调用方直接传字符串）
        if isinstance(obj, str):
            if self._ok(obj):
                out.append(
                    TextUnit(
                        uid=f"{rel}{pointer}",
                        source=obj,
                        kind=TextKind.UI_LABEL,
                        location=TextLocation(file=rel, pointer=pointer or "/"),
                        tags=["unity:json"],
                    )
                )
            return out

        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{pointer}/{k}"
                if isinstance(v, str):
                    if k.lower() in _SKIP_KEYS:
                        continue
                    if self._ok(v):
                        out.append(
                            TextUnit(
                                uid=f"{rel}{p}",
                                source=v,
                                kind=TextKind.UI_LABEL,
                                location=TextLocation(file=rel, pointer=p),
                                tags=["unity:json"],
                            )
                        )
                else:
                    out.extend(self._walk_json(v, rel, p))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                out.extend(self._walk_json(v, rel, f"{pointer}/{i}"))
        return out

    def _walk_csv(self, text: str, rel: str, delim: str) -> list[TextUnit]:
        """CSV/TSV：把每一行非首列的长文本当可翻译项。

        首列通常被当"键"用（id/标识），所以跳过。
        """
        out: list[TextUnit] = []
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        except Exception:  # noqa: BLE001
            return out
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                if ci == 0:
                    continue
                if self._ok(cell):
                    out.append(
                        TextUnit(
                            uid=f"{rel}:R{ri}C{ci}",
                            source=cell,
                            kind=TextKind.UI_LABEL,
                            location=TextLocation(file=rel, pointer=f"R{ri}C{ci}", line=ri + 1),
                            tags=["unity:csv"],
                        )
                    )
        return out

    def _walk_po(self, text: str, rel: str) -> list[TextUnit]:
        out: list[TextUnit] = []
        for m in re.finditer(r'msgid\s+"((?:[^"\\]|\\.)*)"', text):
            raw = m.group(1)
            if not raw or not self._ok(raw):
                continue
            out.append(
                TextUnit(
                    uid=f"{rel}:msgid:{m.start()}",
                    source=raw.encode().decode("unicode_escape", errors="replace"),
                    kind=TextKind.SYSTEM,
                    location=TextLocation(file=rel, pointer=f"@{m.start()}"),
                    tags=["unity:po"],
                )
            )
        return out

    def _walk_kv(self, text: str, rel: str) -> list[TextUnit]:
        out: list[TextUnit] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith(("#", ";", "!")):
                continue
            m = re.match(r"^([^=:]+)[=:](.*)$", s)
            if not m:
                continue
            key, val = m.group(1).strip(), m.group(2).strip()
            if key.lower() in _SKIP_KEYS or not self._ok(val):
                continue
            out.append(
                TextUnit(
                    uid=f"{rel}:L{lineno}",
                    source=val,
                    kind=TextKind.UI_LABEL,
                    location=TextLocation(file=rel, pointer=f"L{lineno}", line=lineno),
                    tags=["unity:kv"],
                )
            )
        return out

    @staticmethod
    def _ok(text: str) -> bool:
        s = text.strip()
        if len(s) < 2:
            return False
        if _NOT_TEXT_RE.match(s):
            return False
        # 看起来像 GUID / 十六进制哈希 / 资源路径的不翻
        if re.fullmatch(r"[0-9a-fA-F]{16,}", s):
            return False
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s):
            return False
        if re.match(r"^(Assets/|Packages/|Resources/)", s):
            return False
        return any(c.isalpha() or ord(c) > 0x2E80 for c in s)

    # ------------------------------------------------------------------
    # 字体
    # ------------------------------------------------------------------

    def discover_fonts(self, game_dir: Path) -> list[FontCoverage]:
        out: list[FontCoverage] = []
        for p in game_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                rel = self._rel(game_dir, p)
                out.append(FontCoverage(font_id=rel, path=rel, family=p.stem, is_game_font=True))
        return out

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
            if u.uid in translations:
                by_file.setdefault(u.location.file, []).append(u)

        for rel, us in by_file.items():
            target = out_dir / rel
            if not target.is_file():
                res.warnings.append(f"目标不存在，跳过：{rel}")
                res.files_skipped += 1
                continue
            suffix = target.suffix.lower()
            try:
                if suffix == ".json":
                    n = self._apply_json(target, us, translations)
                elif suffix in (".csv", ".tsv"):
                    n = self._apply_lines(target, us, translations)
                elif suffix == ".po":
                    n = self._apply_po(target, us, translations)
                elif suffix in (".properties", ".ini", ".lang"):
                    n = self._apply_kv(target, us, translations)
                else:
                    n = self._apply_lines(target, us, translations)
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"{rel} 回写失败：{exc}")
                res.files_skipped += 1
                continue
            if n:
                res.files_written += 1
            else:
                res.warnings.append(f"{rel} 没有任何条目被写入")

        import shutil as _sh

        for mapping, label in ((rebuilt_images or {}, "贴图"), (font_patches or {}, "字体")):
            for rel, src in mapping.items():
                dest = out_dir / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _sh.copy2(src, dest)
                    res.files_written += 1
                except Exception as exc:  # noqa: BLE001
                    res.warnings.append(f"{label}回写失败 {rel}：{exc}")

        res.ok = res.files_written > 0 or not by_file
        if not res.ok and not res.error:
            res.error = "没有任何文件被写入"
        return res

    def _apply_json(self, path: Path, units: list[TextUnit], tr: dict[str, str]) -> int:
        obj = json.loads(path.read_text(encoding="utf-8"))
        n = 0
        for u in units:
            ptr = u.location.pointer
            if self._set_pointer(obj, ptr, tr[u.uid]):
                n += 1
        if n:
            path.write_text(
                json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
            )
        return n

    @staticmethod
    def _set_pointer(obj: Any, pointer: str, value: str) -> bool:
        if not pointer.startswith("/"):
            return False
        parts = [p for p in pointer.split("/") if p]
        cur = obj
        for seg in parts[:-1]:
            if isinstance(cur, list) and seg.lstrip("-").isdigit():
                idx = int(seg)
                if not (-len(cur) <= idx < len(cur)):
                    return False
                cur = cur[idx]
            elif isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                return False
        last = parts[-1]
        if isinstance(cur, list) and last.lstrip("-").isdigit():
            idx = int(last)
            if not (-len(cur) <= idx < len(cur)):
                return False
            cur[idx] = value
            return True
        if isinstance(cur, dict) and last in cur:
            cur[last] = value
            return True
        return False

    def _apply_lines(self, path: Path, units: list[TextUnit], tr: dict[str, str]) -> int:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        n = 0
        for u in units:
            ln = int(u.location.line or 0)
            if 1 <= ln <= len(lines) and u.source in lines[ln - 1]:
                lines[ln - 1] = lines[ln - 1].replace(u.source, tr[u.uid], 1)
                n += 1
        if n:
            path.write_text("".join(lines), encoding="utf-8", newline="")
        return n

    def _apply_po(self, path: Path, units: list[TextUnit], tr: dict[str, str]) -> int:
        """PO 文件：只替换 msgid 之后的第一个 msgstr。

        最简做法：给每个被抽出的 msgid 在其后插入/替换 msgstr。
        """
        text = path.read_text(encoding="utf-8", errors="replace")
        n = 0
        for u in units:
            src = u.source
            esc = src.replace("\\", "\\\\").replace('"', '\\"')
            pat = re.compile(r'(msgid\s+"' + re.escape(esc) + r'"\s*\n)(msgstr\s+"(?:[^"\\]|\\.)*")')
            new_val = tr[u.uid].replace("\\", "\\\\").replace('"', '\\"')
            # new_val 必须**绑定为默认参数**：否则 lambda 闭包捕获的是
            # 循环变量本身，一旦将来有人把 subn 挪到循环外执行，
            # 所有替换都会用最后一轮的 new_val —— 典型的迟绑定陷阱。
            text, k = pat.subn(
                lambda m, _v=new_val: m.group(1) + f'msgstr "{_v}"', text, count=1
            )
            n += k
        if n:
            path.write_text(text, encoding="utf-8", newline="")
        return n

    def _apply_kv(self, path: Path, units: list[TextUnit], tr: dict[str, str]) -> int:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        n = 0
        for u in units:
            ln = int(u.location.line or 0)
            if not (1 <= ln <= len(lines)):
                continue
            m = re.match(r"^([^=:]+[=:])(.*)$", lines[ln - 1].rstrip("\r\n"))
            if not m:
                continue
            tail = "\n" if lines[ln - 1].endswith("\n") else ""
            lines[ln - 1] = f"{m.group(1)}{tr[u.uid]}{tail}"
            n += 1
        if n:
            path.write_text("".join(lines), encoding="utf-8", newline="")
        return n


__all__ = ["UnityAdapter"]
