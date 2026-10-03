r"""★ 反复失败的条目必须**停下来** —— `FAILED` 否则会被每轮重排进待办。

## 现场

`stage_translate` 的跳过判据是

    prev is not None and prev.is_done and prev.target.strip()

而 `is_done` 只认 `TRANSLATED / REVIEWED / LOCKED`。
`FAILED` **不满足** ⇒ 每跑一次 `translate` 就重试一次，**永远如此**。

实测失败原因分布里，几百条是**模型真的做不到**的长插件标签
（`<SG説明:…\I[34]…>` 这种把插件元数据拼进文本的）：模型对它们一律回
`'确认'`、或把整段标签吞掉。**重试一万次也是同一个结果。**

全库 174 个游戏、单条约 20~60 秒 —— 这些条目会一直吃掉时间，
而"后续新加游戏"的守望服务还在等**同一个**热模型
（两个进程并行实测会让 `POST /api/chat` 大面积 300 秒超时）。

## 为什么不能用 `entry.retries`

它看着像是现成的计数器，但 provider 在每次重试时把它**重置**成 0/1
（`ollama_provider.py` 的 `entry.retries = 0` / `= 1`）。
实测 1,140 条失败条目里 **全是 0** —— 根本累积不起来。
所以计数放在会落盘的 `meta["fail_streak"]`。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STAGES = ROOT / "src" / "novaloc" / "pipeline" / "stages.py"


def _code_without_comments() -> str:
    """源码去掉注释与文档字符串（避免断言命中注释里的示例）。"""
    tree = ast.parse(STAGES.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                node.value.value = ""
    return ast.unparse(tree)


def test_max_fail_streak_is_defined() -> None:
    from novaloc.pipeline.stages import _MAX_FAIL_STREAK

    assert _MAX_FAIL_STREAK >= 1, "必须至少给一次重试机会（provider_error 会自愈）"
    assert _MAX_FAIL_STREAK <= 5, f"{_MAX_FAIL_STREAK} 次太多，等于没限制"


def test_failed_entries_are_skipped_once_streak_reached() -> None:
    r"""源码级守卫：`FAILED` + 计数达上限 ⇒ 必须 `continue`（不排进待办）。

    这条特意做成**结构断言**而不是行为测试：要真跑一遍 `stage_translate`
    需要模型、工作区、配置三件套，成本高且容易因环境失败。
    而这里要守的正是"**这段代码还在不在**"——
    它一旦被删掉，症状是"又变慢了"，不会有任何报错。
    """
    code = _code_without_comments()
    assert "fail_streak" in code, "读不到 fail_streak —— 上限判据被删掉了？"
    assert "_MAX_FAIL_STREAK" in code, "读不到 _MAX_FAIL_STREAK"
    # `FAILED` 状态必须参与判定（否则普通条目会被误跳过）
    assert "EntryStatus.FAILED" in code


def test_streak_is_incremented_on_failure() -> None:
    r"""★ 计数必须**累积**，而不是每次重写成 1。

    ## 这个坑差点又踩一次

    第一版想在合并（`{**existing, **entries}`）**之后**读旧值：

        merged = {**existing, **{e.uid: e for e in entries}}
        for e in entries:
            prior = merged[e.uid].meta["fail_streak"]   # ← 已经是**新**对象了

    合并之后旧 `meta` 已被覆盖，`prior` 永远读到本轮自己的值
    ⇒ 计数永远是 1 ⇒ **上限判据永远不触发**，看起来一切正常。

    所以必须在合并**之前**从 `existing` 读旧计数。
    """
    src = STAGES.read_text(encoding="utf-8")
    # 计数块必须出现在合并块之前
    i_streak = src.find('"fail_streak": prior + 1')
    i_merge = src.find("merged = {**existing, **{e.uid: e for e in entries}}")
    assert i_streak != -1, "找不到 fail_streak 的自增"
    assert i_merge != -1, "找不到合并语句"
    assert i_streak < i_merge, (
        "自增必须在合并**之前**，否则读到的旧 meta 已被覆盖 ⇒ 计数永远是 1"
    )
    code = _code_without_comments()
    assert "prior + 1" in code, "计数必须**累加**，不能写死成 1"


def test_skip_is_reported_not_silent() -> None:
    r"""★ 跳过必须**报出来**。静默跳过等于把失败藏起来。"""
    src = STAGES.read_text(encoding="utf-8")
    assert "fail_skipped" in src, "没有统计跳过的条数"
    # 必须有对应的 bus.log 告知用户
    code = _code_without_comments()
    assert "fail_skipped" in code
    assert "跳过" in src, "要有一条人话日志说明跳过了多少条"


@pytest.mark.parametrize("streak,should_skip", [(0, False), (1, False), (2, True), (3, True)])
def test_threshold_boundary(streak: int, should_skip: bool) -> None:
    """边界：第 2 次仍失败才放弃（第 1 次要给重试机会）。"""
    from novaloc.pipeline.stages import _MAX_FAIL_STREAK

    assert (streak >= _MAX_FAIL_STREAK) is should_skip
