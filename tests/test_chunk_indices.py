r"""`_chunk_indices`：批次切分必须**覆盖且不重复**，否则静默漏译。

## 为什么这条能力值得测试

旧实现把批大小写死：

    chunk_size = 12 if kind in ("dialogue", "narration") else 40

实测（`.scratch/_chunk_gain.py`，真实长度分布）这个 12 浪费了大量往返：

| 游戏 | 写死 12 的批数 | 提供者切 | 降幅 |
| --- | --- | --- | --- |
| Dungeon And Darkness | 266 | 112 | 2.4× |
| 072 Project | 800 | 371 | 2.2× |

而全库文本**中位长度只有 9 字符** —— 12 条一批约 108 字符，
离 `max_batch_chars=3000` 差得极远。

改成"问提供者"之后，**新引入的风险是漏译或重译** ——
这是最危险的一类 bug，因为它**静默**：

* 漏掉的条目**永远不会**进 `out` ⇒ 报告里看起来"这条不存在"，
  而不是"这条失败"；
* 重复的条目会翻两次、写两次。

所以 `_chunk_indices` 里加了覆盖性校验，本文件守住它。
"""

from __future__ import annotations

import pytest

from novaloc.models import TextKind, TextLocation, TextUnit
from novaloc.pipeline.stages import Pipeline


def _units(n: int, kind: TextKind = TextKind.DIALOGUE) -> list[TextUnit]:
    return [
        TextUnit(
            uid=f"u{i}",
            source=f"文本{i}",
            kind=kind,
            location=TextLocation(file="dummy.json", pointer=f"/a/{i}"),
        )
        for i in range(n)
    ]


class _Provider:
    """按固定分组返回下标的假 provider。"""

    def __init__(self, groups: list[list[int]], *, boom: bool = False) -> None:
        self._groups = groups
        self._boom = boom
        self.calls = 0

    def _make_batches(self, items: list) -> list[list[int]]:
        self.calls += 1
        if self._boom:
            raise RuntimeError("分批坏了")
        return [list(g) for g in self._groups]


def _chunk(provider: object | None, units: list[TextUnit], kind: str = "dialogue") -> list[list[int]]:
    p = Pipeline.__new__(Pipeline)
    return p._chunk_indices(provider, units, kind)


def test_uses_provider_batches_when_available() -> None:
    r"""提供者给的分组要被**原样采用**（这才是收益来源）。"""
    prov = _Provider([[0, 1, 2], [3, 4]])
    got = _chunk(prov, _units(5))
    assert got == [[0, 1, 2], [3, 4]], got
    assert prov.calls == 1


def test_provider_batches_may_be_larger_than_legacy_twelve() -> None:
    r"""★ 核心收益：批可以**超过 12** —— 旧实现永远给不出 30 条一批。"""
    prov = _Provider([list(range(30))])
    got = _chunk(prov, _units(30))
    assert got == [list(range(30))], "旧实现下这里必然是 12/12/6"
    assert len(got[0]) > 12


def test_incomplete_coverage_falls_back_to_fixed_chunks() -> None:
    r"""★ 提供者漏掉下标时必须退回固定分批 —— **绝不静默漏译**。"""
    prov = _Provider([[0, 1]])  # 只有 2 个，应有 5 个
    got = _chunk(prov, _units(5))
    assert sorted(j for g in got for j in g) == [0, 1, 2, 3, 4], (
        f"覆盖不全，会静默漏译：{got}"
    )


def test_duplicate_indices_fall_back_to_fixed_chunks() -> None:
    r"""★ 下标重复也要退回 —— 否则同一条会被翻两次、写两次。"""
    prov = _Provider([[0, 1, 1, 2, 3, 4]])
    got = _chunk(prov, _units(5))
    flat = [j for g in got for j in g]
    assert sorted(flat) == [0, 1, 2, 3, 4], f"有重复或缺失：{got}"


def test_provider_exception_falls_back_to_fixed_chunks() -> None:
    r"""★ 提供者分批抛异常时**不能**让整轮翻译挂掉，退回固定块。"""
    got = _chunk(_Provider([], boom=True), _units(30))
    flat = sorted(j for g in got for j in g)
    assert flat == list(range(30)), f"退回后必须覆盖全部：{got}"
    # dialogue 的兜底块是 12
    assert [len(g) for g in got] == [12, 12, 6], got


def test_no_provider_uses_fixed_chunks_by_kind() -> None:
    r"""没有提供者（测试注入场景）时按 kind 兜底：dialogue 12 / 其他 40。"""
    assert [len(g) for g in _chunk(None, _units(30), "dialogue")] == [12, 12, 6]
    assert [len(g) for g in _chunk(None, _units(50), "ui_label")] == [40, 10]


def test_provider_without_method_uses_fixed_chunks() -> None:
    r"""别的 provider 实现可能没有 `_make_batches` —— 要能安全退回。"""
    got = _chunk(object(), _units(14), "dialogue")
    assert [len(g) for g in got] == [12, 2], got


def test_indices_are_always_within_range() -> None:
    r"""★ 越界下标会在 `group[j]` 处抛 IndexError ⇒ 整轮翻译中止。

    宁可退回固定分批，也不能让一个坏的批次分组把几小时的活儿弄挂。
    """
    prov = _Provider([[0, 1, 99]])
    got = _chunk(prov, _units(3))
    for g in got:
        for j in g:
            assert 0 <= j < 3, f"下标越界：{got}"


@pytest.mark.parametrize("n", [0, 1, 12, 13, 40, 41])
def test_round_trip_covers_everything(n: int) -> None:
    r"""★ 覆盖性是**唯一不可妥协**的性质：任何 n 都必须不重不漏。"""
    for prov in (
        _Provider([[i] for i in range(n)]) if n else _Provider([]),
        None,
        object(),
    ):
        got = _chunk(prov, _units(n))
        flat = [j for g in got for j in g]
        assert sorted(flat) == list(range(n)), f"n={n} 覆盖不全：{got}"
