"""RPG Maker **VX Ace / VX / XP** 适配器。

## 为什么是这两代

它们是 RPG Maker 在中文圈存量最大的一批游戏，而此前完全没支持：
MV/MZ 的文本在 JSON 里，VX Ace/VX/XP 的文本在 ``Data/*.rxdata`` /
``Data/*.rvdata`` 里，是 **Ruby Marshal 二进制**。
好消息是两代的数据结构与 MV/MZ 高度同构（同一批字段名、
同一套事件指令码），所以字段白名单和指令码表都能沿用，
差别只在"怎么读写文件"这一层 —— 那层由
:mod:`novaloc.engines.rubymarshal` 负责。

## 版本与编码（必须分清楚）

======  ==============  ==============  ==============================
版本    文件后缀        Marshal 版本    字符串编码
======  ==============  ==============  ==============================
XP      ``.rvdata``     4.8 (Ruby 1.8)  CP932（日文 Shift_JIS 变体）
VX      ``.rvdata``     4.8             CP932
VX Ace  ``.rxdata``     4.9 (Ruby 1.9)  UTF-8
======  ==============  ==============  ==============================

编码由 **Marshal 的 minor 版本**决定，不看后缀。所以抽取时记下每个
文件自己的 magic，回写时**原样传回去** —— 这一步错了不会报错，
只会让写出的字节读回来是乱码（CP932 覆盖大量汉字，所以乱得"不彻底"，
最难查的就是这种）。

## 哪些文件/字段会被翻

只翻**白名单**里的字段（``@name``、``@description``、``@message1`` …）
和白名单指令码的参数（``401`` 显示文字、``102`` 选项 …）。
明确**不**翻：

* ``Scripts.rxdata`` / ``Scripts.rvdata`` —— 里面是**Ruby 源码**，
  翻了游戏直接崩。整份文件跳过。
* ``355``/``655``（脚本指令）、``108``/``408``（注释）、``101``（脸图名）、
  ``111``（条件判断）—— 都不是给人看的正文。
* ``@note`` 里的插件标签（``<Tag:value>``）—— 同上，沿用 MV 那边的判据。

## 回写方式

按 **指针** 定位（``/1/@name``、``/@game_title``、真实字段名带 ``@``），
写法与 MV 适配器的 ``_set_pointer`` 一致，只是指针里多了一种
"符号键"的表示。每个文件独立"读-改-写"：先 ``loads``，按指针改
字符串，再 ``dumps`` 回去，绝不重建整个数据结构。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..core.registry import register
from ..models import (
    ExtractReport,
    FontCoverage,
    ImageAsset,
    TextKind,
    TextLocation,
    TextUnit,
)
from .base import ApplyResult, EngineAdapter, EngineInfo
from .rubymarshal import (
    MAGIC_48,
    MAGIC_49,
    MarshalError,
    RValue,
    _Sym,
    dumps,
    loads,
)

log = logging.getLogger(__name__)

#: 数据库文件 → 可翻字段（**白名单**，理由同 MV 适配器：
#: 一律全翻必然把数值、ID、脚本表达式也翻掉）。
#: ▲ 字段名统一用**裸名**（``name`` 而不是 ``@name``）。
#: Ruby 源码里写的是 ``@name``，但 Marshal 读出来的符号名是 ``name``
#: （``_Sym("name").name == "name"``）。两边混用会一条都匹配不上 ——
#: 第一版就踩了这个坑，抽出来 0 条。
DATABASE_FIELDS: dict[str, set[str]] = {
    "Actors": {"name", "nickname", "description"},
    "Armors": {"name", "description", "note"},
    "Classes": {"name", "description", "note"},
    "Enemies": {"name", "note"},
    "Items": {"name", "description", "note"},
    "Skills": {"name", "description", "message1", "message2", "note"},
    "States": {"name", "message1", "message2", "message3", "message4", "note"},
    "Weapons": {"name", "description", "note"},
    "Troops": {"name"},
    "CommonEvents": {"name"},
    "MapInfos": {"name"},
}

#: ``System.rxdata`` / ``System.rvdata`` 里可翻的字段。
#: 同样用裸名。
SYSTEM_FIELDS: dict[str, set[str]] = {
    "VXAce": {"game_title", "currency_unit"},
    "XP": {"game_title", "currency_unit", "words"},
}

#: VX Ace 的 ``@terms`` 里可翻的键（菜单用词，翻错会显得很业余）。
TERMS_FIELDS: set[str] = {
    "basic",
    "commands",
    "params",
    "messages",
    "skill_types",
    "weapon_types",
    "armor_types",
}

#: 事件指令码 → 参数里哪些下标是**正文**。
#: ``401`` 显示文字、``405`` 显示文字续行、``105`` 滚动文字、``102`` 选项。
EVENT_COMMAND_TEXT_INDEX: dict[int, tuple[int, ...]] = {
    401: (0,),
    405: (0,),
    105: (0,),
    102: (),  # 参数整体是选项数组，特殊处理
}

#: 参数整体是字符串数组的指令码（选项、滚动文字续行）。
EVENT_COMMAND_LIST_ARG: dict[int, tuple[int, ...]] = {
    102: (0,),
    405: (0,),
    105: (0,),
}

#: 绝不翻的指令码：355/655 是 Ruby 脚本，108/408 是注释，101 是脸图名，
#: 111 是条件判断（里面有变量号、比较符）。
EVENT_COMMAND_SKIP: frozenset[int] = frozenset({355, 655, 108, 408, 101, 111})

#: ``@note`` 里的插件标签：``<Tag>`` / ``<Tag:value>`` / ``<Tag arg:value>``。
#: 与 MV 适配器同一个判据（标签名里可以带空格，真实插件用过）。
import re  # noqa: E402  —— 放在常量区更清楚

PLUGIN_TAG_RE = re.compile(r"<[A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z0-9_]+)*(?::[^<>\n]*)?>")

#: 明显不是人话的串：纯数字/符号/空白。
_NOT_TEXT_RE = re.compile(r"^[\s\d\W_]+$", re.UNICODE)

#: ``@note`` 里被插件当 JavaScript 跑的内容特征。
NOTE_JS_RE = re.compile(
    r"\$game[A-Za-z]+|\barguments\s*\[|\bfunction\b|=>|\bvar\s+\w|\blet\s+\w"
    r"|\bnew\s+[A-Z]|\.setValue\s*\(|\.value\s*\("
)

#: VX Ace / VX / XP 的数据文件后缀。
DATA_SUFFIXES: tuple[str, ...] = (".rxdata", ".rvdata")

#: 这些文件里**全是代码或二进制**，一律跳过（尤其 Scripts 是 Ruby 源码）。
SKIP_FILES: frozenset[str] = frozenset(
    {
        "scripts",
        # 音画资源索引，没有可翻文本
        "tilesets",
    }
)

#: 判定"是不是 VX Ace/VX/XP 游戏"的特征文件。
_VX_ACE_MARKERS: tuple[str, ...] = (
    "Actors.rxdata",
    "Classes.rxdata",
    "System.rxdata",
    "MapInfos.rxdata",
)
_VX_XP_MARKERS: tuple[str, ...] = (
    "Actors.rvdata",
    "Classes.rvdata",
    "System.rvdata",
    "MapInfos.rvdata",
)

#: 运行时 DLL / 配置文件 → 版本证据。
_VX_ACE_DLLS: tuple[str, ...] = ("RGSS300.dll", "RGSS301.dll")
_VX_DLLS: tuple[str, ...] = ("RGSS202E.dll", "RGSS202J.dll")
_XP_DLLS: tuple[str, ...] = ("RGSS102E.dll", "RGSS102J.dll", "RGSS104E.dll")


# ----------------------------------------------------------------------
# 指针工具
# ----------------------------------------------------------------------
#: 指针里表示"符号键"的前缀。``/1/@@name`` = 数组下标 1 → 符号键 ``@name``。
#: 用 ``@`` 前缀而不是直接写裸名字，是因为 Marshal 的 ivar 名**本来就带 @**
#: （``@name``），而 Hash 的键可能不带 —— 两者必须能区分。
SYM_PREFIX = "@"


def _sym(name: str) -> str:
    """把 Ruby 符号名变成指针里的一段。"""
    return SYM_PREFIX + name if isinstance(name, _Sym) else name


def _resolve(obj: Any, pointer: str) -> tuple[bool, Any]:
    """按指针走到目标，返回 ``(是否找到, 值)``。"""
    if not pointer.startswith("/"):
        return False, None
    cur = obj
    for seg in [p for p in pointer.split("/") if p != ""]:
        cur, ok = _step(cur, seg)
        if not ok:
            return False, None
    return True, cur


def _step(cur: Any, seg: str) -> tuple[Any, bool]:
    """在 ``cur`` 上走一段指针。"""
    if isinstance(cur, list):
        if not seg.lstrip("-").isdigit():
            return None, False
        idx = int(seg)
        if not (-len(cur) <= idx < len(cur)):
            return None, False
        return cur[idx], True
    if isinstance(cur, RValue):
        # 对象/结构的 ivar：键是 _Sym
        key = seg[len(SYM_PREFIX) :] if seg.startswith(SYM_PREFIX) else seg
        for k in cur.ivars:
            if getattr(k, "name", k) == key:
                return cur.ivars[k], True
        return None, False
    if isinstance(cur, dict):
        if seg in cur:
            return cur[seg], True
        # ▲ 指针里"符号键"统一带 ``@`` 前缀，但 Hash 的键可能是**整数**
        #   （地图的 @events 就是"事件 ID → 事件"）或是**不带 @** 的符号。
        #   所以要先把前缀去掉再比。第一版只对 _Sym 键去前缀，
        #   于是 /@events/@1/... 永远找不到 —— 地图事件一条都写不回去，
        #   而且不报错（apply 只在一条都没写成功时才记 warning）。
        key = seg[len(SYM_PREFIX) :] if seg.startswith(SYM_PREFIX) else seg
        for k in cur:
            if isinstance(k, _Sym):
                if k.name == key:
                    return cur[k], True
            elif str(k) == key:
                return cur[k], True
        return None, False
    return None, False


def set_pointer(obj: Any, pointer: str, value: Any) -> bool:
    """按指针写入，返回是否成功（失败绝不静默改错位置）。"""
    if not pointer.startswith("/"):
        return False
    parts = [p for p in pointer.split("/") if p != ""]
    if not parts:
        return False
    cur = obj
    for seg in parts[:-1]:
        cur, ok = _step(cur, seg)
        if not ok:
            return False
    last = parts[-1]
    if isinstance(cur, list):
        if not last.lstrip("-").isdigit():
            return False
        idx = int(last)
        if not (-len(cur) <= idx < len(cur)):
            return False
        cur[idx] = value
        return True
    if isinstance(cur, RValue):
        key = last[len(SYM_PREFIX) :] if last.startswith(SYM_PREFIX) else last
        for k in cur.ivars:
            if getattr(k, "name", k) == key:
                cur.ivars[k] = value
                return True
        return False
    if isinstance(cur, dict):
        if last in cur:
            cur[last] = value
            return True
        # 与 _step 同一条规则：先去 @ 前缀，再按 _Sym 名或 str 比。
        key = last[len(SYM_PREFIX) :] if last.startswith(SYM_PREFIX) else last
        for k in cur:
            if isinstance(k, _Sym):
                if k.name == key:
                    cur[k] = value
                    return True
            elif str(k) == key:
                cur[k] = value
                return True
        return False
    return False


# ----------------------------------------------------------------------
# 文本判据
# ----------------------------------------------------------------------


def _has_letters(s: str) -> bool:
    """串里有没有"给人看的字"（字母或 CJK/假名）。

    ▲ 不要手动枚举 CJK 区间。日文里除了 ``\\u3040-\\u30ff``（假名）和
    ``\\u4e00-\\u9fff``（汉字），还有半角片假名 ``\\uff66-\\uff9f``、
    CJK 扩展区等；枚举必然漏。
    Python 的 ``str.isalpha()`` 对汉字/假名本来就返回 True，
    拿它当判据又短又全。
    """
    if any(ch.isalpha() for ch in s):
        return True
    # 兜底：某些全角符号（长音符 ー 等）不是 alpha，但确实是日文文本的一部分
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff\uff66-\uff9f]", s))


#: 纯 ASCII 的串（无 CJK/假名）。
_ASCII_ONLY_RE = re.compile(r"^[\x00-\x7f]+$")


def _is_translatable(text: Any, field: str = "", *, short_ok: bool = False) -> bool:
    """这段文本值不值得送去翻译。

    ``short_ok=False``（默认）会砍掉两个字符以内的**纯 ASCII** 串 ——
    那是给**数据库字段**用的：``name``/``description`` 里出现 ``"hp"``、
    ``"atk"``、``"ID"`` 基本都是内部键名，不是给人看的。

    ▲ 但**选项**（``102``）与**对话**（``401``）不一样：那里出现 ``"No"``
    就是玩家要读的按钮文字，砍掉等于漏译。这两处传 ``short_ok=True``。
    第一版对所有串一律砍 ``len<=2``，于是 ``"No"`` 这个选项静默消失了。
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if len(s) < 1:
        return False
    if _NOT_TEXT_RE.match(s):
        return False
    if not _has_letters(s):
        return False
    if not short_ok and len(s) <= 2 and _ASCII_ONLY_RE.match(s):
        return False
    return not (field.endswith("note") and _is_plugin_config_note(s))


