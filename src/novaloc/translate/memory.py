"""翻译记忆（TM）。

同一句原文在游戏里往往出现几十次（"Are you sure?" / "Yes" / "No"）。
记忆层先把这些命中掉，能省掉大部分推理开销，也让译文前后一致。

存的是 :class:`~novaloc.models.TranslationEntry`，键为源文归一化后的哈希。
模糊匹配用 ``difflib`` 的 ratio —— 对几千条规模足够快，
真要上十万条再换向量检索也来得及。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ..lang import normalize_for_compare
from ..models import EntryStatus, TranslationEntry

# 这些告警意味着译文不可信，不进记忆库
_BAD_WARNINGS = (
    "placeholder",
    "prompt_leak",
    "empty_translation",
    "looks_untranslated",
    # 混进别的文字系统（阿拉伯/泰/马拉雅拉姆…）—— 也是硬错误，
    # 不能当"已有译文"复用，否则错答案会被缓存并反复喂回来。
    "foreign_script",
)


@dataclass
class MemoryHit:
    entry: TranslationEntry
    score: float
    exact: bool


@dataclass
class TranslationMemory:
    entries: list[TranslationEntry] = field(default_factory=list)
    similarity: float = 0.97
    max_fuzzy_candidates: int = 400
    """大于该规模就跳过模糊匹配，避免 O(n) 扫描拖慢流水线。"""

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._exact: dict[str, TranslationEntry] = {}
        self._norm: dict[str, TranslationEntry] = {}
        self._reindex()

    # ------------------------------------------------------------------

    def _reindex(self) -> None:
        self._exact.clear()
        self._norm.clear()
        for e in self.entries:
            if not e.target or e.status in (EntryStatus.FAILED, EntryStatus.SKIPPED):
                continue
            if any(w.startswith(_BAD_WARNINGS) for w in e.warnings):
                continue
            self._exact.setdefault(e.source, e)
            self._norm.setdefault(normalize_for_compare(e.source), e)

    def add(self, entry: TranslationEntry) -> None:
        with self._lock:
            if not entry.target or entry.status is EntryStatus.FAILED:
                return
            if any(w.startswith(_BAD_WARNINGS) for w in entry.warnings):
                return
            self.entries.append(entry)
            self._exact.setdefault(entry.source, entry)
            self._norm.setdefault(normalize_for_compare(entry.source), entry)

    def extend(self, entries: list[TranslationEntry]) -> None:
        for e in entries:
            self.add(e)

    def __len__(self) -> int:
        return len(self.entries)

    # ------------------------------------------------------------------

    def lookup(self, source: str, *, fuzzy: bool = True) -> MemoryHit | None:
        with self._lock:
            hit = self._exact.get(source)
            if hit is not None:
                return MemoryHit(entry=hit, score=1.0, exact=True)

            key = normalize_for_compare(source)
            hit = self._norm.get(key)
            if hit is not None:
                return MemoryHit(entry=hit, score=1.0, exact=True)

            if not fuzzy or self.similarity >= 1.0:
                return None
            if len(self._norm) > self.max_fuzzy_candidates:
                return None

            best: tuple[float, TranslationEntry] | None = None
            for norm, cand in self._norm.items():
                # 长度差太大直接跳过，省掉昂贵的比对
                if abs(len(norm) - len(key)) > max(4, len(key) * 0.4):
                    continue
                score = SequenceMatcher(None, key, norm).ratio()
                if score >= self.similarity and (best is None or score > best[0]):
                    best = (score, cand)
            if best is None:
                return None
            return MemoryHit(entry=best[1], score=best[0], exact=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self.entries),
                "unique_source": len(self._exact),
                "unique_normalized": len(self._norm),
            }

    def to_entries(self) -> list[TranslationEntry]:
        with self._lock:
            return list(self.entries)
