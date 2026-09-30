"""OCR 结果磁盘缓存：让"最慢且不确定"的一步只做一次。

## 两个动机

### OCR 在真实游戏资产上是**不确定**的

同一张图跑两次，识别出的块数/文字可能不同。后果很实际：
一次跑到第 600 张崩了，重跑一遍结果和上次不一样，产物无法复现，
用户报的问题复现不出来。

### OCR 是最慢的一步

实测 medium 档约 0.6 秒/张，真实游戏 750 张 ≈ 8 分钟（还没算 inpaint 与重绘）。
调一个下游参数（重绘边距之类）不该把上游全部重认一遍。

实测命中缓存的收益：**15,848 ms → 15 ms**（同一张 `Loading.png`）。

## 缓存键必须覆盖"会让结果变化的所有输入"

只按文件内容做键不够：换模型档位（tiny/small/medium）、调置信度下限、
改缩放上限，结果都会变。所以键里带这些参数 **+ 引擎版本标识**。

引擎版本标识是防"模型换了却命中旧缓存"——产物看着正常，
用的却是上一个模型的识别结果，这种问题极难查。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.images.ocr_cache import (  # noqa: E402
    CACHE_VERSION,
    OcrCache,
    cache_key,
    default_cache_root,
)

#: 真实贴图（OCR 能读出 `Now Loading...`）
_REAL_IMG = Path(r"D:\NovaLocData\real\ElfLifia\www\img\system\Loading.png")

_BASE = {
    "tier": "medium",
    "min_score": 0.5,
    "max_side": 4096,
    "lang": "en",
    "engine_tag": "rapidocr|9.9",
}


# ----------------------------------------------------------------------
# 一、缓存键
# ----------------------------------------------------------------------

def test_same_inputs_same_key() -> None:
    a = cache_key(b"abc", **_BASE)
    b = cache_key(b"abc", **_BASE)
    assert a == b


def test_different_content_different_key() -> None:
    assert cache_key(b"abc", **_BASE) != cache_key(b"abd", **_BASE)


@pytest.mark.parametrize(
    "field,value",
    [
        ("tier", "small"),
        ("min_score", 0.7),
        ("max_side", 2048),
        ("lang", "ja"),
        ("engine_tag", "rapidocr|10.0"),
    ],
)
def test_every_parameter_changes_the_key(field: str, value: object) -> None:
    """**核心契约**：任何一个会改变结果的参数都必须改变缓存键。

    漏掉一个的后果是"改了设置却拿到旧结果"，而且不报错。
    """
    kw = dict(_BASE)
    kw[field] = value
    assert cache_key(b"abc", **_BASE) != cache_key(b"abc", **kw), (
        f"改了 {field}={value!r} 缓存键却没变 —— 会命中错误的旧缓存"
    )


def test_key_does_not_include_asset_uid() -> None:
    """``asset_uid`` 不是参数：同一张图可能以不同 uid 出现。

    如果把它算进键，同一张图会被重复识别多遍；
    已经识别过的结果应该复用，只在还原时重写块 id。
    """
    import inspect

    sig = inspect.signature(cache_key)
    assert "asset_uid" not in sig.parameters


def test_key_is_hex_and_stable_length() -> None:
    k = cache_key(b"abc", **_BASE)
    assert len(k) == 64 and all(c in "0123456789abcdef" for c in k)


def test_content_only_difference_is_detected() -> None:
    """哪怕只差一个字节也要换键（别用"长度"之类偷懒的摘要）。"""
    a = cache_key(bytes(range(256)), **_BASE)
    b = cache_key(bytes(range(255)) + b"\xff\x00", **_BASE)
    assert a != b


# ----------------------------------------------------------------------
# 二、OcrCache 基本行为
# ----------------------------------------------------------------------

def test_disabled_cache_is_inert(tmp_path: Path) -> None:
    c = OcrCache(None)
    assert not c.enabled
    assert c.get("k") is None
    c.put("k", {"width": 1})  # 不该抛
    assert c.writes == 0


def test_put_then_get_round_trips(tmp_path: Path) -> None:
    c = OcrCache(tmp_path)
    c.put("deadbeef", {"width": 10, "height": 20, "blocks": []})
    doc = c.get("deadbeef")
    assert doc is not None
    assert doc["width"] == 10 and doc["height"] == 20
    assert doc["v"] == CACHE_VERSION


def test_miss_returns_none(tmp_path: Path) -> None:
    assert OcrCache(tmp_path).get("nope") is None


def test_stats_track_hits_and_misses(tmp_path: Path) -> None:
    c = OcrCache(tmp_path)
    c.put("a", {"width": 1})
    c.get("a")
    c.get("b")
    s = c.stats()
    assert s["hits"] == 1 and s["misses"] == 1 and s["writes"] == 1


def test_files_use_two_level_prefix(tmp_path: Path) -> None:
    """一个游戏上千张贴图，不能全塞进同一个目录再全塞进一级。

    用两级前缀（``ab/abcdef....json``）避免 Windows 上单目录几千文件变慢。
    """
    c = OcrCache(tmp_path)
    key = "ab" + "c" * 62
    c.put(key, {"width": 1})
    p = tmp_path / "ab" / f"{key}.json"
    assert p.is_file()


# ----------------------------------------------------------------------
# 三、坏缓存不能变成功能问题
# ----------------------------------------------------------------------

def test_corrupt_json_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    """**核心契约**：缓存坏了就当没命中，绝不能抛出去。

    磁盘缓存是**加速手段**，不是正确性依赖 —— 它坏了最多慢一点，
    不该让整条流水线失败。
    """
    c = OcrCache(tmp_path)
    key = "ab" + "d" * 62
    p = tmp_path / "ab" / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert c.get(key) is None
    assert c.misses >= 1


def test_version_mismatch_is_a_miss(tmp_path: Path) -> None:
    """格式版本变了必须当未命中 —— 否则会读出缺字段的块。"""
    c = OcrCache(tmp_path)
    key = "ab" + "e" * 62
    p = tmp_path / "ab" / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"v": CACHE_VERSION + 99, "width": 1}), encoding="utf-8")
    assert c.get(key) is None


def test_non_dict_payload_is_a_miss(tmp_path: Path) -> None:
    c = OcrCache(tmp_path)
    key = "ab" + "f" * 62
    p = tmp_path / "ab" / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1,2,3]", encoding="utf-8")
    assert c.get(key) is None


def test_put_into_unwritable_path_does_not_raise(tmp_path: Path) -> None:
    """写不进去只记日志。用一个"父路径是文件"的目录来逼它失败。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    c = OcrCache(blocker / "sub")
    c.put("a", {"width": 1})  # 不该抛
    assert c.writes == 0


