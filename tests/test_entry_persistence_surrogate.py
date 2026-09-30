"""从真实崩溃的**完整 traceback** 反推的两个结论。

`D:\\NovaLoc\\workspaces\\55fdadab74a8\\logs\\pipeline` 记下的真实栈顶：

    File "stages.py", line 416, in go
        self.ws.save_entries(list(merged.values()))
    File "workspace.py", line 311, in save_entries
        _write_jsonl(self.p("translations", "entries.jsonl"), entries)
    File "workspace.py", line 89, in _write_jsonl
        fh.write(it.model_dump_json())
    PydanticSerializationError: Error serializing to JSON:
        UnicodeEncodeError: 'utf-8' codec can't encode character '\\uddd1'

而 `entries.jsonl.tmp` 里躺着 **848 条已完成的译文**。

## 结论 1：崩溃点在**产物落盘**，不在请求发送

我第一轮的定位（httpx 的 `json=` 在发请求前编码失败）**是错的**。
真实情况是：翻译**跑完了 848 条**，然后在 `save_entries()` 里，
pydantic 的 `model_dump_json()` 无法把某个孤立代理项编成 UTF-8。

这解释了为什么代理项出现在 `position 8`：`_write_jsonl` 用
`model_dump_json()`（内部 `to_json()`，**默认 `ensure_ascii=False`**），
所以错误位置的偏移是**明文里的位置**，而不是转义序列。

`Error serializing to JSON` 这句话确实来自 pydantic，
不是 Ollama。我先前把它归因给服务端，也错了。

## 结论 2：净化必须**双端都做**，只做请求端没用

代理项是在**模型回复解析出来的那一刻**进入 `TranslationEntry` 的。
只净化请求体会让请求发得出去，但条目里已经脏了，落盘时照样炸。

所以三层都要：
1. 请求体（`ollama_client._request`）—— 已经做了；
2. 模型回复解析后、写入条目之前（本文件覆盖 `_json_safe`）；
3. **文件写入本身**（`workspace._write_jsonl`）—— 兜底，
   任何来源的脏数据都不该让整个阶段崩掉。

第 3 层尤其重要：`save_entries` 是**产物**落盘，848 条译文就在手里，
因为一个字符丢掉全部成果是不可接受的。

## 结论 3：增量落盘只解决了一半

即使有了增量落盘，如果 `_write_jsonl` 本身会抛，
那么**每一次** checkpoint 都会失败 —— 一条也存不下来。
所以"先净化再序列化"是增量落盘能起作用的前提。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.core.workspace import _write_jsonl  # noqa: E402
from novaloc.models import EntryStatus, TextKind, TranslationEntry  # noqa: E402

# ⚠️ 注意这里用 `chr(0xDDD1)`，**不能**写 `"\uddd1"` 字面量。
# 写了会有一个很讽刺的后果：**Python 读不了这个测试文件自己**。
# pytest 在收集阶段 `compile()` 源文件时就抛
#   UnicodeEncodeError: 'utf-8' codec can't encode character '\uddd1'
# —— 也就是本文件在测的那个 bug，把本文件本身弄成了无法加载的源码。
# 代理项只应存在于**运行期字符串**里，源码里必须用转义构造。
LONE = chr(0xDDD1)
LONE_HIGH = chr(0xD801)
REPLACEMENT = "\ufffd"


def _entry(target: str, uid: str = "u1") -> TranslationEntry:
    return TranslationEntry(
        uid=uid,
        source="Rending Claw",
        target=target,
        status=EntryStatus.TRANSLATED,
        kind=TextKind.ITEM_NAME,
    )


# ----------------------------------------------------------------------
# 复现真实崩溃：不净化时 pydantic 的 model_dump_json 会抛
# ----------------------------------------------------------------------

def test_unfiltered_entry_really_does_crash(tmp_path: Path) -> None:
    """先确认这个失败模式是真的（否则下面的测试就没有意义了）。

    真实栈顶是 `PydanticSerializationError`，所以这里断言那个异常类型。
    """
    from pydantic_core import PydanticSerializationError

    e = _entry(f"撕裂爪{REPLACEMENT}{LONE}击")
    with pytest.raises(PydanticSerializationError):
        e.model_dump_json()


def test_write_jsonl_survives_a_lone_surrogate(tmp_path: Path) -> None:
    """**核心回归**：`_write_jsonl` 不该因为一个字符丢掉 848 条成果。"""
    p = tmp_path / "entries.jsonl"
    entries = [_entry(f"技能{i}") for i in range(40)]
    entries.append(_entry(f"撕裂爪{LONE}击", uid="bad"))
    entries.extend(_entry(f"技能{i}", uid=f"tail{i}") for i in range(5))

    n = _write_jsonl(p, entries)
    assert n == len(entries), f"应写入 {len(entries)} 行，实际 {n}"

    # 文件必须是合法 UTF-8，且每一行都是合法 JSON
    text = p.read_text(encoding="utf-8")
    assert LONE not in text
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert len(rows) == len(entries)
    # 脏字符被替换、其余完好 —— 不能因为一条脏数据丢掉别的
    bad = next(r for r in rows if r["uid"] == "bad")
    assert bad["target"] == f"撕裂爪{REPLACEMENT}击"
    assert next(r for r in rows if r["uid"] == "u1")["target"] == "技能0"
    assert next(r for r in rows if r["uid"] == "tail4")["target"] == "技能4"


def test_write_jsonl_normal_data_untouched(tmp_path: Path) -> None:
    """干净数据必须**逐字节**和以前一样（净化不能顺手改动正常内容）。"""
    p = tmp_path / "entries.jsonl"
    entries = [_entry("正在加载..."), _entry("「攻击力提升」+8", uid="u2")]
    _write_jsonl(p, entries)
    text = p.read_text(encoding="utf-8")
    for e in entries:
        # model_dump_json 的字段顺序固定，直接比整行
        assert e.model_dump_json() in text


def test_write_jsonl_emoji_not_mangled(tmp_path: Path) -> None:
    """emoji 必须完好 —— 只净化**孤立**代理项，真 emoji 是单个码点。"""
    p = tmp_path / "entries.jsonl"
    _write_jsonl(p, [_entry("你获得了🗡和😀！")])
    row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert row["target"] == "你获得了🗡和😀！"
