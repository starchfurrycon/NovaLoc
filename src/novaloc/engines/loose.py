"""散装文件适配器：处理"一堆图片"或"一堆文本文件"。

**这个适配器存在感很低但很重要**：Unity 的 ``.assets``、
AssetBundle、以及各种自研打包格式，我们都不会去盲写（盲写几乎必然
把资源改坏）。可行且安全的路径是让用户先用 AssetStudio / UABEA
把内容**导出**成普通文件，再用本适配器处理。

它同时服务于用户直接丢进来一个图片文件夹的场景 ——
这也是"翻译所有类型游戏"的兜底：不认识的引擎，只要能把资源导出成
普通图片或文本，就能汉化。

判定条件：目录（或它的子目录）里存在图片或文本文件，且**没有**
被更具体的引擎适配器认领。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..core.registry import Context, register
from ..models import ExtractReport, FontCoverage, ImageAsset, TextKind, TextLocation, TextUnit
from .base import ApplyResult, EngineAdapter, EngineInfo

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga", ".gif")
TEXT_SUFFIXES = (".txt", ".json", ".csv", ".tsv", ".xml", ".lang", ".properties", ".ini")

_NOT_TEXT_RE = re.compile(r"^[\s\d\W_]+$", re.UNICODE)

#: 超过这个数量的图片就不再整目录扫了（避免把素材库当成游戏）
MAX_IMAGES = 20000


@register("engine", "loose")
class LooseFilesAdapter(EngineAdapter):
    """散装图片 / 文本文件适配器（兜底）。"""

    id = "loose"
    display_name = "散装文件（图片/文本）"
    #: 优先级最低，只在没有更具体的引擎匹配时使用
    priority = 90

    # ------------------------------------------------------------------

    def detect(self, game_dir: Path) -> EngineInfo:
        info = EngineInfo(engine_id=self.id, display_name=self.display_name, root=game_dir)
        if not game_dir.is_dir():
            return info

        n_img = 0
        n_txt = 0
        # 只看两层，避免在超大素材库上耗时
        for depth, pattern in ((1, "*"), (2, "*/*")):
            for p in game_dir.glob(pattern):
                if not p.is_file():
                    continue
                s = p.suffix.lower()
                if s in IMAGE_SUFFIXES:
                    n_img += 1
                    if n_img >= MAX_IMAGES:
                        break
                elif s in TEXT_SUFFIXES:
                    n_txt += 1

        if not n_img and not n_txt:
            return info

        # 置信度刻意压低：这是兜底适配器，任何具体引擎都比它更可信
        info.confidence = 0.25 if n_img or n_txt else 0.0
        if n_img:
            info.evidence.append(f"发现 {n_img}{'+' if n_img >= MAX_IMAGES else ''} 个图片文件")
        if n_txt:
            info.evidence.append(f"发现 {n_txt} 个文本文件")
        info.evidence.append("作为兜底适配器处理（建议先用 AssetStudio/UABEA 导出资源）")
        return info

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------

    def extract_images(self, game_dir: Path) -> tuple[list[ImageAsset], ExtractReport]:
        report = ExtractReport(adapter=self.id)
        out: list[ImageAsset] = []
        n = 0
        for p in sorted(game_dir.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            n += 1
            if n > MAX_IMAGES:
                report.errors.append(f"图片超过 {MAX_IMAGES} 张，已截断（请缩小范围）")
                break
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < 512:
                continue
            rel = self._rel(game_dir, p)
            out.append(
                ImageAsset(
                    uid=rel.replace("/", "_"),
                    path=rel,
                    kind="loose_image",
                )
            )
        report.files_scanned = n
        report.files_matched = len(out)
        report.units = len(out)
        return out, report

    def extract_text(self, game_dir: Path) -> tuple[list[TextUnit], ExtractReport]:
        report = ExtractReport(adapter=self.id)
        units: list[TextUnit] = []
        files = [
            p for p in sorted(game_dir.rglob("*"))
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES
        ]
        report.files_scanned = len(files)

        for f in files:
            rel = self._rel(game_dir, f)
            before = len(units)
            try:
                if f.suffix.lower() == ".json":
                    units.extend(self._walk_json(json.loads(f.read_text(encoding="utf-8")), rel, ""))
                else:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    for lineno, line in enumerate(text.splitlines(), 1):
                        s = line.strip()
                        if len(s) < 3 or _NOT_TEXT_RE.match(s):
                            continue
                        units.append(
                            TextUnit(
                                uid=f"{rel}:L{lineno}",
                                source=s,
                                kind=TextKind.SYSTEM,
                                location=TextLocation(file=rel, pointer=f"L{lineno}", line=lineno),
                                tags=["loose:text"],
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{rel} 解析失败：{exc}")
            if len(units) > before:
                report.files_matched += 1

        report.units = len(units)
        return units, report

    def _walk_json(self, obj, rel: str, pointer: str) -> list[TextUnit]:
        out: list[TextUnit] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{pointer}/{k}"
                if isinstance(v, str):
                    if len(v.strip()) >= 2 and not _NOT_TEXT_RE.match(v.strip()):
                        if any(c.isalpha() or ord(c) > 0x2E80 for c in v):
                            out.append(
                                TextUnit(
                                    uid=f"{rel}{p}",
                                    source=v,
                                    kind=TextKind.UI_LABEL,
                                    location=TextLocation(file=rel, pointer=p),
                                    tags=["loose:json"],
                                )
                            )
                else:
                    out.extend(self._walk_json(v, rel, p))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                out.extend(self._walk_json(v, rel, f"{pointer}/{i}"))
        return out

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
            res.error = f"复制目录失败：{exc}"
            return res

        import shutil as _sh

        # 文本回写
        by_file: dict[str, list[TextUnit]] = {}
        for u in units:
            if u.uid in translations:
                by_file.setdefault(u.location.file, []).append(u)
        for rel, us in by_file.items():
            target = out_dir / rel
            if not target.is_file():
                res.warnings.append(f"目标不存在：{rel}")
                res.files_skipped += 1
                continue
            try:
                if target.suffix.lower() == ".json":
                    obj = json.loads(target.read_text(encoding="utf-8"))
                    n = sum(
                        1 for u in us
                        if self._set_pointer(obj, u.location.pointer, translations[u.uid])
                    )
                    if n:
                        target.write_text(
                            json.dumps(obj, ensure_ascii=False, indent=2),
                            encoding="utf-8", newline="\n",
                        )
                        res.files_written += 1
                else:
                    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                    n = 0
                    for u in us:
                        ln = int(u.location.line or 0)
                        if 1 <= ln <= len(lines) and u.source in lines[ln - 1]:
                            lines[ln - 1] = lines[ln - 1].replace(u.source, translations[u.uid], 1)
                            n += 1
                    if n:
                        target.write_text("".join(lines), encoding="utf-8", newline="")
                        res.files_written += 1
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"{rel} 回写失败：{exc}")
                res.files_skipped += 1

        # 贴图回写
        for rel, src in (rebuilt_images or {}).items():
            dest = out_dir / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _sh.copy2(src, dest)
                res.files_written += 1
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"贴图回写失败 {rel}：{exc}")

        # 字体回写
        for rel, src in (font_patches or {}).items():
            dest = out_dir / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _sh.copy2(src, dest)
                res.files_written += 1
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"字体回写失败 {rel}：{exc}")

        res.ok = res.files_written > 0 or not by_file
        if not res.ok and not res.error:
            res.error = "没有任何文件被写入"
        return res

    @staticmethod
    def _set_pointer(obj, pointer: str, value: str) -> bool:
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


__all__ = ["LooseFilesAdapter"]
