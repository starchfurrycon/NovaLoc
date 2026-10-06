r"""★★ 守护测试：`locales/*.pak` **不是**游戏汉化 —— 别再把 NW.js 语言包当证据。

## 这是一个真实的、代价很大的错误（必须留测试防复发）

### 我错在哪

我看到游戏目录里有 `locales/zh-CN.pak`，**推断**"游戏自带官方中文"，
于是在 `detect_builtin_chinese_assets` 里加了"判据 0：文件名是中文语言码"。
结果：**57 个游戏被跳过**，我还把它当成成果写进了 CHANGELOG。

### 用户验证后暴露的真相

* 用户启动 `Battle Demon Kirsten` 与 `Sakura Gozen` 验证：
  **改 `locale` 后仍不显示中文**；
* **没有任何 JS 引用 `.pak`**（精确匹配）⇒ 游戏**不读**它；
* **`.pak` 里是 Chromium/NW.js 的 UI 字符串**：

  ```
  '位用户默认个人资料'  '值不符合格式要求'  '书签栏其他书签移动设备书签'  '该政策已忽略'
  ```

  ⇒ RPG Maker MV/MZ 用 **NW.js（内嵌 Chromium）** 跑，
  `locales/` 是 **NW.js 自己的语言包**（106 种语言，人人都有），
  **与游戏汉化毫无关系**。

### 实测代价

```
有 locales/*.pak 的游戏: 43 个
游戏数据**确实已汉化**的: 只剩 1 个
游戏数据**几乎无中文**的: 32 个   ← 被误判跳过（本该翻译的游戏被漏掉）
```

## 我的方法论错误（这是最值得记的部分）

我**读到了** `$dataSystem.locale.match(/^zh/)` 就推断"改 locale 能切语言"，
但**没查**两件事：

1. 那个函数的**调用点**（实测只用在窗口绘制与输入插件）；
2. `.pak` **由谁加载**（答案：谁也不加载）。

⇒ **把"存在"当成了"生效"。**

这是同一轮里的**第三次**同型错误：
* `cv2.imdecode` 绕过生产的解密读取器；
* 用 `to_translation_map` 代替 `parse_translations`；
* 看到 `.pak` 就断定是汉化。

**共同点：没有沿着真实数据流验证，而是从"看到了某个东西"直接跳到结论。**

## 本文件测什么

1. `detect_builtin_chinese_assets` **不再**因 `.pak` 命中
   （`locales/zh-CN.pak` 单独存在时**不该**判为已中文）；
2. `_zh_language_file` 这个函数**仍在**（保留供参考），
   但**不被 `detect_builtin_chinese_assets` 调用** ——
   防止有人看到函数还在就以为这条路可行；
3. 文档里记着这个错误（防止后人重犯）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.batch import detect_builtin_chinese_assets  # noqa: E402

BATCH_SRC = ROOT / "src" / "novaloc" / "batch.py"


def _fake_rpg_game(tmp_path: Path, *, with_pak: bool = True) -> Path:
    """造一个**只有 NW.js 语言包、没有真汉化**的 RPG Maker 游戏。"""
    g = tmp_path / "game"
    data = g / "www" / "data"
    data.mkdir(parents=True)
    (data / "System.json").write_text(
        '{"locale": "ja_JP", "gameTitle": "テスト", "terms": {"basic": ["Lv"]}}',
        encoding="utf-8",
    )
    # 日文原文（**没有**汉化）
    (data / "Map001.json").write_text(
        '{"events": [{"note": "こんにちは、勇者よ"}]}', encoding="utf-8"
    )
    if with_pak:
        loc = g / "locales"
        loc.mkdir()
        # ★ NW.js 的运行时语言包（内容就是 Chromium 的 UI 字符串）
        (loc / "zh-CN.pak").write_bytes(b"\x00\x00\x00\x00BROWSER UI STRINGS")
        (loc / "en-US.pak").write_bytes(b"\x00\x00\x00\x00BROWSER UI STRINGS")
    return g


# ----------------------------------------------------------------------
# 1. ★★ `.pak` 单独存在**不该**判为已中文
# ----------------------------------------------------------------------
def test_locale_pak_alone_is_not_chinese_evidence(tmp_path: Path) -> None:
    r"""★★ 只有 `locales/zh-CN.pak` ⇒ **不该**判为"已是中文"。

    这是那个 32 个游戏被误跳过的直接原因。
    `.pak` 是 **NW.js（内嵌 Chromium）的运行时语言包**，
    每个用 NW.js 的 RPG Maker 游戏都有（106 种语言全都有），
    与游戏汉化**毫无关系**。
    """
    g = _fake_rpg_game(tmp_path, with_pak=True)
    ok, why = detect_builtin_chinese_assets(g)
    assert not ok, (
        f"仅凭 `locales/zh-CN.pak` 就判为已中文（理由：{why}）—— "
        "这正是让 32 个该翻的游戏被跳过的那条误判"
    )


def test_no_pak_game_also_not_chinese(tmp_path: Path) -> None:
    """没有 `.pak`、只有日文原文 ⇒ 当然不该判为已中文（对照组）。"""
    g = _fake_rpg_game(tmp_path, with_pak=False)
    ok, _why = detect_builtin_chinese_assets(g)
    assert not ok


# ----------------------------------------------------------------------
# 2. ★ 结构性守卫：函数保留但**不被调用**
# ----------------------------------------------------------------------
def test_zh_language_file_is_not_used_by_detector() -> None:
    r"""★★ `_zh_language_file` **不能**出现在 `detect_builtin_chinese_assets` 里。

    函数我**保留了**（留给未来参考，且它本身逻辑没问题），
    但**不能**被主判据调用 —— 否则 32 个游戏又会被跳过。

    这条守的是"回退没被无意撤掉"。
    """
    t = BATCH_SRC.read_text(encoding="utf-8")
    # 定位 detect_builtin_chinese_assets 函数体
    start = t.index("def detect_builtin_chinese_assets(")
    # 到下一个顶层 def 为止
    nxt = t.find("\ndef ", start + 1)
    body = t[start : nxt if nxt > 0 else len(t)]
    # 允许出现 `_ = _zh_language_file` 这种"显式不调用"的标记
    assert "_zh_language_file(game_dir)" not in body, (
        "`detect_builtin_chinese_assets` 又调用了 `_zh_language_file` ⇒ "
        "32 个该翻的游戏会再次被跳过"
    )


def test_error_is_documented_in_source() -> None:
    r"""★★ 源码里必须留着这个错误的记录。

    否则后人看到 `_zh_language_file` 还在，会以为"这条路可行"而重新启用它。
    """
    t = BATCH_SRC.read_text(encoding="utf-8")
    assert "判据 0 已回退" in t, "没有记录判据 0 被回退"
    assert "NW.js" in t, "没有写明 `.pak` 是 NW.js 的运行时语言包"
    assert "32" in t, "没有记录实测代价（32 个游戏被误跳过）"


# ----------------------------------------------------------------------
# 3. 保留的函数本身仍然可用（留给未来参考）
# ----------------------------------------------------------------------
def test_zh_language_file_still_works(tmp_path: Path) -> None:
    r"""函数保留着，且**它自己的逻辑没错** —— 错的是"把它当汉化证据"。

    它只是"文件名是中文语言码"的检测器。**用它来判断
    『这是 NW.js 语言包』是对的**；用它判断『游戏已汉化』是错的。
    """
    from novaloc.batch import _zh_language_file

    g = _fake_rpg_game(tmp_path, with_pak=True)
    f = _zh_language_file(g)
    assert f is not None and f.name == "zh-CN.pak"