def _is_plugin_config_note(text: str) -> bool:
    """``@note`` 里是不是插件配置（标签 + 代码），而不是给人看的话。

    与 MV 适配器同源：先看有没有 JS 代码特征，再看"去掉标签后还剩不剩人话"。
    """
    if NOTE_JS_RE.search(text):
        return True
    stripped = PLUGIN_TAG_RE.sub(" ", text)
    if stripped.strip() == "":
        # 整段只有标签
        return True
    # 去掉标签后剩下的部分若没有句子标点，且都很短，判为配置
    words = [w for w in re.split(r"[\s,;:/]+", stripped) if w]
    return not re.search(r"[.!?。！？；;…]", stripped) and len(words) <= 6


# ----------------------------------------------------------------------
# 指令码工具
# ----------------------------------------------------------------------


def _command_code(cmd: Any) -> int | None:
    """取指令码。

    VX Ace 的 ``@code`` 是普通整数；XP 的 ``@code`` 是 ``Fixnum`` 包装对象
    （``RValue("Fixnum", {"@value": 401})``）—— 两种都要认。
    """
    if not isinstance(cmd, RValue):
        return None
    code = None
    for k, v in cmd.ivars.items():
        if getattr(k, "name", k) == "code":
            code = v
            break
    if isinstance(code, int):
        return code
    if isinstance(code, RValue):
        for k, v in code.ivars.items():
            if getattr(k, "name", k) in ("value", "val"):
                if isinstance(v, int):
                    return v
    return None


