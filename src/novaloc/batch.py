"""批量汉化一个游戏库，并把成果**就地写回**原游戏目录。

## 这个模块解决什么

用户有一个持续增长的本地游戏库（实测 194 个目录），希望"不支持中文的
游戏自动翻译"，包括**后续新增的**。逐个手工建项目、跑流水线不现实。

## 设计上的三条硬约束

1. **原目录默认只读**（与项目一直以来的原则一致）。就地写回是用户明确
   要求的，所以必须满足下一条。
2. **写回前必须留备份**。写回是**不可逆**的破坏性操作，一旦译错就毁了
   用户的原始收藏。备份默认开、不能静默关掉。
3. **只写"真的变了"的文件**。`apply` 阶段的 `out/` 是 `unpack` 拷出来的
   **整份游戏副本**（含 .exe/.dll），若整目录覆盖回去，会把几百个
   二进制文件一起重写 —— 慢且危险。所以用**内容哈希**比对，
   只回写内容与原文件不同的那些。

   ⚠️ 这里不能用"比较修改时间"：`unpack` 拷贝会刷新 mtime，
   于是**每个**文件看起来都"变了"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: 已存在这些标志文件就认为"该目录不是游戏，是别的东西"
_SKIP_DIR_NAMES = {
    "$recycle.bin",
    "system volume information",
    "node_modules",
    "__pycache__",
    ".git",
    ".svn",
}

#: 备份目录名（放在数据根下，不污染游戏库）
BACKUP_DIR_NAME = "_novaloc_backup"


@dataclass
class GameEntry:
    """库里的一个候选游戏目录。"""

    path: Path
    name: str
    engine_id: str = ""
    display_name: str = ""
    already_chinese: bool = False
    chinese_evidence: str = ""
    project_id: str = ""
    units: int = 0
    translated: int = 0
    images: int = 0
    font_ok: bool = False
    written_back: int = 0
    status: str = "pending"
    """pending / running / done / skipped / failed / no_text / blocked"""
    message: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        return d


def find_games(
    root: Path,
    *,
    max_depth: int = 2,
    skip_names: set[str] | None = None,
) -> list[Path]:
    """在 ``root`` 下找出候选游戏目录。

    同时支持两种摆放方式（实测用户的库两种都有）：

    * **一层**：``root/<游戏>/``
    * **带散装压缩包**：``root/`` 下直接有 ``*.zip``/``*.7z``，
      以及 ``root/<游戏>/`` 并存

    判定"像游戏目录"的依据（任一满足）：

    * 含 ``*.exe``；
    * 含 ``data/`` 且里面有 ``*.json``/``*.rxdata``（RPG Maker）；
    * 含 ``game/`` 目录（Ren'Py）；
    * 含 ``*_Data/`` 目录（Unity）；
    * 含 ``www/`` 目录（RPG Maker MV）。

    只按**结构**判断，不读文件内容 —— 快且不会被内容误导。
    """
    skip = {n.lower() for n in (skip_names or set())} | _SKIP_DIR_NAMES
    found: list[Path] = []

    def looks_like_game(d: Path) -> bool:
        try:
            names = {p.name.lower() for p in d.iterdir()}
        except OSError:
            return False
        if any(n.endswith(".exe") for n in names):
            return True
        if "game" in names and (d / "game").is_dir():
            return True
        if "www" in names and (d / "www").is_dir():
            return True
        if any(n.endswith("_data") for n in names):
            return True
        data = d / "data"
        if data.is_dir():
            try:
                exts = {p.suffix.lower() for p in data.iterdir() if p.is_file()}
            except OSError:
                return False
            if exts & {".json", ".rxdata", ".rvdata", ".rvdata2", ".dat"}:
                return True
        return False

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return
        for c in children:
            if c.name.lower() in skip:
                continue
            if looks_like_game(c):
                found.append(c)
            elif depth < max_depth:
                walk(c, depth + 1)

    if looks_like_game(root):
        found.append(root)
    else:
        walk(root, 1)
    return found


def _blob_of_strings(obj: object, *, limit: int = 4000) -> str:
    """把嵌套结构里的**所有字符串**拼起来（用于统计汉字占比）。

    ## 为什么必须递归

    RPG Maker 的 ``System.json`` 里 ``terms`` 的值**不是**字符串，
    而是**嵌套列表**：``terms.basic = [["等级","生命值",...], [...]]``。
    最初写成"只取 ``isinstance(v, str)``"就**一个字符串都取不到**，
    于是"已是中文"永远判 False —— 用户库里 18 个中文游戏会被重翻一遍。
    （这个缺陷是 :mod:`tests.test_batch` 抓出来的，不是靠读代码看出来的。）
    """
    out: list[str] = []

    def walk(x: object) -> None:
        if sum(len(s) for s in out) >= limit:
            return
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)
    return "".join(out)[:limit]


def detect_already_chinese(game_dir: Path, engine_id: str) -> tuple[bool, str]:
    """判断这个游戏**本身已经是中文**，不需要再翻。

    ## 判据（保守：只在证据明确时判 True）

    * RPG Maker MV/MZ：``www/data/System.json`` 的 ``terms`` 里汉字占比已经
      很高（界面文案本来就是中文），或 ``locale`` 是 zh；
    * 通用：游戏目录里存在**中文字体**文件（``*zh*``/``*sc*``/``*cn*``
      之类的 ttf/otf）说明发布方已经处理过中文；
    * 通用：已经由本工具处理过（存在 ``*novaloc*`` 字体）。

    判 ``False`` 不代表一定有东西可翻（Unity 常见），只代表"没证据说明
    它是中文"。真正"有没有可翻文本"由抽取阶段的条数决定。
    """
    # ① RPG Maker：看 System.json 里的界面文案
    for sub in ("www/data", "data"):
        sys_json = game_dir / sub / "System.json"
        if sys_json.is_file():
            try:
                d = json.loads(sys_json.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                d = {}
            if isinstance(d, dict):
                blob = _blob_of_strings(d.get("terms"))
                # 门槛取 8：只要够判"是不是中文"就够。
                # 原本写 20，结果一份只有 15 个字的 terms 被**直接跳过** ——
                # 门槛不该替用户决定"样本太少所以不算中文"。
                if len(blob) >= 8:
                    cjk = sum(1 for ch in blob if "\u4e00" <= ch <= "\u9fff")
                    ratio = cjk / len(blob)
                    if ratio > 0.3:
                        return True, f"{sub}/System.json 界面文案汉字占比 {ratio:.0%}"
                loc = str(d.get("locale", "")).lower()
                if loc.startswith("zh"):
                    return True, f"{sub}/System.json locale={loc}"

    # ② 中文字体文件
    try:
        for p in game_dir.rglob("*"):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in {".ttf", ".otf", ".ttc", ".woff", ".woff2"}:
                continue
            low = p.stem.lower()
            if any(k in low for k in ("zh", "chs", "cht", "sc", "tc", "cn", "hans", "hant")):
                return True, f"已内置中文字体：{p.name}"
            if "novaloc" in low:
                return True, f"已由本工具处理过：{p.name}"
    except OSError:
        pass

    return False, ""


def _file_hash(p: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def changed_files(
    source_dir: Path, out_dir: Path, *, exclude_dirs: set[str] | None = None
) -> list[Path]:
    """``out_dir`` 里内容与 ``source_dir`` **不同**的文件（相对路径）。

    用**内容哈希**而不是 mtime —— `unpack` 拷贝会刷新 mtime，
    按时间比会让整目录都算"变了"。
    """
    ex = {n.lower() for n in (exclude_dirs or set())}
    changed: list[Path] = []
    if not out_dir.is_dir():
        return changed
    for p in out_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(out_dir)
        if any(part.lower() in ex for part in rel.parts):
            continue
        origin = source_dir / rel
        if not origin.is_file():
            changed.append(rel)
            continue
        # 先比大小（快），大小相同再比哈希
        try:
            if p.stat().st_size != origin.stat().st_size:
                changed.append(rel)
                continue
            if _file_hash(p) != _file_hash(origin):
                changed.append(rel)
        except OSError:
            changed.append(rel)
    return changed


@dataclass
class WriteBackReport:
    written: list[str] = field(default_factory=list)
    backup_dir: str = ""
    failed: list[str] = field(default_factory=list)


def _unique_backup_dir(backup_root: Path, game_name: str) -> Path:
    """给出一个**确实唯一**的备份目录，绝不与已有目录重名。

    ## 为什么需要它（实测的缺陷）

    原来是 ``backup_root / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"`` ——
    时间戳只精确到**秒**。同一秒内跑两次 :func:`write_back` 就会拿到
    **同一个**目录，而目录里那句 ``if not bak.is_file()`` 会让第二次
    **跳过备份**，同时**照常覆盖**目标文件。

    实测复现（`.scratch/_backup_rollback.py`）：

        # 第一次写回：V0 -> V1，备份里是 V0
        # 第二次写回：V1 -> V2（同一秒），备份目录**同名**
        backup=Data-20261002-191619 （两次相同）
        游戏文件 = V2    备份内容 = V0

    结果：**中间的 V1 版本没有被保存**。回滚到"最初"仍然可行
    （备份里是 V0），但目录名让人以为这是第二次写回的独立备份 ——
    真出事时"少一个可回滚的版本"，而且**完全没有任何提示**。

    ## 修法

    冲突时加 ``-2``、``-3``… 后缀，直到目录名可用。
    这样每次写回都有**自己的**备份目录，`if not bak.is_file()`（用于
    "同一目录内不重复备份同一文件"）也就不会跨次误判。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = backup_root / f"{game_name}-{stamp}"
    if not candidate.exists():
        return candidate
    for n in range(2, 1000):
        candidate = backup_root / f"{game_name}-{stamp}-{n}"
        if not candidate.exists():
            return candidate
    # 极端情况兜底：用微秒，保证不再撞
    return backup_root / f"{game_name}-{stamp}-{time.time_ns() % 1_000_000_000}"


