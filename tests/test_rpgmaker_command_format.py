"""事件指令的**两种序列化形式**都必须能解析。

## 这个 bug（本项目最严重的一个）

一台真实 RPG Maker MZ 游戏的 `data/` 里有 **175,062** 条事件指令，
**全部是字典形式**：

.. code-block:: javascript

    {"code": 401, "indent": 1, "parameters": ["This place is perfect..."]}

而适配器里原先只有：

.. code-block:: python

    if not isinstance(cmd, list) or len(cmd) < 3:
        continue
    code = int(cmd[0]); params = cmd[2]

字典不是 list，所以 **每一条指令都被跳过**：

* **20,065 句台词**、**16,599 个说话人名**、427 组选项 —— 一个字都没翻译；
* 而流水线报的是"翻译完成 3163/3299 条"、质检通过、回写成功，
  因为被翻译的只有事件名（``'EV002'``）和数据库词条。
* 玩家打开游戏：**菜单是中文，剧情全是原文**。

报告全绿、核心功能等于没做 —— 静默失效里最严重的一类。

## 为什么之前没发现

测试夹具里的指令**全是数组形式**（那是 MV/MZ 官方编辑器保存的样子），
所以 `_norm_command` 的字典分支从来没有被走到。
和之前几次一样：**夹具比真实数据"乖"**。

## 一个额外的陷阱

别用 `len(cmd) >= 3` 判断数组形式：字典的 `len()` 是**键的个数**，
``{"code":401,"indent":1,"parameters":[...]}`` 恰好是 3，
会误判成合法数组，然后 `cmd[0]` 抛 `KeyError`。
所以 `_norm_command` 先判 `isinstance(cmd, dict)`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines import get_adapter  # noqa: E402
from novaloc.engines.rpgmaker import RpgMakerAdapter  # noqa: E402


def _adapter() -> RpgMakerAdapter:
    ctx = Context(config=Config(), events=EventBus())
    return get_adapter("rpgmaker", ctx)


# ----------------------------------------------------------------------
# 一、归一化：两种形式都要认
# ----------------------------------------------------------------------

def test_norm_command_array_form() -> None:
    """数组形式（MV/MZ 官方编辑器）：``[code, indent, [params]]``。"""
    ad = _adapter()
    code, params, pkey = ad._norm_command([401, 0, ["你好"]])
    assert code == 401
    assert params == ["你好"]
    assert pkey == "2", "数组形式的参数下标固定是 2"


def test_norm_command_dict_form() -> None:
    """字典形式（真实游戏用的就是这个）。"""
    ad = _adapter()
    code, params, pkey = ad._norm_command(
        {"code": 401, "indent": 1, "parameters": ["This place is perfect"]}
    )
    assert code == 401
    assert params == ["This place is perfect"]
    assert pkey == "parameters", "字典形式的参数是具名键，指针必须用键名"


def test_norm_command_rejects_garbage() -> None:
    ad = _adapter()
    for bad in [None, "x", 42, [], [1], [1, 2], {}, {"code": "zz"}, {"indent": 1}]:
        code, _params, _pkey = ad._norm_command(bad)
        assert code is None, f"{bad!r} 不该被当成合法指令"


def test_dict_len_three_must_not_be_treated_as_array() -> None:
    """**回归**：字典的 ``len()`` 是键数，恰好是 3。

    旧代码用 ``len(cmd) >= 3`` 判断数组形式 —— 字典会通过这个检查，
    然后 ``cmd[0]`` 抛 ``KeyError``。所以必须先判 ``isinstance(cmd, dict)``。
    """
    ad = _adapter()
    d = {"code": 401, "indent": 1, "parameters": ["x"]}
    assert len(d) == 3, "这条测试的前提：字典恰好有 3 个键"
    code, params, pkey = ad._norm_command(d)
    assert code == 401 and params == ["x"] and pkey == "parameters"


# ----------------------------------------------------------------------
# 二、提取：台词 / 说话人 / 选项
# ----------------------------------------------------------------------

def _holder(cmds: list) -> dict:
    return {"list": cmds}


def test_dialogue_extracted_from_dict_commands() -> None:
    """**核心回归**：字典形式的 401 台词必须被提取出来。

    ★ 注意连续的 401 属于**同一段对白**，会合成一条 `TextUnit`
    （`source` 用 ``\\n`` 连接），其余位置记在 `location.siblings`。
    见 `test_consecutive_401_are_merged_into_one_unit`。
    """
    ad = _adapter()
    out = ad._commands_text(
        _holder([
            {"code": 401, "indent": 1, "parameters": ["This place is perfect"]},
            {"code": 401, "indent": 1, "parameters": ["I'll note it down"]},
        ]),
        "Map028.json",
        "/events/2/pages/0/list",
    )
    assert len(out) == 1
    assert str(out[0].source) == "This place is perfect\nI'll note it down"
    assert out[0].location.pointer == "/events/2/pages/0/list/0/parameters/0"
    assert out[0].location.siblings == ["/events/2/pages/0/list/1/parameters/0"]


def test_consecutive_401_are_merged_into_one_unit() -> None:
    r"""★ 连续 `401` = 同一段对白被引擎按显示宽度拆开，必须合成一条翻译。

    ## 真实事故

    MV 会把一句话拆成多条 401：

        [39] code=401  "…suffering of the slaves in the "
        [40] code=401  "Kingdom of Bohelos, where they are treated…"

    逐条翻译时模型只看到 `"…slaves in the "` 这种半句话。
    实测这个游戏 23858 个 401 组里有 **11452 组（48%）** 是拆开的。

    ## 断言什么

    合成**一条**、`source` 用 ``\\n`` 连、其余位置进 ``siblings``
    （回写时要按行拆回去，否则消息框只显示第一行）。
    """
    ad = _adapter()
    out = ad._commands_text(
        _holder([
            {"code": 401, "indent": 1, "parameters": ["A magic device displays the "]},
            {"code": 401, "indent": 1, "parameters": ["suffering of the slaves."]},
        ]),
        "Scenario.json",
        "/2/list",
    )
    assert len(out) == 1, f"连续 401 没被合成一条：{[str(u.source) for u in out]}"
    assert str(out[0].source) == (
        "A magic device displays the \nsuffering of the slaves."
    )
    assert out[0].location.siblings == ["/2/list/1/parameters/0"]


def test_non_401_command_breaks_the_dialogue_group() -> None:
    """★ 中间插入别的指令就必须断开 —— 否则两段不相关的对白会被接在一起。

    题外话：这本来不是"别的指令"的问题，而是**两个角色各自说话**。
    合错了会让 A 的台词和 B 的台词混成一句。
    """
    ad = _adapter()
    out = ad._commands_text(
        _holder([
            {"code": 401, "indent": 1, "parameters": ["First line."]},
            {"code": 101, "indent": 1, "parameters": ["Face", 0, 0, 0, "Naho"]},
            {"code": 401, "indent": 1, "parameters": ["Second line."]},
        ]),
        "Map001.json",
        "/events/0/pages/0/list",
    )
    texts = [str(u.source) for u in out]
    assert "First line." in texts and "Second line." in texts, texts
    assert not any("\n" in t for t in texts if t not in ("First line.", "Second line."))


def test_dialogue_extracted_from_array_commands() -> None:
    """数组形式不能被这次改动弄坏。"""
    ad = _adapter()
    out = ad._commands_text(
        _holder([[401, 0, ["你好"]], [405, 0, ["世界"]]]),
        "Map001.json",
        "/events/0/pages/0/list",
    )
    assert len(out) == 1
    assert str(out[0].source) == "你好\n世界"
    assert out[0].location.pointer == "/events/0/pages/0/list/0/2/0"
    assert out[0].location.siblings == ["/events/0/pages/0/list/1/2/0"]


def test_speaker_name_extracted_from_dict_commands() -> None:
    """101 的 ``params[4]`` 是说话人名（真实数据里 16,599 个）。"""
    ad = _adapter()
    out = ad._commands_text(
        _holder([{"code": 101, "indent": 1, "parameters": ["Face", 0, 0, 0, "Naho"]}]),
        "Map028.json",
        "/events/8/pages/0/list",
    )
    assert len(out) == 1
    assert str(out[0].source) == "Naho"
    assert out[0].location.pointer == "/events/8/pages/0/list/0/parameters/4"


def test_choices_extracted_from_dict_commands() -> None:
    """102 的 params 整体是候选列表，最后一项是取消索引（数字，要跳过）。"""
    ad = _adapter()
    out = ad._commands_text(
        _holder([{"code": 102, "indent": 0, "parameters": ["Yes", "No", 2]}]),
        "Map028.json",
        "/events/1/pages/0/list",
    )
    assert [str(u.source) for u in out] == ["Yes", "No"]
    assert out[0].location.pointer == "/events/1/pages/0/list/0/parameters/0"


def test_script_commands_are_never_translated() -> None:
    """355/655 是 JS 代码和插件参数，翻了必然破坏游戏逻辑。

    真实数据里 355/655 有 **13,103** 条 —— 这条闸门极其重要。
    """
    ad = _adapter()
    out = ad._commands_text(
        _holder([
            {"code": 355, "indent": 1, "parameters": ["$gameVariables.setValue(1, 2)"]},
            {"code": 655, "indent": 1, "parameters": ["Picture ID = 1"]},
            {"code": 657, "indent": 1, "parameters": ["plugin param"]},
            {"code": 108, "indent": 1, "parameters": ["comment"]},
        ]),
        "Map028.json",
        "/events/1/pages/0/list",
    )
    assert out == [], f"脚本/注释类指令被翻译了：{[str(u.source) for u in out]}"


def test_mixed_formats_in_one_list() -> None:
    """同一个 list 里混着两种形式也要都能处理。

    这里两种形式**都是 401**，所以按"同段对白"合成一条；
    关键是两片的指针格式各自正确（字典用 `parameters`，数组用 `2`）。
    """
    ad = _adapter()
    out = ad._commands_text(
        _holder([
            {"code": 401, "indent": 1, "parameters": ["dict 台词"]},
            [401, 0, ["array 台词"]],
        ]),
        "Map001.json",
        "/events/0/pages/0/list",
    )
    assert [str(u.source) for u in out] == ["dict 台词\narray 台词"]
    assert out[0].location.pointer == "/events/0/pages/0/list/0/parameters/0"
    # 数组形式的参数下标是 "2"，且块下标是 1（第二条）
    assert out[0].location.siblings == ["/events/0/pages/0/list/1/2/0"]


# ----------------------------------------------------------------------
# 二b、把合成后的译文拆回各显示槽位
# ----------------------------------------------------------------------


def test_split_uses_newlines_when_counts_match() -> None:
    """★ 行数正好等于槽位数时按行拆 —— 这是最常见的情况。"""
    from novaloc.engines.rpgmaker import _split_across_slots

    assert _split_across_slots("第一行\n第二行", 2) == ["第一行", "第二行"]


def test_split_merges_extra_lines_into_the_last_slot() -> None:
    """★ 行数多于槽位时把多出来的并到最后 —— **绝不能丢字**。"""
    from novaloc.engines.rpgmaker import _split_across_slots

    got = _split_across_slots("一\n二\n三", 2)
    assert got[0] == "一"
    assert "二" in got[1] and "三" in got[1], got
    assert "".join(got).replace("\n", "") == "一二三"


def test_split_divides_evenly_when_too_few_lines() -> None:
    """★ 行数少于槽位时按字符均分，且**不丢字**。"""
    from novaloc.engines.rpgmaker import _split_across_slots

    got = _split_across_slots("一二三四五六", 3)
    assert len(got) == 3
    assert "".join(got) == "一二三四五六", got


def test_split_never_loses_characters() -> None:
    """★ 不变量：无论行数多少，拼起来必须等于原文。

    `n == 1` 是**唯一**的例外：只有一格时原样返回（连换行也不动），
    因为那一格就是消息框全文，换行是玩家看到的换行。
    多格时换行会被**吃掉** —— MV 把参数原样显示，残留的 `\\n`
    会在消息框里变成真的换行、把版面撑坏。
    """
    from novaloc.engines.rpgmaker import _split_across_slots

    for text in ("a", "a\nb", "a\nb\nc\nd", "一二\n三四五\n六", "no newline here"):
        for n in (2, 3, 5):
            got = _split_across_slots(text, n)
            assert len(got) == n, (text, n, got)
            assert "".join(got) == text.replace("\n", ""), (text, n, got)
        # n == 1：原样返回
        assert _split_across_slots(text, 1) == [text]


def test_split_prefers_the_models_own_newlines_over_even_division() -> None:
    """★ 译文自带换行时**必须**按它拆，不能吃掉换行再去均分。

    ## 这是真实缺陷，实测 DemonsRoots 命中 330 条

    模型看到 `\\n` 会自然保留，所以"源文 2 行"的 unit 常常给回
    **自带换行**的译文。老代码把换行吃掉、再按字符数均分：

        target = '移民：\\n只返回 JSON：…'   n = 2
        老结果 = ['移民：', '只返回 JSON：…']   ← 看着像巧合，其实是硬切

    看起来"对"只是因为那一条恰好切对了。真正的证据是比例悬殊的源文：

        source = 'Macho Gorilla:\\nKalinka... I will knock you flat!'
        target = '大猩猩：\\n只有你能做到！'   n = 3

    这时按**字符数**均分 5 个字的译文到 3 格，会得到
    `['大猩', '猩：只有', '你能做到！']` —— 把"大猩猩"从中间劈开。
    按译文的换行拆才对：`['大猩猩：', '只有你能做到！', '']`。
    """
    from novaloc.engines.rpgmaker import _split_across_slots

    # 模型保留了换行：直接用它的结构，字数悬殊也不管
    got = _split_across_slots(
        "大猩猩：\n只有你能做到！", 3,
        "Macho Gorilla:\nKalinka... I will knock you flat!",
    )
    assert got == ["大猩猩：", "只有你能做到！", ""], got
    assert "".join(got) == "大猩猩：只有你能做到！"

    # 不能出现"把一个词从中间劈开"的碎片
    assert not any(len(p) == 2 and p.startswith("大猩") for p in got)


def test_split_uses_source_line_lengths_when_translation_is_one_line() -> None:
    """译文只有一整行时才需要自己找切点 —— 这时按源文各行的长度比例。"""
    from novaloc.engines.rpgmaker import _split_across_slots

    # 源文第二行远长于第一行 ⇒ 译文也该把长的那段放第二格
    got = _split_across_slots(
        "短长句子在这里很长很长", 2,
        "short\na very much longer second line indeed",
    )
    assert len(got) == 2
    assert "".join(got) == "短长句子在这里很长很长"
    assert len(got[1]) > len(got[0]), got

    # 没有 source 信息时退回均分（不崩、不丢字）
    got2 = _split_across_slots("一二三四五六", 3)
    assert "".join(got2) == "一二三四五六"


def test_split_never_emits_an_empty_middle_slot() -> None:
    """比例分配**不许**在字符够分时留下空槽位。

    空槽位在游戏里就是一个空消息行。注意字符**不够**分时（正文 1 个字、
    槽位 2 个）空片是必然的，所以只对 `len(body) >= n` 断言。
    """
    from novaloc.engines.rpgmaker import _split_across_slots

    for body, src, n in (
        ("一二", "aaaaaaaaaaaaaaaaaaaa\nb", 2),
        ("一二三", "a\nbbbbbbbbbbbbbbbb\nc", 3),
        ("一二三四五六", "aaaaaaaaaaaa\nb", 2),
    ):
        got = _split_across_slots(body, n, src)
        assert len(got) == n, got
        assert all(got), f"出现空槽位：{got}"
        assert "".join(got) == body

    # 字符不够分：空片是必然的，但仍不许丢字
    got = _split_across_slots("一", 2, "a\nbbbbbbbbbbbbbbbbbbbb")
    assert "".join(got) == "一"


def test_single_slot_returns_text_unchanged() -> None:
    """只有一格时原样返回（不要自作聪明去拆）。"""
    from novaloc.engines.rpgmaker import _split_across_slots
    assert _split_across_slots("完整一句", 1) == ["完整一句"]


# ----------------------------------------------------------------------
# 二c、回写：合成后的译文必须**真的**落到每一个槽位
# ----------------------------------------------------------------------


def _game_with_scenario(tmp_path: Path, cmds: list) -> Path:
    """造一个最小的 MV 游戏目录（只要有 www/data/Scenario.json）。"""
    game = tmp_path / "game"
    (game / "www" / "data").mkdir(parents=True)
    (game / "www" / "data" / "Scenario.json").write_text(
        json.dumps({"1": cmds}, ensure_ascii=False), encoding="utf-8"
    )
    return game


def _apply_and_read(game: Path, tmp_path: Path) -> list:
    """跑一次 apply，读回写后的 Scenario.json 命令列表。"""
    ad = _adapter()
    units, _rep = ad.extract_text(game)
    assert units, "没抽出条目，测试夹具本身有问题"
    # 说话人名等"不该被动"的位置，假译文原样返回原文 ——
    # 这样"回写动了它们"就一定会被断言抓到。
    tr = {
        u.uid: _FAKE_TRANSLATION.get(
            u.source,
            u.source if u.kind.value == "character_name" else f"【译】{u.source}",
        )
        for u in units
    }
    res = ad.apply(game, tmp_path / "out", units, tr)
    assert res.ok, res.error
    assert res.files_written == 1, f"回写文件数应为 1，实际 {res.files_written}"
    obj = json.loads(
        (tmp_path / "out" / "www" / "data" / "Scenario.json").read_text(encoding="utf-8")
    )
    return obj["1"]


_FAKE_TRANSLATION = {
    "A magic device displays the \nsuffering of the slaves.": "一个魔法装置播放着\n奴隶们受苦的影像。",
}


def test_apply_writes_split_translation_to_every_slot(tmp_path: Path) -> None:
    r"""★ 回写的**核心不变量**：合并后的译文要拆回每一个 401 槽位。

    这是整个改动里风险最高的一步。如果只写了第一格，玩家会看到：

        一个魔法装置播放着        ← 中文
        suffering of the slaves.  ← 原文（没被替换）

    比逐条翻译还糟（原来是两段中文，现在是一中一英）。
    """
    cmds = [
        {"code": 401, "indent": 0, "parameters": ["A magic device displays the "]},
        {"code": 401, "indent": 0, "parameters": ["suffering of the slaves."]},
    ]
    game = _game_with_scenario(tmp_path, cmds)
    out_cmds = _apply_and_read(game, tmp_path)
    got = [c["parameters"][0] for c in out_cmds]
    assert got == ["一个魔法装置播放着", "奴隶们受苦的影像。"], got
    # 原文一个字都不能留
    assert "suffering" not in "".join(got), got


def test_apply_never_duplicates_text_across_slots(tmp_path: Path) -> None:
    r"""★ 反向：不能把整段译文往每一格都写一遍。

    那会让消息框显示两遍同样的话。这个失败方式很隐蔽 ——
    "每个槽位都有中文"看起来是成功的。
    """
    cmds = [
        {"code": 401, "indent": 0, "parameters": ["A magic device displays the "]},
        {"code": 401, "indent": 0, "parameters": ["suffering of the slaves."]},
    ]
    game = _game_with_scenario(tmp_path, cmds)
    out_cmds = _apply_and_read(game, tmp_path)
    got = [c["parameters"][0] for c in out_cmds]
    joined = "".join(got)
    assert joined.count("一个魔法装置播放着") == 1, f"译文被写了两遍：{got}"
    assert joined.count("奴隶们受苦的影像。") == 1, f"译文被写了两遍：{got}"


def test_apply_keeps_commands_it_does_not_touch(tmp_path: Path) -> None:
    """★ 回写不能动别的指令（插件指令、脚本、分支……）。"""
    cmds = [
        {"code": 356, "indent": 0, "parameters": ["Tachie showName"]},
        {"code": 401, "indent": 0, "parameters": ["A magic device displays the "]},
        {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2, "Naho"]},
        {"code": 355, "indent": 0, "parameters": ["var x = 1;"]},
        {"code": 401, "indent": 0, "parameters": ["suffering of the slaves."]},
    ]
    game = _game_with_scenario(tmp_path, cmds)
    out_cmds = _apply_and_read(game, tmp_path)
    assert out_cmds[0] == cmds[0], "插件指令被改了"
    assert out_cmds[3] == cmds[3], "脚本被改了"
    assert out_cmds[2]["parameters"][4] == "Naho", "说话人名被改了"


def test_apply_leaves_untracked_slots_empty(tmp_path: Path) -> None:
    r"""★ 槽位比译文行数多时，多余的格子写空串而不是重复内容。

    宁可显示的短一点，也不要同一句话显示两遍。
    """
    cmds = [
        {"code": 401, "indent": 0, "parameters": ["One."]},
        {"code": 401, "indent": 0, "parameters": ["Two."]},
        {"code": 401, "indent": 0, "parameters": ["Three."]},
    ]
    game = _game_with_scenario(tmp_path, cmds)
    ad = _adapter()
    units, _rep = ad.extract_text(game)
    # 给一条**没有换行**的译文：行数(1) < 槽位数(3)，会走均分分支
    tr = {u.uid: "一二三四五六" for u in units}
    res = ad.apply(game, tmp_path / "out", units, tr)
    assert res.ok, res.error
    obj = json.loads(
        (tmp_path / "out" / "www" / "data" / "Scenario.json").read_text(encoding="utf-8")
    )
    got = [c["parameters"][0] for c in obj["1"]]
    assert "".join(got) == "一二三四五六", f"均分丢了字：{got}"
    assert all("\n" not in g for g in got), f"均分后残留换行：{got}"


# ----------------------------------------------------------------------
# 三、回写：字典形式的指针必须能定位
# ----------------------------------------------------------------------

def test_set_pointer_writes_dict_command_dialogue() -> None:
    """**端到端契约**：提取出来的指针必须能被 `_set_pointer` 写回去。"""
    ad = _adapter()
    obj = {"events": [{"pages": [{"list": [
        {"code": 401, "indent": 1, "parameters": ["original"]},
    ]}]}]}
    assert ad._set_pointer(obj, "/events/0/pages/0/list/0/parameters/0", "译文") is True
    assert obj["events"][0]["pages"][0]["list"][0]["parameters"][0] == "译文"
    # 其它字段一个都不能动
    assert obj["events"][0]["pages"][0]["list"][0]["code"] == 401
    assert obj["events"][0]["pages"][0]["list"][0]["indent"] == 1


def test_set_pointer_writes_array_command_dialogue() -> None:
    ad = _adapter()
    obj = {"list": [[401, 0, ["original"]]]}
    assert ad._set_pointer(obj, "/list/0/2/0", "译文") is True
    assert obj["list"][0][2][0] == "译文"


def test_extract_then_write_roundtrip() -> None:
    """提取 → 回写 → 读回，值要一致，且结构不变。"""
    ad = _adapter()
    obj = {"events": [{"pages": [{"list": [
        {"code": 401, "indent": 1, "parameters": ["Hello there"]},
    ]}]}]}
    units = ad._commands_text(
        obj["events"][0]["pages"][0], "Map001.json", "/events/0/pages/0/list"
    )
    assert len(units) == 1
    ptr = units[0].location.pointer
    assert ad._set_pointer(obj, ptr, "你好") is True
    assert obj["events"][0]["pages"][0]["list"][0]["parameters"][0] == "你好"


def test_real_game_data_has_dict_commands() -> None:
    """拿**真实游戏**确认这个格式确实存在（有数据就校验，没有就跳过）。

    这条是防止"夹具退化"再次发生：如果哪天夹具又全是数组形式，
    上面那些字典用例仍会通过，但真实数据可能已经变了。
    """
    game = Path(r"D:\NovaLocData\real\BeyondPortal")
    maps = sorted((game / "data").glob("Map*.json"))
    if not maps:
        pytest.skip("本机没有真实游戏副本")

    dict_cmds = array_cmds = 0
    for p in maps[:12]:
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001, S112
            continue
        # 注意：`MapInfos.json` 是**数组**，不是字典 —— 直接 .get 会 AttributeError
        if not isinstance(d, dict):
            continue
        for ev in d.get("events") or []:
            if not isinstance(ev, dict):
                continue
            for pg in (ev.get("pages") or []):
                if not isinstance(pg, dict):
                    continue
                for c in (pg.get("list") or []):
                    if isinstance(c, dict) and "code" in c:
                        dict_cmds += 1
                    elif isinstance(c, list):
                        array_cmds += 1
    if dict_cmds == 0 and array_cmds == 0:
        pytest.skip("真实游戏里没有事件指令")
    assert dict_cmds > 0, (
        f"真实游戏里全是数组形式（dict=0, array={array_cmds}）—— "
        "这个格式可能变了，请重新取样"
    )


def test_real_game_dialogue_is_extracted() -> None:
    """**最重要的端到端断言**：真实游戏的台词必须真的被提取出来。

    修复前这里是 0 条（只有事件名），修复后 Map028 一个文件就有 1483 句。
    """
    game = Path(r"D:\NovaLocData\real\BeyondPortal")
    if not (game / "data" / "Map028.json").is_file():
        pytest.skip("本机没有真实游戏副本")
    ad = _adapter()
    units, _rep = ad.extract_text(game)
    kinds = [str(u.kind) for u in units]
    dialogue = kinds.count("TextKind.DIALOGUE")
    assert dialogue > 1000, (
        f"只提取到 {dialogue} 句台词 —— 真实游戏有约 19000 句；"
        "指令格式解析很可能又退化了"
    )


# ----------------------------------------------------------------------
# 四、指针必须真的能写回去（否则回写"成功 0 个文件"）
# ----------------------------------------------------------------------


def test_scenario_pointer_matches_real_structure() -> None:
    r"""★ 提取出的指针必须能**在不改任何东西的前提下**定位到原文。

    这是 `Scenario.json` 那个 `/list/` bug 的回归测试。真实结构是：

    .. code-block:: javascript

        {"1": [ {"code": 401, ...}, ... ]}      // 指令直接在数组里

    所以指针必须是 `/1/0/parameters/0`。一旦又写成
    `/1/list/0/parameters/0`，`_set_pointer` 会找不到 `list` 键 ⇒
    **几万条剧情对白一条都写不回去**，而回写阶段只报"写了 0 个文件"。

    断言方式：把每个指针**读**一遍（用 `_set_pointer` 写回原值），
    全部必须成功。这比检查字符串前缀更强 —— 它真的走了一遍定位逻辑。
    """
    obj = {
        "1": [
            {"code": 401, "indent": 0, "parameters": ["First line."]},
            {"code": 356, "indent": 0, "parameters": ["Tachie showName"]},
            {"code": 401, "indent": 0, "parameters": ["Second line."]},
        ]
    }
    ad = _adapter()
    units = ad._extract_scenarios(obj, "Scenario.json")
    assert units, "没抽出条目"
    for u in units:
        ptrs = [u.location.pointer, *u.location.siblings]
        for ptr in ptrs:
            segs = [s for s in ptr.split("/") if s]
            assert "list" not in segs, (
                f"指针里不该有 `list` 这一层（真实结构没有该键）：{ptr}"
            )
            assert segs[0] == "1", f"指针第一段应该是场景名：{ptr}"
            # 真的走一遍定位：把原值写回去，必须成功
            assert ad._set_pointer(obj, ptr, "PROBE"), (
                f"指针定位失败：{ptr} —— 回写会静默丢掉这条译文"
            )


def test_apply_hard_fails_when_nothing_can_be_written(tmp_path: Path) -> None:
    r"""★ 硬闸门：全部定位失败时**必须报错**，不能报"成功、写了 0 个文件"。

    ## 为什么值得单独一条闸门

    `files_written` 只在 `n > 0` 时自增，所以"一条都没写进去"和
    "没有需要写的东西"在结果对象上**长得一模一样**。而后者是正常的、
    前者是灾难性的（游戏里剧情全是原文）。

    这个失败方式在本项目里真实发生过（`Scenario.json` 的 `/list/` bug），
    而且流水线报的是完成。所以这里必须有一条**显式**判据把它区分开。
    """
    game = _game_with_scenario(
        tmp_path,
        [{"code": 401, "indent": 0, "parameters": ["Hello world."]}],
    )
    ad = _adapter()
    units, _rep = ad.extract_text(game)
    assert units
    # 人为把指针弄坏，模拟"指针与结构不匹配"
    units[0].location.pointer = "/1/list/0/parameters/0"
    res = ad.apply(game, tmp_path / "out", units, {units[0].uid: "你好世界。"})
    assert not res.ok, "全部定位失败却报了成功"
    assert res.error and "回写全部失败" in res.error, res.error
    assert res.files_written == 0


def test_apply_hard_fails_on_massive_partial_skip(tmp_path: Path) -> None:
    r"""★ 硬闸门：**大面积部分失败**也必须报错。

    ## 这是真实事故，不是假想

    上一条闸门只拦"整个文件一条都写不进去"。但 DemonsRoots 真实发生的是
    **部分**失败：`Scenario.json` 的 23159 条指针多了一层 `list`
    （`units.jsonl` 是修 bug 之前抽的、已经过时），于是

    * 剧情对白几乎全部静默丢掉，游戏里留的是**原文**；
    * 同时其它数据文件写得好好的 ⇒ `files_written = 644`；
    * `files_all_failed` 里**没有** `Scenario.json`（它写进去过一条）
      ⇒ 上面那条闸门不响；
    * 阶段报 ✅ 完成、`全部完成。可玩目录：…`。

    用户拿到"菜单是中文、剧情全是原文"的游戏，**所有数字都显示成功**。
    所以判据必须落到**槽位**一级。
    """
    # 连续的 401 会被合并成一条 unit，所以每条之间插一条非 401 指令
    cmds: list = []
    for i in range(200):
        cmds.append({"code": 401, "indent": 0, "parameters": [f"Line {i}."]})
        cmds.append({"code": 101, "indent": 0, "parameters": [""]})
    game = _game_with_scenario(tmp_path, cmds)
    ad = _adapter()
    units, _rep = ad.extract_text(game)
    assert len(units) == 200, len(units)
    tr = {u.uid: f"第{i}行。" for i, u in enumerate(units)}

    # 让**绝大多数**指针坏掉，但留 1 条好的 —— 这样文件仍会被写、
    # `files_all_failed` 不触发，只有槽位级闸门能拦住。
    for u in units[:-1]:
        u.location.pointer = "/1/list/" + u.location.pointer.lstrip("/")
        u.location.siblings = []

    res = ad.apply(game, tmp_path / "out", units, tr)
    assert not res.ok, (
        f"199/200 个位置定位失败却报了成功（写了 {res.files_written} 个文件）"
    )
    assert res.error and "大面积失败" in res.error, res.error
    assert "199/200" in res.error, res.error


def test_apply_does_not_hard_fail_on_a_few_missing_pointers(tmp_path: Path) -> None:
    r"""★ 反向：偶发个别失败**不该**一票否决。

    真实工程里源文件被手改过、某个事件结构特殊都可能让极少数指针失效。
    1% 的阈值就是为了让这种情况照常产出，同时拦住结构性错配。
    """
    cmds: list = []
    for i in range(200):
        cmds.append({"code": 401, "indent": 0, "parameters": [f"Line {i}."]})
        cmds.append({"code": 101, "indent": 0, "parameters": [""]})
    game = _game_with_scenario(tmp_path, cmds)
    ad = _adapter()
    units, _rep = ad.extract_text(game)
    tr = {u.uid: f"第{i}行。" for i, u in enumerate(units)}

    # 只坏 1 条（0.5% < 1%）
    units[0].location.pointer = "/1/list/" + units[0].location.pointer.lstrip("/")
    units[0].location.siblings = []

    res = ad.apply(game, tmp_path / "out", units, tr)
    assert res.ok, f"0.5% 的偶发失败被误判成灾难：{res.error}"
    assert res.files_written == 1


def test_apply_does_not_hard_fail_on_empty_translations(tmp_path: Path) -> None:
    r"""★ 反向：**没有**译文要写时不算失败（不要误判）。

    空项目 / 空译文是正常情况，报错会让用户以为坏了。
    """
    game = _game_with_scenario(
        tmp_path,
        [{"code": 401, "indent": 0, "parameters": ["Hello world."]}],
    )
    ad = _adapter()
    units, _rep = ad.extract_text(game)
    res = ad.apply(game, tmp_path / "out", units, {})
    assert res.ok, f"空译文被误判成失败：{res.error}"


def test_every_real_pointer_resolves() -> None:
    r"""★ **最重要的一条端到端断言**：真实游戏里每一个指针都能定位到原文。

    这条比"某几条能写"强得多：它把**所有**抽取路径（Scenario 的裸数组、
    Map 的 page.list、CommonEvents 的元素、数据库词条…）一次性覆盖。
    实测真实 MV 游戏 27642 个指针全部通过；修 `/list/` 那个 bug 之前，
    单 `Scenario.json` 一个文件就有 23159 个全部失败。

    没有本机游戏副本时跳过（CI 上没有）。
    """
    game = Path(r"D:\NovaLocData\real\DemonsRoots")
    if not (game / "www" / "data" / "Scenario.json").is_file():
        pytest.skip("本机没有真实游戏副本")
    ad = _adapter()
    units, _rep = ad.extract_text(game)
    assert len(units) > 20000, f"只抽到 {len(units)} 条 —— 抽取很可能退化了"

    data = game / "www" / "data"
    checked = bad = 0
    failures: list[str] = []
    cache: dict[str, Any] = {}
    for u in units:
        fn = u.location.file
        # 加密/非 JSON 资源不适合这条判据，只查能解析的 JSON
        if not fn.endswith(".json"):
            continue
        if fn not in cache:
            try:
                cache[fn] = json.loads(
                    (data / fn).read_text(encoding="utf-8-sig")
                )
            except Exception:  # noqa: BLE001
                cache[fn] = None
        obj = cache[fn]
        if obj is None:
            continue
        checked += 1
        if not ad._set_pointer(obj, u.location.pointer, "PROBE"):
            bad += 1
            if len(failures) < 5:
                failures.append(f"{fn}{u.location.pointer}")

    assert checked > 20000, f"只检查了 {checked} 个指针"
    assert bad == 0, (
        f"{bad}/{checked} 个指针定位失败（回写会静默丢掉这些译文）：{failures}"
    )