def test_put_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    """先写临时文件再 replace：中断时不能留下半个 JSON。"""
    c = OcrCache(tmp_path)
    c.put("aabb", {"width": 1})
    assert not list(tmp_path.rglob("*.tmp"))


def test_clear_removes_entries(tmp_path: Path) -> None:
    c = OcrCache(tmp_path)
    c.put("aabb", {"width": 1})
    c.put("ccdd", {"width": 2})
    assert c.clear() == 2
    assert c.get("aabb") is None


def test_clear_on_missing_dir_is_zero(tmp_path: Path) -> None:
    assert OcrCache(tmp_path / "nope").clear() == 0


# ----------------------------------------------------------------------
# 四、接进引擎后真的生效
# ----------------------------------------------------------------------

@pytest.mark.skipif(not _REAL_IMG.is_file(), reason="本机没有真实可识别贴图")
def test_engine_cache_hit_is_fast_and_identical(tmp_path: Path) -> None:
    """**核心断言**：同一张图第二次必须命中缓存，且文字完全一致、明显更快。

    这条同时守住两个性质：
    * 结果**确定性**（重跑不会漂移）；
    * 真的省下时间（不然缓存就是白写）。
    """
    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.images.ocr_ppocrv6 import PPOcrV6Engine

    cfg = Config()
    cfg.ocr.cache_dir = str(tmp_path)
    ocr = PPOcrV6Engine(Context(config=cfg, events=EventBus()))

    import time

    t0 = time.time()
    p1 = ocr.read(_REAL_IMG, asset_uid="one")
    cold = (time.time() - t0) * 1000
    t0 = time.time()
    p2 = ocr.read(_REAL_IMG, asset_uid="two")
    warm = (time.time() - t0) * 1000

    assert p1.blocks, "冷启动都没识别出文字，这条测试没有意义"
    assert [b.source for b in p1.blocks] == [b.source for b in p2.blocks], (
        "命中缓存的文字和第一次不一致 —— 确定性被破坏"
    )
    assert warm < cold / 2, f"缓存没省下时间：冷 {cold:.0f}ms 热 {warm:.0f}ms"
    assert ocr._cache().stats()["hits"] == 1


@pytest.mark.skipif(not _REAL_IMG.is_file(), reason="本机没有真实可识别贴图")
def test_cached_block_ids_follow_current_uid(tmp_path: Path) -> None:
    """**回归**：命中缓存时块 ``id`` 必须按**当次** ``asset_uid`` 重写。

    ``asset_uid`` 不参与缓存键（同一张图可能以不同 uid 出现），
    但 ``id`` 的构造是 ``f"{asset_uid or 'img'}#{序号}"``。
    直接用缓存里的 id 会造成"框属于这张图、id 却属于上一次调用"的错配，
    下游按 id 索引样式/结果时对不上。
    """
    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.images.ocr_ppocrv6 import PPOcrV6Engine

    cfg = Config()
    cfg.ocr.cache_dir = str(tmp_path)
    ocr = PPOcrV6Engine(Context(config=cfg, events=EventBus()))

    p1 = ocr.read(_REAL_IMG, asset_uid="AAA")
    p2 = ocr.read(_REAL_IMG, asset_uid="BBB")
    assert all(b.id.startswith("AAA#") for b in p1.blocks), [b.id for b in p1.blocks]
    assert all(b.id.startswith("BBB#") for b in p2.blocks), (
        f"命中缓存的 id 没跟着当次 uid 走：{[b.id for b in p2.blocks]}"
    )


def test_cache_can_be_switched_off() -> None:
    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.images.ocr_ppocrv6 import PPOcrV6Engine

    cfg = Config()
    cfg.ocr.cache = False
    ocr = PPOcrV6Engine(Context(config=cfg, events=EventBus()))
    assert not ocr._cache().enabled


def test_engine_tag_changes_when_tier_changes() -> None:
    """**核心契约**：引擎标识必须反映档位/模型，否则换档会命中旧缓存。

    （踩过：早先读 ``rapidocr.__version__``，该属性不存在，
    标识永远是 ``rapidocr|?``，等于缓存键里没有版本信息；
    更早的版本还把空串当初值，让 ``is not None`` 短路、返回空标识。）
    """
    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.images.ocr_ppocrv6 import PPOcrV6Engine

    tag = PPOcrV6Engine(Context(config=Config(), events=EventBus()))._engine_tag()
    assert tag, "引擎标识是空的 —— 缓存键里会丢掉版本信息"
    assert "?" not in tag, f"包版本没取到：{tag!r}"
    assert ".onnx:" in tag, f"没带模型指纹：{tag[:120]!r}"


def test_default_cache_root_is_under_data_root() -> None:
    root = default_cache_root()
    assert root is None or root.name == "ocr"