def _command_params(cmd: Any) -> list[Any] | None:
    """取指令参数数组（``@parameters``）。"""
    if not isinstance(cmd, RValue):
        return None
    for k, v in cmd.ivars.items():
        if getattr(k, "name", k) == "parameters" and isinstance(v, list):
            return v
    return None


# ----------------------------------------------------------------------
# 适配器
# ----------------------------------------------------------------------


@register("engine", "rpgvx")
class RpgVxAdapter(EngineAdapter):
    """RPG Maker VX Ace / VX / XP 适配器。"""

    id = "rpgvx"
    display_name = "RPG Maker VX Ace/VX/XP"

    #: 排在 MV/MZ 之后：MV 的 ``System.json`` 特征更强，
    #: 而 VX 系看 ``Data/*.rxdata``。两者不会同时命中。
    priority = 20

    # -- 基础 ----------------------------------------------------------

    def _data_dir(self, game_dir: Path) -> Path | None:
        """找 ``Data/``。VX 系游戏根目录**就是**游戏根（没有 www/ 布局）。"""
        if not game_dir.is_dir():
            return None
        for name in ("Data", "data"):
            d = game_dir / name
            if d.is_dir() and (any(d.glob("*.rxdata")) or any(d.glob("*.rvdata"))):
                return d
        # 兜底：大小写不敏感地找一个含 .rxdata/.rvdata 的目录
        for d in sorted(game_dir.iterdir()):
            if d.is_dir() and (any(d.glob("*.rxdata")) or any(d.glob("*.rvdata"))):
                return d
        return None

    def _rel(self, base: Path, path: Path) -> str:
        """相对路径一律相对游戏根。"""
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            return path.name

    # -- 识别 ----------------------------------------------------------

    def detect(self, game_dir: Path) -> EngineInfo:
        info = EngineInfo(engine_id=self.id, display_name=self.display_name, root=game_dir)
        root = Path(game_dir)
        if not root.is_dir():
            return info
        data = self._data_dir(root)
        if data is None:
            return info

        ace = [m for m in _VX_ACE_MARKERS if (data / m).is_file()]
        xp = [m for m in _VX_XP_MARKERS if (data / m).is_file()]
        if not ace and not xp:
            return info

        if ace:
            info.version = "VX Ace"
            info.confidence = min(1.0, 0.5 + 0.12 * len(ace))
            info.evidence.append(
                f"Data/ 下存在 {len(ace)}/{len(_VX_ACE_MARKERS)} 个 VX Ace 数据文件（*.rxdata）"
            )
        else:
            info.version = "XP"
            info.confidence = min(1.0, 0.5 + 0.12 * len(xp))
            info.evidence.append(
                f"Data/ 下存在 {len(xp)}/{len(_VX_XP_MARKERS)} 个 XP 数据文件（*.rvdata）"
            )

        # 运行时 DLL 是版本强证据（VX 也用 .rvdata，靠 DLL 区分）
        dlls = [d.name for d in root.iterdir() if d.suffix.lower() == ".dll"] if root.is_dir() else []
        for d in dlls:
            if d in _VX_ACE_DLLS:
                info.version = "VX Ace"
                info.confidence = min(1.0, info.confidence + 0.2)
                info.evidence.append(f"存在 {d}（VX Ace 运行时）")
                break
            if d in _VX_DLLS:
                info.version = "VX"
                info.confidence = min(1.0, info.confidence + 0.2)
                info.evidence.append(f"存在 {d}（VX 运行时）")
                break
            if d in _XP_DLLS:
                info.version = "XP"
                info.confidence = min(1.0, info.confidence + 0.2)
                info.evidence.append(f"存在 {d}（XP 运行时）")
                break

        # Marshal 头是最好的证据：直接看 System 文件是 4.8 还是 4.9
        for cand in ("System.rxdata", "System.rvdata"):
            f = data / cand
            if f.is_file():
                try:
                    magic = f.read_bytes()[:2]
                except OSError as exc:  # pragma: no cover - 权限问题
                    info.evidence.append(f"{cand} 读取失败：{exc}")
                    break
                if magic == MAGIC_49:
                    info.version = "VX Ace"
                    info.evidence.append(f"{cand} 是 Marshal 4.9（Ruby 1.9+ ⇒ VX Ace）")
                elif magic == MAGIC_48:
                    info.evidence.append(f"{cand} 是 Marshal 4.8（Ruby 1.8 ⇒ VX/XP）")
                else:
                    info.evidence.append(f"{cand} 的 Marshal 头异常：{magic!r}")
                    info.confidence *= 0.5
                break

        if (root / "Game.ini").is_file():
            info.evidence.append("存在 Game.ini")
            info.confidence = min(1.0, info.confidence + 0.05)
        if not info.version:
            info.version = "VX/VX Ace/XP"
        return info

    # -- 抽取 ----------------------------------------------------------

    def extract_text(self, game_dir: Path) -> tuple[list[TextUnit], ExtractReport]:
        report = ExtractReport(adapter=self.id)
        units: list[TextUnit] = []
        root = Path(game_dir)
        data = self._data_dir(root)
        if data is None:
            report.errors.append("找不到 Data/ 目录（或里面没有 .rxdata/.rvdata）")
            return units, report

        latin = 0
        for f in sorted(data.iterdir()):
            if not f.is_file() or f.suffix.lower() not in DATA_SUFFIXES:
                continue
            stem = f.stem.lower()
            if stem in SKIP_FILES:
                report.skipped[f.name] = 1
                continue
            report.files_scanned += 1
            rel = self._rel(root, f)
            try:
                units.extend(self._extract_file(f, rel, report))
                report.files_matched += 1
            except (MarshalError, OSError, ValueError) as exc:
                report.errors.append(f"{rel}: {exc}")
                continue
            latin += 1

        report.units = len(units)
        log.debug("VX 适配器抽到 %d 条文本（%d 个文件）", len(units), latin)
        return units, report

    def _extract_file(self, f: Path, rel: str, report: ExtractReport) -> list[TextUnit]:
        raw = f.read_bytes()
        magic = raw[:2]
        if magic not in (MAGIC_48, MAGIC_49):
            raise MarshalError(f"Marshal 头异常：{magic!r}")
        obj = loads(raw)
        stem = f.stem
        if stem == "System":
            return self._extract_system(obj, rel, magic)
        if stem == "MapInfos":
            return self._extract_mapinfos(obj, rel, magic)
        if stem == "CommonEvents":
            return self._extract_common_events(obj, rel, magic)
        if stem.startswith("Map") and stem[3:].isdigit():
            return self._extract_map(obj, rel, magic)
        if stem in DATABASE_FIELDS:
            return self._extract_database(obj, rel, magic, stem)
        report.skipped[Path(rel).name] = 1
        return []

    def _mk(
        self,
        source: str,
        rel: str,
        pointer: str,
        magic: bytes,
        kind: TextKind,
        *,
        context: str = "",
        siblings: list[str] | None = None,
    ) -> TextUnit:
        return TextUnit(
            uid=f"{rel}:{pointer}",
            source=source,
            kind=kind,
            context=context,
            location=TextLocation(
                file=rel,
                pointer=pointer,
                encoding="utf-8" if magic == MAGIC_49 else "cp932",
                siblings=siblings or [],
            ),
            engine=self.id,
            adapter=self.id,
        )

    def _extract_database(
        self, obj: Any, rel: str, magic: bytes, stem: str
    ) -> list[TextUnit]:
        """``Actors`` 这类"数组，下标 0 是 None，后面是对象"。"""
        fields = DATABASE_FIELDS[stem]
        out: list[TextUnit] = []
        if not isinstance(obj, list):
            raise MarshalError(f"{stem} 顶层不是数组，而是 {type(obj).__name__}")
        for i, item in enumerate(obj):
            if not isinstance(item, RValue):
                continue
            if item.cls not in (f"RPG::{stem[:-1]}", stem[:-1], f"RPG::{stem}"):
                # 类名对不上就跳过（同名的别的对象），但不要因此整文件失败
                if item.cls and not item.cls.endswith(stem[:-1]):
                    continue
            for k, v in item.ivars.items():
                name = getattr(k, "name", k)
                if name not in fields:
                    continue
                if not _is_translatable(v, name):
                    continue
                out.append(
                    self._mk(
                        v,
                        rel,
                        f"/{i}/{SYM_PREFIX}{name}",
                        magic,
                        self._kind_for(name),
                        context=f"{stem}#{i}",
                    )
                )
        return out

    def _extract_mapinfos(self, obj: Any, rel: str, magic: bytes) -> list[TextUnit]:
        fields = DATABASE_FIELDS["MapInfos"]
        out: list[TextUnit] = []
        if not isinstance(obj, list):
            raise MarshalError("MapInfos 顶层不是数组")
        for i, item in enumerate(obj):
            if not isinstance(item, RValue):
                continue
            for k, v in item.ivars.items():
                name = getattr(k, "name", k)
                if name not in fields or not _is_translatable(v, name):
                    continue
                out.append(
                    self._mk(
                        v, rel, f"/{i}/{SYM_PREFIX}{name}", magic, TextKind.UI_LABEL
                    )
                )
        return out

    def _extract_system(self, obj: Any, rel: str, magic: bytes) -> list[TextUnit]:
        """``System`` 是对象：标题、货币单位、菜单用词。"""
        out: list[TextUnit] = []
        if not isinstance(obj, RValue):
            raise MarshalError("System 顶层不是对象")
        version = "VXAce" if magic == MAGIC_49 else "XP"
        fields = SYSTEM_FIELDS[version]
        for k, v in obj.ivars.items():
            name = getattr(k, "name", k)
            if name in fields:
                if name == "words" and isinstance(v, list):
                    out.extend(self._system_words(v, rel, magic, name))
                    continue
                if _is_translatable(v, name):
                    out.append(
                        self._mk(
                            v,
                            rel,
                            f"/{SYM_PREFIX}{name}",
                            magic,
                            TextKind.UI_LABEL,
                            context="System",
                        )
                    )
                continue
            if name == "terms" and isinstance(v, RValue):
                out.extend(self._terms(v, rel, magic))
        return out

    def _system_words(
        self, words: list[Any], rel: str, magic: bytes, parent: str
    ) -> list[TextUnit]:
        """XP 的 ``@words`` 是一串界面用词。"""
        out: list[TextUnit] = []
        for i, w in enumerate(words):
            if isinstance(w, str) and _is_translatable(w):
                out.append(
                    self._mk(
                        w,
                        rel,
                        f"/{SYM_PREFIX}{parent}/{i}",
                        magic,
                        TextKind.UI_LABEL,
                        context="System/@words",
                    )
                )
        return out

    def _terms(self, terms: RValue, rel: str, magic: bytes) -> list[TextUnit]:
        """VX Ace 的 ``@terms``：``@basic``/``@commands``/``@params``/``@messages``。"""
        out: list[TextUnit] = []
        for k, v in terms.ivars.items():
            name = getattr(k, "name", k)
            if name not in TERMS_FIELDS:
                continue
            base = f"/{SYM_PREFIX}terms/{SYM_PREFIX}{name}"
            if isinstance(v, list):
                for i, s in enumerate(v):
                    if isinstance(s, str) and _is_translatable(s):
                        out.append(
                            self._mk(
                                s,
                                rel,
                                f"{base}/{i}",
                                magic,
                                TextKind.UI_LABEL,
                                context="System/@terms",
                            )
                        )
            elif isinstance(v, str) and _is_translatable(v):
                out.append(
                    self._mk(
                        v,
                        rel,
                        base,
                        magic,
                        TextKind.UI_LABEL,
                        context="System/@terms",
                    )
                )
        return out

    def _extract_common_events(self, obj: Any, rel: str, magic: bytes) -> list[TextUnit]:
        """``CommonEvents``：数组，元素是对象，``@list`` 里是指令。"""
        out: list[TextUnit] = []
        if not isinstance(obj, list):
            raise MarshalError("CommonEvents 顶层不是数组")
        for i, item in enumerate(obj):
            if not isinstance(item, RValue):
                continue
            for k, v in item.ivars.items():
                name = getattr(k, "name", k)
                if name == "name" and _is_translatable(v, name):
                    out.append(
                        self._mk(
                            v,
                            rel,
                            f"/{i}/{SYM_PREFIX}name",
                            magic,
                            TextKind.NAME,
                            context=f"CommonEvents#{i}",
                        )
                    )
                elif name == "list" and isinstance(v, list):
                    out.extend(
                        self._commands_text(
                            v, rel, magic, f"/{i}/{SYM_PREFIX}list", f"CommonEvents#{i}"
                        )
                    )
        return out

    def _extract_map(self, obj: Any, rel: str, magic: bytes) -> list[TextUnit]:
        """地图：``@events`` 字典（事件 ID → 事件对象），每个事件有 ``@pages``。"""
        out: list[TextUnit] = []
        if not isinstance(obj, RValue):
            raise MarshalError("地图顶层不是对象")
        events = None
        for k, v in obj.ivars.items():
            if getattr(k, "name", k) == "events":
                events = v
                break
        if not isinstance(events, dict):
            return out
        for ev_id, ev in events.items():
            if not isinstance(ev, RValue):
                continue
            pages = None
            for k, v in ev.ivars.items():
                if getattr(k, "name", k) == "pages" and isinstance(v, list):
                    pages = v
                    break
            if pages is None:
                continue
            for pi, page in enumerate(pages):
                if not isinstance(page, RValue):
                    continue
                for k, v in page.ivars.items():
                    if getattr(k, "name", k) == "list" and isinstance(v, list):
                        base = (
                            f"/{SYM_PREFIX}events/{SYM_PREFIX}{ev_id}"
                            f"/{SYM_PREFIX}pages/{pi}/{SYM_PREFIX}list"
                        )
                        out.extend(
                            self._commands_text(
                                v, rel, magic, base, f"Map {rel}#{ev_id}"
                            )
                        )
        return out

    def _commands_text(
        self, cmds: list[Any], rel: str, magic: bytes, base: str, context: str
    ) -> list[TextUnit]:
        r"""把连续 ``401``（显示文字）合成一条，其余按指令取正文。

        ▲ 为什么要合成：与 MV 完全一样的问题 —— VX 系的 ``401`` 也是
        "消息框的第 i 行"，一句话被作者按显示宽度切成好几条。
        分开翻只会得到半句话（MV 那边实测 48% 的 401 组被切开）。
        """
        out: list[TextUnit] = []
        i = 0
        n = len(cmds)
        while i < n:
            code = _command_code(cmds[i])
            params = _command_params(cmds[i])
            if code == 401 and params is not None:
                # 收集连续的 401
                group: list[tuple[int, str]] = []
                j = i
                while j < n:
                    c2 = _command_code(cmds[j])
                    p2 = _command_params(cmds[j])
                    if c2 != 401 or p2 is None or not p2 or not isinstance(p2[0], str):
                        break
                    group.append((j, p2[0]))
                    j += 1
                texts = [t for _, t in group]
                joined = "\n".join(texts)
                if _is_translatable(joined, "message", short_ok=True):
                    # ▲ 指针里的第一个数字是**指令在数组里的下标**（``idx``），
                    #   第二个才是参数下标（401 的参数 0 就是正文）。
                    #   第一版把参数下标当成了指令下标，于是所有 401 的指针
                    #   都落在同一条指令上 —— 回写时全写到一个位置，
                    #   表面上"成功"，实际只改了一行。
                    ptrs = [f"{base}/{idx}/{SYM_PREFIX}parameters/0" for idx, _ in group]
                    out.append(
                        self._mk(
                            joined,
                            rel,
                            ptrs[0],
                            magic,
                            TextKind.DIALOGUE,
                            context=context,
                            siblings=ptrs[1:],
                        )
                    )
                i = j
                continue
            if code in EVENT_COMMAND_SKIP or code is None or params is None:
                i += 1
                continue
            idxs = EVENT_COMMAND_LIST_ARG.get(code)
            if idxs:
                for pi in idxs:
                    if pi >= len(params):
                        continue
                    val = params[pi]
                    if isinstance(val, list):
                        # 选项数组：每个选项一条
                        for si, s in enumerate(val):
                            if isinstance(s, str) and _is_translatable(s, short_ok=True):
                                out.append(
                                    self._mk(
                                        s,
                                        rel,
                                        f"{base}/{i}/{SYM_PREFIX}parameters/{pi}/{si}",
                                        magic,
                                        TextKind.UI_LABEL
                                        if code == 102
                                        else TextKind.DIALOGUE,
                                        context=context,
                                    )
                                )
                    elif isinstance(val, str) and _is_translatable(val, short_ok=True):
                        out.append(
                            self._mk(
                                val,
                                rel,
                                f"{base}/{i}/{SYM_PREFIX}parameters/{pi}",
                                magic,
                                TextKind.UI_LABEL if code == 102 else TextKind.DIALOGUE,
                                context=context,
                            )
                        )
            i += 1
        return out

    @staticmethod
    def _kind_for(field: str) -> TextKind:
        """按字段名给一个 **已存在的** TextKind。

        ▲ ``TextKind`` 里没有 ``LABEL``/``DESCRIPTION``/``CHOICE`` 这些名字，
        写错会在运行期抛 ``AttributeError``（枚举成员是编译期属性，
        类型检查器不一定拦得住）。
        """
        if field.endswith("description") or field.endswith("profile"):
            return TextKind.ITEM_DESC
        if field.startswith("message"):
            return TextKind.BATTLE_MESSAGE
        if field.endswith("note"):
            return TextKind.NOTE
        return TextKind.NAME

    # -- 贴图 / 字体 ----------------------------------------------------

    def extract_images(self, game_dir: Path) -> tuple[list[ImageAsset], ExtractReport]:
        """VX 系的贴图在 ``Graphics/``（XP/VX/VX Ace 都一样），**不加密**。

        只收常见位图；缩略图/图标表之类交给上层按尺寸筛。
        """
        report = ExtractReport(adapter=self.id)
        assets: list[ImageAsset] = []
        root = Path(game_dir)
        gdir = None
        for name in ("Graphics", "graphics"):
            if (root / name).is_dir():
                gdir = root / name
                break
        if gdir is None:
            return assets, report
        exts = {".png", ".bmp", ".jpg", ".jpeg"}
        for f in sorted(gdir.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            report.files_scanned += 1
            assets.append(
                ImageAsset(
                    uid=self._rel(root, f),
                    path=self._rel(root, f),
                    kind="texture",
                )
            )
        report.files_matched = len(assets)
        return assets, report

    def discover_fonts(self, game_dir: Path) -> list[FontCoverage]:
        """VX 系的字体在 ``Fonts/`` 下，多为 ``.ttf``/``.fon``。

        路径相对**游戏根**取，与 :meth:`wire_fonts` 保持一致。
        """
        root = Path(game_dir)
        out: list[FontCoverage] = []
        for name in ("Fonts", "fonts"):
            d = root / name
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".ttc", ".fon"):
                    rel = self._rel(root, f)
                    out.append(FontCoverage(font_id=rel, path=rel, family=f.stem))
            break
        return out

    def wire_fonts(self, out_dir: Path, installed: dict[str, str], **_: Any) -> list[str]:
        """把补好的字体放进 ``Fonts/``，并说明"还差哪一步"。

        ## 为什么不自动改数据文件

        VX 系的字体是**脚本层**决定的（``Font.default_name = ["Verdana"]``
        写在 ``Scripts.rxdata`` 里的 ``Window_Base``），不是配置文件。
        ``Scripts.rxdata`` 是 Ruby 源码，自动改写等于替作者改代码 ——
        改了很可能让游戏起不来，而且用户完全看不出为什么。
        所以这里只把字体**放到该放的位置**并返回一句人话，
        让用户自己（或用别的方式）改脚本里的字体名。

        ``installed`` 是 ``{原字体名: 目标文件名}``（与基类一致）；
        返回**人类可读的说明**列表（进日志与报告），不是文件名列表。
        """
        notes: list[str] = []
        if not installed:
            return notes
        root = Path(out_dir)
        # 目录名保持原样：VX 系脚本里写的就是 "Fonts/xxx.ttf"
        font_dir = root / "Fonts"
        if not font_dir.is_dir():
            font_dir.mkdir(parents=True, exist_ok=True)
            notes.append("新建字体目录：Fonts/")

        written: list[str] = []
        for _orig, new in installed.items():
            src = Path(new)
            if not src.is_file():
                continue
            dst = font_dir / src.name
            if dst.exists() and dst.read_bytes() == src.read_bytes():
                continue
            dst.write_bytes(src.read_bytes())
            written.append(f"Fonts/{src.name}")

        if written:
            notes.append(f"已放入字体：{', '.join(written)}")
            notes.append(
                "▲ VX 系的字体名由脚本决定（Scripts.rxdata 里的 "
                "Font.default_name）。已把字体放进 Fonts/，"
                "但要让游戏真正用上，还需把脚本里的字体名改成该文件名"
                "（本工具不自动改 Ruby 源码，以免改坏游戏）。"
            )
        return notes

    # -- 回写 ----------------------------------------------------------

    def apply(
        self,
        game_dir: Path,
        out_dir: Path,
        units: list[TextUnit],
        translations: dict[str, str],
        *,
        font_patches: Any = None,
        rebuilt_images: Any = None,
    ) -> ApplyResult:
        """按文件分组，逐个"读-改-写"。

        ▲ **必须保持原文件的 Marshal magic**：4.8 的文件里字符串是 CP932，
        用 4.9 写出去 Ruby 会按 UTF-8 解释 → 乱码。所以每个文件的 magic
        从它自己读到的头里取，不猜、也不统一。
        """
        res = ApplyResult(ok=False, out_dir=str(out_dir))
        root = Path(game_dir)
        out_root = Path(out_dir)
        # ▲ prepare_out 是**静态方法**（签名 `(game_dir, out_dir)`），
        #   写成 `self.prepare_out(out_dir)` 会 TypeError（少一个参数）。
        self.prepare_out(game_dir, out_dir)

        by_file: dict[str, list[TextUnit]] = {}
        for u in units:
            if u.uid in translations and translations[u.uid]:
                by_file.setdefault(u.location.file, []).append(u)
        if not by_file:
            res.ok = True
            res.warnings.append("没有需要回写的文本")
            return res

        for rel, group in sorted(by_file.items()):
            src = root / rel
            if not src.is_file():
                res.files_skipped += 1
                res.warnings.append(f"源文件不存在，跳过：{rel}")
                continue
            try:
                raw = src.read_bytes()
                magic = raw[:2]
                obj = loads(raw)
                applied = 0
                failed: list[str] = []
                for u in group:
                    val = translations[u.uid]
                    if not self._write_unit(obj, u, val):
                        failed.append(u.location.pointer)
                        continue
                    applied += 1
                dst = out_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(dumps(obj, magic=magic))
                res.files_written += 1
                if failed:
                    res.warnings.append(
                        f"{rel}：{applied} 条写入成功，{len(failed)} 条定位失败"
                        f"（{', '.join(failed[:3])}{'…' if len(failed) > 3 else ''}）"
                    )
            except (MarshalError, OSError, ValueError) as exc:
                res.files_skipped += 1
                res.warnings.append(f"{rel} 回写失败：{exc}")

        # 未改动但需要保留的数据文件也要拷过去，否则 out/ 不完整
        copied = self._copy_untouched(root, out_root, set(by_file))
        res.files_written += copied

        if font_patches:
            res.warnings.append("VX 系的字体替换需手动确认（已跳过自动改写数据文件）")

        res.ok = res.files_written > 0 and not res.error
        return res

    def _write_unit(self, obj: Any, u: TextUnit, value: str) -> bool:
        """写入一条；有 ``siblings`` 时按行拆回各个槽位。"""
        from .rpgmaker import _split_across_slots  # noqa: PLC0415 - 复用已验证的实现

        ptrs = [u.location.pointer, *u.location.siblings]
        if len(ptrs) == 1:
            return set_pointer(obj, ptrs[0], value)
        parts = _split_across_slots(value, len(ptrs), u.source)
        ok = True
        for p, part in zip(ptrs, parts, strict=False):
            if not set_pointer(obj, p, part):
                ok = False
        return ok

    def _copy_untouched(self, root: Path, out_root: Path, handled: set[str]) -> int:
        """把没改过的数据文件原样复制，让 out/ 是一份完整可玩的游戏。

        只复制 ``Data/`` 下的 ``.rxdata``/``.rvdata`` —— 贴图与音频由
        各自的阶段处理，这里不越界。
        """
        data = self._data_dir(root)
        if data is None:
            return 0
        n = 0
        for f in data.iterdir():
            if not f.is_file() or f.suffix.lower() not in DATA_SUFFIXES:
                continue
            rel = self._rel(root, f)
            if rel in handled:
                continue
            dst = out_root / rel
            if dst.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(f.read_bytes())
                n += 1
            except OSError as exc:  # pragma: no cover
                log.debug("复制 %s 失败：%s", rel, exc)
        return n