def write_back(
    source_dir: Path,
    out_dir: Path,
    *,
    backup_root: Path,
    dry_run: bool = False,
    exclude_dirs: set[str] | None = None,
) -> WriteBackReport:
    """把 ``out_dir`` 里**变了的**文件覆盖回 ``source_dir``，先备份原件。"""
    rep = WriteBackReport()
    rels = changed_files(source_dir, out_dir, exclude_dirs=exclude_dirs)
    if not rels:
        return rep

    backup_dir = _unique_backup_dir(backup_root, source_dir.name)
    rep.backup_dir = str(backup_dir)

    for rel in rels:
        src = out_dir / rel
        dst = source_dir / rel
        bak = backup_dir / rel
        try:
            if dry_run:
                rep.written.append(str(rel))
                continue
            # 备份原件（保留相对结构）
            if dst.is_file():
                bak.parent.mkdir(parents=True, exist_ok=True)
                if not bak.is_file():
                    shutil.copy2(dst, bak)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            rep.written.append(str(rel))
        except OSError as exc:
            rep.failed.append(f"{rel}: {exc}")
    return rep


def iter_library_watch(
    root: Path,
    *,
    interval_s: float = 60.0,
    stop: Callable[[], bool] | None = None,
    seen: set[str] | None = None,
) -> Iterator[list[Path]]:
    """持续产出"新出现且还没处理过"的游戏目录，用于守望后续新增。

    ``seen`` 由调用方持有（本函数会往里加），这样跨轮次不会重复处理。
    """
    known = seen if seen is not None else set()
    while True:
        try:
            games = find_games(root)
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描游戏库失败：%s", exc)
            games = []
        fresh = [g for g in games if str(g).lower() not in known]
        if fresh:
            for g in fresh:
                known.add(str(g).lower())
            yield fresh
        if stop is not None and stop():
            return
        time.sleep(interval_s)
