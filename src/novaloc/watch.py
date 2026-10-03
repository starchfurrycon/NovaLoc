r"""守望模式（``novaloc auto --watch``）的状态与判定。

## 为什么把这部分单独抽出来

守望模式是"**后续新加游戏自动汉化**"这条需求的全部实现。它原本整个
内联在 `cli.auto()` 里，后果是：

* **一个测试都没有** —— `cli.auto()` 里有 `while True: sleep()`，
  要测"新游戏被认出来"就得跑一个永不返回的函数；
* 于是"重启后会不会把整库重跑一遍"这种**代价极高**的行为
  （实测全库跑一轮很久）只能靠人肉观察。

抽出来之后，"哪些算新游戏""状态怎么落盘""状态文件坏了怎么办"
都能用普通单测钉住。

## 状态文件

`<数据根>/watch-state.json`：::

    {
      "library": "E:\\\\lush\\\\1\\\\newlytransport",
      "updated": "2026-10-05 12:34:56",
      "seen": ["e:\\\\lush\\\\1\\\\newlytransport\\\\game a", ...]
    }

## 为什么 `seen` 里存**小写路径**而不是游戏名

* 游戏名会重名（库里实测有同名不同目录的）；
* Windows 路径大小写不敏感，但字符串比较敏感 ⇒ 不统一小写的话
  `E:\Lush\...` 与 `e:\lush\...` 会被当成两个游戏，于是**重复处理**。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: 状态文件名（放在数据根下，跟工作区/备份同级）。
STATE_NAME = "watch-state.json"


def normalize(p: Path | str) -> str:
    """把路径统一成"用于比较"的形式。

    ▲ 用 `str(p).lower()` 而**不是** `Path.resolve()`：

    * `resolve()` 在目录**暂时不可达**（网络盘掉线、移动硬盘没插）时
      行为依平台而异，而"游戏目录当前不可达"正是守望模式**必须**
      还能正确认出"已经见过它"的场景 —— 否则一掉线就把整库重跑；
    * 库路径本身已经是 `find_games` 给出的绝对路径。
    """
    return str(p).lower()


@dataclass
class WatchState:
    """守望模式"见过哪些游戏"的持久化状态。"""

    path: Path
    library: Path | None = None
    seen: set[str] = field(default_factory=set)
    #: 读状态文件时出错的话，这里是原因（只用于报告，不阻止运行）。
    load_error: str = ""
    #: 状态文件**本来不存在**（第一次跑）—— 与"文件坏了"要分开报告。
    fresh: bool = False

    # ---------------------------------------------------------------- 读
    @classmethod
    def load(cls, path: Path, *, library: Path | None = None) -> WatchState:
        st = cls(path=path, library=library)
        if not path.is_file():
            st.fresh = True
            return st
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ★ 文件坏了**不能**当成"什么都没见过"就往下走 ——
            #   那会把整库重跑一遍。调用方拿到 load_error 后应当
            #   把**库里现有游戏**全部记入 seen（见 `bootstrap`）。
            st.load_error = f"{type(exc).__name__}: {exc}"
            log.warning("守望状态文件读不出来（%s）：%s", path, st.load_error)
            return st
        if not isinstance(raw, dict):
            st.load_error = f"顶层不是对象，而是 {type(raw).__name__}"
            return st
        names = raw.get("seen")
        if isinstance(names, list):
            st.seen = {str(x).lower() for x in names if isinstance(x, str)}
        else:
            st.load_error = "缺少 seen 列表"
        return st

    # ---------------------------------------------------------------- 写
    def save(self) -> None:
        """落盘。写不出去只记日志 —— 守望模式不该因为状态文件而中断。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "library": str(self.library) if self.library else "",
                        "updated": _now(),
                        "seen": sorted(self.seen),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("守望状态写不出去：%s", exc)

    # ------------------------------------------------------------ 判定
    def remember(self, games: Iterable[Path]) -> None:
        self.seen.update(normalize(g) for g in games)

    def is_new(self, game: Path) -> bool:
        return normalize(game) not in self.seen

    def new_games(self, games: Iterable[Path]) -> list[Path]:
        """挑出**没见过**的游戏（保持传入顺序）。"""
        return [g for g in games if self.is_new(g)]

    def bootstrap(self, games: Iterable[Path]) -> None:
        """把现有游戏全部记为"已见"。

        ★ 什么时候**必须**调用：状态文件第一次创建、或读坏了。

        理由：`seen` 为空 ⇒ 库里现有每一个游戏都算"新出现"⇒
        整库重跑一遍。这是"重启后静默重跑整库"那个缺陷的另一半
        （第一半是 `seen` 之前只在内存里，见 `cli.auto` 注释）。
        """
        self.remember(games)

    # ------------------------------------------------------------ 报告
    def describe(self) -> str:
        if self.fresh:
            return f"新建守望状态（{self.path.name}）"
        if self.load_error:
            return f"守望状态损坏（{self.load_error}），已按现有游戏重建"
        return f"读回 {len(self.seen)} 个已见游戏（{self.path.name}）"


def _now() -> str:
    from time import strftime

    return strftime("%Y-%m-%d %H:%M:%S")
