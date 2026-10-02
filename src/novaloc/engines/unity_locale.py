r"""利用游戏**自带的本地化表** —— 把译文填进已有的中文列。

## 为什么这是最划算的一条路

实测发现 Unity 游戏里有一类文案是**明文 CSV 本地化表**（Naninovel 等
对话框架的产物），而且**中文列本来就存在**，只是空的：

    key,comment,speaker,source,en,es,es-419,ja,pt-BR,ru,uk,zh-CN,zh-TW
    ~b03152b8,,MalePinkDemon,Hello.,...

对比"原地替换二进制字符串"（见 :mod:`unity_patch`），这条路的好处是：

* **不需要猜二进制布局** —— 就是改一个 CSV 单元格；
* **不受长度限制** —— 单元格长度本来就可以变（CSV 是变长的）；
* **用的是游戏自己的机制** —— 游戏会照着 `zh-CN` 列显示，不用改代码、
  不用换字体就能得到"原生中文"的显示效果。

所以它是"文本在 `.assets` 里"的 Unity 游戏**首选**的汉化方式。
:mod:`unity_patch` 是它不适用时的兜底。

## 安全边界

本地化表在 `.assets` 里是**一段变长字符串**（整张 CSV 就是一条记录），
所以改它**会改变这条记录的长度** → 会让后面的偏移失效。

因此本模块**只产出改好的 CSV 文件**（放到工作区），**不直接写回 `.assets`**。
写回由用户用 UABEA / AssetStudio 完成，或者等我们实现了变长改写再做。

这是**有意的取舍**：宁可让用户多一步，也不产出"游戏打不开"的结果。
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: 认作"中文列"的表头名（大小写不敏感）。
ZH_COLUMN_NAMES = (
    "zh-cn",
    "zh_cn",
    "zh-hans",
    "zhans",
    "chinese",
    "chinese (s)",
    "chinesesimplified",
    "zh",
    "cn",
    "schinese",
    "zh-hans-cn",
)

#: 源文列的候选名（按优先级）。
SOURCE_COLUMN_NAMES = ("source", "en", "english", "original", "text", "ja", "jp")

#: 表头里出现这些字样，才认为这是一张本地化表（避免把普通数据 CSV 当本地化表）
_LOCALE_HINT = re.compile(
    r"\bzh[-_]?(?:cn|hans|tw|hant)\b|\bja\b|\bpt-BR\b|\bes-419\b|"
    r"\blocale\b|\blanguage\b|\bspeaker\b|\bkey\b",
    re.IGNORECASE,
)


@dataclass
class LocaleRow:
    """本地化表里的一行（= 一条待翻文案）。"""

    row_index: int
    key: str
    source: str
    #: 该行在表里的原始字段（回写时按列名定位）
    cells: dict[str, str] = field(default_factory=dict)


@dataclass
class LocaleTable:
    """一张解析出来的本地化表。"""

    file: str
    offset: int
    header: list[str]
    rows: list[LocaleRow]
    zh_column: str = ""
    source_column: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.rows) and bool(self.zh_column)

    @property
    def pending(self) -> list[LocaleRow]:
        """中文列还是空的行 —— 这些才是真正需要翻的。"""
        if not self.zh_column:
            return []
        return [r for r in self.rows if not r.cells.get(self.zh_column, "").strip()]


def _find_header_line(text: str, *, limit: int = 60) -> int:
    """找到**真正的表头行**，返回它的行号（0 起）；找不到返回 -1。

    ## 为什么不能直接用第一行

    实测 Naninovel 的本地化文档前面有**注释行**：

        ; INTERNAL USE ONLY <source> to Japanese <ja> localization document ...
        key,comment,speaker,source,en,es,es-419,ja,pt-BR,ru,uk,zh-CN,zh-TW
        ~b03152b8,,MalePinkDemon,Hello.,...

    第一版直接取第一行当表头，于是把 `; INTERNAL USE ONLY` 当成了列名，
    解析出来的"表"只有一行、列全错位，还把注释里的词当成了要翻的文案
    （`zh-CN` 列其实在第二行才出现）。

    判据：**不含注释前缀、含逗号、且能匹配到语言列标记**的那一行。
    """
    for i, line in enumerate(text.split("\n")[:limit]):
        s = line.strip()
        if not s or s.startswith((";", "#", "//")):
            continue
        if s.count(",") < 1:
            continue
        if _LOCALE_HINT.search(s):
            return i
    return -1


def looks_like_locale_csv(text: str) -> bool:
    """这段文本像不像**本地化表**？

    只看表头区域，并且要求：有真正的表头行、有语言列标记、至少 2 列。
    这样普通的游戏数据 CSV（比如关卡配置）不会被误认。
    """
    return _find_header_line(text) >= 0


def _pick_column(header: list[str], names: tuple[str, ...]) -> str:
    """按名字（不区分大小写、忽略空格）挑一列。"""
    norm = {h.strip().lower(): h for h in header}
    for n in names:
        if n in norm:
            return norm[n]
    return ""


def parse_locale_table(text: str, *, file: str = "", offset: int = 0) -> LocaleTable:
    """把一段 CSV 文本解析成本地化表。

    ## 为什么用 :mod:`csv` 而不是 ``split(",")``

    本地化表的**源文列里全是逗号、引号、换行**（台词本来就长这样）。
    用 `split` 会把一行切成十几段，列全错位，然后把**台词的前半句**
    当成完整条目翻掉 —— 这种错很难发现，因为输出看起来"有中文"。
    `csv` 模块按 RFC4180 正确处理引号与转义。
    """
    tbl = LocaleTable(file=file, offset=offset, header=[], rows=[])

    # 跳过注释行，从**真正的表头**开始解析（否则注释会被当成列名）
    hi = _find_header_line(text)
    if hi < 0:
        tbl.error = "找不到表头行（前面可能全是注释）"
        return tbl
    body = "\n".join(text.split("\n")[hi:])

    try:
        reader = csv.reader(io.StringIO(body))
        header = next(reader, None)
    except (csv.Error, StopIteration) as exc:
        tbl.error = f"CSV 解析失败：{exc}"
        return tbl
    if not header:
        tbl.error = "空表"
        return tbl

    header = [h.strip() for h in header]
    tbl.header = header
    tbl.zh_column = _pick_column(header, ZH_COLUMN_NAMES)
    tbl.source_column = _pick_column(header, SOURCE_COLUMN_NAMES)
    if not tbl.zh_column:
        tbl.error = f"表头里没有中文列（表头：{header[:8]}）"
        return tbl
    if not tbl.source_column:
        tbl.error = f"表头里没有源文列（表头：{header[:8]}）"
        return tbl
    if tbl.source_column == tbl.zh_column:
        tbl.error = "源文列与中文列是同一列"
        return tbl

    src_idx = header.index(tbl.source_column)
    key_idx = header.index("key") if "key" in [h.lower() for h in header] else -1
    if key_idx < 0:
        for i, h in enumerate(header):
            if h.lower() in ("id", "name", "key"):
                key_idx = i
                break

    try:
        for i, row in enumerate(reader):
            if not row or all(not c.strip() for c in row):
                continue
            if src_idx >= len(row):
                continue
            source = row[src_idx]
            if not source.strip():
                continue
            cells = {h: (row[j] if j < len(row) else "") for j, h in enumerate(header)}
            key = row[key_idx] if 0 <= key_idx < len(row) else str(i)
            tbl.rows.append(LocaleRow(row_index=i, key=key, source=source, cells=cells))
    except csv.Error as exc:
        tbl.error = f"CSV 行解析失败（第 {len(tbl.rows)} 行后）：{exc}"
    return tbl


def render_locale_table(tbl: LocaleTable, translations: dict[str, str]) -> str:
    """把译文按 ``key -> 译文`` 填进中文列，渲染回 CSV 文本。

    ## 转义交给 csv 模块

    手写 ``join(",")`` 会在译文含逗号/引号/换行时**破坏整张表**。
    台词里这些字符很常见（`他说："住手！"`），所以必须由 `csv.writer`
    统一处理。
    """
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(tbl.header)
    for r in tbl.rows:
        values = [r.cells.get(h, "") for h in tbl.header]
        t = translations.get(r.key, "")
        if t:
            values[tbl.header.index(tbl.zh_column)] = t
        w.writerow(values)
    return out.getvalue()


def extract_from_slots(slots: list) -> list[LocaleTable]:
    """从"可改写字符串区间"里挑出本地化表并解析。

    ``slots`` 是 :class:`~novaloc.engines.unity_patch.StringSlot` 列表。
    整张 CSV 在 `.assets` 里就是**一条记录**，所以能直接解析。
    """
    out: list[LocaleTable] = []
    for s in slots:
        if len(s.text) < 64:
            continue
        if not looks_like_locale_csv(s.text):
            continue
        tbl = parse_locale_table(s.text, file=s.file, offset=s.offset)
        if tbl.ok and tbl.pending:
            out.append(tbl)
    return out
