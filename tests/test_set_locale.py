r"""★★ 「把自带中文的游戏设为中文」—— `novaloc.locale` 的测试。

## 为什么需要这个功能

用户指正：

> "请将支持中文的游戏设置为中文（比如游戏的配置设置文件等处），
>  防止我找不到语言设置处"

**实测这是真问题**：

```
57 个游戏自带 locales/zh-CN.pak，但其中 **45 个**的
data/System.json 里 locale 不是中文（ja_JP / en_US / ko_KR）
⇒ 玩家看到日文/英文，而设置菜单里未必有语言选项
```

## 机制（依据是**游戏自己的 JS 源码**，不是我猜的）

```javascript
// rpg_objects.js（MV）/ rmmz_objects.js（MZ）
return $dataSystem.locale.match(/^ja/);
return $dataSystem.locale.match(/^zh/);   ← 是否显示中文由这里决定
```

⚠️ 我前面**猜错过两次**：先猜 `config.rpgsave`（lz-string），
又猜 `config.rmmzsave`（zlib），都解不出来。
**读游戏源码才是权威依据** —— 这个教训值得留在测试文件里。

## 本文件测什么

1. 计划逻辑：有中文包 + locale 非中文 ⇒ 该改；已是中文 ⇒ 不改；
   没有中文包 ⇒ 不改（翻了也没用，它没有中文数据）；
2. **★ 安全性**：只改 `locale` 一个字段，**其余字节完全不动**；
3. **★ 备份是硬前提**：没有工作区时**拒绝写入**；
4. 幂等：跑两次第二次不改；
5. 找不到 `System.json` / JSON 坏了 ⇒ 不写不崩。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.locale import (  # noqa: E402
    DEFAULT_ZH_LOCALE,
    apply_plan,
    plan_game,
    zh_locale_packs,
)


def _game(tmp_path: Path, *, locale: str = "ja_JP", zh_pack: bool = True) -> Path:
    """造一个最小 RPG Maker 游戏目录。"""
    g = tmp_path / "game"
    data = g / "www" / "data"
    data.mkdir(parents=True)
    # 故意用**不规整的键序与空白**，验证"其余字节不动"
    sj = data / "System.json"
    sj.write_text(
        '{\n  "gameTitle": "テスト",\n  "locale": "' + locale + '",\n'
        '  "currencyUnit": "G",\n  "terms": {"basic": ["Lv"]}\n}\n',
        encoding="utf-8",
    )
    if zh_pack:
        loc = g / "locales"
        loc.mkdir()
        (loc / "zh-CN.pak").write_bytes(b"\x00binary")
        (loc / "en.pak").write_bytes(b"\x00binary")
    return g


# ----------------------------------------------------------------------
# 1. 计划逻辑
# ----------------------------------------------------------------------
def test_plans_change_when_zh_pack_and_not_zh(tmp_path: Path) -> None:
    g = _game(tmp_path, locale="ja_JP")
    p = plan_game(g)
    assert p.will_change, f"有中文包但 locale=ja_JP，应当计划改动：{p.reason}"
    assert p.current == "ja_JP"
    assert p.target == DEFAULT_ZH_LOCALE


def test_no_change_when_already_zh(tmp_path: Path) -> None:
    """已经是中文 ⇒ 不动（避免无意义改写）。"""
    g = _game(tmp_path, locale="zh_TW")
    p = plan_game(g)
    assert not p.will_change
    assert "已经" in p.reason


def test_no_change_without_zh_pack(tmp_path: Path) -> None:
    r"""★ 没有中文包 ⇒ **不动**。

    改了也没用（游戏没有中文数据可读），只会让玩家看到空白/回退语言。
    这个判据防止"对所有游戏都改 locale"。
    """
    g = _game(tmp_path, locale="ja_JP", zh_pack=False)
    p = plan_game(g)
    assert not p.will_change
    assert "没有自带中文" in p.reason


def test_no_change_without_system_json(tmp_path: Path) -> None:
    """找不到 `System.json` ⇒ 不动（不是 RPG Maker 或结构不同）。"""
    g = tmp_path / "g2"
    (g / "locales").mkdir(parents=True)
    (g / "locales" / "zh-CN.pak").write_bytes(b"x")
    p = plan_game(g)
    assert not p.will_change
    assert "System.json" in p.reason


def test_broken_json_is_safe(tmp_path: Path) -> None:
    """`System.json` 坏了 ⇒ 不动不崩。"""
    g = _game(tmp_path)
    (g / "www" / "data" / "System.json").write_text("{broken", encoding="utf-8")
    p = plan_game(g)
    assert not p.will_change


# ----------------------------------------------------------------------
# 2. ★ 只改一个字段
# ----------------------------------------------------------------------
def test_apply_changes_only_locale_value(tmp_path: Path) -> None:
    r"""★★ 只有 `locale` 的**值**变，其余字节**完全不动**。

    为什么重要：`System.json` 里混着大量非文本数据（`terms` 数组等）。
    重写整个 JSON 会改键序、规范化数字 —— "内容没变但字节全变"，
    差分不可读，且一旦序列化与游戏期望不一致可能让游戏读不了。
    """
    g = _game(tmp_path, locale="ja_JP")
    sj = g / "www" / "data" / "System.json"
    before = sj.read_bytes()
    ws = tmp_path / "ws"

    plan = plan_game(g)
    ok, why = apply_plan(plan, workspace=ws)
    assert ok, why

    after = sj.read_bytes()
    # 值确实变了
    assert json.loads(after.decode("utf-8"))["locale"] == DEFAULT_ZH_LOCALE
    # 且**只有那一处**变了：把新值换回旧值应得到完全相同的字节
    restored = after.replace(
        f'"locale": "{DEFAULT_ZH_LOCALE}"'.encode(), b'"locale": "ja_JP"'
    )
    assert restored == before, "除 locale 值以外还有别的字节被改动"


def test_apply_is_idempotent(tmp_path: Path) -> None:
    """跑两次，第二次不改（幂等）。"""
    g = _game(tmp_path)
    ws = tmp_path / "ws"
    p1 = plan_game(g)
    ok1, _ = apply_plan(p1, workspace=ws)
    assert ok1
    p2 = plan_game(g)
    assert not p2.will_change, "第二次仍计划改动 ⇒ 不幂等"


# ----------------------------------------------------------------------
# 3. ★★ 备份是硬前提
# ----------------------------------------------------------------------
def test_refuses_without_workspace(tmp_path: Path) -> None:
    r"""★★ 没有工作区可放备份 ⇒ **拒绝写入**。

    备份是本模块**唯一**的回滚手段。为了"改成功"而跳过备份
    是不可接受的取舍 —— 宁可不动。
    """
    g = _game(tmp_path, locale="ja_JP")
    sj = g / "www" / "data" / "System.json"
    before = sj.read_bytes()

    plan = plan_game(g)
    ok, why = apply_plan(plan, workspace=None)
    assert not ok, "没有备份位置却写入成功"
    assert sj.read_bytes() == before, "拒绝写入时文件仍被改了"


def test_backup_is_written(tmp_path: Path) -> None:
    """备份确实落盘，且内容是**原文件**。"""
    g = _game(tmp_path, locale="ja_JP")
    sj = g / "www" / "data" / "System.json"
    before = sj.read_bytes()
    ws = tmp_path / "ws"

    ok, _ = apply_plan(plan_game(g), workspace=ws)
    assert ok
    bak = ws / "locale_backup" / "System.json"
    assert bak.is_file(), "没有写备份"
    assert bak.read_bytes() == before, "备份内容不是原文件"
    rec = json.loads((ws / "locale_set.json").read_text(encoding="utf-8"))
    assert rec["locale_before"] == "ja_JP"
    assert rec["locale_after"] == DEFAULT_ZH_LOCALE


# ----------------------------------------------------------------------
# 4. 语言包识别
# ----------------------------------------------------------------------
def test_zh_locale_packs_detects_chinese(tmp_path: Path) -> None:
    """只挑中文包，不把 `en.pak` 也算进来。"""
    g = _game(tmp_path)
    packs = zh_locale_packs(g)
    assert any("zh" in x.lower() for x in packs)
    assert not any(x.lower().startswith("en") for x in packs), f"混进了非中文包：{packs}"


def test_zh_locale_packs_excludes_shader_dll(tmp_path: Path) -> None:
    r"""★ 不该把 `sc.stylizedwater2.runtime.dll` 当成语言包。

    实测误判：`sc` 是合法语言码（简体中文），而它是 Unity Shader 库。
    判据复用 `batch._ZH_FILE_CODES`，所以这里守的是"复用没走样"。
    """
    g = _game(tmp_path)
    managed = g / "Game_Data" / "Managed"
    managed.mkdir(parents=True)
    (managed / "sc.stylizedwater2.runtime.dll").write_bytes(b"MZ")
    packs = zh_locale_packs(g)
    assert not any("dll" in x.lower() for x in packs), f"把 DLL 当语言包了：{packs}"


# ----------------------------------------------------------------------
# 5. 结构性守卫
# ----------------------------------------------------------------------
def test_terms_keys_do_not_include_locale() -> None:
    r"""★★ `locale` **不能**在 NovaLoc 的翻译字段白名单里。

    这是本模块"改 locale 不会被流水线覆盖"的**前提**。
    若哪天有人把 `locale` 加进 `rpgmaker.py` 的白名单，
    这个前提就没了 —— 那条测试会失败，提醒先重新想清楚优先级。
    """
    from novaloc.locale import TERMS_KEYS

    assert "locale" not in TERMS_KEYS
    src = (ROOT / "src" / "novaloc" / "engines" / "rpgmaker.py").read_text(encoding="utf-8")
    # 找 System.json 的白名单行
    for line in src.splitlines():
        if "System.json" in line and "{" in line:
            assert "locale" not in line, (
                "`locale` 被加进了 rpgmaker 的字段白名单 ⇒ "
                "流水线会覆盖本模块的改动，需重新设计优先级"
            )
            break
    else:
        pytest.skip("没找到 System.json 白名单行（可能已被重构）")
