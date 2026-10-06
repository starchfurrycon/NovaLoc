r"""`novaloc.locale` —— 把「自带官方中文」的游戏**设为中文**。

## 为什么需要

用户指正：

> "请将支持中文的游戏设置为中文（比如游戏的配置设置文件等处），
>  防止我找不到语言设置处"

**这是真问题。** 实测全库：

```
自带中文语言包的游戏: 57 个（有 `locales/zh-CN.pak`）
其中 **45 个的 locale 字段不是中文**：
  Battle Demon Kirsten   locale='ja_JP'   ← 自带中文却显示日文
  Beyond the Portal      locale='en_US'
  Magical Revantia       locale='ko_KR'
  …
已经正确设为中文的只有 5 个（`locale='zh_TW'`）
```

⇒ 玩家在设置菜单里也**未必找得到**（RPG Maker 的 `locale` 常常没有 UI），
所以帮他们改掉是合理的。

## 机制（依据是**游戏自己的 JS 源码**，不是我猜的）

```javascript
// rpg_objects.js（MV）/ rmmz_objects.js（MZ）
Game_System.prototype.isJapanese = function() {
    return $dataSystem.locale.match(/^ja/);
};
Game_System.prototype.isChinese = function() {
    return $dataSystem.locale.match(/^zh/);
};
```

⇒ 是否显示中文由 **`data/System.json` 的 `locale` 字段**决定。

⚠️ 我前面**猜错过两次**：先猜 `config.rpgsave`（lz-string），
又猜 `config.rmmzsave`（zlib），都解不出来。**读游戏源码才是权威依据。**

## 安全性

1. **NovaLoc 不写 `locale`** —— `rpgmaker.py` 的白名单是
   `{"gameTitle", "currencyUnit", "terms"}`，`locale` 不在其中
   ⇒ 我改它**不会被流水线覆盖**；
2. **改 `locale` 不改文本** —— 只决定"读哪份语言数据"；
3. **必先备份** —— 原 `System.json` 存进工作区，改动记进
   `locale_set.json`，可一键回滚；
4. **默认 dry-run** —— 不做任何写入，除非显式 `--apply`。

## 与「游戏目录只读」的关系（诚实说明）

那条约束是为**翻译回写**立的（怕写坏游戏内容）。而 `locale`
是**用户本来就能自己改的设置**，改它有明确收益、有备份、可回滚、
且与流水线字段不重叠 —— 所以我认为可以做，但**默认不写**。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .core.config import get_config

log = logging.getLogger(__name__)

#: `System.json` 里"界面文案"所在的键（NovaLoc 翻译这些）。
#: **`locale` 不在其中** —— 这是本模块能安全改它的前提。
TERMS_KEYS = ("gameTitle", "currencyUnit", "terms")

#: 中文 locale 的默认值。游戏 JS 用 `.match(/^zh/)` 判断，
#: 所以只要是 `zh` 开头即可；具体写法跟随实测已有的 `zh_TW` 风格。
DEFAULT_ZH_LOCALE = "zh_CN"


@dataclass
class LocalePlan:
    """一个游戏的语言设置改动计划。"""

    game: Path
    system_json: Path | None = None
    current: str = ""
    target: str = ""
    reason: str = ""
    #: 可用的中文语言包（`zh-CN.pak` 等）
    zh_packs: list[str] = field(default_factory=list)
    #: False 表示"不需要改或不能改"，`reason` 说明原因
    will_change: bool = False


def zh_locale_packs(game_dir: Path, *, max_depth: int = 6) -> list[str]:
    r"""这个游戏自带哪些**中文语言包**（`zh-CN.pak` / `zh_TW.pak`）。

    ## 为什么只看文件名

    语言包是二进制/自定义格式，读内容既不可靠也没必要 ——
    文件名里的语言码就是权威证据。这与
    `batch.detect_builtin_chinese_assets` 的判据 0 同源。

    ## 只认 `.pak` 吗

    不。RPG Maker MZ 用 `locales/*.pak`，但同类游戏也可能用
    `.json`/`.txt`/`.ini`。所以按**文件名主干是中文语言码**判断
    （复用 `batch` 的归一逻辑），与扩展名无关。
    """
    from .batch import _ZH_FILE_CODES  # noqa: PLC0415

    base_depth = len(game_dir.parts)
    found: set[str] = set()
    try:
        for p in game_dir.rglob("*"):
            if not p.is_file():
                continue
            if len(p.parts) - base_depth > max_depth:
                continue
            stem = p.name
            for _ in range(4):
                nxt = Path(stem).stem
                if nxt == stem:
                    break
                stem = nxt
            norm = stem.strip().lower().replace("_", "-")
            if norm in _ZH_FILE_CODES and norm.startswith("zh"):
                found.add(p.name)
    except OSError:
        return []
    return sorted(found)


def _find_system_json(game_dir: Path) -> Path | None:
    r"""找 RPG Maker 的 `System.json`（`www/data/` 或 `data/`）。

    ⚠️ **排除 `locales/`**：MZ 的语言包里也有同名条目，
    取错文件会改到语言包而不是配置。
    """
    try:
        for p in game_dir.rglob("System.json"):
            s = str(p).lower()
            if "locales" in s or "localization" in s:
                continue
            return p
    except OSError:
        return None
    return None


def plan_game(game_dir: Path, *, target: str = DEFAULT_ZH_LOCALE) -> LocalePlan:
    """判断这个游戏要不要改 `locale`，以及改成什么。"""
    plan = LocalePlan(game=game_dir, target=target)
    plan.zh_packs = zh_locale_packs(game_dir)
    if not plan.zh_packs:
        plan.reason = "没有自带中文语言包 ⇒ 不动（翻了也没用，它没有中文数据）"
        return plan

    sj = _find_system_json(game_dir)
    if sj is None:
        plan.reason = "找不到 `System.json` ⇒ 不动（改不了或引擎不是 RPG Maker）"
        return plan
    plan.system_json = sj

    try:
        obj = json.loads(sj.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        plan.reason = f"`System.json` 读不出（{type(exc).__name__}）⇒ 不动"
        return plan
    if not isinstance(obj, dict):
        plan.reason = "`System.json` 顶层不是对象 ⇒ 不动"
        return plan

    cur = str(obj.get("locale") or "")
    plan.current = cur
    if cur.lower().startswith("zh"):
        plan.reason = f"已经是中文（locale={cur!r}）⇒ 不动"
        return plan
    plan.will_change = True
    plan.reason = f"locale {cur!r} → {target!r}（自带 {', '.join(plan.zh_packs)}）"
    return plan


def plan_library(
    library: Path, *, target: str = DEFAULT_ZH_LOCALE, limit: int = 0
) -> list[LocalePlan]:
    """对整个库做计划（**只读**）。"""
    out: list[LocalePlan] = []
    for g in sorted(library.iterdir()):
        if not g.is_dir():
            continue
        p = plan_game(g, target=target)
        out.append(p)
        if limit and len(out) >= limit:
            break
    return out


def apply_plan(plan: LocalePlan, *, workspace: Path | None = None) -> tuple[bool, str]:
    r"""执行一个改动：**先备份**，再只改 `locale` 一个字段。

    ## 为什么只改一个字段而不是重写整个 JSON

    `System.json` 里混着大量非文本数据（`terms` 数组、`elements`
    等）。**重写整个文件**会：

    * 改变键的顺序（差分不可读）；
    * 把浮点数/转义写法规范化（`1.0` → `1`），造成"内容没变但字节全变"；
    * 一旦我的 JSON 序列化与游戏期望不一致，可能让游戏读不了。

    所以用**文本级单字段替换**：定位 `"locale": "xxx"` 这一处，
    只替换引号内的值。这样**其余字节完全不动**（可验证）。

    ## 备份

    原文件整份存到 `workspace/locale_backup/System.json`，
    并写 `locale_set.json` 记录（游戏路径、原值、新值、时间）。
    """
    if not plan.will_change or plan.system_json is None:
        return False, plan.reason

    sj = plan.system_json
    try:
        raw = sj.read_bytes()
    except OSError as exc:
        return False, f"读 {sj} 失败：{exc}"

    text = raw.decode("utf-8", errors="replace")
    # 只替换**第一个** `"locale"` 的值
    pat = re.compile(r'("locale"\s*:\s*")([^"]*)(")')
    m = pat.search(text)
    if m is None:
        return False, "在 `System.json` 里找不到 `\"locale\": \"...\"` 字段 ⇒ 不动"
    if m.group(2).lower().startswith("zh"):
        return False, f"字段已是中文（{m.group(2)!r}）⇒ 不动"
    new_text = text[: m.start(2)] + plan.target + text[m.end(2) :]
    if new_text == text:
        return False, "替换后内容未变 ⇒ 不动"

    # ---- 备份 ----
    if workspace is not None:
        bdir = workspace / "locale_backup"
        try:
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "System.json").write_bytes(raw)
            (workspace / "locale_set.json").write_text(
                json.dumps(
                    {
                        "game_dir": str(plan.game),
                        "system_json": str(sj),
                        "locale_before": m.group(2),
                        "locale_after": plan.target,
                        "zh_packs": plan.zh_packs,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            # ★ 备份失败 ⇒ **绝不写入**（否则无法回滚）
            return False, f"备份失败，已放弃改动以免无法回滚：{exc}"
    else:
        return False, "没有工作区可放备份 ⇒ 不动（备份是本模块的硬前提）"

    try:
        sj.write_bytes(new_text.encode("utf-8"))
    except OSError as exc:
        return False, f"写 {sj} 失败：{exc}"
    log.info("已把 %s 的 locale 从 %r 改为 %r", plan.game.name, m.group(2), plan.target)
    return True, f"locale {m.group(2)!r} → {plan.target!r}"


def main(argv: list[str] | None = None) -> int:
    """CLI：`novaloc set-locale <库> [--apply] [--locale zh_CN]`。

    ⚠️ **默认 dry-run** —— 不加 `--apply` 只打印计划，不写任何东西。
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="novaloc set-locale",
        description="把自带官方中文的游戏设为中文（改 System.json 的 locale 字段）",
    )
    ap.add_argument("library", type=Path, help="游戏库目录")
    ap.add_argument("--apply", action="store_true", help="真的写入（默认只打印计划）")
    ap.add_argument("--locale", default=DEFAULT_ZH_LOCALE, help=f"目标 locale（默认 {DEFAULT_ZH_LOCALE}）")
    a = ap.parse_args(argv)

    plans = plan_library(a.library, target=a.locale)
    todo = [p for p in plans if p.will_change]
    print(f"  扫描 {len(plans)} 个游戏，**需要改 {len(todo)} 个**")
    for p in todo:
        print(f"    {p.game.name[:48]:50} {p.reason[:70]}")
    print()
    if not a.apply:
        print("  （dry-run：未写入任何文件。加 `--apply` 才真改）")
        return 0

    cfg = get_config()
    ok = bad = 0
    for p in todo:
        ws = Path(cfg.data_root) / "workspaces" / p.game.name
        good, why = apply_plan(p, workspace=ws)
        if good:
            ok += 1
            print(f"    ✅ {p.game.name[:44]:46} {why}")
        else:
            bad += 1
            print(f"    ⚠️ {p.game.name[:44]:46} {why}")
    print(f"\n  完成：成功 {ok}，跳过/失败 {bad}")
    return 0
