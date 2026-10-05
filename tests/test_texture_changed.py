r"""★ 贴图 `changed` 的判据：**译文与原文实质不同**才算改动。

## 实测缺陷（原来的实现）

`TextureResult.changed` 原来是：

```python
@property
def changed(self) -> bool:
    return self.translated > 0
```

只看"有几块有译文"，**不看译文是否与原文不同**。实测后果：

```
localize.json:  "changed": true, "translated": 1, "ok": true
blocks:         src='LOV'  tgt='LOV'          ← 模型原样"译"回来
rebuilt/ 与原图 sha1 **完全相同**（1065872 B，字节级一致）
```

于是 `stages.py` 因为 `changed` 为真**照常落盘**，写了一份与原图逐字节
相同的文件；报告说"138 张已汉化"，实际**一张都没改动**。

这是"报告与实际不一致"的典型：数字看着对，磁盘上什么都没发生。

## 判据用 `_content`（丢标点/空白后比较）

* `'LOV'` → `'LOV'` ⇒ 不改动 ✅
* `'Loading'` → `'载入中'` ⇒ 改动 ✅
* `'（…）'` → `'……'` ⇒ 只差标点，**不算**汉化成果 ✅
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.images.service import BlockOutcome, TextureResult  # noqa: E402
from novaloc.models import ImageAsset  # noqa: E402


def _res(pairs: list[tuple[str, str, bool]]) -> TextureResult:
    """用 ``(src, tgt, ok)`` 三元组造一个 TextureResult。

    `TextureResult.asset` 是必填的（`image` 可省）——
    这里造一个最小的 `ImageAsset` 占位。
    """
    return TextureResult(
        asset=ImageAsset(uid="t", path="t.png"),
        outcomes=[
            BlockOutcome(block_id=str(i), source=s, target=t, ok=ok)
            for i, (s, t, ok) in enumerate(pairs)
        ],
    )


def test_echo_translation_is_not_a_change() -> None:
    """★ 核心：译文 == 原文 ⇒ **不算改动**（这正是实测的 138 张）。"""
    r = _res([("LOV", "LOV", True)])
    assert r.translated == 1, "translated 仍然算 1（模型确实给了译文）"
    assert r.changed is False, "但内容没变，所以 changed 必须是 False"


def test_real_translation_is_a_change() -> None:
    """真的翻了 ⇒ 算改动。"""
    assert _res([("Loading", "载入中", True)]).changed is True


def test_punctuation_only_difference_is_not_a_change() -> None:
    """只换标点不算汉化成果（`'（…）'` → `'……'`）。"""
    assert _res([("（…）", "……", True)]).changed is False


def test_empty_target_is_not_a_change() -> None:
    """空译文 ⇒ 不改动。"""
    assert _res([("Hello", "", True)]).changed is False


def test_failed_block_is_not_a_change() -> None:
    """`ok=False` 的块即使有译文也不算（它没被接受）。"""
    assert _res([("Hello", "你好", False)]).changed is False


def test_one_real_change_among_echoes() -> None:
    """多块里只要**有一块**真的不同 ⇒ 算改动。"""
    r = _res([("LOV", "LOV", True), ("Start", "开始", True), ("OK", "OK", True)])
    assert r.changed is True


def test_all_echoes_is_not_a_change() -> None:
    """多块**全部**原样回显 ⇒ 不算改动（避免整图空转落盘）。"""
    r = _res([("LOV", "LOV", True), ("OK", "OK", True), ("Back", "Back", True)])
    assert r.changed is False


def test_no_outcomes_is_not_a_change() -> None:
    """没有任何块 ⇒ 不改动。"""
    assert _res([]).changed is False
