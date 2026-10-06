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
import re
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


def _zh_lang_dir_names() -> set[str]:
    """被认作"中文语言目录"的名字（小写比较）。

    覆盖面故意放宽：漏判的代价是**白烧几小时 GPU + 把好好的中文游戏改掉**，
    误判的代价只是"少翻一个游戏"（用户还能用 `--force` 强制翻）。
    这个不对称决定了门槛该松还是该紧。
    """
    return {
        "zh", "zh-cn", "zh_cn", "zh-hans", "zh_hans", "zh-sg",
        "zh-tw", "zh_tw", "zh-hant", "zh_hant", "zh-hk", "zhhk",
        "chinese", "chs", "cht", "sc", "tc", "cn",
        "chinese(simplified)", "chinese(traditional)",
        "简体", "繁体", "中文", "简中", "繁中",
    }


#: 用来确认"目录里真有中文"的文本类扩展名
_ZH_TEXT_EXT = {
    ".txt", ".json", ".csv", ".xml", ".yml", ".yaml",
    ".bytes", ".tsv", ".ini", ".po", ".resx", ".strings",
}

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")

#: 中文语言码的**文件名主干**集合（已把 `_` 归一成 `-`、转小写）。
#: 见 :func:`_zh_language_file`：用**文件名**而不是目录名或内容作证据。
_ZH_FILE_CODES: frozenset[str] = frozenset(
    {
        "zh", "zh-cn", "zh-hans", "zh-sg", "zh-tw", "zh-hant", "zh-hk",
        "zhcn", "zhtw", "chs", "cht", "schinese", "tchinese", "chinese",
        "cn", "sc", "tc", "zho", "chi", "zh-cn-hans", "zh-hant-tw",
        "zh-hans-cn", "中文", "简体", "繁体", "简中", "繁中", "汉化", "漢化",
    }
)


def _zh_language_file(game_dir: Path, *, max_depth: int = 6) -> Path | None:
    r"""游戏里有没有**文件名就是中文语言码**的资产（`zh-CN.pak` / `zh_CN.json`）。

    ## 为什么单独抽一个函数

    它是 :func:`detect_builtin_chinese_assets` 的**判据 0**，逻辑独立
    （按**文件名**而不是目录名或内容），所以单独可测 —— 见
    `tests/test_zh_language_file.py`。

    ## 为什么要**递归剥后缀**

    真实文件名形如：

    ```
    zh-CN.pak          ← 剥 1 次得 `zh-CN`
    zh_CN.pak.info     ← 剥 1 次得 `zh_CN.pak`，剥 2 次才得 `zh_CN`
    zh-Hans.xaml       ← 剥 1 次得 `zh-Hans`
    ```

    只看一层后缀会漏掉 `zh-CN.pak.info` 这类。剥 3 层足够覆盖实测形态。

    ## 为什么要归一 `_` → `-` 并转小写

    实测同一个语言有 `zh_CN` / `zh-CN` / `ZH-cn` 多种写法。
    归一后只需维护一份代码集合，避免"为新写法又加一条"。

    ## 边界：不误抓"看起来像语言码"的普通文件

    只接受**完整主干**匹配（剥完后整串等于某个语言码），
    所以 `zh-CN-notes.txt`（剥完是 `zh-CN-notes`）**不会**命中 ——
    它是"提到中文的文件"，不是"中文语言包"。

    ## ★ 为什么要排除**两字母代码 + 多段主干**

    实测误判（`.scratch/_zh_code_evidence.py` 的全库排查）：

    ```
    Fox Sex Farm_Data\Managed\sc.stylizedwater2.runtime.dll   ← 剥后缀得 'sc'
    Robolife2_Data\Managed\sc.posteffects.runtime.dll         ← 同样
    ```

    `sc` 是合法的两字母语言码（简体中文），而这两个 **DLL** 名剥掉
    `.dll` 后恰好是 `sc.stylizedwater2.runtime`，再剥才到 `sc`。
    它是 Unity 的 **Shader 库**，与语言毫无关系。

    ⇒ 判据收紧为：**两字母主干**（`sc`/`tc`/`cn`/`zh`）**必须没有多余的点**，
    即整串就是那个代码本身。`sc.xxx.yyy` 因此被排除，
    而 `zh-CN.pak`（主干 `zh-CN`，两段但用 `-`）仍然命中。

    这个收紧**零成本**：实测 57 个命中里 **52 个是 `zh-CN.pak`**，
    两字母形态只有 `zh.dat` 一例。
    """
    base_depth = len(game_dir.parts)
    try:
        for p in game_dir.rglob("*"):
            if not p.is_file():
                continue
            if len(p.parts) - base_depth > max_depth:
                continue
            original = p.name
            stem = original
            for _ in range(4):  # `x.pak.info` 要剥两次
                nxt = Path(stem).stem
                if nxt == stem:
                    break
                stem = nxt
            norm = stem.strip().lower().replace("_", "-")
            if norm not in _ZH_FILE_CODES:
                continue
            # ★ 裸两字母码（`sc`/`tc`/`cn`/`zh`）**必须没有多余的点**。
            #
            # ⚠️ 守卫要看**原始文件名**，不能看 `stem` —— 后者已经把所有点剥掉了，
            #    我第一版就写错在这里（`'.' in stem` 永远为假，守卫形同虚设）。
            #
            # 实测误判：`sc.stylizedwater2.runtime.dll` 剥到 `sc` 后
            # 命中语言码，而它是 Unity 的 **Shader 库**，与语言无关。
            if "-" not in norm and original.count(".") > 1:
                continue
            return p
    except OSError:
        return None
    return None


