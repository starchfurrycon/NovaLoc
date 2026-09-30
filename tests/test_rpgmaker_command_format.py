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
    """**核心回归**：字典形式的 401 台词必须被提取出来。"""
    ad = _adapter()
    out = ad._commands_text(
        _holder([
            {"code": 401, "indent": 1, "parameters": ["This place is perfect"]},
            {"code": 401, "indent": 1, "parameters": ["I'll note it down"]},
        ]),
        "Map028.json",
        "/events/2/pages/0/list",
    )
    assert len(out) == 2
    assert str(out[0].source) == "This place is perfect"
    assert out[0].location.pointer == "/events/2/pages/0/list/0/parameters/0"
    assert out[1].location.pointer == "/events/2/pages/0/list/1/parameters/0"


def test_dialogue_extracted_from_array_commands() -> None:
    """数组形式不能被这次改动弄坏。"""
    ad = _adapter()
    out = ad._commands_text(
        _holder([[401, 0, ["你好"]], [405, 0, ["世界"]]]),
        "Map001.json",
        "/events/0/pages/0/list",
    )
    assert len(out) == 2
    assert out[0].location.pointer == "/events/0/pages/0/list/0/2/0"


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
    """同一个 list 里混着两种形式也要都能处理。"""
    ad = _adapter()
    out = ad._commands_text(
        _holder([
            {"code": 401, "indent": 1, "parameters": ["dict 台词"]},
            [401, 0, ["array 台词"]],
        ]),
        "Map001.json",
        "/events/0/pages/0/list",
    )
    assert [str(u.source) for u in out] == ["dict 台词", "array 台词"]
    assert out[0].location.pointer == "/events/0/pages/0/list/0/parameters/0"
    # 数组形式的参数下标是 "2"，且块下标是 1（第二条）
    assert out[1].location.pointer == "/events/0/pages/0/list/1/2/0"


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
