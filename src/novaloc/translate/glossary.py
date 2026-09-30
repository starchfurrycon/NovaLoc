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
