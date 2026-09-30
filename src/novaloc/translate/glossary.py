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
