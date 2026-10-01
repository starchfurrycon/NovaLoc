"""回写阶段的「空译文」闸门。

## 这个闸门为什么必须有

真实踩过（ElfLifia 工作区）：手工删掉 `translations/entries.jsonl` 之后
重跑 `translate fonts qa apply`，结果**只有 apply 真的跑了**，
输出是：

    回写 0 条文本、1 张贴图、1 个字体 → …\\out
    ✅ 回写产物 完成：已写入 2 个文件
    🎉 全部完成。

**报告全绿，游戏里一个字都没翻。** 这是本项目反复栽跟头的那一类
失效 —— 静默失效：退出码 0、没有 ERROR、没有任何提示，
但核心功能完全没做。

判据因此写成：**抽取到文本、却一条都回写不出去 ⇒ 必须中止**。

### 为什么不用"译文数 < 某个比例"

那会误伤真正的情况：一个游戏可能大部分文本本来就无需翻译
（纯数字、已是中文、被 `skip` 规则排除）。"比例低"不等于"出错了"。
`抽取到 N 条但**一条都没有**` 才是零假阳性的信号 ——
真不需要翻译时 `units` 本身就是空的。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from _fake_game import build_fake_game  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.core.workspace import Workspace  # noqa: E402
from novaloc.models import Project  # noqa: E402
from novaloc.pipeline import Pipeline, PipelineError  # noqa: E402


def _ws(tmp_path: Path, game: Path) -> Workspace:
    proj = Project(id="applyguard", name="gate", game_dir=str(game), engine="rpgmaker")
    ws = Workspace(proj, tmp_path / "ws")
    ws.save()
    return ws


def _pipeline(ws: Workspace) -> Pipeline:
    return Pipeline(ws, Context(config=Config(), events=EventBus(), workspace=ws))


@pytest.fixture
def extracted_ws(tmp_path: Path) -> Workspace:
    """已抽取文本、但**没有任何翻译记录**的工作区。"""
    game = build_fake_game(FIXTURES / "rpgmaker_game")
    ws = _ws(tmp_path, game)
    pipe = _pipeline(ws)
    res = pipe.stage_extract()
    assert res.ok, res.error
    assert ws.load_units(), "前置条件：必须抽到文本，否则测不到这个闸门"
    assert not ws.load_entries(), "前置条件：不能有译文"
    return ws


def test_apply_refuses_when_no_translations_at_all(extracted_ws: Workspace) -> None:
    """★ 有文本要翻、却零条可回写 ⇒ 必须失败，不许"成功"。"""
    pipe = _pipeline(extracted_ws)
    with pytest.raises(PipelineError) as ei:
        pipe.stage_apply()
    msg = str(ei.value)
    assert "没有任何一条可回写的译文" in msg, msg
    # 报错要能指向真正的原因，否则用户不知道去查什么
    assert "entries.jsonl" in msg, msg


def test_apply_records_a_failed_stage_not_a_silent_success(
    extracted_ws: Workspace,
) -> None:
    """失败要留在 `results` 里，且**不能**是 ok=True。"""
    pipe = _pipeline(extracted_ws)
    with pytest.raises(PipelineError):
        pipe.stage_apply()
    assert pipe.results, "阶段结果必须被记录"
    last = pipe.results[-1]
    assert last.stage == "apply"
    assert last.ok is False, "绝不能记成成功"


def test_empty_units_is_not_an_error(tmp_path: Path) -> None:
    """**零假阳性**对照：本来就没有文本要翻时，不许报错。

    这条是判据的另一半。少了它，"抽取到文本"这个前置条件
    就可能被写成"总是报错"，把正常场景也拦下。
    """
    game = build_fake_game(FIXTURES / "rpgmaker_game")
    ws = _ws(tmp_path, game)
    # 不跑 extract ⇒ units 为空
    assert not ws.load_units()
    pipe = _pipeline(ws)
    res = pipe.stage_apply()
    assert res.ok, f"没有文本时不该失败：{res.error}"


def test_missing_entries_file_with_units_present(extracted_ws: Workspace) -> None:
    """`entries.jsonl` **文件不存在**（不只是空）时同样要拦住。

    这是真实触发场景：文件被删掉。`load_entries()` 对缺失文件
    返回空列表（这是对的，见 `test_jsonl_tolerance`），
    所以闸门必须靠"units 非空 + translations 为空"这个组合判断，
    而不是靠"文件存不存在"。
    """
    p = extracted_ws.p("translations", "entries.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    pipe = _pipeline(extracted_ws)
    with pytest.raises(PipelineError) as ei:
        pipe.stage_apply()
    assert "没有任何一条可回写的译文" in str(ei.value)
