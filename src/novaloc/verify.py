r"""`novaloc.verify` —— 写回后的**自动验收**。

## 为什么需要（实测事故驱动的）

本轮发生了一起严重事故：NovaLoc 把 RPG Maker `note` 字段里
**会被 `eval` 的 JS 代码**当文本翻了：

```
备份: '<JS On Expire State>\ntarget.addState(80);\n</JS On Expire State>'
当前: '确认<JS On Expire State>\n目标生命值恢复至 80。\n</JS On Expire State>'
```

VisuMZ 插件用 **`new Function()`** 执行它 ⇒
`SyntaxError: Unexpected number` ⇒ **游戏启动即崩**。

**而这是我靠用户反馈才发现的** —— 工具自己完全没报错，
JSON 校验、严格校验、BOM 检查**全部通过**（因为坏掉的是
"字符串里的代码语义"，不是 JSON 结构）。

⇒ 结论：**必须实际启动游戏并抓它的日志**，否则这类问题查不出来。

## 三个检查（本模块）

1. **`check_js_blocks`** —— 扫所有 `<JS …>` 区块里有没有中文
   （直接守住这次的事故，**不需要启动游戏**，最快的哨兵）；
2. **`check_data_integrity`** —— 严格 JSON 校验（拒绝 `NaN`/`Infinity`）
   + 检查"数字字段被翻成文本"；
3. **`check_launch`** —— **真的启动游戏**几秒，用
   `--enable-logging=stderr` 抓 `SyntaxError`/`Error`。

## 判据方向（代价不对称）

* **宁可误报也不漏报**：漏报的代价是"用户的游戏打不开"，
  误报的代价只是"多看一条警告"；
* 但**启动检查会占 GPU/CPU**，所以只在 `apply` 之后跑一次，
  且默认超时很短（免得卡住流水线）。
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

HAN = re.compile(r"[\u4e00-\u9fff]")
#: 会被 `new Function()` / `eval` 执行的区块
JS_BLOCK_RE = re.compile(r"<JS\b[^>]*>(.*?)</JS\b[^>]*>", re.S | re.I)
#: 启动日志里的**致命**模式（区别于 NW.js 的常规噪声）
FATAL_LOG_RE = re.compile(
    r"(SyntaxError|ReferenceError|TypeError:|"
    r"Failed to load|Unable to load|"
    r"Uncaught\s+\w*Error|is not a function|is not defined)",
    re.I,
)
#: NW.js/Chromium 自己的噪声（**不算**游戏错误）
NOISE_RE = re.compile(
    r"(crash_report_database|account_consistency|password_store|"
    r"top_sites_backend|login_database|device_event_log|"
    r"web_database|push_messaging|libprotobuf|History sqlite|"
    r"Failed to read descriptor|GPU stall|SharedImageManager)",
    re.I,
)
#: RPG Maker 的 entry exe 名（按可能性排序）
EXE_CANDIDATES = ("Game.exe", "game.exe", "nw.exe", "Game", "*.exe")

#: **不该验收**文件名的特征（小写子串匹配**文件名**，不是整条路径）。
#:
#: ## 为什么必须排除（实测误报）
#:
#: `Breeding Log 1.04 64` 的 `生殖活動記録_Data\savedata.json` 是**游戏存档**，
#: 它**自带 UTF-8 BOM**（BOM 解掉后 JSON 完全合法）。
#: 我的第一版验收器把它当数据文件 ⇒ 报"严格 JSON 校验失败" ⇒ **误报**。
#:
#: 而它**不在** `rpgmaker.DATABASE_FIELDS` 的 11 个数据文件白名单里
#: ⇒ **NovaLoc 从来没碰过它**。
_SKIP_NAME_PARTS = (
    "savedata",
    ".rpgsave",
    ".rmmzsave",
    ".bdic",   # Chromium 的拼写词典
    ".pak",    # NW.js 的运行时语言包
)

#: **不该验收**的路径段（按 `/` 切分后的**整段**匹配）。
#:
#: ⚠️ ★★ 必须按**整段**匹配，不能按子串 —— 我第一版用
#: ``"appdata" in path`` 就翻车了：`game/data/Items.json` 里**含有**
#: `appdata`（`gamedata` 那 7 个字母）⇒ 把**所有数据文件**都排除了
#: ⇒ **验收形同虚设**。
#:
#: 这个 bug 是"数据文件带 BOM 应报错"那条测试暴露的（5 条测试同时失败）。
_SKIP_DIR_SEGMENTS = (
    "save",
    "saves",
    "userdata",
    "dictionaries",  # Chromium 的拼写词典
    "locales",       # NW.js 的运行时语言包
)


def _is_user_data(p: pathlib.Path) -> bool:
    """这个文件是**用户数据/运行时资源**（不该验收）吗？

    ## 判据

    * **文件名**含 `savedata`/`.rpgsave`/`.rmmzsave`/`.bdic`/`.pak`；
    * **路径段**（按 `/` 切分）**整段等于**
      `save`/`saves`/`userdata`/`dictionaries`/`locales`。

    ## ⚠️ 为什么按"段"而不按"子串"

    子串匹配会误排：`game/data/X.json` 含 `appdata`（`gamedata`）。
    实测这会让**所有数据文件**被跳过 ⇒ 验收等于没有。
    """
    name = p.name.lower()
    if any(part in name for part in _SKIP_NAME_PARTS):
        return True
    segments = {seg for seg in str(p).replace("\\", "/").lower().split("/") if seg}
    return bool(segments & set(_SKIP_DIR_SEGMENTS))


@dataclass
class VerifyResult:
    """一个游戏写回后的验收结果。"""

    game: pathlib.Path
    ok: bool = True
    #: 检查项名 → 问题列表
    problems: dict[str, list[str]] = field(default_factory=dict)
    #: 跑过的检查
    checks_run: list[str] = field(default_factory=list)

    def add(self, check: str, msg: str) -> None:
        self.problems.setdefault(check, []).append(msg)
        self.ok = False

    def summary(self) -> str:
        if self.ok:
            return f"✅ {self.game.name}：验收通过（{', '.join(self.checks_run)}）"
        bits = []
        for name, msgs in self.problems.items():
            bits.append(f"{name} {len(msgs)} 处")
        return f"❌ {self.game.name}：{'；'.join(bits)}"


# ----------------------------------------------------------------------
# 检查 1：`<JS …>` 区块里不能有中文（最快、最直接的哨兵）
# ----------------------------------------------------------------------
def check_js_blocks(game: pathlib.Path, *, max_mb: int = 16) -> list[str]:
    r"""扫所有数据文件，找 `<JS …>` 区块里含中文的地方。

    ## 为什么这是**最该先跑**的检查

    它**不需要启动游戏**（毫秒级），而且直接对应本轮的事故：
    任何 `<JS>` 区块里的中文都会让 `new Function()` 抛
    `SyntaxError`。

    ## 为什么不能只看 `note` 字段

    实测 `<JS>` 区块出现在 `States.json`/`Skills.json`/`Enemies.json`
    等多个文件的 `note` 里，而且插件的 eval 面比 `note` 更广
    ⇒ **扫原始文本**比按字段白名单扫更可靠。
    """
    problems: list[str] = []
    for p in sorted(game.rglob("*.json")):
        if not p.is_file() or _is_user_data(p):
            continue
        try:
            if p.stat().st_size > max_mb * 1024 * 1024:
                continue
            t = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<JS" not in t:
            continue
        for m in JS_BLOCK_RE.finditer(t):
            body = m.group(1)
            if HAN.search(body):
                problems.append(
                    f"{p.relative_to(game)}：`<JS>` 区块含中文（会被 "
                    f"`new Function()` 判语法错）→ {body.strip()[:70]!r}"
                )
    return problems


# ----------------------------------------------------------------------
# 检查 2：数据完整性（严格 JSON + 类型对比）
# ----------------------------------------------------------------------
def _strict_loads(t: str) -> object:
    """严格 JSON 解析：**拒绝** `NaN`/`Infinity`（JS 的 `JSON.parse` 也拒绝）。"""

    def _bad(c: str) -> object:
        raise ValueError(f"非法 JSON 常量 {c}（JS 的 JSON.parse 不接受）")

    return json.loads(t, parse_constant=_bad)


def check_data_integrity(game: pathlib.Path, *, max_mb: int = 16) -> list[str]:
    r"""数据文件必须能被**严格**解析，且没有"数字字段被翻成文本"。

    ## 为什么要"严格"

    Python 的 `json.loads` **默认接受** `NaN`/`Infinity`，
    而 JavaScript 的 `JSON.parse` **拒绝**它们。
    所以用默认参数校验会**漏掉**这类会让游戏崩的数据。

    ## 为什么要查类型

    RPG Maker 的数据里混着数字字段（`price`/`params`/`damage`）。
    若翻译把某处数字当成文本替换（`0` → `'零'`），
    JS 读到 `"price": 零` 就报 `SyntaxError: Unexpected number`。
    """
    problems: list[str] = []
    for p in sorted(game.rglob("*.json")):
        if not p.is_file() or _is_user_data(p):
            continue
        try:
            if p.stat().st_size > max_mb * 1024 * 1024:
                continue
            t = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # ★ BOM 检查：**必须显式查** —— Python 的 json.loads 会静默跳过
        #   UTF-8 BOM，而 **JavaScript 的 JSON.parse 不会**
        #   （会报 Unexpected token / Unexpected number）。
        #   实测：加了这条测试才发现我的检查漏了它。
        if t.startswith("\\ufeff"):
            problems.append(
                f"{p.relative_to(game)}：带 UTF-8 BOM（JS 的 JSON.parse 不接受，"
                f"Python 的 json.loads 却会静默跳过 ⇒ 必须显式查）"
            )
            continue
        # 严格解析
        try:
            _strict_loads(t)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{p.relative_to(game)}：严格 JSON 校验失败 → {exc}")
            continue
        # 明显"数字位置出现中文"的形态（便宜且有效）
        #
        # ⚠️ **不能把 params 当成数字字段** —— RPG Maker 里有两处同名：
        #   * Classes.json 的 params 是**数字二维数组**（成长曲线）；
        #   * System.json 的 	erms.params 是**术语名数组**（"最大生命值"）
        #     ⇒ 那儿的字符串是**合法的翻译**（实测误报就是这一处）。
        # 同理 exp 也可能是术语。所以只留**语义上一定是数字**的字段。
        for m in re.finditer(
            r'"(price|damage|hp|mp|atk|def|mat|mdf|agi|luk)"\s*:\s*([^,}\]]{1,32})',
            t,
        ):
            v = m.group(2).strip()
            if HAN.search(v):
                problems.append(f"{p.relative_to(game)}：数字字段 {m.group(1)} 含中文 → {v[:40]!r}")
        # params 只在 Classes.json（成长曲线）里一定是数字；
        # 那里若含中文却**完全没有数字**，就是被翻坏了。
        if p.name == "Classes.json":
            for m in re.finditer(r'"params"\s*:\s*(\[[^\]]{0,300})', t):
                seg = m.group(1)
                if HAN.search(seg) and not re.search(r"\[?\s*\d", seg):
                    problems.append(
                        f"{p.relative_to(game)}：params 成长曲线含中文且无数字 → {seg[:50]!r}"
                    )
    return problems


# ----------------------------------------------------------------------
# 检查 3：**真的启动游戏**，抓启动期错误
# ----------------------------------------------------------------------
def find_exe(game: pathlib.Path) -> pathlib.Path | None:
    """找 RPG Maker 的启动 exe（**排除**辅助进程）。"""
    bad = ("notification_helper", "crashpad", "console", "unins")
    for pat in EXE_CANDIDATES:
        for p in sorted(game.glob(pat)):
            if p.is_file() and not any(b in p.name.lower() for b in bad):
                return p
    # 退一步：递归找（但排除子目录里的辅助 exe）
    for p in sorted(game.rglob("*.exe")):
        if any(b in p.name.lower() for b in bad):
            continue
        if p.parent == game:
            return p
    return None


def check_launch(
    game: pathlib.Path,
    *,
    seconds: float = 12.0,
    exe: pathlib.Path | None = None,
) -> list[str]:
    r"""启动游戏 `seconds` 秒，抓 `stderr` 里的**致命**错误。

    ## 为什么要"真的启动"

    本轮的事故里，**JSON 校验全通过**（坏掉的是字符串里的代码语义），
    只有运行时 `new Function()` 才暴露出来。
    ⇒ 启动是**唯一**能发现这类问题的手段。

    ## 实现要点

    * 用 `--enable-logging=stderr` 让 NW.js 把 `console.error` 输出到 stderr；
    * 抓完**必须杀进程**（包括子进程 —— NW.js 是多进程的）；
    * **过滤 NW.js 自身噪声**（crash_report / password_store / GPU 等），
      否则每次都会有几十条假警报；
    * 超时**不算失败**（游戏正常启动时进程本来就会一直跑）。

    ## 代价（诚实记录）

    会占用 GPU/CPU 若干秒。所以只在 `apply` 之后跑一次，
    且默认 12 秒。
    """
    exe = exe or find_exe(game)
    if exe is None:
        return ["找不到可执行文件 ⇒ 跳过启动检查（不是错误，只是没查到）"]

    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="novaloc_verify_") as td:
        err_path = pathlib.Path(td) / "err.txt"
        out_path = pathlib.Path(td) / "out.txt"
        try:
            with (
                err_path.open("wb") as ferr,
                out_path.open("wb") as fout,
                # ★ `CREATE_NO_WINDOW`：不给游戏弹窗（后台跑）
                #   但 NW.js 仍会创建自己的窗口；这里只保证**不额外**开控制台
                subprocess.Popen(  # noqa: S603
                    [str(exe), "--enable-logging=stderr"],
                    cwd=str(game),
                    stdout=fout,
                    stderr=ferr,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ) as proc,
            ):
                time.sleep(seconds)
                _kill_tree(proc)
        except OSError as exc:
            return [f"启动失败：{exc}"]

        time.sleep(1.0)
        try:
            raw = err_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""

    for ln in raw.splitlines():
        if not ln.strip():
            continue
        if NOISE_RE.search(ln):
            continue
        if FATAL_LOG_RE.search(ln):
            # 去掉时间戳前缀，只留有用部分
            clean = re.sub(r"^\[[^\]]*\]\s*", "", ln).strip()
            problems.append(f"启动日志：{clean[:150]}")
    return problems


def _kill_tree(proc: subprocess.Popen) -> None:
    r"""杀掉进程**及其子进程**。

    ⚠️ NW.js 是**多进程**的（主进程 + GPU + renderer + utility）。
    只杀主进程会留下孤儿进程继续占 GPU —— 实测会让后续的
    翻译与验收都变慢。
    """
    try:
        import psutil  # noqa: PLC0415

        parent = psutil.Process(proc.pid)
        kids = parent.children(recursive=True)
        for k in kids:
            try:
                k.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        parent.kill()
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except OSError:
        pass


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def verify_game(
    game: pathlib.Path,
    *,
    launch: bool = True,
    seconds: float = 12.0,
) -> VerifyResult:
    """对**已写回**的游戏跑三个检查。

    ``launch=False`` 只跑前两个（快，不占资源）——
    适合在流水线里每轮跑；启动检查建议在 `apply` 之后跑一次。
    """
    res = VerifyResult(game=game)

    # ① JS 区块（最快，先跑）
    res.checks_run.append("js_blocks")
    for msg in check_js_blocks(game):
        res.add("js_blocks", msg)

    # ② 数据完整性
    res.checks_run.append("data_integrity")
    for msg in check_data_integrity(game):
        res.add("data_integrity", msg)

    # ③ 启动（贵，放最后；前两项已失败就没必要再启）
    if launch:
        if not res.ok:
            log.info("%s 前两项检查已失败，跳过启动检查（省资源）", game.name)
            return res
        res.checks_run.append("launch")
        for msg in check_launch(game, seconds=seconds):
            res.add("launch", msg)
    return res


def main(argv: list[str] | None = None) -> int:
    """CLI：`python -m novaloc.verify <游戏目录> [--no-launch] [--seconds N]`。"""
    import argparse

    ap = argparse.ArgumentParser(
        prog="novaloc.verify",
        description="写回后自动验收：JS 区块 / 数据完整性 / 启动日志",
    )
    ap.add_argument("game", type=pathlib.Path)
    ap.add_argument("--no-launch", action="store_true", help="跳过启动检查（快，不占 GPU）")
    ap.add_argument("--seconds", type=float, default=12.0, help="启动后观察秒数（默认 12）")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = verify_game(a.game, launch=not a.no_launch, seconds=a.seconds)
    print(res.summary())
    for name, msgs in res.problems.items():
        print(f"\n  ── {name}（{len(msgs)} 处）──")
        for m in msgs[:20]:
            print(f"    ❌ {m}")
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
