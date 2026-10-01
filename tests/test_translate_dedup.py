"""同批内按原文去重：不重复问模型同一句话。

## 为什么值得做（真实数据）

真实游戏待办 22087 条里，**26.3% 是重复原文**。极端例子是角色的
**说话人名**：4334 条待翻、不同原文只有 **33** 种 —— 99.2% 是重复的
`'<right>  Ulula  </right>'`（同一个名字被复制到几百个事件里）。
不去重等于同一句话问模型几千次。

去重后同一批 40 条（3 种唯一）只花 3.0 秒，`deduped=37`。

## 最容易错的地方：去重不能丢回写目标

**不能按 `uid` 去重。** 引擎给每处出现都分配了不同的 `uid`
（`location.pointer` 不同：`/events/2/pages/0/list/23/parameters/0`），
它们是**不同的回写目标**。如果去重时把重复项合并掉、只返回唯一条目，
`apply` 就只会改到一处 —— 游戏里第一句台词是中文，后面几百处还是原文，
而且**不报任何错**。

所以契约是：**返回的条目数必须等于输入条目数，且每个 `uid` 恰好出现一次**，
重复项只是共用译文。本文件的测试主要就在钉这一点。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context, TranslateItem  # noqa: E402
from novaloc.models import TextKind, TextLocation, TextUnit  # noqa: E402
from novaloc.translate.ollama_provider import OllamaTranslationProvider  # noqa: E402


def _provider() -> OllamaTranslationProvider:
    return OllamaTranslationProvider(Context(config=Config(), events=EventBus()))


def _items(srcs: list[str]) -> list[TranslateItem]:
    return [
        TranslateItem(
            unit=TextUnit(
                uid=f"u{i}",
                source=s,
                kind=TextKind.CHARACTER_NAME,
                location=TextLocation(file="Map.json", pointer=f"/events/{i}/name"),
            ),
            glossary=[],
            context_lines=[],
        )
        for i, s in enumerate(srcs)
    ]


# ----------------------------------------------------------------------
# 一、纯逻辑：`_dedupe` 的映射
# ----------------------------------------------------------------------

def test_dedupe_keeps_first_occurrence_order() -> None:
    p = _provider()
    uniq, first_of, owners = p._dedupe(_items(["A", "B", "A", "C", "B", "A"]))
    assert [i.unit.source for i in uniq] == ["A", "B", "C"]
    assert first_of == {0: 0, 1: 1, 2: 3}
    assert owners == [0, 1, 0, 2, 1, 0]


def test_dedupe_all_unique_is_identity() -> None:
    p = _provider()
    srcs = ["a", "b", "c", "d"]
    uniq, first_of, owners = p._dedupe(_items(srcs))
    assert len(uniq) == 4
    assert owners == [0, 1, 2, 3]
    assert first_of == {0: 0, 1: 1, 2: 2, 3: 3}


def test_dedupe_all_same_collapses_to_one() -> None:
    p = _provider()
    uniq, first_of, owners = p._dedupe(_items(["x"] * 50))
    assert len(uniq) == 1
    assert owners == [0] * 50
    assert first_of == {0: 0}


def test_dedupe_empty() -> None:
    uniq, first_of, owners = _provider()._dedupe([])
    assert uniq == [] and first_of == {} and owners == []


def test_dedupe_preserves_each_items_own_uid() -> None:
    """**核心契约**：唯一条目保留的是**首次出现**那条自己的 uid。

    重复项的 uid 在最后摊回阶段各自赋值，所以这里只要确认
    唯一列表里没有张冠李戴。
    """
    p = _provider()
    items = _items(["A", "A", "A"])
    uniq, _first_of, _owners = p._dedupe(items)
    assert [i.unit.uid for i in uniq] == ["u0"]


# ----------------------------------------------------------------------
# 二、端到端（不调模型，替换掉内部调用）
# ----------------------------------------------------------------------

class _FakeProvider(OllamaTranslationProvider):
    """把 `_call_batch` / `_call_single` 换掉，只验证映射与摊回。

    ⚠️ 假译文**不能**是 `"[A]"` 这种方括号形式：守卫会把 `[...]`
    当成占位符，报 `placeholder_broken: 多出占位符`，条目变 FAILED、
    译文变空串 —— 那测的就不是去重而是守卫了。
    （第一次就踩了这个坑，测试失败信息看起来像"摊回丢了译文"。）
    所以这里用「译」+ 原文，既不是占位符也不像未翻译。
    """

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.batch_calls: list[list[str]] = []

    @staticmethod
    def _fake_target(src: str) -> str:
        return "译" + src

    def _call_batch(self, batch_items, masked):  # noqa: ANN001, ARG002
        self.batch_calls.append([it.unit.source for it in batch_items])
        return {i: self._fake_target(it.unit.source) for i, it in enumerate(batch_items)}

    def _call_single(self, item, masked, **kwargs):  # noqa: ANN001, ARG002
        return self._fake_target(item.unit.source)


def test_end_to_end_returns_one_entry_per_input() -> None:
    """**最重要的断言**：40 条输入 → 40 条输出，uid 各不相同。

    少一条就意味着游戏里某处台词永远不会被翻译，而且不报错。
    """
    ctx = Context(config=Config(), events=EventBus())
    p = _FakeProvider(ctx)
    srcs = ["<right>  Ulula  </right>"] * 18 + ["Naho"] * 12 + ["GAME OVER"] * 10
    out = p.translate_batch(_items(srcs), "zh-CN")
    assert len(out) == 40, f"返回 {len(out)} 条，输入 40 条"
    assert len({e.uid for e in out}) == 40, "uid 有重复或被合并"
    assert [e.uid for e in out] == [f"u{i}" for i in range(40)], "uid 顺序被打乱"


def test_end_to_end_only_translates_unique_sources() -> None:
    """只有 3 种唯一原文，就不该出现第 4 种。"""
    ctx = Context(config=Config(), events=EventBus())
    p = _FakeProvider(ctx)
    srcs = ["A"] * 18 + ["B"] * 12 + ["C"] * 10
    p.translate_batch(_items(srcs), "zh-CN")
    sent = [s for batch in p.batch_calls for s in batch]
    assert sorted(set(sent)) == ["A", "B", "C"]
    assert len(sent) == 3, f"问了 {len(sent)} 条，应该只问 3 条"


def test_duplicates_share_the_translation() -> None:
    ctx = Context(config=Config(), events=EventBus())
    p = _FakeProvider(ctx)
    srcs = ["A"] * 5 + ["B"] * 3
    out = p.translate_batch(_items(srcs), "zh-CN")
    assert [e.target for e in out[:5]] == ["译A"] * 5
    assert [e.target for e in out[5:]] == ["译B"] * 3


def test_each_duplicate_keeps_its_own_uid_and_source() -> None:
    """摊回时 `uid`/`source` 必须是**这一条自己的**，不能照抄首次出现那条。"""
    ctx = Context(config=Config(), events=EventBus())
    p = _FakeProvider(ctx)
    out = p.translate_batch(_items(["same", "same"]), "zh-CN")
    assert out[0].uid == "u0" and out[1].uid == "u1"
    assert out[0].source == out[1].source == "same"


def test_deduped_entry_records_provenance() -> None:
    """重复项要留下"从哪条抄来的"，方便审校时追查。"""
    ctx = Context(config=Config(), events=EventBus())
    p = _FakeProvider(ctx)
    out = p.translate_batch(_items(["same", "same"]), "zh-CN")
    assert "deduped_from_uid" not in out[0].meta, "首次出现不该带这个标记"
    assert out[1].meta.get("deduped_from_uid") == "u0"


def test_stats_counts_deduped() -> None:
    ctx = Context(config=Config(), events=EventBus())
    p = _FakeProvider(ctx)
    p.translate_batch(_items(["A"] * 10 + ["B"]), "zh-CN")
    assert p.stats.get("deduped") == 9, f"应为 9，实际 {p.stats.get('deduped')}"


def test_no_dedup_when_nothing_repeats() -> None:
    ctx = Context(config=Config(), events=EventBus())
    p = _FakeProvider(ctx)
    out = p.translate_batch(_items(["a", "b", "c"]), "zh-CN")
    assert len(out) == 3
    assert not p.stats.get("deduped"), "没有重复时不该记 deduped"


def test_empty_input_is_handled() -> None:
    ctx = Context(config=Config(), events=EventBus())
    p = _FakeProvider(ctx)
    assert p.translate_batch([], "zh-CN") == []
