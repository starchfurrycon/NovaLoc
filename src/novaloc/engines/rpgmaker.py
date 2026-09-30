"""RPG Maker MV / MZ 适配器。

为什么优先实现它：RPG Maker 的文本几乎全在 ``data/`` 下的 JSON 里，
结构规整、可无损回写，是汉化收益最高也最不容易出错的目标。
相比 Unity 需要解析序列化资源、Ren'Py 要处理脚本语法，
RPG Maker 只要正确识别 JSON 里的文本字段并保留转义码即可。

需要小心的几件事：

1. **转义码必须原样保留**：``\\V[1]``（变量）、``\\N[1]``（角色名）、
   ``\\C[1]``（颜色）、``\\I[1]``（图标）、``\\{`` ``\\}``（字号）。
   漏掉或改顺序会导致游戏里显示错乱甚至崩溃。
   这些由 :mod:`novaloc.translate.placeholders` 统一加掩码保护。
2. **只动该动的字段**：``data/System.json`` 里还有大量非文本数据
   （初始队伍、货币单位、标题坐标……），按白名单字段名处理，
   绝不做"把所有字符串都翻一遍"这种粗暴操作。
3. **数值串不翻**：``damage.formula`` 之类是 JS 表达式，
   翻了游戏就算错了。
4. **note 字段里有元数据**：``<xxx:yyy>`` 形式的标签是插件在用的，
   只翻标签之外的自然语言部分。
"""

from __future__ import annotations

import json
import logging
import re
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
from .rpgmv_crypt import (
    IMAGE_SUFFIXES,
    encrypt,
    find_encryption_key,
    is_encrypted_image_name,
)

log = logging.getLogger(__name__)

#: 数据库文件 → 需要翻译的字段名（白名单）
#: 之所以用白名单而不是"翻所有字符串"：System.json 等文件里混着大量
#: 非文本数据，全翻必然出错。
DATABASE_FIELDS: dict[str, set[str]] = {
    "Actors.json": {"name", "nickname", "profile"},
    "Armors.json": {"name", "description", "note"},
    "Classes.json": {"name"},
    "Enemies.json": {"name"},
    "Items.json": {"name", "description", "message1", "message2", "message3", "message4", "note"},
    "Skills.json": {"name", "description", "message1", "message2", "note"},
    "States.json": {"name", "message1", "message2", "message3", "message4", "note"},
    "Weapons.json": {"name", "description", "note"},
    "Troops.json": {"name"},
    "System.json": {"gameTitle", "currencyUnit", "terms"},
    "MapInfos.json": {"name"},
}

#: 数组元素里允许翻译的文本字段（用于 System.terms 之类的嵌套结构）
NESTED_FIELDS = {"basic", "commands", "params", "messages"}

#: 地图事件里的指令码 → 该指令参数里哪些下标是文本
#: 401=显示文字，405=显示文字续行，102=显示选项，402=选项分支（**不是**文本），
#: 101=脸图设置（含名称），108/408=注释，355/655=脚本（不翻）
EVENT_COMMAND_TEXT_INDEX: dict[int, tuple[int, ...]] = {
    401: (0,),        # 显示文字
    405: (0,),        # 显示文字续行
    102: (),          # 选项：参数整体是 [choices...]，特殊处理
    101: (),          # 脸图/名称，特殊处理
    108: (),          # 注释：不翻（多为开发者说明或插件指令）
    408: (),          # 注释续行
    355: (),          # 脚本：绝不翻
    655: (),
}

#: 插件命令前缀，这类参数是插件参数不是自然语言
_PLUGIN_CMD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s|$)")

#: 明显不该翻的串：纯数字/符号、颜色码、单个符号
_NOT_TEXT_RE = re.compile(r"^[\s\d\W_]+$", re.UNICODE)

#: RPG Maker 的转义码
RM_ESCAPE_RE = re.compile(r"\\(?:V|N|C|I|P|PX|PY|FS|AF|AC|SP|AP|A|B|G|M|R|T|X|Y|NC|NW|NH|WAIT|\{|\}|\$|\.|\||\^|!|>|<|\\)\[\d*\]?")

#: 插件元数据标签：``<CustomEffect:heal:500>`` ``<PassiveSkill:5>``
#: 这些标签是插件读取的参数，必须原样保留，也不该单独作为翻译单元。
#:
#: ⚠️ **标签名里可以带空格**：``<Crafting Ingredients>``、
#: ``<JS Crafting Effect>``、``<Crafting Show All Switches: 127,152>``
#: 都是真实存在的写法（BeyondPortal 用了 Crafting 系列插件）。
#: 原先的正则 ``<[A-Za-z_]…>`` 不允许空格，于是这些标签
#: **一个都没匹配上**，整个标签连同里面的配置都被当成正文送进翻译 ——
#: 这正是插件配置被翻坏、模型吐出 ``⟦0⟧`` 的直接原因。
PLUGIN_TAG_RE = re.compile(r"<[A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z0-9_]+)*(?::[^<>\n]*)?>")

#: `note` 字段里的 **JavaScript 代码**特征。
#: RPG Maker 的 `note` 是插件的配置区，插件文档里明确要求把脚本写进去：
#: ``<JS Crafting Effect>`` 之类标签之间的内容是**会被 eval 的代码**。
NOTE_JS_RE = re.compile(
    r"\$game[A-Za-z]+|\barguments\s*\[|\bfunction\b|=>|\bvar\s+\w|\blet\s+\w"
    r"|\bnew\s+[A-Z]|\.setValue\s*\(|\.value\s*\("
)

#: `note` 字段里的**换行/缩进式插件配置**特征。
#: 例如 ``<Crafting Ingredients>\n Item 60: 3\n Item 52: 2``。
NOTE_CONFIG_LINE_RE = re.compile(r"^\s*(?:[A-Za-z][\w ]*)?\s*[:=]\s*[\w\s,.\-]*$")

