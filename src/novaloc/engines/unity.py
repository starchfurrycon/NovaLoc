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
3. **抽取并原地改写**序列化资源（``.assets``）里的字符串 —— 见第 4 点约束；
4. 报告**哪些东西没被处理**（AssetBundle、超出容量的译文），
   并给出可行的替代方案（让用户用 UABEA/AssetStudio 导出后再走"散装文件"模式）。

## `.assets` 改写的安全边界（实测结论，别再凭直觉推翻）

旧版本这里写的是"**绝不做**：不去猜 `.assets` 的二进制布局然后盲写"。
**这个结论一半对、一半错**，实测把它分开了：

* ✗ **让资源库重建整个文件**（UnityPy 的 `save()`）：66 KB 的 `.assets`
  存回去只剩 5.8 KB，写回原值也**无法还原** → 这条路**确实不能走**；
* ✓ **原地等长替换**：只要译文的字节数不超过原文，就能做到文件总大小不变、
  所有内部偏移不变。实测 36 MB 的文件改 1,874 处，区间外改动 **0 字节**，
  写回原值后**逐字节相同**。

所以现在的做法是"能装下就改，装不下就如实报告"，**绝不截断**。
细节与实测数据见 :mod:`novaloc.engines.unity_patch`。
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

#: ⛔ **绝不是游戏文本**的文件名 —— 引擎/运行时的日志与调试输出。
#:
#: ## 事故：`output_log.txt` 让抽取出来 53 万条"文本"
#:
#: 实测一个真实 Unity 游戏（Mono 后端），`<Game>_Data/output_log.txt`
#: 有 **33 MB**，是 Unity 播放器的**运行日志**：
#:
#:     Initialize engine version: 4.6.7f1 (bb67e21913cc)
#:     GfxDevice: creating device client; threaded=1
#:     Direct3D:
#:     Begin MonoManager ReloadAssembly
#:     Platform assembly: D:\...\Managed\UnityEngine.dll
#:
#: 后缀是 `.txt`、内容大部分是可读英文，所以它**完全符合**
#: "明文文本文件"的判据，被抽成 **530,507 条** `system` 类文本单元
#: （占该游戏全部单元的 98%）。
#:
#: 后果不是"多花了点时间"，而是三件更糟的事：
#:
#: 1. **翻译预算被垃圾吃掉** —— 53 万条要跑几十小时，用户以为工具卡死；
#: 2. **回写会把游戏目录塞满译文日志** —— 日志里掺进中文，
#:    以后排查问题的人读不懂它；
#: 3. **质检报告完全失真** —— 99.9% 的"文本"是日志行，
#:    任何覆盖率数字都不再有意义。
#:
#: 日志是**运行时产物**，不是**游戏内容**。判据必须按**文件名**排除，
#: 而不能只按后缀 —— 后缀和"是不是游戏文本"没有关系。
_NOT_GAME_TEXT = {
    "output_log.txt",       # Unity 播放器日志（最常见）
    "player.log",           # Unity 另一常见名
    "player-prev.log",
    "editor.log",
    "build.log",
    "buildreport.txt",
    "unity.log",
    "crashreport.txt",
    "error.log",
    "stdout.txt",
    "stderr.txt",
    "readme.txt",           # 说明文件：翻它没有意义，还会污染统计
    "readme.md",
    "changelog.txt",
    "license.txt",
    "credits.txt",
}

#: 名字**模式**（不能穷举时用）：`*_log.txt`、`crash_*.txt` 这类。
_NOT_GAME_TEXT_RE = re.compile(
    r"^(?:output|player|editor|build|unity|crash|error|debug|trace)[-_]?"
    r"(?:log|report|dump)?(?:[-_]?\d+)?\.(?:txt|log)$",
    re.IGNORECASE,
)