def detect_builtin_chinese_assets(
    game_dir: Path, *, max_depth: int = 6, min_zh_files: int = 3
) -> tuple[bool, str]:
    r"""游戏里有没有**自带的、可直接切换的中文语言资产**。

    ## ★ 为什么需要这个判据（实测漏判，代价很大）

    用户指正：

    > 有可能很多支持中文的游戏被你误处理了，因为支持中文的同时支持别的语言，
    > 导致游戏里有别的语言的资产，而你就进行了多余操作。
    > 比如 Alice in Cradle 就支持中文，可是你依旧在翻译。
    > 支持中文的游戏只需要将其换为其自带的中文就行了（比如在其设置里）。

    实测确认 `AliceInCradle_ver029`：

        AliceInCradle_Data\StreamingAssets\localization\
            en/  ko-kr/  th/  zh-cn/  zh-tc/  _/  __AdditionalFonts/
        zh-cn\ 里 70 个文件、`ev_book.txt` 内容就是中文对白
        （「好痛……」诺艾儿用手捂着肿胀的皮肤。）

    ⇒ **官方中文一直在那里**，玩家在设置里选一下就有。
    而我给它翻了 127,693 条，白烧几个小时 GPU。

    ## 为什么 `detect_already_chinese` 原来抓不到

    它只查两样：RPG Maker 的 `System.json` 里 `terms` 的汉字占比、
    以及有没有中文字体文件。**"多语言资产"这种形态它完全看不见** ——
    Unity 游戏把每种语言放进独立目录，主配置里一个中文都没有，
    字体还往往是共用/自定义的。

    ## 判据（两个条件都要满足）

    1. **目录名是语言码**（`zh-cn`/`chn`/`简体`…）；
    2. **该目录里确有一批文件含汉字**（默认 ≥3 个）。

    第 2 条不能省：只看目录名会把"预留了但没填内容"的空壳语言目录
    误判成有中文。
    """
    names = _zh_lang_dir_names()
    base_depth = len(game_dir.parts)

    # ---- ★★ 判据 0（新增）：**语言码文件名**本身就是证据 ----
    #
    # ## 为什么必须加这一条（实测：判据对整个 `locales/` 形态失效）
    #
    # Godot 等引擎的本地化形态是**语言码文件名**：
    #
    #     locales\am.pak  ar.pak  bg.pak  …  **zh-CN.pak**  zh-CN.pak.info
    #
    # 而原有的两条判据**同时**失败：
    #
    # 1. 目录名是 `locales`（**不是** `zh-cn`）⇒ 条件"目录名是语言码"不满足；
    # 2. `.pak` **不在** `_ZH_TEXT_EXT` 白名单里，而且它是**二进制**
    #    ⇒ 就算加进白名单，`read_text` 也读不出汉字 ⇒ 条件"含汉字文件"不满足。
    #
    # 实测后果（用户指正："文件夹里带中文选项的游戏很多，收益应该很高"）：
    #
    # ```
    # 全库 194 个游戏，**57 个（29.4%）** 带中文语言资产
    # 其中 **7 个已被处理过**（判据没拦住，已白烧）：
    #   [Summoner Veil] 49,733 条    Battle Demon Kirsten 31,263 条
    #   Ambrosia 18,785 条           072 Project 15,003 条   …
    # 另有 **50 个尚未处理**（合计 72.8 GB）
    # ```
    #
    # ## 为什么这条判据是**安全**的（不读内容、不判单条）
    #
    # * **不读文件内容** ⇒ 对二进制语言包（`.pak`/`.bundle`）同样有效；
    # * **不判断单条文本** ⇒ 没有 `'清宮 真白'`（全汉字日文人名）那种误杀；
    # * 一个文件叫 `zh-CN.pak` 与 50 个 `xx.pak` 并列在 `locales/` 里，
    #   这是"官方支持中文"的**结构性铁证**。
    #
    # 判据方向符合项目一贯原则（见 `_zh_lang_dir_names` 的 docstring）：
    # **漏判的代价**（白烧几小时 GPU + 改坏一个好游戏）远大于
    # **误判的代价**（少翻一个游戏，用户可 `--force`）。
    #
    # ⚠️ 与原有判据**并列**，不替换 —— 后者仍能抓到"目录形态"的游戏。
    lang_file = _zh_language_file(game_dir)
    if lang_file is not None:
        try:
            rel = lang_file.relative_to(game_dir)
        except ValueError:
            rel = lang_file
        return True, f"自带中文语言文件：{rel}"

    try:
        candidates = [
            d
            for d in game_dir.rglob("*")
            if d.is_dir()
            and len(d.parts) - base_depth <= max_depth
            and d.name.strip().lower() in names
        ]
    except OSError:
        return False, ""

    for d in candidates:
        n_zh = 0
        try:
            for f in d.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in _ZH_TEXT_EXT:
                    continue
                try:
                    if f.stat().st_size > 4 * 1024 * 1024:
                        continue
                    blob = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if _ZH_RE.search(blob):
                    n_zh += 1
                    if n_zh >= min_zh_files:
                        break
        except OSError:
            continue
        if n_zh >= min_zh_files:
            try:
                rel = d.relative_to(game_dir)
            except ValueError:
                rel = d
            return True, f"自带中文语言资产：{rel}（{n_zh}+ 个含汉字文件）"

    return False, ""


