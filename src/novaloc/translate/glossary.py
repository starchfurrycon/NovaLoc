"""术语表：匹配、抽取、检索。

三级策略，从便宜到贵：

1. **字面匹配**（正则，支持全词/大小写）—— 覆盖绝大多数专有名词；
2. **提示注入** —— 把命中的条目塞进提示词，让模型自行遵守；
3. **向量检索**（可选）—— 用 Ollama 的 embed 接口在长文本里找相关术语，
   适合条目上千的大项目。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import GlossaryEntry


def _pattern_for(entry: GlossaryEntry) -> re.Pattern[str]:
    src = re.escape(entry.source)
    if entry.whole_word:
        # 对含空格/标点的串用 \b 不靠谱，改用"非字母数字"边界
        src = rf"(?<![0-9A-Za-z_\u4e00-\u9fff]){src}(?![0-9A-Za-z_\u4e00-\u9fff])"
    flags = 0 if entry.case_sensitive else re.IGNORECASE
    return re.compile(src, flags)


class Glossary:
    def __init__(self, entries: list[GlossaryEntry] | None = None) -> None:
        self.entries: list[GlossaryEntry] = []
        self._compiled: list[tuple[GlossaryEntry, re.Pattern[str]]] = []
        if entries:
            self.set_entries(entries)

    def set_entries(self, entries: list[GlossaryEntry]) -> None:
        # 长的优先，避免 "Sword" 抢在 "Sword of Light" 前面
        self.entries = sorted(entries, key=lambda e: -len(e.source))
        self._compiled = [(e, _pattern_for(e)) for e in self.entries if e.source]

    def __len__(self) -> int:
        return len(self.entries)

    def match(self, text: str, limit: int = 24) -> list[GlossaryEntry]:
        """返回在 ``text`` 中命中的术语条目。"""
        hits: list[GlossaryEntry] = []
        for entry, pat in self._compiled:
            if pat.search(text):
                hits.append(entry)
                if len(hits) >= limit:
                    break
        return hits

    def apply_literal(self, text: str) -> str:
        """把命中术语的译文直接替换进文本（用于小语种/低资源模型的兜底）。

        注意：只替换源串本身，不尝试处理词形变化 —— 因此默认不启用。
        """
        out = text
        for entry, pat in self._compiled:
            out = pat.sub(entry.target, out)
        return out

    def conflicting_duplicates(self) -> list[tuple[str, list[str]]]:
        """找出同一源词对应多个译法的条目，供 UI 提示用户。"""
        buckets: dict[str, set[str]] = {}
        for e in self.entries:
            buckets.setdefault(e.source.lower(), set()).add(e.target)
        return [(k, sorted(v)) for k, v in buckets.items() if len(v) > 1]

    def to_prompt_entries(self, text: str, limit: int = 60) -> list[GlossaryEntry]:
        return self.match(text, limit=limit)


@dataclass
class GlossarySuggestion:
    source: str
    count: int
    sample: str


_NAMEY_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,3})\b")


# ----------------------------------------------------------------------
# 内置游戏术语表
# ----------------------------------------------------------------------
#
# ## 为什么需要它（实测出来的）
#
# 单条 UI 缩写**几乎不携带上下文**，模型只能猜。本机实测
# `translategemma:4b` 对 27 条真实游戏字符串的批量翻译结果：
#
#     原文    译文
#     HP  →  生命值      ✅
#     MP  →  生命值      ❌ 应该是"魔法值"
#     EXP →  经验值      ✅
#
# 两种译法都"像那么回事"，质检的规则也挑不出错（它不是漏译、不是占位符
# 丢失、源文也不相同），但玩家一眼就能看出是错的 —— 属于最容易被骂的
# 一类问题。而且这类缩写**只用几十个词**，覆盖了绝大多数游戏 UI 的
# 高频短串，用固定术语表解决既便宜又确定。
#
# ## 这些条目怎么选
#
# 只收**有公认标准译法、且误译概率高**的高频 UI 词。刻意不收：
#
# * 有歧义的词（`Charge`、`Turn`、`Check`、`Critical`）——
#   放进术语表反而会**锁定一个错的译法**，比让模型按语境判断更糟；
# * 褒贬不一的译法（`Menu` 菜单/选单）—— 只收争议小的。
#
# ## 优先级
#
# 用户的术语表**永远覆盖**内置条目（同源词时用户赢），见
# :func:`merge_builtin`。内置表只是"开箱可用"的起点，不是限制。

_BUILTIN_TERMS: tuple[tuple[str, str, str], ...] = (
    # --- 战斗资源缩写：这一组是实测踩坑的地方 ---
    ("HP", "生命值", "战斗资源"),
    ("MP", "魔法值", "战斗资源"),
    ("SP", "技能值", "战斗资源"),
    ("TP", "技巧值", "战斗资源"),
    ("EXP", "经验值", "战斗资源"),
    ("XP", "经验值", "战斗资源"),
    ("Gold", "金币", "资源"),
    ("Gil", "金币", "资源"),
    # --- 战斗指令 ---
    ("Attack", "攻击", "战斗指令"),
    ("Defend", "防御", "战斗指令"),
    ("Guard", "防御", "战斗指令"),
    ("Flee", "逃跑", "战斗指令"),
    ("Escape", "逃跑", "战斗指令"),
    ("Skill", "技能", "战斗指令"),
    ("Skills", "技能", "战斗指令"),
    ("Ability", "能力", "战斗指令"),
    ("Abilities", "能力", "战斗指令"),
    ("Inventory", "物品栏", "菜单"),
    ("Equip", "装备", "菜单"),
    ("Equipment", "装备", "菜单"),
    ("Status", "状态", "菜单"),
    # --- 系统菜单 ---
    ("Save", "存档", "系统"),
    ("Load", "读档", "系统"),
    ("Autosave", "自动存档", "系统"),
    ("Auto-save", "自动存档", "系统"),
    ("Quicksave", "快速存档", "系统"),
    ("Quickload", "快速读档", "系统"),
    ("Options", "设置", "系统"),
    ("Settings", "设置", "系统"),
    ("Configure", "配置", "系统"),
    ("Quit", "退出", "系统"),
    ("Exit", "退出", "系统"),
    ("Continue", "继续", "系统"),
    ("Resume", "继续", "系统"),
    ("Retry", "重试", "系统"),
    ("Restart", "重新开始", "系统"),
    ("Confirm", "确认", "系统"),
    ("Cancel", "取消", "系统"),
    ("Return", "返回", "系统"),
    ("Back", "返回", "系统"),
    ("Next", "下一步", "系统"),
    ("Apply", "应用", "系统"),
    ("Default", "默认", "系统"),
    ("Reset", "重置", "系统"),
    # --- 属性 ---
    ("Strength", "力量", "属性"),
    ("Defense", "防御力", "属性"),
    ("Agility", "敏捷", "属性"),
    ("Intelligence", "智力", "属性"),
    ("Luck", "幸运", "属性"),
    ("Vitality", "体力", "属性"),
    ("Endurance", "耐力", "属性"),
    # --- 常见机制 ---
    ("Damage", "伤害", "机制"),
    ("Health", "生命值", "机制"),
    ("Mana", "法力", "机制"),
    ("Stamina", "体力", "机制"),
    ("Level Up", "升级", "机制"),
    ("Level", "等级", "机制"),
    ("Save Point", "存档点", "机制"),
    ("Checkpoint", "检查点", "机制"),
    ("Difficulty", "难度", "机制"),
    ("Tutorial", "教程", "机制"),
    ("Achievement", "成就", "机制"),
    ("Achievements", "成就", "机制"),
    ("Trophy", "奖杯", "机制"),
)


def builtin_entries() -> list[GlossaryEntry]:
    """内置游戏术语表的条目列表。

    全部 `whole_word=True`：否则 `MP` 会在 `MP3` 里命中、
    `SP` 会在 `SPECIAL` 里命中。边界用"非字母数字"而不是 `\\b`，
    所以 `HP:`、`(MP)`、`HP值` 这类写法也能正确命中。
    """
    return [
        GlossaryEntry(
            source=src, target=tgt, whole_word=True,
            case_sensitive=False, category=cat,
            note="内置术语（可在设置里覆盖或关闭）",
        )
        for src, tgt, cat in _BUILTIN_TERMS
    ]


def merge_builtin(
    user_entries: list[GlossaryEntry], *, enabled: bool = True
) -> list[GlossaryEntry]:
    """把内置术语并入用户术语表。

    **用户的条目永远赢**：同一个源词时保留用户的译法。这条很重要 ——
    术语表是用户的领域知识（他的游戏里 `MP` 可能真的叫"气"），
    内置表不该覆盖它，否则"开箱可用"就变成了"不可改"。
    """
    if not enabled:
        return list(user_entries)

    user_srcs = {e.source.strip().lower() for e in user_entries}
    merged = list(user_entries)
    merged.extend(e for e in builtin_entries() if e.source.lower() not in user_srcs)
    return merged


# 这些词太常见，作为术语候选没意义
_STOPWORDS = {
    "the", "and", "you", "your", "this", "that", "with", "from", "have", "will",
    "what", "when", "where", "which", "there", "their", "them", "then", "than",
    "into", "onto", "over", "under", "about", "after", "before", "again",
    "yes", "no", "not", "but", "for", "are", "was", "were", "his", "her",
    "she", "him", "they", "our", "out", "one", "two", "all", "any",
    "can", "could", "would", "should", "must", "may", "might", "just", "only",
    "very", "more", "most", "some", "such", "each", "every", "both", "few",
    "press", "click", "select", "cancel", "back", "next", "start", "load",
    "save", "exit", "quit", "options", "settings", "help", "continue", "new",
    "game", "play", "stop", "pause", "resume", "retry", "restart",
}


def suggest_terms(texts: list[str], *, min_count: int = 3, limit: int = 200) -> list[GlossarySuggestion]:
    """从语料里粗提可能的专有名词，用来给用户一个术语表草稿。

    这是启发式规则（连续首字母大写的词），不追求完美 ——
    目的是把人工审校的起点从"从零开始"变成"删掉几个错的"。
    """
    counts: dict[str, int] = {}
    samples: dict[str, str] = {}
    for t in texts:
        for m in _NAMEY_RE.finditer(t):
            phrase = m.group(1).strip()
            low = phrase.lower()
            if low in _STOPWORDS:
                continue
            if len(phrase) < 3:
                continue
            counts[phrase] = counts.get(phrase, 0) + 1
            samples.setdefault(phrase, t[:120])

    out = [
        GlossarySuggestion(source=k, count=v, sample=samples.get(k, ""))
        for k, v in counts.items()
        if v >= min_count
    ]
    out.sort(key=lambda s: -s.count)
    return out[:limit]


# ----------------------------------------------------------------------
# 引擎内建标签：**确定性覆盖**，不交给模型猜
# ----------------------------------------------------------------------
#
# ## 为什么单独做一张表
#
# `_BUILTIN_TERMS` 是**提示词注入**用的（把命中条目塞进提示词，让模型
# 自觉遵守）。实测那条路对本项目的小模型**整体是负收益**，所以默认不启用
# （结论见 `stage_translate` 的注释）。
#
# 但有一类词不能靠"让模型遵守"：**引擎内建的 UI 标签**。
# 真实事故：RPG Maker MV 的 `System.json` → `terms.basic` 是
#
#     ["Level","Lv","HP","HP","MP","MP","TP","TP"]
#
# 模型把 `HP`/`MP`/`TP` **全译成了"生命值"**。玩家看到三个同名字段。
# 同类事故：`僧侶` 和 `魔術師` 都被译成"法师"，两个职业变成同一个。
#
# 这些是**单条、无上下文**的缩写（在 JSON 里就是独立的 `"HP"` 字符串），
# 模型只能猜，而每种猜法都"像那么回事" —— 漏译、占位符、源文差异
# 三类检查全抓不到，写回游戏就是既成事实。
#
# ## 为什么是"覆盖"而不是"更强的注入"
#
# 提示词注入对 4B 模型**不保证**遵守（实测有漏译和串词）。而这类标签是
# **有限且固定**的引擎标识符，不是用户的领域知识 —— 一共就 8 个
# `terms.basic` 位置加几十个常见职业名，用表确定性地译，比"求模型听话"
# 可靠得多。
#
# ## 安全边界（很重要）
#
# 只在 `source` 归一化后**精确等于**标签时覆盖，**绝不做子串替换**。
# `System.json` 里的这些值本身就是整条文本，所以精确匹配足够；
# 而子串替换会把 `Restores 50 HP.` 弄成"恢复50 生命值。"这种中英夹杂。
#
# 用户术语表依然优先（见 `merge_engine_labels`）。


@dataclass(frozen=True)
class EngineLabel:
    """一个引擎内建标签及其确定性译法。"""

    key: str
    """归一化后的匹配键（小写、去空白与首尾标点、全角转半角）。"""

    target: str
    """确定性译法。"""

    note: str
    """为什么这个译法是准的（写给日后维护的人看）。"""


#: 归一化时要剥掉的边缘字符（标点、空白、全角空格）
_LABEL_TRIM = " \t\r\n\u3000.:：。、,，;；!！?？'\"“”‘’()（）[]【】"


def _normalize_label(text: str) -> str:
    """把标签归一化成一个匹配键。

    处理真实数据里的三种不统一：

    * 大小写（`Hp` / `hp`）
    * 全角写法（`ＨＰ` —— 日文数据里常见）
    * 首尾标点与空白（`"HP"`、`HP.`、`HP:`、`(MP)`）
    """
    t = text.strip()
    # 全角 ASCII（Ｕ＋ＦＦ０１..ＦＦ５Ｅ）转半角
    t = "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in t
    )
    t = t.strip(_LABEL_TRIM)
    return t.strip().lower()


#: 引擎标签表。
#:
#: `note` 里写清依据 —— 这些译法是要**直接写进游戏**的，
#: 日后有人想改必须知道当初为什么这么定。
_ENGINE_LABELS: tuple[EngineLabel, ...] = (
    # --- RPG Maker MV/MZ `System.json` → `terms.basic` ---
    #
    # 标准译法取自 RPG Maker 中文版/社区通用译法。关键是三者**互不相同**：
    # 用"生命值/魔法值/技巧值"而不是"生命值/魔法值/特殊值"，
    # 因为 TP 在 MV/MZ 里是"积攒后放技能"的资源，中文版叫"技巧值"。
    EngineLabel("hp", "生命值", "RPG Maker 标准译法；与 MP/TP 区分"),
    EngineLabel("mp", "魔法值", "RPG Maker 标准译法；与 HP/TP 区分"),
    EngineLabel("tp", "技巧值", "RPG Maker MV/MZ 标准译法；与 HP/MP 区分"),
    EngineLabel("exp", "经验值", "RPG Maker 标准译法"),
    EngineLabel("xp", "经验值", "EXP 的另一种写法，同一事物"),
    EngineLabel("sp", "技能值", "常见的技能点资源"),
    # --- terms.basic 的其余两项（Level / Lv）---
    #
    # `Lv` 是 `Level` 的缩写写法，两者在 `terms.basic` 里各占两个位置
    # （索引 0/1）。中文习惯：全称用"等级"，缩写用"等级"或"Lv"。
    # 这里都译"等级" —— 因为中文里"等级"本身就短，不需要再缩。
    EngineLabel("level", "等级", "RPG Maker 标准译法"),
    EngineLabel("lv", "等级", "Level 的缩写；中文里直接写「等级」即可"),
    # --- 货币单位 ---
    # `g` 和 `gold` 是同一个东西（RPG Maker `currencyUnit` 默认 "G"），
    # 译法相同是**正确**的，不算碰撞。
    EngineLabel("g", "金币", "RPG Maker `currencyUnit` 默认值就是 G"),
    EngineLabel("gold", "金币", "通用译法"),
    # --- 常见职业名（日文原文，真实数据里出现过） ---
    #
    # 事故：`僧侶` 和 `魔術師` 都被译成"法师"。这两个是**不同职业**
    # （那个游戏里职业决定技能池），撞成同词玩家分不清。
    EngineLabel("僧侶", "僧侣", "与「魔術師」区分；日式奇幻标准职业"),
    EngineLabel("魔術師", "魔术师", "与「僧侶」区分；字面直译"),
    EngineLabel("戦士", "战士", "日式奇幻标准职业"),
    EngineLabel("勇者", "勇者", "日式奇幻标准职业；本身已是汉字"),
    EngineLabel("魔法使い", "魔法师", "与「僧侶」区分"),
    EngineLabel("盗賊", "盗贼", "日式奇幻标准职业"),
    EngineLabel("狩人", "猎人", "日式奇幻标准职业"),
    EngineLabel("騎士", "骑士", "日式奇幻标准职业"),
    EngineLabel("格闘家", "格斗家", "日式奇幻标准职业"),
)


def engine_labels() -> list[EngineLabel]:
    """返回引擎标签表。"""
    return list(_ENGINE_LABELS)


_ENGINE_LABEL_INDEX: dict[str, str] = {lbl.key: lbl.target for lbl in _ENGINE_LABELS}


def engine_label_target(source: str | None) -> str | None:
    """如果 ``source`` **整条就是**一个引擎标签，返回它的确定性译法。

    否则返回 ``None``（表示"交给模型翻译"）。

    ⚠️ **只做精确匹配，绝不做子串替换。** 这是本函数最重要的性质：

    >>> engine_label_target("MP")
    '魔法值'
    >>> engine_label_target("Restores 50 HP.")
    >>> # None —— 句子交给模型，覆盖会把中文句子搞成中英夹杂

    `None` 表示"不接管"，不是"出错"。
    """
    if not source:
        return None
    return _ENGINE_LABEL_INDEX.get(_normalize_label(source))


def merge_engine_labels(user_entries: list[GlossaryEntry]) -> None:
    """**就地**把用户的术语表接进引擎标签索引（用户赢）。

    术语表是用户的领域知识 —— 他的游戏里 `MP` 可能真的叫"气"。
    内置标签只提供默认值，不能锁死。

    之所以是"就地改索引"而不是"返回一张新表"：引擎标签是**覆盖**语义
    （要拿去盖掉模型输出），而不是"注入提示词"，
    所以用户条目需要进的是同一张查找表。
    """
    for e in user_entries:
        key = _normalize_label(e.source)
        if key and e.target.strip():
            _ENGINE_LABEL_INDEX[key] = e.target.strip()


__all__ = [
    "EngineLabel",
    "Glossary",
    "GlossarySuggestion",
    "builtin_entries",
    "engine_label_target",
    "engine_labels",
    "merge_builtin",
    "merge_engine_labels",
    "suggest_terms",
]
