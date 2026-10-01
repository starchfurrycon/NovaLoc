"""工作区：一个汉化项目的全部中间产物与状态。

目录布局::

    <data_root>/workspaces/<project_id>/
      project.json          项目元信息
      project.src           原始游戏目录路径
      extracted/
        units.jsonl         抽取出的文本单元
        images.jsonl        发现的贴图资产
        report.json         抽取报告
      translations/
        entries.jsonl       译文（流水线可反复覆盖，用户编辑也写这里）
        glossary.json       术语表
        memory.jsonl        翻译记忆库
      fonts/
        analysis.json       字体覆盖审计
        patches.json        字体补丁结果
        fallback/           注入用的中文字体
      images/
        analyzed/           带文字框标注的可视化图（人工复核用）
        rebuilt/            重绘后的贴图
      out/                  最终回写出的可玩游戏目录
      qa/
        report.json         质检报告
      logs/

**原始游戏目录永远只读**，所有写操作都在此处副本上进行。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ..models import (
    ExtractReport,
    FontCoverage,
    FontPatchResult,
    GlossaryEntry,
    ImageAsset,
    Project,
    ProjectCharset,
    TextUnit,
    TranslationEntry,
    json_dumps,
)
from . import paths
from .sanitize import sanitize_tree

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class WorkspaceBusyError(RuntimeError):
    """工作区被另一个进程占用。

    单独定义成异常类型（而不是返回布尔/打日志）是为了让调用方**必须**处理：
    并发写会静默抹掉对方成果，绝不能"警告一下继续跑"。
    """


def _pid_alive(pid: int) -> bool:
    """判断某个 pid 是否还活着（用于识别**陈旧锁**）。

    ## Windows 上不能用 `os.kill(pid, 0)`

    POSIX 里 `os.kill(pid, 0)` 是标准做法（`ESRCH` = 不存在）。
    但**实测 Windows 上它对已退出的进程不抛任何异常**
    （CPython 的实现是调 `OpenProcess` + `TerminateProcess`，
    对死 pid 直接静默返回）：

        已退出进程 pid=37100
        os.kill(37100, 0) → 没有抛异常

    用它判活会**永远返回 True** —— 于是陈旧锁永远解不开，
    用户被一个早已不存在的进程永久挡住，和"锁不释放"是同一个病。

    所以 Windows 走 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`
    + `GetExitCodeProcess`：打不开句柄 = 进程不存在；
    打得开但退出码不是 `STILL_ACTIVE` = 已退出。
    实测三种情况都对（已退出 False / 自己 True / 不存在的 pid False）。

    判错的方向必须是"保守"：**宁可认为它还活着**（于是报错让用户确认），
    也不要把一个真在跑的进程误判成死的、然后两个进程一起写。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但没权限动它 → 活着
    except OSError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows：`OpenProcess` + `GetExitCodeProcess`。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        # 只要"能不能查到"，不要别的权限 —— 权限越小越不容易被拒。
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False  # 打不开 = 不存在（或已退出）
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True  # 查不到 → 保守判活
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 任何异常都保守判活
        return True


def _tmp_sibling(path: Path) -> Path:
    """给 ``path`` 生成一个**本进程独有**的临时文件名（同目录）。

    ## 为什么不能固定叫 ``<name>.tmp``

    原子写是"先写 tmp、再 rename 覆盖正式文件"。两个进程同时写同一个
    ``.tmp`` 时，Windows 上第二个 rename 会

        [WinError 32] 另一个程序正在使用此文件，进程无法访问

    **真实事故**：翻译阶段跑了 **50 分钟**（21655 条），
    我在另一个终端并发跑 `apply`（它也要写 `entries.jsonl`），
    两边争同一个 `entries.jsonl.tmp` —— 翻译在**最后一步保存时崩掉**，
    而且 `entries.jsonl` 中间还出现了 1 行截断（读到一半被替换）。

    ## 为什么不是加锁就够了

    加锁（见 `Workspace.lock()`）能挡住"同时跑两个阶段"，
    但挡不住"同一个进程里两个线程"，也挡不住用户手工编辑文件。
    **独有 tmp 名**是更底层的一道保险：最坏情况也只是"最后一次写入赢"，
    绝不会出现半个文件。

    用 `os.getpid()` + 线程 id：同进程不同线程也不会撞。
    """
    import os
    import threading

    tag = f"{os.getpid()}-{threading.get_ident()}"
    return path.with_name(f"{path.name}.{tag}.tmp")


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_sibling(path)
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _read_jsonl(path: Path, model: type[T]) -> list[T]:
    """读 jsonl，**跳过**坏行但把跳过的行**记下来**。

    ## 为什么"跳过"是对的，"不吭声"是错的

    单行坏了不该毁掉整个列表 —— 45k 条译文里坏一行就把整批丢掉，
    用户会以为翻译全没了。所以跳过。

    但**必须留下痕迹**：这些文件是用户可以手改的（`entries.jsonl`
    就是给审校页和脚本改的）。改了之后写坏一行，静默跳过意味着
    "用户改的译文凭空消失"，而报告里一切正常 —— 正是本项目反复
    栽跟头的那一类（静默部分丢弃）。

    所以这里返回列表，同时 `log.warning` 报出**行号与原因**。
    """
    if not path.exists():
        return []
    out: list[T] = []
    bad: list[tuple[int, str]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(model.model_validate_json(line))
            except Exception as exc:  # noqa: BLE001 - 单行坏了不该毁掉整个列表
                bad.append((lineno, f"{type(exc).__name__}: {exc}"))
    if bad:
        detail = "；".join(f"第 {n} 行（{msg[:80]}）" for n, msg in bad[:3])
        more = f"，另有 {len(bad) - 3} 行" if len(bad) > 3 else ""
        log.warning(
            "%s：跳过 %d 行无法解析的内容（%s%s）。已有 %d 条正常读入"
            " —— 这些行不会出现在后续阶段，请检查文件是否被改坏。",
            path.name,
            len(bad),
            detail,
            more,
            len(out),
        )
    return out


def _json_safe(model: BaseModel) -> str:
    """``model_dump_json()``，但把无法编码的孤立代理项替换掉。

    `model_dump_json()` 在遇到孤立代理项时会抛
    `PydanticSerializationError`（其内部 `to_json()` 用
    `ensure_ascii=False`，直接编 UTF-8）。这里改用
    `model_dump(mode="json")` + 净化 + `json.dumps`，
    保证**永远**能写出合法 UTF-8。

    只用 `errors="replace"` 之类的"事后补救"不行 —— 序列化那一步本身
    就抛了，根本走不到写文件。必须在**序列化之前**把字符换掉。
    """
    data = sanitize_tree(model.model_dump(mode="json"))
    # 必须和 `model_dump_json()` 的输出**逐字节一致**：
    # `ensure_ascii=False` 让中文是明文（便于用户直接看和 diff），
    # `separators=(",", ":")` 复现 pydantic 的紧凑格式（无空格）。
    # 少了 separators 会给每个键值多一个空格，整仓库的 jsonl 产物
    # 都会变样，用户已有的文件 diff 会炸开。
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _write_jsonl(path: Path, items: Iterable[BaseModel]) -> int:

    path.parent.mkdir(parents=True, exist_ok=True)
    # 独有 tmp 名，避免并发写入撞在同一个 `.tmp` 上（见 `_tmp_sibling`）。
    tmp = _tmp_sibling(path)
    n = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for it in items:
            # 产物落盘是**最后一道防线**：任何来源的孤立代理项都不能让
            # 整个阶段崩掉。真实事故：一次翻译跑完 848 条后在
            # `save_entries` 里炸了 ——
            #   PydanticSerializationError: UnicodeEncodeError: 'utf-8'
            #   codec can't encode character '\uddd1'
            # （`model_dump_json()` 内部 `to_json()` 默认 `ensure_ascii=False`，
            #   所以报错位置是明文里的偏移，看着像别处的问题）
            # 848 条已经拿到手的译文因为一个字符全丢，不可接受。
            #
            # 净化放在 `model_dump_json()` **之后**：这样无论字段是
            # `str` 还是 `list[str]`、无论模型定义怎么变，都覆盖得到，
            # 也不必给每个模型加校验器。`sanitize_for_json` 只动孤立
            # 代理项，真 emoji（单个码点）不受影响。
            fh.write(_json_safe(it))
            fh.write("\n")
            n += 1
    tmp.replace(path)
    return n


class Workspace:
    def __init__(self, project: Project, root: Path) -> None:
        self.project = project
        self.root = root
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 跨进程互斥
    # ------------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.root / ".novaloc.lock"

    @contextlib.contextmanager
    def lock(self, *, what: str = "流水线") -> Iterator[None]:
        """独占这个工作区，防止两个 `novaloc run` 同时写同一批文件。

        ## 真实事故（这次是我自己踩的）

        翻译阶段跑到第 50 分钟（21655 条）时，我在另一个终端并发跑了
        `apply`。两边都要写 `translations/entries.jsonl`：

        * 两边抢同一个 `entries.jsonl.tmp`，Windows 上是
          `[WinError 32] 另一个程序正在使用此文件，进程无法访问`，
          翻译在**最后一步保存时崩掉**；
        * 更糟的是 `entries.jsonl` **中间**出现了 1 行截断
          （读到一半被替换），说明"原子改名"并不是在所有时序下都安全。

        已经用"独有 tmp 名"（`_tmp_sibling`）修掉了截断，
        但"同时跑两个阶段"本身仍然是**逻辑错误**：两个进程各自读到
        旧的 `entries.jsonl`，各自写回自己的版本，**后写的把先写的成果全抹掉**。
        这种情况没有任何报错，用户只会发现"莫名其妙少了一批译文"。

        ## 为什么用"锁文件 + 进程存活检测"而不是 `msvcrt.locking`

        文件锁在进程被强杀（Ctrl+C、关机、`taskkill`）时**不会释放**，
        用户下次运行会永远被挡住，而且查不出原因。
        这里记 PID：锁文件里写着持有者的 pid，
        新进程发现那个 pid **已经不存在**就认为锁是陈旧的、直接接管。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        lp = self.lock_path
        for attempt in range(2):
            try:
                fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = ""
                try:
                    holder = lp.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    holder = ""
                pid_txt = holder.split()[0] if holder.split() else ""
                alive = False
                if pid_txt.isdigit():
                    alive = _pid_alive(int(pid_txt))
                if alive and attempt == 0:
                    raise WorkspaceBusyError(
                        f"这个工作区正被另一个进程使用（pid {pid_txt}）。\n"
                        f"锁文件：{lp}\n"
                        "同时跑两个阶段会互相覆盖成果，所以这里直接停下。\n"
                        "若确认那个进程已经不在了，删掉锁文件再试。"
                    ) from None
                # 陈旧锁：持有者已死 → 接管。
                try:
                    lp.unlink()
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {what}\n")
            break
        else:  # pragma: no cover - 两次都撞上，属于异常情况
            raise WorkspaceBusyError(f"无法取得工作区锁：{lp}")

        try:
            yield
        finally:
            # 只删自己写的锁，避免误删别人的（接管竞态）
            try:
                if lp.is_file() and lp.read_text(encoding="utf-8").strip().startswith(
                    str(os.getpid())
                ):
                    lp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 创建 / 打开
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        game_dir: str | Path,
        *,
        project_id: str | None = None,
        target_lang: str = "zh-Hans",
        source_langs: list[str] | None = None,
    ) -> Workspace:
        src = Path(game_dir).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"游戏目录不存在：{src}")
        if not src.is_dir():
            raise NotADirectoryError(f"不是目录：{src}")
        # 防止把工作区建到游戏目录里（会造成递归扫描）
        src_res = src.resolve()

        proj = Project(
            name=name,
            game_dir=str(src_res),
            target_lang=target_lang,
            source_langs=source_langs or ["auto"],
        )
        if project_id:
            proj.id = project_id

        root = paths.workspaces_dir() / proj.id
        if root.exists():
            shutil.rmtree(root)
        for sub in (
            "extracted", "translations", "fonts", "fonts/fallback",
            "images", "images/analyzed", "images/rebuilt", "out", "qa", "logs",
        ):
            (root / sub).mkdir(parents=True, exist_ok=True)

        ws = cls(proj, root)
        ws.save()
        (root / "project.src").write_text(str(src_res), encoding="utf-8")
        return ws

    @classmethod
    def open(cls, project_id: str) -> Workspace:
        root = paths.workspaces_dir() / project_id
        pf = root / "project.json"
        if not pf.exists():
            raise FileNotFoundError(f"项目不存在：{project_id}")
        proj = Project.model_validate_json(pf.read_text(encoding="utf-8"))
        return cls(proj, root)

    @classmethod
    def list(cls) -> list[Project]:
        out: list[Project] = []
        base = paths.workspaces_dir()
        if not base.exists():
            return out
        for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            pf = d / "project.json"
            if not pf.is_file():
                continue
            try:
                out.append(Project.model_validate_json(pf.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
        return out

    def delete(self) -> None:
        if self.root.exists() and self.root.is_dir() and self.root.parent == paths.workspaces_dir():
            shutil.rmtree(self.root)

    def save(self) -> None:
        with self._lock:
            self.project.bump()
            _atomic_write(self.root / "project.json", self.project.model_dump_json(indent=2))

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def p(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def source_dir(self) -> Path:
        """用户给的**原始**游戏目录。**只读**，任何阶段都不许往里写。"""
        return Path(self.project.game_dir)

    @property
    def unpacked_root(self) -> Path:
        """解包产物的容器目录。里面每个子目录对应一个归档。"""
        return self.p("unpacked")

    #: 判定"这个解包目录就是游戏根"的标志
    _GAME_ROOT_MARKERS = ("game", "data", "www", "assets", "renpy", "Managed")

    def resolve_effective_source(self) -> Path:
        """算出各阶段真正该读的根目录，并把结论写进记录（可审计）。

        ## 为什么不能简单地"返回 unpacked_root"

        第一版就是那么写的，结果端到端测试抓到了：`unpacked_root` 是
        **归档的容器**（`unpacked/00_resources/…`），把它当 `game_dir`
        会让适配器算出的相对路径带上 `00_resources/` 前缀 ——
        于是 `apply()` 在 `out/` 里找不到 `00_resources/game/script.rpy`，
        译文一条都写不进去，而**所有阶段都报 ok**。

        正确做法是把 `game_dir` 指向**归档解出来的那一层**，
        这样 `game/script.rpy` 就是 `game/script.rpy`，与明文目录一致。

        ## 多个归档怎么办

        选"看起来最像游戏根"的那个（含 `game/`、`data/`、`www/` 等标志）。
        如果都不像，取第一个并记一条警告。

        **这是已知限制**：多个归档分别装脚本/贴图时，只有被选中的那个
        会被汉化。与其猜（猜错会产出"看着成功、实际一半没翻"的结果），
        不如明确记下来让用户看见。
        """
        records = self.load_unpacked()
        if not records or not self.unpacked_root.is_dir():
            return self.source_dir

        dirs = [Path(r["dest"]) for r in records if r.get("dest")]
        dirs = [d for d in dirs if d.is_dir()]
        if not dirs:
            return self.source_dir

        chosen = dirs[0]
        for d in dirs:
            if any((d / m).exists() for m in self._GAME_ROOT_MARKERS):
                chosen = d
                break

        if len(dirs) > 1:
            self.project.notes = (
                (self.project.notes + "\n") if self.project.notes else ""
            ) + (
                f"⚠️ 发现 {len(dirs)} 个归档，本次只处理 {chosen.name}"
                "（其他归档的资源未纳入）。可先自己解包后用散装模式处理。"
            )
        return chosen

    @property
    def effective_source(self) -> Path:
        """各阶段真正应该去读的根目录：解过包就是归档解出来的那一层。"""
        return self.resolve_effective_source()

    @property
    def out_dir(self) -> Path:
        return self.p("out")

    def load_unpacked(self) -> list[dict[str, Any]]:
        """读回"哪些归档被解到哪儿了"的记录（`unpack` 阶段写的）。

        没解过包时返回空列表 —— 这是**正常情况**（多数游戏是明文目录），
        所以不报错、不警告。
        """
        try:
            raw = self.read_json("unpacked.json", default=[])
        except Exception:  # noqa: BLE001
            return []
        return list(raw) if isinstance(raw, list) else []

    def archive_dest_for(self, rel_file: str) -> Path | None:
        """给定一个相对文件路径，找出它属于哪个归档的解包树。

        回写时要用：`out/game/script.rpy` 要写回"含 `game/script.rpy`
        的那个归档"。多个归档时装脚本的那个才对得上。
        """
        for rec in self.load_unpacked():
            dest = Path(rec.get("dest", ""))
            if dest and (dest / rel_file).is_file():
                return dest
        return None

    # ------------------------------------------------------------------
    # 文本单元
    # ------------------------------------------------------------------

    def save_units(self, units: list[TextUnit]) -> None:
        _write_jsonl(self.p("extracted", "units.jsonl"), units)

    def load_units(self) -> list[TextUnit]:
        return _read_jsonl(self.p("extracted", "units.jsonl"), TextUnit)

    def save_extract_report(self, report: ExtractReport) -> None:
        _atomic_write(self.p("extracted", "report.json"), report.model_dump_json(indent=2))

    def load_extract_reports(self) -> list[ExtractReport]:
        f = self.p("extracted", "report.json")
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        if isinstance(data, list):
            return [ExtractReport.model_validate(d) for d in data]
        return [ExtractReport.model_validate(data)]

    # ------------------------------------------------------------------
    # 译文
    # ------------------------------------------------------------------

    def save_entries(self, entries: list[TranslationEntry]) -> None:
        _write_jsonl(self.p("translations", "entries.jsonl"), entries)

    def load_entries(self) -> list[TranslationEntry]:
        return _read_jsonl(self.p("translations", "entries.jsonl"), TranslationEntry)

    def save_glossary(self, entries: list[GlossaryEntry]) -> None:
        _write_jsonl(self.p("translations", "glossary.jsonl"), entries)

    def load_glossary(self) -> list[GlossaryEntry]:
        return _read_jsonl(self.p("translations", "glossary.jsonl"), GlossaryEntry)

    def save_memory(self, entries: list[TranslationEntry]) -> None:
        _write_jsonl(self.p("translations", "memory.jsonl"), entries)

    def load_memory(self) -> list[TranslationEntry]:
        return _read_jsonl(self.p("translations", "memory.jsonl"), TranslationEntry)

    # ------------------------------------------------------------------
    # 贴图
    # ------------------------------------------------------------------

    def save_images(self, images: list[ImageAsset]) -> None:
        _write_jsonl(self.p("extracted", "images.jsonl"), images)

    def load_images(self) -> list[ImageAsset]:
        return _read_jsonl(self.p("extracted", "images.jsonl"), ImageAsset)

    # ------------------------------------------------------------------
    # 字体
    # ------------------------------------------------------------------

    def save_font_coverage(self, items: list[FontCoverage]) -> None:
        _write_jsonl(self.p("fonts", "analysis.jsonl"), items)

    def load_font_coverage(self) -> list[FontCoverage]:
        return _read_jsonl(self.p("fonts", "analysis.jsonl"), FontCoverage)

    def save_font_patches(self, items: list[FontPatchResult]) -> None:
        _write_jsonl(self.p("fonts", "patches.jsonl"), items)

    def load_font_patches(self) -> list[FontPatchResult]:
        return _read_jsonl(self.p("fonts", "patches.jsonl"), FontPatchResult)

    def save_charset(self, cs: ProjectCharset) -> None:
        _atomic_write(self.p("fonts", "charset.json"), cs.model_dump_json(indent=2))

    def load_charset(self) -> ProjectCharset | None:
        f = self.p("fonts", "charset.json")
        if not f.exists():
            return None
        try:
            return ProjectCharset.model_validate_json(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 通用 JSON
    # ------------------------------------------------------------------

    def write_json(self, rel: str, obj: Any) -> Path:
        p = self.p(*rel.split("/"))
        _atomic_write(p, json_dumps(obj))
        return p

    def read_json(self, rel: str, default: Any = None) -> Any:
        p = self.p(*rel.split("/"))
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default

    def log_line(self, name: str, text: str) -> None:
        p = self.p("logs", name)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")

    def iter_source_files(self, *, follow_symlinks: bool = False) -> Iterator[Path]:
        """遍历原始游戏目录里的文件（只读）。"""
        root = self.source_dir
        for p in root.rglob("*"):
            try:
                if p.is_symlink() and not follow_symlinks:
                    continue
                if p.is_file():
                    yield p
            except OSError:
                continue