def detect_already_chinese(game_dir: Path, engine_id: str) -> tuple[bool, str]:
    """判断这个游戏**本身已经是中文**，不需要再翻。

    ## 判据（保守：只在证据明确时判 True）

    * RPG Maker MV/MZ：``www/data/System.json`` 的 ``terms`` 里汉字占比已经
      很高（界面文案本来就是中文），或 ``locale`` 是 zh；
    * 通用：游戏里存在**自带的、可直接切换的中文语言资产**
      （见 :func:`detect_builtin_chinese_assets`）——
      这一条是本轮补上的，原来漏判导致把 Alice in Cradle 这类
      官方支持中文的游戏重翻了一遍；
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

    # ①b ★ 自带的、可切换的中文语言资产（多语言游戏的主力形态）
    hit, why = detect_builtin_chinese_assets(game_dir)
    if hit:
        return True, why

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


def prune_written_out(
    out_dir: Path,
    backup_dir: Path,
    written: list[str],
    *,
    safe_to_clear: bool = False,
) -> tuple[int, int, list[str]]:
    r"""写回成功后回收 ``out/``（默认整目录清空，保守时退化为逐文件）。

    返回 ``(删除文件数, 释放字节数, 跳过的相对路径)``。

    ## ★ 为什么需要它（实测发现的结构性浪费）

    `prepare_out` 是**整目录复制**（`base.py`：注释说"游戏运行时可能依赖
    大量未被修改的资源，少复制一个就可能启动失败"）。这个决定本身是对的，
    但它让 `out/` 变成**整个游戏的副本**。实测：

    | 游戏 | `out/` | 备份 | 备份覆盖 `out` 的文件 |
    |---|---|---|---|
    | Academy Love Saga | 1235 文件 / 1955 MB | 66 文件 | **65 / 1050** |
    | AliQ | 49 文件 / 1403 MB | 8 文件 | 8 / 9 |

    `out/` 合计 **11.5 GB**（占工作区 14.4 GB 的 80%），备份只有 1.7 GB。

    ## ★★ 关键实测：写回成功后，`out/` 里**全部**文件都等于现盘

    我原本以为"没进 `written` 的文件是'被拦下的新版本'，必须保留"。
    **实测推翻了它** —— 抽 ButtKnight 的 40 个文件逐字节比对现盘：

        与现盘完全相同（纯冗余）: 40 / 40
        与现盘不同（需保留）    :  0 / 40

    原因：`changed_files()` 是用**大小 + 哈希**判定"变了没"的，
    所以进了 `written` 的就是"内容真的不同"的那些；
    其余文件在 `out/` 里就是**原样副本**，与现盘逐字节相同 ⇒ 删了无损失。

    ⇒ 所以"写回**完全成功**"时，整目录清空是安全的。

    ## 为什么仍要分两种模式

    ``safe_to_clear=True``（**写回零失败、零中止**时传）
        整目录清空。理由见上。

    ``safe_to_clear=False``（有失败或有中止时传）
        退化成逐文件：**只删"在 `written` 里且备份里有原件"的**。
        这时 `out/` 里可能真有"没能写回的新版本"，
        那是唯一副本，必须留着（`should_stop` 中止、占位符不合格被拒
        都属于这一类）。

    ``backup_dir`` 全程**只读** —— 本函数绝不删备份，那是回滚的唯一依据。
    """
    if not out_dir.is_dir():
        return 0, 0, []

    if safe_to_clear:
        # 整目录清空（保留目录本身，调用方/用户仍能看出它的位置）
        removed = 0
        freed = 0
        for p in list(out_dir.rglob("*")):
            if not p.is_file():
                continue
            try:
                size = p.stat().st_size
                p.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
        return removed, freed, []

    removed = 0
    freed = 0
    skipped: list[str] = []
    for rel in written:
        p = out_dir / rel
        if not p.is_file():
            continue
        bak = backup_dir / rel
        if not bak.is_file():
            # 没有备份 ⇒ 保留（可能是唯一的改后版本）
            skipped.append(rel)
            continue
        try:
            size = p.stat().st_size
            p.unlink()
        except OSError:
            skipped.append(rel)
            continue
        removed += 1
        freed += size
    return removed, freed, skipped


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
    #: 非空 ⇒ 中途被 `should_stop` 叫停，这里放原因。
    #: 调用方**必须**把它报给用户：此时 `source_dir` 是"部分新部分旧"。
    aborted: str = ""


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


def preserve_pristine_backup(backup_root: Path, game_name: str) -> Path | None:
    """把"最初的原版"备份标记为 ``-orig``，保证它永远不被覆盖、可回滚。

    ## 为什么需要它（隐患，不是臆测）

    `_unique_backup_dir` 保证"每次写回都有自己的目录"，所以
    **第二次**写回会新建一个备份目录 —— 这本身没问题，但有个前提：
    **第二次真的跑起来了**。隐患在于：

    * ``auto --watch`` 正在对一个游戏做写回（备份目录已建、内容还在拷）；
    * 我（或用户）又跑了一次 ``auto``，它判定"这次还没有备份"，
      于是又建一个目录、又写一遍。

    结果：**没有一份备份是"最初的原版"** —— 每份备份的都是
    "上一次被改过的版本"。真出事想回滚到出厂状态时，**回不去**。

    ## 判据：**只在"第一次写回刚发生"这个窗口内标记**

    写回**之后**调用（此时新备份目录刚建好）。标记条件是：

    1. 该游戏**还没有**任何 ``-orig-`` 目录；**且**
    2. 该游戏的备份目录**恰好只有一个**（说明这是第一次写回）。

    条件 2 是关键：如果已经有 2 个未标记目录，说明至少写回过两次，
    那两份都**不是**原版（第二份备份的是第一遍改过的内容），
    此时**什么都不标记** —— 宁可没有 orig，也不要把"改过的版本"
    谎称成原版。

    ## 诚实说明

    这是**启发式**，不是密码学保证：它只能保证从加上本函数之后，
    第一次写回的原版一定被标记。在此之前遗留的备份目录如果只有一个，
    会被当成 orig —— 而它可能已经被改过。这一点不假装。

    返回被标记的目录（没标记任何东西就返回 ``None``）。
    """
    if not backup_root.is_dir():
        return None

    prefixed: list[tuple[Path, str]] = []
    has_orig = False
    for d in sorted(backup_root.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith(f"{game_name}-orig-"):
            has_orig = True
            continue
        # ★ 严格前缀：必须是 `<游戏名>-` 开头，且后缀形如 `YYYYMMDD-HHMMSS`
        #   （或带 `-2` 这类去重后缀）。
        #   用裸 `startswith(game_name)` 会把 `MyGame2-...` 当成 `MyGame`
        #   的备份 —— 实测这是真 bug，见
        #   `tests/test_batch_pristine_backup.py::test_does_not_touch_other_games`。
        rest = d.name[len(game_name) :] if d.name.startswith(game_name) else None
        if rest is None or not rest.startswith("-"):
            continue
        stamp = rest[1:]
        if not stamp or not all(c.isdigit() or c == "-" for c in stamp):
            continue
        prefixed.append((d, stamp))

    if has_orig or len(prefixed) != 1:
        return None

    src, stamp = prefixed[0]
    dest = backup_root / f"{game_name}-orig-{stamp}"
    if dest.exists():
        return None
    try:
        src.rename(dest)
    except OSError:
        return None
    return dest


def write_back(
    source_dir: Path,
    out_dir: Path,
    *,
    backup_root: Path,
    dry_run: bool = False,
    exclude_dirs: set[str] | None = None,
    should_stop: Callable[[], str] | None = None,
) -> WriteBackReport:
    """把 ``out_dir`` 里**变了的**文件覆盖回 ``source_dir``，先备份原件。

    ## ``should_stop``：**逐文件**的紧急刹车（实测发现的窗口）

    调用方在进入写回**之前**已经查过一次"游戏在不在跑"。但那样还不够：

    写回是**逐个文件** copy 的，一个上万个文件的游戏要好几秒。
    如果用户在那一刻**正好把游戏启动起来**，就会在写回进行到一半时
    开始读文件 —— 读到的新旧混合文件 ⇒ 崩溃，或者退出时把旧数据
    写回存档 ⇒ **存档损坏**（不可逆）。

    ``should_stop`` 每个文件之前调一次，返回**非空字符串**表示"立刻停"，
    那个字符串会记进 ``rep.aborted``。默认 ``None`` = 不检查
    （保持原有行为，避免影响其它调用方）。

    停下来时**已经写了的不回滚** —— 因为回滚本身要再写一遍文件，
    在"游戏正在读"的时刻做这个反而更危险。此时 ``source_dir`` 是
    "部分新部分旧"，但**备份是完整的**（备份在写之前做，逐文件进行），
    可以据此恢复。
    """
    rep = WriteBackReport()
    rels = changed_files(source_dir, out_dir, exclude_dirs=exclude_dirs)
    if not rels:
        return rep

    backup_dir = _unique_backup_dir(backup_root, source_dir.name)
    rep.backup_dir = str(backup_dir)

    for rel in rels:
        # ★ 每个文件之前重新确认一次（见 docstring 里的窗口说明）
        if should_stop is not None:
            why = should_stop()
            if why:
                rep.aborted = why
                break
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