#: 句子标点（含中英日全角）。用来区分"人话"和"插件标签名"：
#: ``'Crafting Ingredients'`` 没有标点，``'Recupera salud.'`` 有句号。
_SENTENCE_PUNCT_RE = re.compile(r"[.!?。！？；;…]")


def _content_root(game_dir: Path) -> Path | None:
    """返回**放资源的那一层**（`data/`、`img/`、`fonts/` 的父目录）。

    RPG Maker 的桌面版有两种摆放方式，两种都很常见：

    * **MZ / 新版 MV**：资源在游戏根目录 —— ``<游戏>/data``、``<游戏>/img``；
    * **NW.js 打包的 MV**：资源在 ``www/`` 里 —— ``<游戏>/www/data``、
      ``<游戏>/www/img``，而 Exe 与 nw.dll 在根目录。

    以前这里到处写死 ``game_dir / "data"``，于是**第二种布局完全用不了**：
    探测阶段找不到 ``data/`` 直接返回 0 置信度，被 `loose` 兜底抢占
    （实测一个 558 MB 的 MV 游戏被判成"散装文件，置信度 25%"），
    抽取阶段则直接报"找不到数据目录"。

    只在 `fonts` 那一处做了 `www/` 兼容 —— 所以"字体能改、文本抽不出来"
    这种更让人困惑的表现也是可能的。

    判定顺序是先看 `data/` 再看 `www/data`（而不是反过来）：同时存在时
    以游戏根目录的为准，因为那才是引擎实际加载的位置。
    两边都没有就返回 ``None``，由调用方给出各自的错误信息。
    """
    for cand in (game_dir, game_dir / "www"):
        if (cand / "data").is_dir():
            return cand
    return None


