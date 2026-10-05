r"""★ 贴图：**原文原样回显**的组必须被剔除，且**不能销毁原图文字**。

## 为什么这条守卫的位置极其关键

流程是「**先 inpaint 抹掉全部文字 → 再逐块把译文画回去**」。

所以如果某组译文就是原文（模型原样回显）而我们在**画**的时候跳过它：

```
第 5 步 inpaint：把那块文字**抹掉了**
第 6 步 绘制：  因为"没译文"而跳过 ⇒ 不重画
结果：          那块文字**永久消失**，图上留一块空白
```

**这比"没翻译"更糟** —— 原文是玩家能看懂的日文/英文，空白是看不懂的。

⇒ 必须在 `inpaint_boxes` **之前**把它从待处理集合里剔掉，
这样它既不参与抹字、也不参与重绘，**原图那块原样保留**。

## 实测的真实案例

`[Summoner Veil]` 的图集里有一块 `src='LOV'`，模型回了 `tgt='LOV'`。
落盘的文件与原图 **sha1 完全相同**，而 `localize.json` 报
`changed:true / ok:true`。

## ▲ 本文件重点测"索引对齐"

`groups` / `texts` / `translations` 是**按组下标**对齐的三个列表。
剔除中间某一组时若只删一个列表，就会**索引错位** ⇒
把 A 组的译文画到 B 组的框里。那比不剔除严重得多。

所以这里的用例**故意**构造"第 0 组回显、第 1 组正常"，
并断言第 1 组的译文**仍然落在第 1 组**。

## 怎么把 OCR 换成假的（踩过两次坑）

* `self.ocr` 是**惰性属性**，底层字段是 `_ocr` —— patch `_detect_blocks`
  之类不存在的方法不会生效，真实 PP-OCR 照跑（实测白花 14 秒）；
* `self.ocr.read()` 必须返回真 **`OcrPage`**（有 `.error` / `.blocks`），
  不能返回 `(blocks, [])` 元组。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import get_config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images.ocr_ppocrv6 import OcrPage  # noqa: E402
from novaloc.images.service import TextureTranslator  # noqa: E402
from novaloc.models import ImageTextBlock  # noqa: E402

W, H = 200, 120


def _png(tmp_path: Path) -> Path:
    """造一张纯色 PNG（内容不重要，测的是"哪块被画/被抹"）。"""
    import cv2

    p = tmp_path / "t.png"
    cv2.imwrite(str(p), np.full((H, W, 3), 60, np.uint8))
    return p


def _blocks() -> list[ImageTextBlock]:
    """两块互不重叠的文字，分别给不同 source。

    ▲ 必须是**多字符**文本 —— `_is_plausible_text_block` 会丢掉单字符块
      （那是防"立绘上误读出的 '0'/'S'"的真实守卫），用单字符做夹具
      会被它挡掉，测试就测不到我们要测的东西。
    """
    return [
        ImageTextBlock(
            id="b0", source="LOV", target="", box=(10, 10, 70, 40), confidence=0.9
        ),
        ImageTextBlock(
            id="b1", source="Start Game", target="", box=(10, 70, 150, 105), confidence=0.9
        ),
    ]


def _tt(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> TextureTranslator:
    """造一个 TextureTranslator，OCR 与翻译都替换成确定性的假实现。"""
    tt = TextureTranslator(Context(config=get_config(), events=EventBus()))
    blocks = _blocks()

    class _FakeOcr:
        def read(self, image, asset_uid="", **kw):  # noqa: ANN001, ANN003
            return OcrPage(blocks=list(blocks), width=W, height=H)

    # 惰性属性底层字段是 `_ocr`
    monkeypatch.setattr(tt, "_ocr", _FakeOcr(), raising=False)

    def fake_translate(texts, lang, existing, glossary, ctx_lines, p, dry_run=False):  # noqa: ANN001
        # ▲ 必须返回 ``dict[int, str]`` —— 真实 `_translate_texts` 的返回类型。
        #   我第一版返回 list，下游 `translations.get(gi)` 立刻抛
        #   AttributeError。mock 的**返回类型**要和真货一致，否则
        #   测出来的失败是"mock 不像"，不是"代码有错"。
        return {i: mapping.get(t, t) for i, t in enumerate(texts)}

    monkeypatch.setattr(tt, "_translate_texts", fake_translate)
    # 样式测量依赖真实像素，给固定值省时间（只影响颜色，不影响本测试的判据）
    monkeypatch.setattr(
        "novaloc.images.service.extract_colors",
        lambda img, box: ((255, 255, 255), (0, 0, 0), 0.0),
    )
    return tt


def test_all_echoes_keeps_image_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""★ 全部回显 ⇒ **原图一个像素都不改**（不抹字、不重绘）。"""
    import cv2

    p = _png(tmp_path)
    tt = _tt(monkeypatch, {"LOV": "LOV", "Start Game": "Start Game"})
    res = tt.process(p, target_lang="zh-Hans")

    assert res.outcomes, "应该有 outcome 记录"
    assert all(not o.ok for o in res.outcomes), [
        (o.source, o.target, o.ok) for o in res.outcomes
    ]
    assert all(o.status.value == "skipped" for o in res.outcomes)
    assert res.changed is False
    assert any("相同" in w for o in res.outcomes for w in o.warnings), res.outcomes
    assert any("全部被判为" in w for w in res.warnings), res.warnings
    # ★ 图像未被改动
    orig = cv2.imread(str(p))
    assert np.array_equal(res.image, orig), "全部回显时不应改动图像"


def test_echo_is_filtered_but_real_translation_still_drawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""★★ 索引对齐：第 0 组回显、第 1 组正常 ⇒ 只有第 1 组被画。"""
    p = _png(tmp_path)
    tt = _tt(monkeypatch, {"LOV": "LOV", "Start Game": "开始游戏"})
    res = tt.process(p, target_lang="zh-Hans")

    ok = [o for o in res.outcomes if o.ok]
    skipped = [o for o in res.outcomes if not o.ok]
    assert len(skipped) == 1, [(o.source, o.target, o.ok) for o in res.outcomes]
    assert skipped[0].source == "LOV"
    assert len(ok) == 1, "正常那组应被画出来"
    # ★ 关键：画出的是**第 1 组**的译文，没有错位
    assert ok[0].source == "Start Game", ok[0]
    assert ok[0].target == "开始游戏", ok[0]
    assert res.changed is True


def test_wrapped_source_is_treated_as_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`'(Start Game)'` 与 `'Start Game'` **实义字符相同** ⇒ 也算回显。

    这是模型很常见的包装花招（外面套一层括号/引号），
    `_is_source_echo` 的第 3 条判据（只比实义字符）正是为它写的。
    """
    p = _png(tmp_path)
    tt = _tt(monkeypatch, {"LOV": "LOV", "Start Game": "(Start Game)"})
    res = tt.process(p, target_lang="zh-Hans")
    assert res.changed is False, [(o.source, o.target, o.ok) for o in res.outcomes]
