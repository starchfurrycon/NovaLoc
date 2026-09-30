"""贴图译文的**来源优先级**，以及"改完代码重跑结果不变"这个坑。

## 事故经过（值得记下来）

发现贴图上的 `'LUK'` 被模型音译成 `'卢克'`（`LUK = Luck = 幸运`），
于是把属性缩写加进确定性引擎术语表，确认
`engine_label_target('LUK') == '幸运'`，重跑 `images_localize` ——
**图上还是 `'卢克'`。**

原因是 `stage_images_localize` 会把**上一轮跑出来的译文**读回来当缓存：

    existing = self._existing_image_translations()   # 从 localize.json 读 blocks[].target

而 `_translate_texts` 当时是"先查缓存、再查引擎表"，于是上一轮模型的
错答案抢先命中，新加的确定性表**根本没机会生效**。

同一张网还漏掉第二类坏值：`'[o]' → '[o]中文译文'`。
`_translation_is_usable` 原先**只在模型结果上调用**，
缓存命中直接 `out[i] = hit`，所以存在 `localize.json` 里的坏译文
永远挡不掉。

## 修法

1. 优先级改成 **引擎术语 > 缓存 > 模型**；
2. 三个来源**全部**送进统一校验 `_accept()`。

教训一句话：**缓存只该缓存"猜"，不该缓存"事实"；
而校验必须对所有来源一视同仁。**
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images.service import TextureTranslator  # noqa: E402


class _RecordingProvider(TextureTranslator):
    """记录"到底问了模型哪些串"，用来证明确定性表真的拦住了调用。"""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.asked: list[str] = []

    def _make_fn(self):
        def _fn(items, target_lang):  # noqa: ANN001, ARG001
            self.asked.extend(it.unit.source for it in items)
            # 返回一个"会把 LUK 译错"的假模型输出，模拟真实情况
            class _E:
                def __init__(self, t: str) -> None:
                    self.target = t

            return [_E(f"模型译:{it.unit.source}") for it in items]

        return _fn


def _call(
    tr: TextureTranslator,
    texts: list[str],
    existing: dict[str, str] | None = None,
) -> dict[int, str]:
    tr._translate_fn = getattr(tr, "_make_fn", lambda: None)()
    return tr._translate_texts(
        texts,
        "zh-CN",
        existing or {},
        [],
        [],
        Path("fake.png_"),
        False,
    )


# ----------------------------------------------------------------------
# 一、引擎术语必须**优先于**缓存
# ----------------------------------------------------------------------

def test_engine_label_beats_stale_cache() -> None:
    """**核心回归**：缓存里存着 `'卢克'`，重跑也必须变成 `'幸运'`。

    这条就是"改了代码、重跑了、结果一模一样"的那个 bug。
    """
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    out = _call(tr, ["LUK"], existing={"luk": "卢克"})
    assert out[0] == "幸运", f"缓存里的错答案赢了确定性表：{out[0]!r}"


def test_engine_label_beats_stale_cache_for_all_stats() -> None:
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    stale = {k.lower(): "旧错答案" for k in ["MHP", "MMP", "ATK", "DEF", "MAT", "MDF", "AGI", "LUK"]}
    texts = ["MHP", "MMP", "ATK", "DEF", "MAT", "MDF", "AGI", "LUK"]
    out = _call(tr, texts, existing=stale)
    for i, src in enumerate(texts):
        assert out[i] != "旧错答案", f"{src} 还是用了缓存的错答案"
        assert out[i] != "模型译:" + src, f"{src} 不该问模型"


def test_engine_labels_never_reach_the_model() -> None:
    """走确定性表的块**不该**产生模型调用 —— 省时间也避免翻错。"""
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    _call(tr, ["ATK", "DEF", "LUK", "AGI"])
    assert tr.asked == [], f"这些块不该问模型：{tr.asked}"
    assert tr.engine_label_hits == 4


def test_non_label_text_still_goes_to_model() -> None:
    """非引擎术语（真正的句子）该问模型还得问。"""
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    _call(tr, ["GAME OVER", "Welcome to the island"])
    assert tr.asked == ["GAME OVER", "Welcome to the island"]
    assert tr.engine_label_hits == 0


# ----------------------------------------------------------------------
# 二、缓存里的坏值也必须被校验挡住
# ----------------------------------------------------------------------

def test_stale_cache_bad_value_is_rejected() -> None:
    """**第二个核心回归**：`localize.json` 里存着
    `'[o]' → '[o]中文译文'`，重跑时必须被丢掉并重新问模型。

    原先校验只在模型结果上跑，缓存命中直接返回，
    所以这个坏译文**永远挡不掉** —— 自动检查形同虚设。
    """
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    out = _call(tr, ["[o]"], existing={"[o]": "[o]中文译文"})
    assert out.get(0) != "[o]中文译文", "缓存里的坏译文没被挡住"
    assert "[o]" in tr.asked, "坏翻译被丢弃后应该重新问模型"


def test_stale_cache_meta_answer_is_rejected() -> None:
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    out = _call(tr, ["weird"], existing={"weird": "已识别文字，无法确定具体含义"})
    assert out.get(0) != "已识别文字，无法确定具体含义"


def test_stale_cache_good_value_is_reused() -> None:
    """正常缓存**要**复用 —— 否则每轮重跑都要把 750 张图重问一遍。"""
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    out = _call(tr, ["GAME OVER"], existing={"game over": "游戏结束"})
    assert out[0] == "游戏结束"
    assert tr.cache_hits == 1
    assert tr.asked == [], "命中缓存不该再问模型"


# ----------------------------------------------------------------------
# 三、优先级顺序本身
# ----------------------------------------------------------------------

def test_label_beats_cache_beats_model() -> None:
    """三个来源同时可用时的胜者，必须是**确定性事实**。"""
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    texts = ["ATK", "GAME OVER", "Unknown Sentence"]
    existing = {"atk": "旧错答案", "game over": "游戏结束"}
    out = _call(tr, texts, existing=existing)
    assert out[0] == engine_label("ATK"), "引擎术语应优先"
    assert out[1] == "游戏结束", "非术语应命中缓存"
    assert out[2].startswith("模型译:"), "都没命中才问模型"
    assert tr.asked == ["Unknown Sentence"]


def engine_label(k: str) -> str:
    from novaloc.translate.glossary import engine_label_target

    return engine_label_target(k) or ""


def test_dry_run_does_not_call_model() -> None:
    ctx = Context(config=Config(), events=EventBus())
    tr = _RecordingProvider(ctx)
    tr._translate_fn = tr._make_fn()
    out = tr._translate_texts(
        ["ATK", "GAME OVER"], "zh-CN", {}, [], [], Path("f.png_"), True
    )
    # 引擎术语在 dry_run 下依然生效（它是查表，不是"翻译"）
    assert out.get(0) == engine_label("ATK")
    assert tr.asked == []
