r"""★ `Scenario.json` —— **34,151 条剧情对白**曾被整个文件静默跳过。

## 事故经过

`data/Scenario.json` 是某真实 MV 游戏 **11 MB、981 个场景**的剧本文件，
里面有 **37,146 条 ``code 401``（显示文字）** ——
也就是这个游戏的**主要剧情对白**。

而 `extract_text` 的派发链里**根本没有它的分支**：

```python
if f.name == "MapInfos.json": ...
elif f.name == "System.json": ...
elif f.name.startswith("Map") ...: ...
elif f.name == "CommonEvents.json": ...
elif f.name in DATABASE_FIELDS: ...
elif f.name == "Tilesets.json": ...
# ← 没有 else，Scenario.json 静默掉队
```

于是：

* 工具报"抽取 29,174 条文本"，**看着很正常**；
* 实际 **34,151 条去重剧情对白一个字都没抽到**；
* 玩家打开游戏：菜单是中文，**剧情全是英文**。

这比"报错"严重得多 —— **报告全绿，核心功能等于没做**。
同一个游戏的 `CommonEvents.json` 反而**是被处理的**，
所以"有些事件对话翻了、主线剧情没翻"这种半成品状态更难被发现。

## 本套件守住的三件事

1. `Scenario.json` 的 `code 401` 必须抽出来；
2. **场景名不是文案**（实测是 `'1'`、`'EV22'` 这类内部标识），不许抽；
3. 没被认领的数据文件**必须看得见** —— 不许再出现"静默跳过"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines.rpgmaker import RpgMakerAdapter  # noqa: E402


def _adapter() -> RpgMakerAdapter:
    return RpgMakerAdapter(Context(config=Config(), events=EventBus()))


def _scenario(*lines: str) -> dict:
    """造一个剧本文件：`场景名 → 指令列表`（与实测结构一致）。"""
    return {
        "1": [
            {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2, ""]},
            *[
                {"code": 401, "indent": 0, "parameters": [ln]}
                for ln in lines
            ],
        ]
    }


# ---------------------------------------------------------------------------
# 一、必须抽出来
# ---------------------------------------------------------------------------


def test_scenario_dialogue_is_extracted() -> None:
    """★ 核心回归：`code 401` 的正文必须被抽出。

    这一条如果不写，整个文件会**静默**跳过而所有测试照样全绿 ——
    因为"少抽了一整个文件"不会让任何断言失败。

    ★ 注意连续的 401 会被合成一条（同一段对白按显示宽度拆开），
    所以这里两行在同一个 `TextUnit` 里，用 ``\\n`` 连接。
    每一行都必须在，否则就是漏抽。
    """
    obj = _scenario(
        "Bohelos's throne room is just beyond here! We're charging in, girl!",
        "Are you prepared? The battle's about to begin!",
    )
    units = _adapter()._extract_scenarios(obj, "Scenario.json")
    texts = [u.source for u in units]
    joined = "\n".join(texts)
    assert "Bohelos's throne room is just beyond here! We're charging in, girl!" in joined
    assert "Are you prepared? The battle's about to begin!" in joined
    assert len(units) == 1, f"连续的 401 应当合成一条：{texts}"


def test_separate_dialogue_blocks_stay_separate() -> None:
    """★ 反向：被 `101`（显示文字）隔开的两段对白**不能**被合成。

    `101` 是"开始一个新的消息框"，它天然是两段对白的分界。
    合错了会让上一个角色的台词和下一个角色的接成一句。
    """
    obj = {
        "1": [
            {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2, "Naho"]},
            {"code": 401, "indent": 0, "parameters": ["First speaker here."]},
            {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2, "Mebius"]},
            {"code": 401, "indent": 0, "parameters": ["Second speaker here."]},
        ]
    }
    units = _adapter()._extract_scenarios(obj, "Scenario.json")
    texts = [u.source for u in units]
    assert "First speaker here." in texts, texts
    assert "Second speaker here." in texts, texts
    assert not any("\n" in t for t in texts), f"两段对白被合成了一条：{texts}"


def test_scenario_multiple_scenes() -> None:
    """981 个场景的结构：每个场景各自是一段指令列表。"""
    obj = {
        "1": [{"code": 401, "indent": 0, "parameters": ["First scene line."]}],
        "EV22": [{"code": 401, "indent": 0, "parameters": ["Second scene line."]}],
        "00boheA_deai1": [
            {"code": 401, "indent": 0, "parameters": ["Third scene line."]}
        ],
    }
    units = _adapter()._extract_scenarios(obj, "Scenario.json")
    texts = {u.source for u in units}
    assert texts == {"First scene line.", "Second scene line.", "Third scene line."}


# ---------------------------------------------------------------------------
# 二、场景名不是文案
# ---------------------------------------------------------------------------


def test_scene_names_are_not_extracted() -> None:
    """★ 场景名是内部标识（`'1'`/`'EV22'`/`'00boheA_deai1'`），不是文案。

    抽了会多出上千条纯噪音，而且回写场景名可能破坏插件查找。
    """
    obj = {
        "EV22": [{"code": 401, "indent": 0, "parameters": ["A line."]}],
        "00boheA_deai1": [{"code": 401, "indent": 0, "parameters": ["B line."]}],
    }
    texts = {u.source for u in _adapter()._extract_scenarios(obj, "Scenario.json")}
    assert "EV22" not in texts
    assert "00boheA_deai1" not in texts


# ---------------------------------------------------------------------------
# 三、脚本绝不翻译
# ---------------------------------------------------------------------------


def test_scenario_scripts_are_not_extracted() -> None:
    """`code 356`（插件指令）与 `355/655`（脚本）绝不能当文案。

    这个文件的 `code 356` 有 **88,048 条**（是文案量的两倍多），
    误抽会直接把游戏弄坏。
    """
    obj = {
        "1": [
            {"code": 356, "indent": 0, "parameters": ["Tachie showName \\N[4]"]},
            {"code": 355, "indent": 0, "parameters": ["$gameVariables.setValue(1, 2)"]},
            {"code": 401, "indent": 0, "parameters": ["The only real line."]},
        ]
    }
    texts = {u.source for u in _adapter()._extract_scenarios(obj, "Scenario.json")}
    assert texts == {"The only real line."}, f"脚本被当成文案：{texts}"


# ---------------------------------------------------------------------------
# 三b、插件注释（`//` 开头）不是文案
# ---------------------------------------------------------------------------


def test_plugin_comments_are_not_extracted() -> None:
    r"""★ `code 401` 里以 `//` 开头的行是**插件指令**，不是给玩家的文本。

    MV 的 `401` 同时承载对白和用 `//` 写的插件配置，例如
    `//メッセージバックを黒く`（让消息框变黑）、`//エンド`、`//移動`。
    这些是给插件读的**配置**，翻译它们没有任何显示效果，
    只会把开发者的注释变成中文。

    真实游戏里实测有一批这样的行被抽出来并翻译成了中文。
    """
    obj = {
        "1": [
            {"code": 401, "indent": 0, "parameters": ["//メッセージバックを黒く"]},
            {"code": 401, "indent": 0, "parameters": ["//演出\r"]},
            {"code": 401, "indent": 0, "parameters": ["\r//移動\r"]},
            {"code": 401, "indent": 0, "parameters": ["This one IS dialogue."]},
        ]
    }
    texts = {u.source for u in _adapter()._extract_scenarios(obj, "Scenario.json")}
    assert texts == {"This one IS dialogue."}, f"插件注释被当成文案：{texts}"


def test_slash_in_the_middle_is_still_dialogue() -> None:
    """★ 反向：`//` 出现在**行中间**时仍然是正常对白。

    例如 URL、或者台词里本来就有的斜杠。只有**行首**的 `//` 才是注释。
    """
    obj = {
        "1": [
            {"code": 401, "indent": 0, "parameters": ["See https://example.com for it."]},
            {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2, ""]},
            {"code": 401, "indent": 0, "parameters": ["Go left // then right."]},
        ]
    }
    texts = {u.source for u in _adapter()._extract_scenarios(obj, "Scenario.json")}
    assert len(texts) == 2, f"正常的斜杠被误判成注释：{texts}"


# ---------------------------------------------------------------------------
# 四、兜底判据必须严（别把数据定义文件当剧本）
# ---------------------------------------------------------------------------


def test_looks_like_scenario_map_accepts_real_shape() -> None:
    a = _adapter()
    assert a._looks_like_scenario_map(_scenario("A line."))
    assert a._looks_like_scenario_map(
        {str(i): [{"code": 401, "indent": 0, "parameters": [f"line {i}"]}] for i in range(50)}
    )


@pytest.mark.parametrize(
    "obj",
    [
        # Animations.json：id → 动画定义
        {"1": {"frames": [1, 2, 3], "name": "fire"}, "2": {"frames": [4]}},
        # TrpParticles.json：id → 粒子参数
        {"1": {"alpha": {"x": 1}, "ADD": True}},
        # 空
        {},
        # 值是列表但元素不是指令
        {"1": ["a", "b"], "2": ["c"]},
        # 不是 dict
        [{"code": 401}],
    ],
)
def test_looks_like_scenario_map_rejects_other_shapes(obj: object) -> None:
    """★ 兜底判据必须**严** —— 判错一次就是几万条噪音写进抽取结果。"""
    assert not _adapter()._looks_like_scenario_map(obj)


def test_skipped_files_are_reported() -> None:
    """★ 没被认领的数据文件必须**看得见**。

    早先循环末尾那段"统计被跳过的原因"是个空壳
    （`for _ in range(0): pass`），于是**没有任何输出**告诉用户
    有文件被跳过。11 MB、34,151 条对白就这样静默消失。
    """
    import inspect

    src = inspect.getsource(RpgMakerAdapter.extract_text)
    assert "skipped[f.name]" in src, "跳过的文件没有记录，用户看不见"


# ---------------------------------------------------------------------------
# 五、★ 关键的"接线"测试：抽取器写好了，**必须真的被调用**
# ---------------------------------------------------------------------------


def test_dispatcher_actually_calls_scenario_extractor(tmp_path: Path) -> None:
    """★ 抽取器存在 ≠ 抽取器会被调用。

    这个 bug 的本质**不是**"缺少抽取逻辑"，而是**派发链里没有接线**：
    `_extract_scenarios` 完全可以写得完美，只要 `extract_text` 不调用它，
    34,151 条对白照样丢。

    所以用**真实目录**跑一遍 `extract_text`，验证 Scenario.json 的文本
    确实出现在最终结果里。只测 `_extract_scenarios` 是**测不到**
    这个 bug 的 —— 那正是它当初能藏住的原因。
    """
    root = tmp_path / "game"
    (root / "www" / "data").mkdir(parents=True)
    (root / "www" / "js").mkdir(parents=True)
    (root / "www" / "js" / "rmmz_core.js").write_text("// mz", encoding="utf-8")

    (root / "www" / "data" / "System.json").write_text(
        '{"gameTitle":"T","locale":"en_US","terms":{"basic":["Lv"]}}', encoding="utf-8"
    )
    (root / "www" / "data" / "MapInfos.json").write_text(
        '[null,{"id":1,"name":"Town"}]', encoding="utf-8"
    )
    # ★ 剧本文件 —— 这是必须被抽到的
    (root / "www" / "data" / "Scenario.json").write_text(
        '{"1":[{"code":101,"indent":0,"parameters":["",0,0,2,""]},'
        '{"code":401,"indent":0,"parameters":["The main story line."]}]}',
        encoding="utf-8",
    )
    # 一个**不该**被当剧本的数据定义文件
    (root / "www" / "data" / "TrpParticles.json").write_text(
        '{"1":{"alpha":{"x":1},"ADD":true}}', encoding="utf-8"
    )

    units, report = _adapter().extract_text(root)
    texts = {u.source for u in units}
    assert "The main story line." in texts, (
        f"Scenario.json 里的剧情对白没被抽到 —— 派发链没接线！实际抽到：{sorted(texts)}"
    )
    # 数据定义文件不该被当剧本
    assert "ADD" not in texts
    assert "x" not in texts


def test_scenario_is_covered_twice_on_purpose(tmp_path: Path) -> None:
    """★ 守两遍是**故意的**，不是冗余。

    实测：**删掉** `elif f.name == "Scenario.json"` 这一支之后，
    上面那条测试**照样通过** —— 因为 `else` 兜底的结构判据
    （`_looks_like_scenario_map`）认得出这个文件，把它抽了出来。

    也就是说这个文件有**两层**保护：

    1. **具名分支** —— 精确、读代码时一眼看得见；
    2. **结构兜底** —— 将来插件改名成 `Story.json`/`Scenarios2.json`
       时仍然能兜住。

    这条测试把"两层都在"钉住：如果有人为了"去重"删掉具名分支，
    第 2 层还在（不会坏）；但如果有人把 `else` 兜底也删了，
    第 2 层就没了 —— 那时只有具名分支能挡，插件改名就会再次静默丢文件。
    """
    import inspect

    src = inspect.getsource(RpgMakerAdapter.extract_text)
    assert 'f.name == "Scenario.json"' in src, "具名分支被删了"
    assert "_looks_like_scenario_map" in src, "结构兜底被删了"
