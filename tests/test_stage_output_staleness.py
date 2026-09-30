"""产出目录必须**每轮清空**：陈旧产物不能冒充本轮结果。

## 这个 bug 长什么样（真实游戏实测）

`images_localize` 第二轮的规则变严了（噪声块不再翻译），报告说：

    贴图汉化 完成：69/750 张贴图已汉化（173 处文字）

但 `images/rebuilt/` 里躺着 **87** 个文件 —— 多出的 **18 个是上一轮的**。
而 `apply` 是**按目录内容**回写的，于是：

* 报告：69 张；
* 实际写进游戏：87 张，其中 18 张用的是**已被新规则判定为不该处理**
  的旧产物。

这是最坏的一类静默失效：报告数字看着合理，游戏里却多了没申报的改动。
而且它只在"重跑、且规则/模型变了"的时候出现 —— 第一次跑永远是对的。

## 为什么会这样

`out_dir` 建好之后只做"写成功的覆盖写出"，不清理。
贴图汉化是按"这张图有没有可改的地方"决定写不写的，
所以"这一轮没写"和"上一轮写过了"在目录里长得一模一样。

## 修法

进阶段先清空产出目录（只清**产出**目录，源游戏目录永远只读）。
清理失败只记日志不中断 —— 陈旧产物是瑕疵，让整个阶段失败是更大的问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.pipeline.stages import _clear_dir  # noqa: E402


def test_clear_dir_removes_files(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"y")
    assert _clear_dir(tmp_path) == 2
    assert not list(tmp_path.iterdir())


def test_clear_dir_keeps_the_directory_itself(tmp_path: Path) -> None:
    """目录本身要留着 —— 下游按固定路径写，不需要重新创建。"""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.png").write_bytes(b"x")
    _clear_dir(tmp_path)
    assert tmp_path.is_dir()
    assert not list(tmp_path.rglob("*"))


def test_clear_dir_handles_nested_tree(tmp_path: Path) -> None:
    """真实布局是 `img/system/x.png_` 这种嵌套路径。"""
    d = tmp_path / "img" / "system" / "Respaldo"
    d.mkdir(parents=True)
    for i in range(5):
        (d / f"f{i}.png_").write_bytes(b"z")
    (tmp_path / "img" / "top.png").write_bytes(b"z")
    assert _clear_dir(tmp_path) == 6
    assert not list(tmp_path.rglob("*"))
    assert tmp_path.is_dir()


def test_clear_dir_on_missing_dir_is_zero(tmp_path: Path) -> None:
    assert _clear_dir(tmp_path / "nope") == 0


def test_clear_dir_on_empty_dir_is_zero(tmp_path: Path) -> None:
    assert _clear_dir(tmp_path) == 0


def test_clear_dir_counts_only_files(tmp_path: Path) -> None:
    """计数是"删掉的文件数"，用于日志；空目录不算。"""
    (tmp_path / "empty").mkdir()
    (tmp_path / "one.png").write_bytes(b"x")
    assert _clear_dir(tmp_path) == 1


def test_stage_clears_out_dir_before_writing() -> None:
    """**核心契约**：阶段真的调用了清理，而不只是定义了这个函数。"""
    import inspect

    from novaloc.pipeline import stages as mod

    src = inspect.getsource(mod.Pipeline.stage_images_localize)
    assert "_clear_dir(out_dir)" in src, (
        "images_localize 没有清空产出目录 —— 陈旧产物会冒充本轮结果"
    )
    # 清理必须在写第一张图之前
    assert src.index("_clear_dir(out_dir)") < src.index("imwrite_bgr"), (
        "清理发生在写入之后，等于没清"
    )


@pytest.mark.parametrize("stage", ["stage_images_localize"])
def test_only_output_dir_is_cleared_not_source(stage: str) -> None:
    """**安全契约**：只清工作区产出目录，绝不碰源游戏目录。

    源游戏目录是只读的（本项目硬约束），误删是不可逆的事故。
    """
    import inspect

    from novaloc.pipeline import stages as mod

    src = inspect.getsource(getattr(mod.Pipeline, stage))
    # 传给 _clear_dir 的只允许是 out_dir（工作区内的产出目录）
    for line in src.splitlines():
        if "_clear_dir(" in line and "def " not in line:
            assert "_clear_dir(out_dir)" in line, (
                f"清理了一个非产出目录：{line.strip()}"
            )