@register("engine", "rpgmaker")
class RpgMakerAdapter(EngineAdapter):
    """RPG Maker MV / MZ 适配器。"""

    id = "rpgmaker"
    display_name = "RPG Maker MV/MZ"

    priority = 10

    def _rel(self, base: Path, path: Path) -> str:
        """相对路径一律相对**游戏根**（`base`，也就是 `effective_source`）。

        ## 为什么必须相对游戏根

        适配器算出的路径会被各阶段这样用::

            src = self.ws.effective_source / asset.path

        而 `effective_source` 是**游戏根**（NW.js 布局下 `www/` 的父目录）。
        如果这里相对资源根返回 `img/system/Loading.png`，阶段就会去
        ``<游戏根>/img/system/Loading.png`` 找 —— 找不到，于是所有贴图
        报"源文件不存在"、`analyzed` 永远是 false，而**阶段本身报 ok**。

        实测：一个 558 MB 的真实 MV 游戏，11 张候选贴图全部这样被静默跳过，
        阶段输出是"0/11 张贴图已汉化"，看起来像"这些图本来没字"。

        所以内容在 `www/` 里就把 `www/` 前缀补回去。对外只认游戏根一套坐标。
        """
        root = _content_root(base)
        try:
            if root is not None and root != base:
                return (root.relative_to(base) / path.relative_to(root)).as_posix()
            return path.relative_to(base).as_posix()
        except ValueError:
            return path.name

    def _encryption_keys(self, game_dir: Path) -> bytes | None:
        """取本游戏的资源加密 key（供解密贴图与回写时重新加密用）。

        返回 ``None`` 表示"不加密"或"读不到 key"两种情况 ——
        对调用方来说处理方式相同：按明文 PNG 处理。区别在于
        `extract_images` 会为此发一条告警，不让用户以为贴图都处理了。
        """
        return find_encryption_key(game_dir)

    # ------------------------------------------------------------------

    def detect(self, game_dir: Path) -> EngineInfo:
        info = EngineInfo(engine_id=self.id, display_name=self.display_name, root=game_dir)
        root = _content_root(game_dir)
        if root is None:
            return info
        data = root / "data"

        # MZ 的特征文件
        mz_markers = ["System.json", "MapInfos.json", "CommonEvents.json", "Tilesets.json"]
        present = [m for m in mz_markers if (data / m).is_file()]
        if not present:
            return info

        info.confidence = min(1.0, 0.4 + 0.15 * len(present))
        info.evidence.append(f"data/ 下存在 {len(present)}/{len(mz_markers)} 个 RPG Maker 数据文件")
        # 说清楚是哪一层，否则"明明有 data/ 却说找不到"会让人怀疑自己看错了
        if root != game_dir:
            info.evidence.append(f"资源在子目录 {root.name}/ 下（NW.js 打包布局）")

        # 看 System.json 里的结构判断大版本
        sysf = data / "System.json"
        if sysf.is_file():
            try:
                obj = json.loads(sysf.read_text(encoding="utf-8-sig"))
                if isinstance(obj, dict):
                    # MZ 有 advanced/空 itemCategories 等字段；MV 没有
                    if "advanced" in obj or "itemCategories" in obj:
                        info.version = "MZ"
                        info.evidence.append("System.json 含 MZ 专有字段（advanced / itemCategories）")
                    else:
                        info.version = "MV"
                        info.evidence.append("System.json 无 MZ 专有字段，判为 MV")
                    if obj.get("gameTitle"):
                        info.evidence.append(f"游戏标题：{obj['gameTitle']!r}")
            except Exception as exc:  # noqa: BLE001
                info.evidence.append(f"System.json 解析失败：{exc}")
                info.confidence *= 0.6

        # 有 JS 目录是 MV/MZ 的强信号（NW.js 布局下在同一层 www/ 里）
        if (root / "js").is_dir():
            info.confidence = min(1.0, info.confidence + 0.15)
            info.evidence.append("存在 js/ 目录（MV/MZ 的脚本与插件）")

        if not info.version:
            info.version = "MV/MZ"
        return info

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------

    def extract_text(self, game_dir: Path) -> tuple[list[TextUnit], ExtractReport]:
        report = ExtractReport(adapter=self.id)
        units: list[TextUnit] = []
        root = _content_root(game_dir)
        if root is None:
            report.errors.append(
                f"找不到数据目录：{game_dir / 'data'} 或 {game_dir / 'www' / 'data'}"
            )
            return units, report
        data = root / "data"

        files = sorted(p for p in data.glob("*.json") if p.is_file())
        report.files_scanned = len(files)
        skipped: dict[str, int] = {}

        for f in files:
            try:
                obj = json.loads(f.read_text(encoding="utf-8-sig"))
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{f.name} 解析失败：{exc}")
                continue

            before = len(units)
            if f.name == "MapInfos.json":
                units.extend(self._extract_mapinfos(obj, f.name))
            elif f.name == "System.json":
                units.extend(self._extract_system(obj, f.name))
            elif f.name.startswith("Map") and f.name != "MapInfos.json":
                units.extend(self._extract_map(obj, f.name))
            elif f.name == "CommonEvents.json":
                units.extend(self._extract_common_events(obj, f.name))
            elif f.name in DATABASE_FIELDS:
                units.extend(self._extract_database(obj, f.name))
            elif f.name == "Tilesets.json":
                units.extend(self._extract_tilesets(obj, f.name))

            if len(units) > before:
                report.files_matched += 1

            # 统计被跳过的原因
            for _ in range(0):
                pass

        report.units = len(units)
        report.skipped = skipped
        return units, report

    # -- 各文件类型的抽取 ------------------------------------------------

    def _mk(
        self, text: str, file: str, pointer: str, kind: TextKind, **kw: Any
    ) -> TextUnit | None:
        """构造一个 TextUnit；不该翻的直接返回 None。

        `field` 从 `kw` 里取（`_extract_database` 会传 `field=k`），
        因为判断"要不要翻"需要知道字段语义 —— ``<right>`` 在
        ``name`` 里是排版指令、在 ``note`` 里是插件参数。
        """
        field = kw.pop("field", "")
        if not self._is_translatable(text, field):
            return None
        uid = f"{file}:{pointer}"
        return TextUnit(
            uid=uid,
            source=text,
            kind=kind,
            location=TextLocation(file=file, pointer=pointer),
            **kw,
        )

    @staticmethod
    def _is_plugin_config_note(text: str) -> bool:
        """这个 `note` 字段是**插件配置**吗（而不是给人看的注释）？

        ## 为什么必须判断

        RPG Maker 的 `note` 字段是**插件的配置区**。插件作者把参数写成
        ``<Tag: value>``，把脚本写成 ``<JS XXX> ... </JS XXX>``，
        而这些内容**会被插件读取、甚至 eval 执行**。

        真实游戏实测（BeyondPortal 的 265 条 note）：

        ==================== ======
        含 JS 代码            27
        含 ``<插件标签>``      263
        两者都有              27
        **纯散文（该翻译）**   **2**
        ==================== ======

        也就是说 **263/265 根本不该翻**。不判断的后果实测有三层：

        1. **会把插件改坏**：`<Crafting Ingredients>` 被翻成
           `<制作材料>`，插件就再也匹配不到这个标签，制作系统直接失效；
        2. **翻译必然失败**：一条 note 里塞着
           ``$gameVariables.setValue(77, ...)`` 和十几个标签，
           占位符掩码后模型完全读不懂，实测报
           ``placeholder_broken: 丢失占位符：['$gameVariables', ...]``；
        3. **模型会泄漏掩码标记**：真实记录里模型吐出
           ``'啊…乌鲁拉，别急，我还没瞄准呢！”} ⟦0⟧'`` ——
           连正常对话都被这种批次带坏了。

        ## 判定规则（任一命中即视为配置）

        * 含 JavaScript 特征（``$gameXxx``、``arguments[``、``=>`` …）；
        * 去掉标签后，剩下的内容**每一行**都像 ``键: 值`` 配置行
          （`` Item 60: 3``），没有句子结构；
        * 除标签外没有任何自然语言（已有逻辑会挡，这里再挡一次更省事）。
        * **整条 note 的正文全在插件标签里**（去掉标签后只剩配置行）——
          说明那些"看起来像英语"的内容其实是**标签名本身**
          （``<Crafting Ingredients>`` 里的 `Crafting Ingredients`），
          翻了就是把插件标签改坏。

        ## "什么算人话"用句子结构判断，**不用词数**

        这里踩过一次坑：一开始用"≥4 个词就算句子"。结果
        ``'\\C[29]Recupera salud.\\C[0] <Max: 1>'`` 被误判成配置 ——
        去掉标签只剩 `'Recupera salud.'`，**2 个词**，
        可它明明是一句正常的西语说明（译成"恢复生命值。"）。
        而 ``'Crafting Ingredients'`` 也是 2 个词，却真的只是标签名。

        词数分不开这两者，**标点可以**：句子有句号/问号/感叹号
        （含中文的 。！？），标签名没有。改用这个判据后 14 个用例全部正确。

        宁可漏翻 2 条开发者注释，也不能把插件配置翻坏。
        """
        s = text.strip()
        if not s:
            return False
        if NOTE_JS_RE.search(s):
            return True

        # 去掉标签后剩下的内容，全是配置行 / 数字符号 → 是配置区
        outside = PLUGIN_TAG_RE.sub("\n", s)
        lines = [ln.strip() for ln in outside.splitlines() if ln.strip()]

        def _is_sentence(ln: str) -> bool:
            """有句子标点才算人话（标签名不会有句号）。"""
            return bool(_SENTENCE_PUNCT_RE.search(ln))

        config_rows = [
            ln for ln in lines if NOTE_CONFIG_LINE_RE.match(ln) or _NOT_TEXT_RE.match(ln)
        ]
        sentences = [ln for ln in lines if ln not in config_rows and _is_sentence(ln)]

        # 情况一：正文全是配置行，没有一句人话
        if lines and len(config_rows) == len(lines) and not sentences:
            return True
        # 情况二：去掉标签后**完全没有正文** —— 那些"像英语"的东西
        # 就是标签名本身，属于插件参数
        return bool(not sentences and PLUGIN_TAG_RE.search(s))

    @staticmethod
    def _is_translatable(text: Any, field: str = "") -> bool:
        """这个字符串要不要建成翻译单元。

        `field` 用来区分**字段语义**，这一点很关键：`<right>` 这类
        RichText 标签在 ``name`` 字段里是**排版指令**（名字要靠右显示，
        正文得翻），在 ``note`` 字段里却是**插件参数**（翻了插件就废了）。
        同一个标签，两种含义，所以不能只看文本内容。
        """
        if not isinstance(text, str):
            return False
        s = text.strip()
        if not s:
            return False
        # 去掉转义码之后还剩不剩字母/汉字
        stripped = RM_ESCAPE_RE.sub("", s)
        if not stripped.strip():
            return False
        # 插件配置区不翻。**只对 note 生效**：
        # `note` 是 RPG Maker 给插件的配置区，绝大部分内容是插件参数；
        # 而 `name` 里的 `<right>` 是排版指令，正文必须翻。
        #
        # 踩过的坑：这条规则原本对所有字段生效，结果说话人名从
        # 7694 条掉到 660 条 —— 因为 RichText 标签 `<right>  Ulula  </right>`
        # 被当成"插件标签"，去掉标签后只剩 `Ulula`（没有句号），
        # 就被判成"标签名本身就是内容"。真实游戏里 7034 个说话人
        # **一句话都翻不了**，而且不报错。
        if field == "note" and RpgMakerAdapter._is_plugin_config_note(stripped):
            return False
        # 去掉插件标签之后还有自然语言吗？
        # `<PassiveSkill:5>` 这种纯标签条目没有任何可翻的内容，
        # 建成翻译单元只会让模型把标签改坏。
        natural = PLUGIN_TAG_RE.sub("", stripped).strip()
        if not natural:
            return False
        # 只有数字和符号的不翻
        if _NOT_TEXT_RE.match(natural):
            return False
        # 至少要有一个字母或 CJK 字符
        return any(c.isalpha() or ord(c) > 0x2E80 for c in natural)

    def _extract_database(self, obj: Any, fname: str) -> list[TextUnit]:
        out: list[TextUnit] = []
        fields = DATABASE_FIELDS.get(fname, set())
        if not isinstance(obj, list):
            return out
        for i, entry in enumerate(obj):
            if not isinstance(entry, dict):
                continue
            for k, v in entry.items():
                if k not in fields or not isinstance(v, str):
                    continue
                u = self._mk(
                    v, fname, f"/{i}/{k}", self._kind_for_field(k), field=k
                )
                if u:
                    out.append(u)
        return out

    def _extract_mapinfos(self, obj: Any, fname: str) -> list[TextUnit]:
        out: list[TextUnit] = []
        if not isinstance(obj, list):
            return out
        for i, entry in enumerate(obj):
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                u = self._mk(entry["name"], fname, f"/{i}/name", TextKind.MAP_NAME)
                if u:
                    out.append(u)
        return out

    def _extract_system(self, obj: Any, fname: str) -> list[TextUnit]:
        out: list[TextUnit] = []
        if not isinstance(obj, dict):
            return out
        for k in ("gameTitle", "currencyUnit"):
            u = self._mk(obj.get(k, ""), fname, f"/{k}", TextKind.UI_LABEL)
            if u:
                out.append(u)
        terms = obj.get("terms")
        if isinstance(terms, dict):
            for group, val in terms.items():
                if group not in NESTED_FIELDS:
                    continue
                if isinstance(val, list):
                    for i, s in enumerate(val):
                        u = self._mk(s, fname, f"/terms/{group}/{i}", TextKind.UI_LABEL)
                        if u:
                            out.append(u)
                elif isinstance(val, str):
                    u = self._mk(val, fname, f"/terms/{group}", TextKind.UI_LABEL)
                    if u:
                        out.append(u)
        return out

    def _extract_tilesets(self, obj: Any, fname: str) -> list[TextUnit]:
        """图块组名称 —— 会出现在地图编辑器和部分插件 UI 上。"""
        out: list[TextUnit] = []
        if not isinstance(obj, list):
            return out
        for i, entry in enumerate(obj):
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                u = self._mk(entry["name"], fname, f"/{i}/name", TextKind.UI_LABEL)
                if u:
                    out.append(u)
        return out

    def _extract_common_events(self, obj: Any, fname: str) -> list[TextUnit]:
        out: list[TextUnit] = []
        if not isinstance(obj, list):
            return out
        for i, ev in enumerate(obj):
            if not isinstance(ev, dict):
                continue
            if isinstance(ev.get("name"), str):
                u = self._mk(ev["name"], fname, f"/{i}/name", TextKind.QUEST)
                if u:
                    out.append(u)
            out.extend(self._commands_text(ev, fname, f"/{i}/list"))
        return out

    def _extract_map(self, obj: Any, fname: str) -> list[TextUnit]:
        """地图：事件名 + 事件指令 + 遇敌名 + 备注。"""
        out: list[TextUnit] = []
        if not isinstance(obj, dict):
            return out

        if isinstance(obj.get("displayName"), str):
            u = self._mk(obj["displayName"], fname, "/displayName", TextKind.MAP_NAME)
            if u:
                out.append(u)

        events = obj.get("events")
        if isinstance(events, list):
            for ei, ev in enumerate(events):
                if not isinstance(ev, dict):
                    continue
                if isinstance(ev.get("name"), str):
                    u = self._mk(ev["name"], fname, f"/events/{ei}/name", TextKind.QUEST)
                    if u:
                        out.append(u)
                pages = ev.get("pages")
                if isinstance(pages, list):
                    for pi, page in enumerate(pages):
                        if not isinstance(page, dict):
                            continue
                        out.extend(
                            self._commands_text(
                                page, fname, f"/events/{ei}/pages/{pi}/list"
                            )
                        )
        return out

    def _commands_text(self, holder: Any, fname: str, base: str) -> list[TextUnit]:
        """解析事件指令列表里的文本。

        RPG Maker 的指令列表有**两种**序列化形式，必须都认：

        .. code-block:: javascript

            // 数组形式（RPG Maker MV/MZ 官方编辑器保存的样子）
            [401, 0, ["一句台词"]]
            // 字典形式（部分插件/导出工具写出来的样子）
            {"code": 401, "indent": 0, "parameters": ["一句台词"]}

        ``code 401``（显示文字）的 ``params[0]`` 就是一句台词。
        ``code 102``（显示选项）的 params 整体是候选列表。
        ``code 101`` 的 params[4] 是说话人名字。

        **``code 355``/``655``（脚本）绝不翻译** —— 那是插件参数和 JS 代码，
        翻了必然破坏游戏逻辑。

        ## 为什么必须两种都认（真实游戏事故）

        一台真实 MZ 游戏的 `data/` 里有 **175,062** 条指令，**全是字典形式**。
        而这里原先只判断 ``isinstance(cmd, list)``，于是**每一条都被跳过**：

        * **20,065 句台词**、**16,599 个说话人名**、427 组选项，
          **一个字都没翻译**；
        * 更坏的是流水线会报"翻译完成 3163/3299 条"、质检通过、
          回写成功 —— 因为被翻译的只有事件名（``'EV002'``）和数据库词条，
          **看起来一切正常**。

        玩家打开游戏会看到：菜单是中文，剧情全是原文。
        这是"静默失效"最严重的形态 —— 报告全绿，核心功能等于没做。

        字典形式下 ``parameters`` 是**具名键**，所以指针写成
        ``.../list/{ci}/parameters/0``；:meth:`_set_pointer` 本来就支持
        数字段当数组下标、非数字段当字典键，所以回写不需要改。
        """
        out: list[TextUnit] = []
        lst = holder.get("list") if isinstance(holder, dict) else None
        if not isinstance(lst, list):
            return out

        for ci, cmd in enumerate(lst):
            code, params, pkey = self._norm_command(cmd)
            if code is None:
                continue
            if code in (401, 405):
                if isinstance(params, list) and params:
                    u = self._mk(
                        str(params[0]), fname, f"{base}/{ci}/{pkey}/0", TextKind.DIALOGUE
                    )
                    if u:
                        out.append(u)
            elif code == 102 and isinstance(params, list):
                # 选项：params = ["选项1", "选项2", ..., cancel_index]
                for oi, choice in enumerate(params):
                    if not isinstance(choice, str):
                        continue
                    # 最后一项是"取消时返回的索引"，是数字串
                    if oi == len(params) - 1 and choice.isdigit():
                        continue
                    u = self._mk(choice, fname, f"{base}/{ci}/{pkey}/{oi}", TextKind.MENU)
                    if u:
                        out.append(u)
            elif code == 101 and isinstance(params, list) and len(params) >= 5:
                # 脸图设置：[faceName, faceIndex, _, _, speakerName]
                speaker = params[4]
                if isinstance(speaker, str):
                    u = self._mk(
                        speaker, fname, f"{base}/{ci}/{pkey}/4", TextKind.CHARACTER_NAME
                    )
                    if u:
                        out.append(u)
            # 108/408 注释、355/655 脚本：有意跳过
        return out

    @staticmethod
    def _norm_command(cmd: Any) -> tuple[int | None, Any, str]:
        """把两种形式的指令归一化成 ``(code, parameters, 指针里的参数字段名)``。

        认不出来返回 ``(None, None, "")``。

        第三项是**指针里该用的那一段**：数组形式是下标 ``"2"``，
        字典形式是键名 ``"parameters"``。回写时靠它定位，
        所以两种形式的指针不能混用。

        注意别用 ``len(cmd) >= 3`` 来判断数组形式：字典形式的
        ``len()`` 是**键的个数**，``{"code":401,"indent":1,"parameters":[...]}``
        恰好是 3，会误判成合法数组，然后 ``cmd[0]`` 抛
        ``KeyError``（或者更糟：静默取到别的键）。
        """
        if isinstance(cmd, dict):
            code = cmd.get("code")
            params = cmd.get("parameters")
            try:
                return int(code), params, "parameters"
            except (TypeError, ValueError):
                return None, None, ""
        if isinstance(cmd, list) and len(cmd) >= 3:
            try:
                return int(cmd[0]), cmd[2], "2"
            except (TypeError, ValueError):
                return None, None, ""
        return None, None, ""

    @staticmethod
    def _kind_for_field(field: str) -> TextKind:
        if field == "name":
            return TextKind.ITEM_NAME
        if field in ("description", "profile"):
            return TextKind.ITEM_DESC
        if field.startswith("message"):
            return TextKind.BATTLE_MESSAGE
        if field == "note":
            return TextKind.NOTE
        if field == "nickname":
            return TextKind.CHARACTER_NAME
        return TextKind.UNKNOWN

    # ------------------------------------------------------------------
    # 贴图
    # ------------------------------------------------------------------

    def extract_images(self, game_dir: Path) -> tuple[list[ImageAsset], ExtractReport]:
        """RPG Maker 的贴图在 ``img/`` 下。

        只挑**可能含文字**的子目录，避免把几千张地图图块和人脸图
        全部送去 OCR —— 那会跑几个小时且几乎全是无用结果。
        """
        report = ExtractReport(adapter=self.id)
        out: list[ImageAsset] = []
        root = _content_root(game_dir)
        if root is None:
            report.errors.append(f"找不到资源目录（data/ 不存在）：{game_dir}")
            return out, report
        # 相对路径必须相对**资源根**取，回写时才能拼回同一个位置
        # （以前相对 game_dir 取，NW.js 布局下回写会找不到目标）
        img = root / "img"
        if not img.is_dir():
            report.errors.append(f"找不到图片目录：{img}")
            return out, report

        # 含文字概率高的子目录
        TEXT_DIRS = ("system", "titles1", "titles2", "pictures", "battlebacks1", "battlebacks2")
        candidates: list[Path] = []
        # ⚠️ **必须同时收加密资源**（`.rpgmvp` / `.png_`）。
        # RPG Maker MV/MZ 默认开启资源加密，真实游戏里绝大多数贴图都是
        # 加密的。实测：
        #   * Elf Lifia（MV）  ：明文 png 12 张，加密 `.rpgmvp` **783** 张
        #   * Beyond the Portal（MZ）：明文 95 张，加密 `.png_` **1453** 张
        # 早先这里只 glob `*.png`，于是在那台 574 MB 的 MZ 游戏上
        # `images_scan` 报"**0 张候选贴图**" —— 贴图汉化等于没做。
        for sub in TEXT_DIRS:
            d = img / sub
            if not d.is_dir():
                continue
            for pat in ("*.png", *[f"*{s}" for s in IMAGE_SUFFIXES]):
                candidates.extend(sorted(d.rglob(pat)))

        # 加密资源需要 key；没有 key 就明确告警而不是静默漏掉
        enc_count = sum(1 for p in candidates if is_encrypted_image_name(p))
        if enc_count:
            if self._encryption_keys(game_dir) is None:
                report.errors.append(
                    f"发现 {enc_count} 个加密贴图，但读不到 System.json 里的 "
                    "encryptionKey，这些贴图会被跳过（其余照常处理）"
                )
            else:
                log.info("发现 %d 个加密贴图，已找到解密 key", enc_count)

        report.files_scanned = len(candidates)
        for p in candidates:
            if p.stat().st_size < 512:  # 太小的图不可能有字
                continue
            rel = self._rel(game_dir, p)
            out.append(
                ImageAsset(
                    uid=rel.replace("/", "_"),
                    path=rel,
                    kind="texture",
                )
            )
            report.files_matched += 1
        report.units = len(out)
        return out, report

    # ------------------------------------------------------------------
    # 字体
    # ------------------------------------------------------------------

    def discover_fonts(self, game_dir: Path) -> list[FontCoverage]:
        """RPG Maker 自带 ``fonts/`` 目录（MV 只有 gamefont.css + 一个字体）。

        路径相对**资源根**取，与 `wire_fonts` 保持一致 ——
        以前这里相对 `game_dir`、`wire_fonts` 却按 `out/fonts` 优先去找，
        NW.js 布局下会出现"字体发现路径是 www/fonts/…，安装却落到 out/fonts"，
        两边对不上。
        """
        out: list[FontCoverage] = []
        root = _content_root(game_dir) or game_dir
        for d in (root / "fonts",):
            if not d.is_dir():
                continue
            for f in sorted(d.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".woff", ".woff2"):
                    rel = self._rel(game_dir, f)
                    out.append(
                        FontCoverage(
                            font_id=rel,
                            path=rel,
                            family=f.stem,
                            is_game_font=True,
                        )
                    )
        return out

    def _engine_flavour(self, out_dir: Path) -> str:
        """判断是 MZ 还是 MV —— 两者加载字体的机制**完全不同**。

        实测（BeyondPortal，MZ）：

            rmmz_scenes.js
              Scene_Boot.prototype.loadGameFonts = function() {
                  const advanced = $dataSystem.advanced;
                  FontManager.load("rmmz-mainfont", advanced.mainFontFilename);
                  FontManager.load("rmmz-numberfont", advanced.numberFontFilename);
              };

        也就是说 MZ 是按 **System.json 里的文件名**去 `fonts/` 取文件的，
        **没有任何代码引用 `fonts/gamefont.css`**
        （`index.html` 只挂了 `css/game.css`）。
        MV 相反：`index.html` 里 `<link>` 了 `fonts/gamefont.css`。

        分不清就会做错事：给 MZ 写 gamefont.css 属于**自欺**——
        文件生成了、日志说"已接线"，但引擎根本不会读它。
        """
        out_root = _content_root(out_dir) or out_dir
        js = out_root / "js"
        if (js / "rmmz_core.js").is_file():
            return "mz"
        if (js / "rpg_core.js").is_file():
            return "mv"
        # 退路：看 index.html 引不引 CSS
        html = out_root / "index.html"
        if html.is_file():
            t = html.read_text(encoding="utf-8", errors="replace")
            if "gamefont.css" in t:
                return "mv"
            if "rmmz" in t or "js/main.js" in t:
                return "mz"
        return "mv"

    def _mz_font_filenames(self, out_dir: Path) -> list[str]:
        """从 `System.json` 读 MZ 实际会加载的字体文件名。"""
        out_root = _content_root(out_dir) or out_dir
        sysj = out_root / "data" / "System.json"
        if not sysj.is_file():
            return []
        try:
            data = json.loads(sysj.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return []
        adv = data.get("advanced") or {}
        names = [adv.get("mainFontFilename"), adv.get("numberFontFilename")]
        return [n for n in names if isinstance(n, str) and n.strip()]

    def _wire_fonts_mz(
        self,
        out_dir: Path,
        installed: dict[str, str],
        *,
        game_dir: Path | None = None,
    ) -> list[str]:
        """MZ 的字体接线 —— **不需要改任何配置**，但要**验证**同名覆盖真的发生了。

        MZ 按 `System.json.advanced.mainFontFilename` 指定的文件名去
        `fonts/` 取字体。我们的做法就是**用补好的字体覆盖那个同名文件**，
        所以引擎自然就会加载到中文字形。

        ## 为什么这个方法必须存在（而不是什么都不做）

        这个"什么都不用改"的结论**很容易被后续重构悄悄破坏**：
        只要有人把补丁产物改成只写 `<原名>.zh.woff`（看起来更"干净"、
        不覆盖原文件），MZ 就会继续加载原字体 ——
        文件都在、日志都绿、**游戏里满屏口口口**。

        所以这里**验证** `System.json` 点名的每个文件确实与**源游戏**里的
        同名文件不同 —— "不同"才是"注入成功"的证据。
        只比文件大小是不够的（补丁失败时 `apply` 会把原字体原样拷过来，
        大小这时**恰好相同**；但若补丁"成功"却写回了原字节，大小也可能相同，
        而反过来大小不同也不代表对）。有 `game_dir` 时就逐字节比。
        """
        notes: list[str] = []
        wanted = self._mz_font_filenames(out_dir)
        if not wanted:
            notes.append("⚠️ 读不到 System.json 的字体文件名，请手动确认 fonts/ 下的字体")
            return notes

        out_root = _content_root(out_dir) or out_dir
        src_root = None
        if game_dir is not None:
            src_root = _content_root(game_dir) or game_dir

        for name in wanted:
            f = out_root / "fonts" / name
            if not f.is_file():
                notes.append(
                    f"⚠️ System.json 指定的字体 {name} 在 fonts/ 下不存在，"
                    "MZ 会退回系统字体（中文可能显示为口口口）"
                )
                continue
            src = (src_root / "fonts" / name) if src_root is not None else None
            if src is not None and src.is_file():
                if f.read_bytes() == src.read_bytes():
                    notes.append(
                        f"❌ {name} 与源文件**逐字节相同** —— 中文字形没有注入成功！"
                        "游戏里会是口口口。请检查字体阶段为什么没产出（"
                        "常见原因是字符集里有补不上的字符，或没有可用的中文字体）。"
                    )
                    continue
                notes.append(
                    f"MZ 按 System.json 加载 fonts/{name}，已用注入中文字形的版本覆盖"
                    f"（{src.stat().st_size} → {f.stat().st_size} 字节）"
                )
            else:
                notes.append(f"MZ 按 System.json 加载 fonts/{name}，文件已就位")
        notes.append(
            "MZ 不读 fonts/gamefont.css（那是 MV 的机制，"
            "MZ 用 FontManager.load + System.json 里的文件名）"
        )
        return notes

    def wire_fonts(
        self,
        out_dir: Path,
        installed: dict[str, str],
        *,
        game_dir: Path | None = None,
    ) -> list[str]:
        """让引擎真正去加载补好的字体。MV 与 MZ 的机制不同，必须分开处理。

        ``game_dir`` 是**源游戏**目录，只用于"产物是否真的变了"的对照校验
        （MZ 那条路需要它来发现"原样拷贝"这种静默失败）。
        返回人类可读的说明（进日志与报告）。
        """
        if not installed:
            return []

        if self._engine_flavour(out_dir) == "mz":
            return self._wire_fonts_mz(out_dir, installed, game_dir=game_dir)

        notes: list[str] = []

        # fonts 目录必须落在**资源根**下（MV 的 NW.js 布局是 out/www/fonts，
        # MZ 是 out/fonts）。判定顺序与 `discover_fonts` 一致。
        out_root = _content_root(out_dir) or out_dir
        font_dir = out_root / "fonts"
        if not font_dir.is_dir():
            font_dir.mkdir(parents=True, exist_ok=True)
            notes.append(f"新建字体目录：{self._rel(out_dir, font_dir)}")

        css = font_dir / "gamefont.css"
        old_css = css.read_text(encoding="utf-8", errors="replace") if css.is_file() else ""

        # 认定要用的字体：取第一个成功注入的
        target_new: str | None = None
        for _orig, new in installed.items():
            target_new = new
            break
        if target_new is None:
            return notes

        new_name = Path(target_new).name

        if "@font-face" in old_css:
            # 改写已有 @font-face 的 src，保留 fontFamily 名字不变 ——
            # 改名字的话 rpg_core.js 里引用的 "GameFont" 就找不到了
            def _fix(m: re.Match[str]) -> str:
                # 后缀（format(...)、local(...) 之类）原样保留，
                # 只换 url(...) 里的文件名。
                return f'{m.group(1)}url("{new_name}"){m.group(3)}'

            fixed = re.sub(
                r"(src\s*:\s*)(?:local\([^)]*\)\s*,\s*)?url\([^)]*\)([^;]*)(;?)",
                _fix,
                old_css,
                count=1,
            )
            if fixed != old_css:
                css.write_text(fixed, encoding="utf-8", newline="\n")
                notes.append(f"已改写 {self._rel(out_dir, css)} 的 @font-face 指向 {new_name}")
            elif new_name and new_name in old_css:
                # 目标字体正好和 CSS 里已写的是同一个文件名 ——
                # 那是**覆盖同名文件**的正常情况（`font_id` 与目标同名），
                # CSS 无需改动，也不需要警告。
                css.write_text(old_css, encoding="utf-8", newline="\n")
                notes.append(f"{css.name} 已指向 {new_name}（同名覆盖，无需改写）")
            else:
                css.write_text(old_css, encoding="utf-8", newline="\n")
                notes.append(
                    f"⚠️ {css.name} 里没找到可改写的 src，请手动确认字体指向"
                    f"（期望指向 {new_name}）"
                )
            return notes

        # 没有 CSS（或没有 @font-face）：写一份完整的
        family = "GameFont"
        # rpg_core.js 同样在资源根下（NW.js 布局是 out/www/js/）
        core_js = out_root / "js" / "rpg_core.js"
        core_text = (
            core_js.read_text(encoding="utf-8", errors="replace") if core_js.is_file() else ""
        )
        m = re.search(r"fontFamily\s*:\s*['\"]([^'\"]+)['\"]", core_text)
        if m:
            family = m.group(1)
            notes.append(f"从 rpg_core.js 读到字体族名：{family}")
        else:
            notes.append(f"未能读取字体族名，按默认值 {family} 写入")

        css.write_text(
            "@font-face {\n"
            f'  font-family: "{family}";\n'
            f'  src: url("{new_name}");\n'
            "}\n",
            encoding="utf-8",
            newline="\n",
        )
        notes.append(f"已生成 {self._rel(out_dir, css)}（font-family: {family}）")
        return notes

    # ------------------------------------------------------------------
    # 回写
    # ------------------------------------------------------------------

    def apply(
        self,
        game_dir: Path,
        out_dir: Path,
        units: list[TextUnit],
        translations: dict[str, str],
        *,
        font_patches: dict[str, Path] | None = None,
        rebuilt_images: dict[str, Path] | None = None,
    ) -> ApplyResult:
        res = ApplyResult(out_dir=out_dir)
        try:
            self.prepare_out(game_dir, out_dir)
        except Exception as exc:  # noqa: BLE001
            res.error = f"复制游戏目录失败：{exc}"
            return res

        # 按文件归组，避免反复读写同一个 JSON
        by_file: dict[str, list[TextUnit]] = {}
        for u in units:
            if u.uid in translations:
                by_file.setdefault(u.location.file, []).append(u)

        # 写入目标必须落在**资源根**下（NW.js 布局是 out/www/data）。
        # `units` 是上一步从同一个根抽出来的，这里必须用同一套判定，
        # 否则每个文件都会"目标不存在，跳过" —— 那是最糟的失败方式：
        # 阶段报 ok、回写计数却是 0。
        out_root = _content_root(out_dir) or out_dir
        for fname, us in by_file.items():
            target = out_root / "data" / fname
            if not target.is_file():
                res.warnings.append(f"目标文件不存在，跳过：{fname}")
                res.files_skipped += 1
                continue
            try:
                obj = json.loads(target.read_text(encoding="utf-8-sig"))
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"{fname} 读取失败：{exc}")
                res.files_skipped += 1
                continue

            n = 0
            for u in us:
                text = translations[u.uid]
                if not text:
                    continue
                if self._set_pointer(obj, u.location.pointer, text):
                    n += 1
                else:
                    res.warnings.append(f"定位失败：{fname}{u.location.pointer}")

            if n:
                try:
                    target.write_text(
                        json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8",
                        newline="\n",
                    )
                    res.files_written += 1
                except Exception as exc:  # noqa: BLE001
                    res.warnings.append(f"{fname} 写入失败：{exc}")
                    res.files_skipped += 1

        # 贴图：把重绘结果覆盖到 out 目录
        if rebuilt_images:
            n = 0
            keys = self._encryption_keys(out_dir)
            for rel, src in rebuilt_images.items():
                dest = out_dir / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # ---- 加密资源必须**重新加密**才能被游戏读取 ----
                    # RPG Maker MV/MZ 默认开启资源加密，真实游戏里
                    # 99% 的贴图是 `.rpgmvp` / `.png_`。引擎只认带头
                    # 的格式，把纯 PNG 直接拷进去 = 图全裂。
                    # 解密在读取侧做（见 `images.service.imread_bgr`），
                    # 所以这里必须补上逆操作。
                    if is_encrypted_image_name(rel) and keys is not None:
                        plain = Path(src).read_bytes()
                        dest.write_bytes(encrypt(plain, keys))
                    else:
                        import shutil as _sh

                        _sh.copy2(src, dest)
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    res.warnings.append(f"贴图回写失败 {rel}：{exc}")
            if n:
                res.files_written += n

        # 字体：把补好的字体放进 out，并让 CSS 指向它
        if font_patches:
            n = 0
            installed: dict[str, str] = {}
            for rel, src in font_patches.items():
                dest = out_dir / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    import shutil as _sh

                    _sh.copy2(src, dest)
                    # 两份都要有，缺一不可：
                    #
                    # 1. `dest`（= out/<rel>）是**游戏会去加载的名字**：
                    #    fonts 阶段产出的字体叫 `<原字体名>.zh.ttf`，与
                    #    `font_id`（`<原字体名>.ttf`）不同名，所以必须把它
                    #    复制成 `font_id` 那个名字，游戏才找得到。
                    # 2. 再复制一份**保留补丁自己的文件名**，这样 CSS 可以
                    #    明确指向新字体；只靠覆盖同名文件的话，"用上了新字体"
                    #    这件事在产物里没有任何痕迹可查。
                    #
                    # 值给 `wire_fonts` 用的是**补丁自己的文件名**。
                    # 早先这里写的是 `installed[rel] = rel`（原文件名），
                    # 于是 CSS 被"改写成原来的名字"（`fixed == old_css`），
                    # 看起来流程走完了，实际上新字体从未被引用 ——
                    # 游戏继续加载旧字体，用户看到口口口。
                    extra = dest.parent / Path(src).name
                    if extra != dest:
                        _sh.copy2(src, extra)
                    installed[rel] = src.as_posix() if isinstance(src, Path) else str(src)
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    res.warnings.append(f"字体回写失败 {rel}：{exc}")
            if n:
                res.files_written += n
                # 光放文件不够，还得让引擎去用它
                try:
                    for note in self.wire_fonts(out_dir, installed, game_dir=game_dir):
                        res.warnings.append(f"[字体接线] {note}")
                except Exception as exc:  # noqa: BLE001
                    res.warnings.append(f"字体接线失败（字体文件已就位，可能需手动指向）：{exc}")

        res.ok = res.files_written > 0 or not by_file
        if not res.ok and not res.error:
            res.error = "没有任何文件被写入"
        return res

    # ------------------------------------------------------------------

    @staticmethod
    def _set_pointer(obj: Any, pointer: str, value: str) -> bool:
        """按 ``/a/b/0/c`` 形式的指针写入。

        指针里的每一段如果是数字就当数组下标，否则当字典键。
        返回是否写入成功（定位失败返回 False，绝不静默改错位置）。
        """
        if not pointer.startswith("/"):
            return False
        parts = [p for p in pointer.split("/") if p != ""]
        if not parts:
            return False
        cur = obj
        for seg in parts[:-1]:
            if isinstance(cur, list):
                if not seg.lstrip("-").isdigit():
                    return False
                idx = int(seg)
                if not (-len(cur) <= idx < len(cur)):
                    return False
                cur = cur[idx]
            elif isinstance(cur, dict):
                if seg not in cur:
                    return False
                cur = cur[seg]
            else:
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
        if isinstance(cur, dict):
            cur[last] = value
            return True
        return False


__all__ = ["RpgMakerAdapter"]
