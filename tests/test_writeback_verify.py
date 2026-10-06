r"""★★★ 写回后自动验收：三个检查必须能抓到真实的坏数据。

## 事故驱动（本轮最严重的一次）

NovaLoc 把 RPG Maker `note` 里**会被 `eval` 的 JS 代码**当文本翻了：

```
备份: '<JS On Expire State>\ntarget.addState(80);\n</JS On Expire State>'
当前: '确认<JS On Expire State>\n目标生命值恢复至 80。\n</JS On Expire State>'
```

VisuMZ 用 **`new Function()`** 执行 ⇒ `SyntaxError: Unexpected number`
⇒ **游戏启动即崩**。

## ★ 为什么必须有"启动检查"

**JSON 校验、严格校验、BOM 检查全部通过** ——
因为坏掉的是"**字符串里的代码语义**"，不是 JSON 结构。
**只有实际启动游戏**才能暴露它。

而这次是我靠**用户反馈**才发现的 ⇒ 工具自己没报错，这是根本缺陷。

## 本文件测什么

1. `check_js_blocks` **能抓到** JS 区块里的中文（用真实坏数据形态）；
2. `check_js_blocks` **不误报**正常的 JS 区块与正常中文文本；
3. `check_data_integrity` **拒绝** `NaN`/`Infinity`（JS 的 `JSON.parse` 也拒绝，
   而 Python 默认接受 ⇒ 必须显式拒绝）；
4. `check_data_integrity` **不误报** `System.json` 的 `terms.params`
   （那是**术语名数组**，中文是合法翻译 —— 实测误报过）；
5. `check_launch` 的**噪声过滤**：NW.js 的 crash_report/password_store
   等不算游戏错误；
6. 缺 exe 时**不崩**（只是查不到）；
7. `verify_game` 的短路：前两项失败就**跳过启动检查**（省 GPU）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.verify import (  # noqa: E402
    check_data_integrity,
    check_js_blocks,
    check_launch,
    find_exe,
    verify_game,
)


def _data(tmp_path: Path) -> Path:
    d = tmp_path / "game" / "data"
    d.mkdir(parents=True)
    return d


# ----------------------------------------------------------------------
# 1. ★★ JS 区块里的中文必须被抓到（真实事故形态）
# ----------------------------------------------------------------------
def test_catches_translated_js_block(tmp_path: Path) -> None:
    r"""★★ 用**真实事故**里的坏数据测。

    ```
    <JS On Expire State>
    目标生命值恢复至 80。      ← 原本是 `target.addState(80);`
    </JS On Expire State>
    ```
    """
    d = _data(tmp_path)
    (d / "States.json").write_text(
        json.dumps(
            [
                {"id": 1, "note": "<JS On Expire State>\n目标生命值恢复至 80。\n</JS On Expire State>"},
                {"id": 2, "note": "<JS On Expire State>\n目标状态已添加\n</JS On Expire State>"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    probs = check_js_blocks(tmp_path / "game")
    assert len(probs) == 2, f"应当抓到 2 处，实际 {len(probs)}：{probs}"


def test_clean_js_block_passes(tmp_path: Path) -> None:
    """正常的 JS 区块（代码）**不该**被报。"""
    d = _data(tmp_path)
    (d / "States.json").write_text(
        json.dumps(
            [{"id": 1, "note": "<JS On Expire State>\ntarget.addState(80);\n</JS On Expire State>"}]
        ),
        encoding="utf-8",
    )
    assert check_js_blocks(tmp_path / "game") == []


def test_chinese_outside_js_block_is_fine(tmp_path: Path) -> None:
    r"""★ 中文**在 JS 区块之外**是正常的（那就是我们翻的东西）。

    这条守的是"不误伤翻译成果" —— 若把 `note` 里的
    `<Help Description>` 中文也报出来，验收就永远失败。
    """
    d = _data(tmp_path)
    (d / "States.json").write_text(
        json.dumps(
            [{"id": 1, "name": "中毒", "note": "<Help Description>\n每回合损失生命值\n</Help Description>"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert check_js_blocks(tmp_path / "game") == []


# ----------------------------------------------------------------------
# 2. ★★ 严格 JSON 校验（拒绝 NaN/Infinity）
# ----------------------------------------------------------------------
def test_rejects_nan_and_infinity(tmp_path: Path) -> None:
    r"""★★ `NaN`/`Infinity` 必须被拒 —— JS 的 `JSON.parse` 不接受它们。

    ⚠️ Python 的 `json.loads` **默认接受** ⇒ 用默认参数校验会**漏掉**
    这类会让游戏崩的数据。所以本检查用 `parse_constant` 显式拒绝。
    """
    d = _data(tmp_path)
    (d / "Map001.json").write_text('{"x": NaN}', encoding="utf-8")
    (d / "Map002.json").write_text('{"y": Infinity}', encoding="utf-8")
    probs = check_data_integrity(tmp_path / "game")
    assert len(probs) == 2, f"应当拒 2 个，实际 {len(probs)}：{probs}"


def test_accepts_valid_json(tmp_path: Path) -> None:
    d = _data(tmp_path)
    (d / "Map001.json").write_text('{"x": 1, "y": [1,2,3]}', encoding="utf-8")
    assert check_data_integrity(tmp_path / "game") == []


# ----------------------------------------------------------------------
# 3. ★★ 不误报 `terms.params`（实测误报过）
# ----------------------------------------------------------------------
def test_terms_params_with_chinese_is_not_an_error(tmp_path: Path) -> None:
    r"""★★ `System.json` 的 `terms.params` 是**术语名数组**，中文是**合法翻译**。

    实测误报：我的第一版把 `params` 当成"数字字段"，于是
    `["最大生命值", "最大魔法值"]` 被报成"数字字段含中文" ⇒
    **验收永远失败**，等于这个功能没用。

    RPG Maker 里 `params` 有两处同名：
    * `Classes.json` 的 `params` = **数字二维数组**（成长曲线）；
    * `System.json` 的 `terms.params` = **术语名数组**。
    """
    d = _data(tmp_path)
    (d / "System.json").write_text(
        json.dumps(
            {"terms": {"params": ["最大生命值", "最大魔法值"], "basic": ["等级", "生命值"]}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert check_data_integrity(tmp_path / "game") == [], "把 terms.params 的中文误报成错误"


def test_numeric_field_with_chinese_is_an_error(tmp_path: Path) -> None:
    """真正的数字字段含中文 ⇒ 应当报（那才是 `Unexpected number` 的来源）。"""
    d = _data(tmp_path)
    (d / "Items.json").write_text(
        json.dumps([{"id": 1, "price": "零"}], ensure_ascii=False),
        encoding="utf-8",
    )
    probs = check_data_integrity(tmp_path / "game")
    assert probs and "price" in probs[0], f"没报数字字段含中文：{probs}"


# ----------------------------------------------------------------------
# 4. 启动检查：噪声过滤与健壮性
# ----------------------------------------------------------------------
def test_missing_exe_does_not_crash(tmp_path: Path) -> None:
    """没有 exe ⇒ 返回说明而不是抛异常（不是错，只是查不到）。"""
    (tmp_path / "game").mkdir()
    assert find_exe(tmp_path / "game") is None
    probs = check_launch(tmp_path / "game")
    assert probs and "找不到可执行文件" in probs[0]


def test_noise_patterns_are_filtered() -> None:
    r"""★ NW.js 自身的噪声**不算**游戏错误。

    实测它每次启动都输出几十条（crash_report / password_store /
    GPU / libprotobuf）。不过滤的话验收会**永远失败**，
    于是没人再看它的输出 —— 那等于没有验收。
    """
    from novaloc.verify import FATAL_LOG_RE, NOISE_RE

    noise = [
        "[1006/172726.861:ERROR:crash_report_database_win.cc(469)] failed to stat report",
        "[110880:69564:1006/172726.948:ERROR:login_database.cc(663)] Password store database is too new",
        "[libprotobuf ERROR] Can't parse message of type \"web_app.WebAppProto\"",
        "[110880:104232:1006/172727.082:ERROR:top_sites_backend.cc(78)] Failed to initialize database",
    ]
    for ln in noise:
        assert NOISE_RE.search(ln), f"漏滤噪声：{ln[:50]}"

    fatal = [
        'INFO:CONSOLE(2032)] "SyntaxError: Unexpected number',
        'INFO:CONSOLE(1)] "TypeError: Cannot read property',
        "Uncaught ReferenceError: foo is not defined",
    ]
    for ln in fatal:
        assert FATAL_LOG_RE.search(ln), f"漏抓致命错误：{ln[:50]}"


# ----------------------------------------------------------------------
# 5. 短路：前两项失败就不启动（省 GPU）
# ----------------------------------------------------------------------
def test_skips_launch_when_earlier_checks_fail(tmp_path: Path) -> None:
    """前两项失败 ⇒ **不启动游戏**（省 GPU，且启动也没意义）。"""
    d = _data(tmp_path)
    (d / "States.json").write_text(
        json.dumps([{"note": "<JS X>\n目标状态已添加\n</JS X>"}], ensure_ascii=False),
        encoding="utf-8",
    )
    res = verify_game(tmp_path / "game", launch=True)
    assert not res.ok
    assert "launch" not in res.checks_run, "前两项已失败却仍跑了启动检查"


def test_clean_game_runs_all_checks(tmp_path: Path) -> None:
    """干净数据 ⇒ 三项都跑（没有 exe 时第三项只记"找不到"）。"""
    d = _data(tmp_path)
    (d / "Map001.json").write_text('{"x": 1}', encoding="utf-8")
    res = verify_game(tmp_path / "game", launch=True)
    assert "js_blocks" in res.checks_run
    assert "data_integrity" in res.checks_run


# ----------------------------------------------------------------------
# 6. ★★ 用户数据（存档/配置）**不该验收**
# ----------------------------------------------------------------------
def test_save_data_with_bom_is_not_an_error(tmp_path: Path) -> None:
    r"""★★ 游戏存档带 BOM **不该**报错 —— 那是它自己的格式，不是我们的产物。

    实测误报（Breeding Log 1.04 64）：

    `
    生殖活動記録_Data\savedata.json：严格 JSON 校验失败
      → Unexpected UTF-8 BOM
    `

    而那是**存档文件**，BOM 解掉后 JSON 完全合法，
    且它**不在** rpgmaker.DATABASE_FIELDS 的 11 个数据文件白名单里
    ⇒ **NovaLoc 从来没碰过它**。

    ⇒ 判据：只验收我们可能写过的东西。存档/配置属于用户数据。
    """
    game = tmp_path / "game"
    d = game / "X_Data"
    d.mkdir(parents=True)
    # 存档：带 BOM
    (d / "savedata.json").write_bytes(b"\xef\xbb\xbf" + b'{"keys":[1,2]}')
    # 配置存档
    (game / "save").mkdir()
    (game / "save" / "config.rpgsave").write_bytes(b"N4IghgNg7mCeDOAR")
    # 数据文件（**我们翻的**，合法）
    (game / "data").mkdir()
    (game / "data" / "Items.json").write_text('[{"id":1,"name":"药水"}]', encoding="utf-8")

    assert check_data_integrity(game) == [], "把存档文件当成验收对象了"
    assert check_js_blocks(game) == []


def test_data_file_with_bom_is_still_an_error(tmp_path: Path) -> None:
    r"""★ **数据文件**（我们翻的）带 BOM ⇒ **仍应报**（那是真的坏）。

    这条守的是"排除"没排过头 —— 若把 data/*.json 也放过，
    验收就失去意义了。
    """
    game = tmp_path / "game"
    d = game / "data"
    d.mkdir(parents=True)
    (d / "Items.json").write_bytes(b"\xef\xbb\xbf" + b'[{"id":1}]')
    probs = check_data_integrity(game)
    assert probs, "数据文件带 BOM 却没报"


def test_user_data_patterns() -> None:
    """排除规则的覆盖面（含实测遇到的那几个）。"""
    from novaloc.verify import _is_user_data

    for s in (
        "X_Data/savedata.json",
        "save/config.rpgsave",
        "save/global.rmmzsave",
        "locales/zh-CN.pak",
        "Dictionaries/en-US-9-0.bdic",
        "Game/userdata/foo.json",
    ):
        assert _is_user_data(Path(s)), f"没排除 {s}"
    for s in ("data/Items.json", "www/data/System.json", "Game/data/States.json"):
        assert not _is_user_data(Path(s)), f"误排了数据文件 {s}"