#: 明显的日志/调试**行**内容 —— 命中太多行时整份文件都判为日志。
#:
#: 按**行**兜底还有一层意义：日志文件可能被改名（用户自己改、
#: 打包工具改），文件名判据会失效，但内容特征不会。
_LOG_LINE_RE = re.compile(
    r"^(?:Initialize engine version|GfxDevice:|Begin MonoManager|"
    r"Platform assembly:|Loading \d+|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|"
    r"\[.*?\]\s*(?:Info|Warning|Error)\b|UnloadTime:|"
    r"Fallback handler could not load|Non platform assembly:)",
    re.MULTILINE,
)


def _looks_like_runtime_log(path: Path, sample: str) -> bool:
    """这份文件是不是引擎的运行日志（而不是游戏文本）？

    两道判据，任一命中即排除：

    1. **文件名**命中 `_NOT_GAME_TEXT` 或 `_NOT_GAME_TEXT_RE`；
    2. **内容**里日志行占比过高（改名也躲不过）。
    """
    if path.name.lower() in _NOT_GAME_TEXT or _NOT_GAME_TEXT_RE.match(path.name):
        return True
    lines = [ln for ln in sample.splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    hits = len(_LOG_LINE_RE.findall(sample))
    return hits >= max(3, len(lines) * 0.02)


#: ⛔ **运行时基础设施目录** —— 引擎自带的、与游戏内容无关的目录树。
#:
#: ## 事故：`Mono/etc/` 让抽取结果 100% 是垃圾
#:
#: 排掉 `output_log.txt` 之后，同一个游戏还剩 **9,586 条**单元，
#: 而它们**全部**来自这两个文件：
#:
#:     AlienQuest-EVE_Data/Mono/etc/mono/browscap.ini     → 9,073 条
#:     AlienQuest-EVE_Data/Mono/etc/mono/mconfig/config.xml →   513 条
#:
#: 这是 Unity 内嵌 Mono 运行时的**基础设施**：
#:
#: * `browscap.ini` 是**浏览器能力数据库**（用户代理字符串模式表），
#:   2009 年生成的，里面全是 `Mozilla/4.0 (compatible; MSIE 6.0…)` 之类的行；
#: * `mconfig/config.xml` 是 Mono 自己的**配置**（`<handler section="…">`、
#:   `type="Mono.MonoConfig.FeatureNodeHandler, mconfig, Version=…"`）。
#:
#: 换句话说：**这个游戏一个可处理的明文文本文件都没有**，
#: 而工具报了 9,586 条"待翻译文本"，全是运行时噪音。
#:
#: 这比 `output_log.txt` 那个事故更隐蔽 —— 数量小了三个数量级，
#: 看起来"挺像一个正常的小游戏"，于是没有触发任何怀疑。
#: **判据不能按"看起来像不像文本"来定，要按"属不属于游戏内容"来定。**
_RUNTIME_INFRA_DIRS = (
    "mono/etc",         # Unity 内嵌 Mono 的运行时配置与数据
    "mono/2.0",         # 旧版 Mono 的 BCL
    "mono/4.0",
    "il2cpp_data/etc",  # IL2CPP 的同类基础设施
    "resources",        # Unity 内建资源（图标、默认材质）—— 不是策划文案
)


def _in_runtime_infra(path: Path) -> bool:
    """路径是否落在引擎的运行时基础设施目录里。

    ⚠️ **必须用 `as_posix()` 归一化分隔符。**
    在 Windows 上 `Path("Mono/etc/mono/x.ini").parts` 是
    `('Mono','etc','mono','x.ini')`，用 `"/".join(parts)` 拼出来
    仍然是反斜杠 —— 于是 `"/mono/etc/" in low` **永远为假**，
    排除规则静默失效（实测：改完之后数字一条没变，才发现是这个）。

    这类"路径匹配看起来对、实际因分隔符而永不命中"的 bug 不会报错，
    只会让功能**静默不生效**。所以这里统一 `as_posix()`。
    """
    low = path.as_posix().lower()
    return any(f"/{d}/" in f"/{low}/" for d in _RUNTIME_INFRA_DIRS)


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

    def _workspace_dir(self) -> Path | None:
        """当前工作区目录（用于放只读扫描的清单与边车文件）。

        ## 为什么先认 `ctx.workspace` 对象

        `Context` 的真实字段是 ``workspace``（一个 :class:`Workspace` 对象），
        **不是** `workspace_dir` 字符串。第一版只按属性名找
        `workspace_dir`/`ws_dir`，于是**永远返回 None** ——
        后果是清单不落盘、边车文件不生成、回写阶段静默什么都不做。
        而它**看起来是成功的**（抽取照常报出几万条文本），
        所以这个 bug 只能靠"实际回写没生效"发现。

        现在按可靠性依次尝试：Workspace 对象的目录 → 显式路径属性 → 配置。
        """
        ws = getattr(self.ctx, "workspace", None)
        if ws is not None:
            for attr in ("root", "dir", "path", "workspace_dir"):
                v = getattr(ws, attr, None)
                if isinstance(v, Path):
                    return v
                if isinstance(v, str) and v:
                    return Path(v)
        for attr in ("workspace_dir", "ws_dir"):
            v = getattr(self.ctx, attr, None)
            if isinstance(v, Path):
                return v
            if isinstance(v, str) and v:
                return Path(v)
        cfg = getattr(self.ctx, "config", None)
        if cfg is not None:
            for attr in ("workspace_dir", "data_root"):
                v = getattr(cfg, attr, None)
                if isinstance(v, Path):
                    return v
                if isinstance(v, str) and v:
                    return Path(v)
        return None

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
        skipped_logs: list[str] = []
        skipped_infra: list[str] = []
        for r in roots:
            for p in r.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                # 1) 运行时基础设施目录（Mono 配置、BCL、内建资源）
                if _in_runtime_infra(p.relative_to(r)):
                    skipped_infra.append(str(p.relative_to(r)))
                    continue
                # 2) 按**文件名**排掉已知的运行时日志（省钱：不用读 33 MB）
                if p.name.lower() in _NOT_GAME_TEXT or _NOT_GAME_TEXT_RE.match(p.name):
                    skipped_logs.append(p.name)
                    continue
                files.append(p)
        report.files_scanned = len(files)

        for f in files:
            before = len(units)
            try:
                # 按**内容**再排一次：改名后的日志靠这一步拦住。
                # 只读开头 64 KB 做判断，不把 33 MB 全读进来。
                head = f.read_bytes()[:65536].decode("utf-8", errors="replace")
                if _looks_like_runtime_log(f, head):
                    skipped_logs.append(f.name)
                    continue
                units.extend(self._extract_file(f, game_dir))
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{f.name} 解析失败：{exc}")
            if len(units) > before:
                report.files_matched += 1

        # 记下本轮的 units —— 回写阶段要用 `location.pointer` 里的偏移
        # 去定位二进制资源里的字符串。适配器实例在一条流水线内复用，
        # 所以这个缓存是安全的；取不到时回写阶段会安全地什么都不做。
        self._last_units = units

        if skipped_infra:
            report.skipped["runtime_infra_files"] = len(skipped_infra)
            report.errors.append(
                f"已排除 {len(skipped_infra)} 个引擎运行时基础设施文件"
                "（`Mono/etc/`、`il2cpp_data/etc/`、`Resources/`）—— "
                "这些是 Mono/IL2CPP 自己的配置与内建资源，"
                "不是游戏文本（实测某个游戏 100% 的'待翻译文本'都来自这里）。"
            )
        if skipped_logs:
            # 明确报告排除了什么 —— 静默排除会让用户以为"日志里的字也翻了"
            report.skipped["runtime_logs"] = len(skipped_logs)
            report.errors.append(
                f"已排除 {len(skipped_logs)} 个运行时日志/说明文件"
                f"（{[*dict.fromkeys(skipped_logs)][:3]} 等）—— "
                "这些是引擎和打包器生成的调试输出，不是游戏内容；"
                "翻译它们会白烧几小时并把质检统计冲垮。"
            )

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
            # 与其只说"没处理"，不如告诉用户**里面有多少文本**。
            # 只读扫描序列化资源，把候选字符串数与清单文件路径报出来 ——
            # 清单的价值是**审计与兜底**：用户能核对工具打算改什么，
            # 装不下的条目和未解包的 AssetBundle 也能靠它转外部工具。
            scan_note = ""
            try:
                from .unity_strings import scan_game

                rep_u = scan_game(game_dir)
                if rep_u.total:
                    uniq = len({c.text for c in rep_u.candidates})
                    scan_note = (
                        f"扫描发现其中约 **{uniq}** 条候选文案"
                        f"（{rep_u.total} 处出现）。"
                    )
                    report.skipped["unity_serialized_strings"] = uniq
                    # ⚠️ **清单写到工作区，绝不写进游戏目录。**
                    # 游戏目录是只读契约（`apply` 只写 `workspaces/<id>/out`），
                    # 往里丢一个 CSV 就是破坏它 —— 哪怕这个文件无害，
                    # "工具会在我的游戏里新建文件"本身就是用户不该担心的事。
                    #
                    # 工作区从 `ctx` 取；取不到就**只报数、不落盘**
                    # （宁可少一个便利文件，也不能往游戏目录写东西）。
                    out_dir = self._workspace_dir()
                    if out_dir is not None:
                        try:
                            from .unity_strings import write_csv

                            out_csv = out_dir / "qa" / "unity_strings.csv"
                            write_csv(rep_u, out_csv)
                            scan_note += f" 清单已写到工作区的 `qa/{out_csv.name}`。"
                        except OSError as exc:
                            log.debug("写 Unity 字符串清单失败：%s", exc)
            except Exception as exc:  # noqa: BLE001
                log.debug("序列化资源只读扫描失败：%s", exc)

            note = (
                f"发现 {len(set(serialized))} 个序列化资源文件"
                + (f"与 {len(bundles)} 个 AssetBundle" if bundles else "")
                + "，其中的字符串已按「原地等长改写」处理（见下方统计）。"
                "AssetBundle 本身**未解包**，其中的文本不在本次范围内——"
                "需要的话用 UABEA / AssetStudio 先解包再跑一遍。"
                + (scan_note if scan_note else "")
            )
            report.errors.append(note)
            report.skipped["serialized_assets"] = len(set(serialized))
            if bundles:
                report.skipped["asset_bundles"] = len(bundles)

        # ---- 散装二进制资源里的字符串（原地等长改写）----
        #
        # ## 这里改变了一个旧结论（实测推翻的）
        #
        # 老注释写的是"`.assets` 绝不改写"。**一半对、一半错** ——
        # 实测把两种写法分开了：
        #
        # * ✗ 让资源库重建整个文件（UnityPy 的 `save()`）：66 KB 存回去
        #   只剩 5.8 KB，写回原值也无法还原 → **否定**；
        # * ✓ **原地等长替换**：只要译文字节数不超过原文，文件总大小不变、
        #   所有内部偏移不变。实测 36 MB 的文件改 1,874 处，
        #   区间外改动字节 **0**，写回原值后**逐字节相同**。
        #
        # 所以现在支持了，但有**硬约束**：装不下就跳过并如实报告
        # （见 `unity_patch`）。绝不截断 —— 截断会静默丢内容。
        try:
            assets_units = self._extract_serialized(game_dir)
            units.extend(assets_units)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"二进制资源字符串提取失败：{exc}")

        report.units = len(units)
        return units, report

    def _slot_sidecar(self) -> Path | None:
        """记录"字符串在文件里的精确位置"的边车文件路径。

        回写阶段必须知道每个条目的**字节偏移与容量**，所以抽取时就要存下来。
        放在工作区（拿不到工作区就返回 None，此时该能力静默降级为"只抽取"，
        因为**绝不往游戏目录写文件**）。
        """
        ws = self._workspace_dir()
        if ws is None:
            return None
        return ws / "extracted" / "unity_slots.json"

    def _extract_serialized(self, game_dir: Path) -> list[TextUnit]:
        """从 ``*_Data`` 下的二进制资源里提取可原地改写的字符串。

        只处理**散装文件**（``resources.assets``、``sharedassets*.assets``、
        ``level*``）。AssetBundle 需要先解包，不在这一步。
        """
        from .unity_patch import extract_slots

        data_dirs = [
            d for d in game_dir.iterdir() if d.is_dir() and d.name.endswith("_Data")
        ]
        if not data_dirs:
            return []

        units: list[TextUnit] = []
        all_slots: list[dict] = []
        for d in data_dirs:
            for p in sorted(d.glob("*.assets")) + sorted(d.glob("level*")):
                if not p.is_file():
                    continue
                rel = self._rel(game_dir, p)
                try:
                    if p.stat().st_size > 512 * 1024 * 1024:
                        # 超大文件跳过并**明确记下**，不静默跳过
                        log.info("跳过超大序列化资源：%s", rel)
                        continue
                    raw = p.read_bytes()
                except OSError:
                    continue
                for s in extract_slots(raw, filename=rel):
                    uid = f"{rel}@0x{s.offset:x}"
                    units.append(
                        TextUnit(
                            uid=uid,
                            source=s.text,
                            # 用 `TextLocation.byte_offset` 存偏移 —— 它是模型里
                            # 本来就有的字段，语义正好。第一版把偏移塞进
                            # `pointer` 字符串（`0x…`）再解析回来，属于滥用字段，
                            # 而且任何别的地方一改 pointer 格式就会静默失效。
                            location=TextLocation(
                                file=rel, byte_offset=s.offset, encoding="utf-8"
                            ),
                            # `max_bytes` 就是这段字符串在文件里的**可用容量**。
                            # 回写阶段靠它判断"装不装得下"，所以必须带上 ——
                            # 这样即使边车文件写不出来（没有可写工作区），
                            # 原地改写依然能工作。边车只是补强，不是依赖。
                            max_bytes=s.capacity,
                            kind=TextKind.UNKNOWN,
                        )
                    )
                    all_slots.append(s.to_dict())

        sidecar = self._slot_sidecar()
        if sidecar is not None and all_slots:
            try:
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(
                    json.dumps(all_slots, ensure_ascii=False), encoding="utf-8"
                )
            except OSError as exc:
                log.debug("写边车文件失败：%s", exc)
        return units

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

    #: TMP 字体资源的扩展名。``.asset`` 是文本序列化的 TMP_FontAsset，
    #: ``.bytes`` / 无扩展名的是烘焙进 AssetBundle 的版本。
    TMP_ASSET_SUFFIXES = (".asset",)

    def _find_tmp_assets(self, game_dir: Path) -> list[Path]:
        """找出目录里像 TMP 字体资源（含图集）的文件。

        TextMeshPro 的字体资源由两部分组成：``TMP_FontAsset``（字形表 +
        ``m_AtlasTextures`` 引用）和一张**预渲染的图集贴图**。只换 TTF 是
        **无效**的：游戏渲染时直接用图集里的位图，根本不会去读 TTF。
        所以这里要先探测有没有图集，再决定怎么说。
        """
        hits: list[Path] = []
        for p in game_dir.rglob("*"):
            if not p.is_file():
                continue
            s = p.suffix.lower()
            if s in self.TMP_ASSET_SUFFIXES:
                # .asset 里出现 TMP 关键字才算（避免把普通资源当字体）
                try:
                    head = p.read_bytes()[:4096]
                except OSError:
                    continue
                if b"TMP_FontAsset" in head or b"m_AtlasTextures" in head or b"m_FaceInfo" in head:
                    hits.append(p)
            elif s in (".png",) and "atlas" in p.name.lower():
                hits.append(p)
        return hits

    def wire_fonts(self, out_dir: Path, installed: dict[str, str]) -> list[str]:
        """Unity 的字体接线：放好字体，并**如实说明 TMP 图集这道坎**。

        为什么不能像 Ren'Py / RPG Maker 那样自动改配置：

        Unity 的 UI 文字几乎都走 TextMeshPro，而 TMP 渲染用的是**预烘焙
        图集**（``m_AtlasTextures`` 指向一张贴图，字形是这张贴图上的
        位图块）。换掉 TTF 文件对已烘焙的资源**没有任何影响** ——
        游戏不会去读那个 TTF。要让中文出现，必须重新烘焙图集，
        而重新烘焙需要：
        * 解析 ``TMP_FontAsset`` 的二进制/文本序列化格式，
        * 按原有字号/图集尺寸把新字体的字形重新光栅化并排进图集，
        * 重算 ``m_GlyphTable`` / ``m_CharacterTable`` / ``m_FaceInfo``。

        这是一个独立的、相当大的工程（且必须用 Unity 自身的排版度量才能
        和游戏完全一致），本项目当前**没有**实现。所以这里：

        1. 把补好的字体复制到 ``StreamingAssets/_novaloc_fonts/`` ——
           ``StreamingAssets`` 会被原样打进构建，是**运行时能被读到**
           的最稳妥位置；
        2. 如果探测到 TMP 图集资源，明确指出"需要重新烘焙"，并给出用
           Unity 编辑器一键重建的步骤；
        3. 绝不假装已经修好 —— 虚假的成功比明确的失败更浪费时间。

        有一个真实存在的例外情形值得说明：如果游戏用的是
        **动态（Dynamic）TMP 字体资源**（``m_AtlasPopulationMode`` 为
        ``Dynamic``），它运行时会按需把字形加进图集，此时把
        ``StreamingAssets`` 里的 TTF 换掉**是**有效的。探测到这种情况
        时下面会提示用户优先尝试。
        """
        notes: list[str] = []
        if not installed:
            return notes

        sa = self._streaming_assets(out_dir)
        notes.extend(self.copy_fonts_into(out_dir, installed, sa / "_novaloc_fonts"))

        tmp_assets = self._find_tmp_assets(out_dir)
        if tmp_assets:
            shown = ", ".join(self._rel(out_dir, p) for p in tmp_assets[:3])
            more = f" 等 {len(tmp_assets)} 个" if len(tmp_assets) > 3 else ""
            notes.append(
                f"检测到 TextMeshPro 字体资源（{shown}{more}）："
                "TMP 用**预烘焙图集**渲染文字，替换 TTF 不会生效，必须重新烘焙图集。"
            )
            notes.append(
                "手动做法：用 Unity 编辑器打开工程 → Window > TextMeshPro > "
                "Font Asset Creator → Source Font File 选本目录下 _novaloc_fonts/ 里的补字字体 "
                "→ Character Set 选 Custom Characters 并粘贴项目用到的字符集 "
                "→ Generate Font Atlas → Save 覆盖原字体资源。"
            )
            notes.append(
                "⚠️ 重烘焙会改变字体的排版度量，界面可能出现轻微错位；"
                "建议先在副本上验证。"
            )
        else:
            notes.append(
                "未检测到 TMP 字体资源。若游戏界面出现口口口，说明字体烘焙在 "
                "AssetBundle / .assets 二进制里：请先用 AssetStudio/UABEA 导出，"
                "再用 Unity 编辑器重新烘焙，或改用散装文件模式处理。"
            )

        notes.append(
            f"补好的字体已放在 {self._rel(out_dir, sa / '_novaloc_fonts')}。"
            "若游戏使用**动态（Dynamic）** TMP 字体资源，它会在运行时按需取字，"
            "把该目录下的字体替换进去即可能直接生效。"
        )
        return notes

    def _streaming_assets(self, out_dir: Path) -> Path:
        """定位输出目录里的 ``StreamingAssets``（没有就用 ``*_Data`` 下新建）。"""
        for cand in out_dir.glob("*/StreamingAssets"):
            if cand.is_dir():
                return cand
        for cand in out_dir.glob("*_Data"):
            if cand.is_dir():
                return cand / "StreamingAssets"
        return out_dir / "StreamingAssets"

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

        # ---- 二进制资源里的字符串：原地等长改写 ----
        #
        # 必须放在 `prepare_out` **之后** —— 那一步已经把游戏目录拷进
        # `out/`，所以这里是改 `out/` 里的副本，原游戏目录始终只读。
        patch_note = self._apply_serialized(game_dir, out_dir, translations, units)
        if patch_note:
            res.files_written += patch_note["files"]
            res.warnings.extend(patch_note["warnings"])
            res.warnings.append(patch_note["summary"])

        res.ok = res.files_written > 0 or not by_file
        if not res.ok and not res.error:
            res.error = "没有任何文件被写入"
        return res

    def _apply_serialized(
        self,
        game_dir: Path,
        out_dir: Path,
        translations: dict[str, str],
        units: list[TextUnit] | None = None,
    ) -> dict | None:
        """把译文原地写回二进制资源（严格等长）。返回小结，没做事则 None。

        ## units 从哪来（一个实测踩过的坑）

        **绝不能只依赖 `self._last_units`。** 实测：`stage_apply` 拿到的
        适配器实例与 `stage_extract` 用的**不是同一个对象**，所以
        `self._last_units` 是**空的** —— 结果是"抽取到 3 条、翻译 3 条、
        回写 0 条"，最后报 `没有任何文件被写入`。

        这个坑很隐蔽：我在**直接调用**里验证过它是对的（因为那里复用了
        同一个适配器对象），只有走完整流水线才暴露。

        所以按可靠性依次取：

        1. `apply()` 传进来的 ``units`` —— 这就是 `stage_apply` 从
           `ws.load_units()` 读出来的那一份，**uid 保证与 translations 对得上**；
        2. ``self._last_units``（同对象复用时的快捷路径）；
        3. 边车文件 —— 只用来补强容量/原文，**不**提供 uid 映射。

        任何一条能给出偏移就够了；三者全无才返回 None。
        """
        from .unity_patch import patch_translations

        # ① 优先用调用方传进来的 units；没有再退回实例缓存
        src_units = list(units or []) or list(getattr(self, "_last_units", None) or [])
        if not src_units:
            return None

        per_file: dict[str, dict[int, str]] = {}
        originals: dict[tuple[str, int], str] = {}
        capacities: dict[tuple[str, int], int] = {}

        # ② 从 units 取偏移与容量（不依赖边车）
        #
        # 注意：这里**不要求** unit 带 `max_bytes` —— 容量还可以来自边车。
        # 老工作区（`max_bytes` 字段是本版本才加的）里它就是 None，
        # 此时只要边车在，回写依然应当工作。要求两者都有会让
        # "早期抽取的工作区"静默失效，而这正是最难查的那类问题。
        for u in src_units:
            off = u.location.byte_offset
            if not isinstance(off, int) or off < 0:
                continue
            key = (u.location.file, off)
            originals.setdefault(key, u.source)
            if isinstance(u.max_bytes, int) and u.max_bytes > 0:
                capacities[key] = u.max_bytes

            t = translations.get(u.uid)
            if t:
                per_file.setdefault(u.location.file, {})[off] = t

        if not originals or not per_file:
            return None

        # ③ 边车有就用它补强（容量以抽取时记录为准）
        sidecar = self._slot_sidecar()
        if sidecar is not None and sidecar.is_file():
            try:
                for s in json.loads(sidecar.read_text(encoding="utf-8")):
                    rel = str(s["file"])
                    off = int(s["offset"])
                    originals[(rel, off)] = str(s["text"])
                    capacities[(rel, off)] = int(s["capacity"])
            except (OSError, ValueError) as exc:
                log.debug("边车文件读不出（不影响回写）：%s", exc)

        rep = patch_translations(
            game_dir,
            out_dir,
            rel_files=set(per_file),
            translations={
                (rel, off): t for rel, d in per_file.items() for off, t in d.items()
            },
            originals=originals,
            capacities=capacities,
        )
        return {
            "files": rep.files_changed,
            "warnings": [f"二进制资源：{e}" for e in rep.errors],
            "summary": "二进制资源字符串：" + rep.summary(),
        }

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
