r"""UnityFS（AssetBundle）内的文本：**只读抽取 + 定点回写**。

## 这个模块为什么长这样（全部来自实测，不是设计想象）

`docs/BUNDLE_SURVEY.md` 记录了勘查过程。三个结论决定了本模块的形态：

### ① 不做"通用解包器"

72 个 Unity 游戏里 46 个含 UnityFS 包，但逐游戏打开最大的包实测：

    无文本        25 个
    仅少量(1~4)   11 个
    有文本          10 个

反例：**Isekai Sex Boutique** 有 5857 个包，包里只有
``'Enter Your Name'``、字体名、版权声明；**変態カノジョ** 的 4065 个
TextAsset 全是 ``'チンコしまいAG_1074.fade'`` 这种**动画名**。

⇒ 所以本模块**不**提供"把包全解开"的入口，只提供
「**扫出真正需要翻译的槽位**」——扫不到就不碰这个包。

### ② 只认"可判定的"文本形态

勘查 10 个有文本的游戏，发现形态分成两类：

* **多语言槽位组**（Jerez's Arena）：一个容器里 N 个 ``{tag, content}``，
  tag 取值如 ``'Chinese (Simplified)'`` / ``'English'`` / ``'Japanese'``。
  ⇒ 这是**最理想**的形态："该译哪些"是**可判定**的。
* **裸文本字段**（其余 9 个）：``m_text`` / ``m_Localized`` /
  ``scriptText`` / ``Jptext``+``Entext`` …，字段名**每个游戏不同**。

本模块对两类都支持，但对"裸文本字段"**只认一份保守白名单**
（见 `BARE_TEXT_FIELDS`）—— 因为"靠像不像句子"来猜，实测会把
``m_FaceInfo.m_FamilyName``（``'Noto Sans CJK TC'``，字体名）也算进来。

### ③ ★ 最重要的一条：**已有简体中文的槽位必须跳过**

Jerez's Arena 的 9392 个槽位组实测（`.scratch/_reconcile.py`）::

    槽位组总数              9392
    其中已有中文            8533   ← 必须跳过
    其中全空                 911
    ★有源文但无中文            0   ← 真实收益（这个样本上是 0）
    ------------------------------------------------
    裸字段（白名单）         4304
    其中已是中文             4259   （``.storyText``，繁体字幕）

若不跳过那 8,533 组，就会**覆盖游戏自带的官方译文**（质量倒退）。

⇒ `slot_needs_translation()` 是本模块的**核心判据**，不是可选项。

⚠️ **诚实说明收益**：初版勘查把这里的"缺中文"误算成 3,653 条，
据此高估了收益。更正后的实测是 **0** —— 也就是说
**本模块在当前库上抓到的"真需翻译的包内文本"极少**
（Jerez's Arena 上只剩 4304 个裸字段里的 45 个非中文项）。
实现它的价值主要是**把"包内文本"纳入覆盖范围并如实报告**，
而不是指望它带来成千上万条新翻译。详见 `docs/BUNDLE_SURVEY.md` §5。

另外实测 tag 里有 **275 个 ``'English '``（尾随空格）**
⇒ 所有 tag 比较**必须** `strip()`。
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: UnityFS 包文件头
UNITYFS_MAGIC = b"UnityFS"

#: 视为"目标语言已经是中文"的槽位 tag（比较前会 ``strip()``）。
#: 实测 Jerez's Arena 用 ``Chinese (Simplified)`` / ``Chinese (Taiwan)``；
#: 另有游戏用 ``Content_zh-cn`` / ``Content_zh-tw`` 这种形态。
ZH_TAGS = frozenset(
    {
        "chinese (simplified)",
        "chinese (taiwan)",
        "chinese (traditional)",
        "chinese",
        "zh-cn",
        "zh-hans",
        "zh-tw",
        "zh-hant",
        "zh",
        "content_zh-cn",
        "content_zh-tw",
        "content_zh",
        "簡体字",
        "简体中文",
        "繁體中文",
    }
)

#: 视为"源语言"的槽位 tag（按优先级从高到低 —— 日文优先，
#: 因为二次元游戏的原文多为日文，日→中比英→中更贴近原意）。
SRC_TAGS: tuple[str, ...] = (
    "japanese",
    "ja",
    "jp",
    "content_ja",
    "content_jp",
    "english",
    "en",
    "content_english",
    "content_en",
    "korean",
    "ko",
)

#: 容器字段名 —— 这些字段的值是"一组语言槽位"（``[{tag, content}, ...]``）。
SLOT_LIST_FIELDS = frozenset(
    {"localizetext", "localizetexts", "localizedtext", "localizedtexts", "names"}
)

#: 槽位组内"文本载荷"的字段名（实测只有 ``content`` 与 ``text``）。
SLOT_VALUE_FIELDS = ("content", "text", "value")

#: ★ 裸文本字段白名单（保守！）。
#:
#: 只收**语义明确**、跨游戏通用的字段名。实测来源：
#:
#: * ``m_text``     —— TMP_Text（Academy Love Saga 81 处、SummerClover 1450 处…）
#: * ``m_Localized``/``m_Key`` —— Unity Localization 包
#:   （Ideal_Hikikomori 781 + 206 处）
#: * ``scriptText`` —— SummerClover 128 处
#: * ``commandText``—— Ghost Marriage 37 处
#: * ``commentText``—— Ride_Me_Taxi_Driver 136 处
#: * ``Jptext``/``Entext`` —— Night_of_Revenge 各 37 处（日/英成对）
#:
#: ⚠️ **故意不收** ``m_Name`` / ``m_text`` 之外的 ``Name`` / ``MotionName`` /
#: ``m_FamilyName`` —— 实测这些是**资产名、动画名、字体名**
#: （``'Noto Sans CJK TC'``、``'チンコしまいAG_1074.fade'``），
#: 翻它们没有意义还可能改坏引用。
BARE_TEXT_FIELDS = frozenset(
    {
        "m_text",
        "m_localized",
        "m_key",
        "scripttext",
        "commandtext",
        "commenttext",
        "jptext",
        "entext",
        "defaultvalue",
        "dialoguetext",
        "messagetext",
        "storytext",
    }
)

#: 路径片段黑名单 —— 命中即**整条排除**（即使字段名在白名单里）。
#: 用于挡住 ``m_FaceInfo.m_FamilyName`` 这类"字段名像但语义不对"的。
_BAD_PATH = re.compile(
    r"FaceInfo|FamilyName|m_Script\b|m_GameObject|m_FileID|m_PathID|"
    r"guid|GUID|AssetBundle|shader|Shader|atlas|Atlas|"
    r"blendModeMaterials|Sprite|sprite",
)

#: ``.a.b[2].c`` → ``['a','b',2,'c']`` 的解析（本模块路径格式的唯一权威）
_SEG_RE = re.compile(r"^([^\[]+)(?:\[(\d+)\])?$")

#: 日文假名 —— 判"这段是不是日文"用（只作**辅助**，不作判据）。
#:
#: ⚠️ **必须排除标点**。``\u3040-\u30ff`` 这个区间里混着
#: ``\u30fb``（片假名中点 ``・``）、``\u30fc``（长音符 ``ー``）等**标点**，
#: 它们也常用于**中文**人名（实测 ``'「我是克菈蒂雅・奎涅爾。…」'``
#: 因为含 ``・`` 被误判成日文，于是这条**已是中文**的字幕被当成待翻译）。
#: 下面的写法用"平假名 + 片假名"两个**纯字母**区间拼出来，天然不含标点。
_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
#: 有"可翻译的实义字符"吗？—— 字母或汉字。
#:
#: 存在这个判据是因为实测：Jerez's Arena 的 `.m_text` 里有大量
#: ``'……'`` / ``'「……」'`` / ``'！？'`` 这类**纯标点**条目
#: （TMP 打字机效果的空格/停顿占位）。它们**没有可翻译内容**，
#: 送进模型只会浪费预算并可能得到更糟的标点。
_MEANINGFUL = re.compile(r"[A-Za-z\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")

#: 判"这段是什么语言"所需的最短长度。短于此的串**没法定语言**，
#: 而"误判为需翻译"会送垃圾进模型（实测 ``'喀。'`` / ``'「哦？」'``），
#: "误判为已是中文"只是少翻一条极短碎片 —— 代价不对称，选后者。
_MIN_LANG_LEN = 5


def _parse_path(path: str) -> list[str | int]:
    """把 ``.a.b[2].c`` 解析成 ``['a','b',2,'c']``。"""
    out: list[str | int] = []
    for seg in path.strip(".").split("."):
        if not seg:
            continue
        m = _SEG_RE.match(seg)
        if m is None:
            raise ValueError(f"路径片段无法解析：{seg!r}（完整：{path!r}）")
        out.append(m.group(1))
        if m.group(2) is not None:
            out.append(int(m.group(2)))
    return out


def _walk_containers(
    node: Any,
    path: str = "",
    depth: int = 0,
    *,
    max_depth: int = 14,
) -> list[tuple[str, list[Any]]]:
    """递归找出所有"语言槽位组"：``(字段路径, 槽位列表)``。

    判据：某字段的值是**非空 list**，且其**首元素是含 ``tag`` 的 dict**。
    这是从 Jerez's Arena 实测出来的形态，不是猜测。
    """
    found: list[tuple[str, list[Any]]] = []
    if depth > max_depth:
        return found
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}"
            if (
                isinstance(v, list)
                and v
                and isinstance(v[0], dict)
                and "tag" in v[0]
            ):
                found.append((p, v))
            else:
                found.extend(_walk_containers(v, p, depth + 1, max_depth=max_depth))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(_walk_containers(v, f"{path}[{i}]", depth + 1, max_depth=max_depth))
    return found


def _walk_bare(
    node: Any,
    path: str = "",
    depth: int = 0,
    *,
    max_depth: int = 14,
) -> list[tuple[str, str]]:
    """递归找出"裸文本字段"：``(字段路径, 值)``，只认 `BARE_TEXT_FIELDS`。"""
    out: list[tuple[str, str]] = []
    if depth > max_depth:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}"
            if isinstance(v, str) and k.lower() in BARE_TEXT_FIELDS and v.strip():
                if not _BAD_PATH.search(p):
                    out.append((p, v))
            else:
                out.extend(_walk_bare(v, p, depth + 1, max_depth=max_depth))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_walk_bare(v, f"{path}[{i}]", depth + 1, max_depth=max_depth))
    return out


def _slot_text(item: Any) -> str | None:
    """取一个语言槽位的文本载荷（``content`` / ``text`` / ``value``）。"""
    if not isinstance(item, dict):
        return None
    for k in SLOT_VALUE_FIELDS:
        v = item.get(k)
        if isinstance(v, str):
            return v
    return None


def _norm_tag(item: Any) -> str | None:
    """取归一化的 tag（★ 必须 strip：实测有 275 个 ``'English '``）。"""
    if not isinstance(item, dict):
        return None
    t = item.get("tag")
    if not isinstance(t, str):
        return None
    return t.strip().lower()


@dataclass(slots=True)
class BundleSlot:
    """包内一个**可翻译槽位**。

    * ``variants``：该槽位的全部语言变体 ``{归一化 tag: 文本}``。
      对"裸文本字段"只有一个伪 tag ``"__bare__"``。
    * ``pointer``：回写用的定位串（本模块唯一权威格式）。
    """

    bundle: Path
    """包文件绝对路径。"""

    asset: str
    """包内资产名（``m_Name``），用作定位锚。"""

    field_path: str
    """资产内的字段路径，如 ``.csvLines[12].localizeText[3]``。"""

    variants: dict[str, str] = field(default_factory=dict)
    bare: bool = False
    """True 表示这是"裸文本字段"（无多语言结构）。"""

    def source_text(self) -> tuple[str, str]:
        """挑出**源文**，返回 ``(tag, text)``；没有源文则 ``("", "")``。

        优先级见 `SRC_TAGS`（日文优先）。若都不匹配，退化用第一个非空值
        —— 那说明该游戏的 tag 命名不在已知集合里，**仍应翻译**，
        因为我们已经有"目标语言是否已有中文"这个独立判据兜底。
        """
        for t in SRC_TAGS:
            v = self.variants.get(t)
            if v and v.strip():
                return t, v
        for t, v in self.variants.items():
            if t not in ZH_TAGS and v and v.strip():
                return t, v
        return "", ""

    def has_target(self) -> bool:
        """目标语言（中文）是否**已存在且非空**。"""
        return any(
            (v or "").strip() for t, v in self.variants.items() if t in ZH_TAGS
        )

    def pointer(self) -> str:
        """回写定位串：``<包名>:<资产名>:<字段路径>``。

        用**资产名**而不是 path_id 作锚：path_id 是 64 位有符号数，
        在 JSON 里易失真；资产名在实测样本里唯一且稳定。
        资产名为空时退化用 ``-``，此时靠字段路径在包内唯一匹配。
        """
        return f"{self.bundle.name}:{self.asset or '-'}:{self.field_path}"


def _text_is_chinese(text: str) -> bool:
    """这段文本**已经是中文**吗？（用汉字占比 + 假名否定）

    ⚠️ 这不是"语言识别"，只服务于一个判断：
    "这段文字**不需要再翻成中文**了吗？"

    * 含假名 ⇒ 日文 ⇒ 不是（仍然要翻）；
    * **长度 < 5 字符 ⇒ 判为"是"**：短串没法定语言，而
      "误判为需翻译"的代价是**送垃圾进模型**（实测多出 442 条，
      值如 ``'喀。'`` / ``'「哦？」'``）；
    * 否则汉字数 ≥ ``长度 // 3`` ⇒ 认定已是中文。

    ## 为什么必须有这个函数（实测事故）

    Jerez's Arena 的包里有一个**根级字段** ``.storyText``（4302 处），
    内容是**繁体中文字幕**：``'「我知道了，那些錢我會一分不少的還給你。」'``。

    最初版本的 `slot_needs_translation` 只对"语言槽位组"检查"是否已有中文"，
    对**裸字段**只检查"非空" ⇒ 于是把 **4477 处已是中文的裸字段**
    判成"需要翻译"，多出 4477 条**莫须有的翻译任务**。

    这个 bug 是靠"与手工对账"发现的（`.scratch/_verify_bundle2.py`）——
    模块数出 5687，除数对不上，才回头查出口径不一致。
    """
    s = text.strip()
    if not s:
        return False
    if _KANA.search(s):
        return False
    # 短串保守判为"已是中文"（见 docstring：误判为需翻译 = 送垃圾进模型）
    if len(s) < _MIN_LANG_LEN:
        return True
    han = len(_HAN.findall(s))
    return han >= len(s) // 3


def slot_needs_translation(slot: BundleSlot) -> bool:
    """★ **核心判据**：这个槽位该不该翻成中文？

    四条，全部来自 ``docs/BUNDLE_SURVEY.md`` 的实测：

    1. **已有中文 ⇒ 不翻**。实测 Jerez's Arena 有 8533/9392（90.9%）
       的槽位**自带官方中文**，再翻一遍既是浪费预算，
       更会**覆盖官方译文**（质量倒退）。
    2. **无源文 ⇒ 不翻**。实测有 911/9392 是空槽位。
    3. **源文本身已是中文 ⇒ 不翻**（裸字段版本，见 `_text_is_chinese`）。
    4. 裸字段与容器**同一标准** —— 初版只对容器查"已有中文"，
       结果把 4477 处已经是中文的 ``.storyText`` 判成待翻译
       （靠对账发现，见 `_text_is_chinese` 的 docstring）。

    ⚠️ **已知取舍**：繁体中文槽位（``Chinese (Taiwan)``）算"已有中文"
    ⇒ **不翻、也不转简体**。理由：本工具做的是**翻译**（日/英 → 中），
    繁→简是**字形转换**，属于另一个功能；且实测这类内容已有官方译文，
    贸然改写会**覆盖官方版本**。此取舍如实记录在
    `docs/BUNDLE_SURVEY.md`。
    """
    if slot.bare:
        text = slot.variants.get("__bare__", "")
        if not text.strip():
            return False
        # 纯标点（实测 ``'……'`` / ``'「……」'``）⇒ 没有可翻译内容
        if not _MEANINGFUL.search(text):
            return False
        # ★ 裸字段也必须查"是否已是中文"（初版漏了这一步）
        return not _text_is_chinese(text)
    if slot.has_target():
        return False
    _tag, src = slot.source_text()
    if not src.strip():
        return False
    if not _MEANINGFUL.search(src):
        return False
    # 源文本身已是中文（tag 映射错乱的游戏）⇒ 不翻
    return not _text_is_chinese(src)


@dataclass
class BundleScanReport:
    """一次包扫描的结果 —— 数字全部可核验，便于写进文档。"""

    bundles_seen: int = 0
    bundles_with_text: int = 0
    slot_groups: int = 0
    bare_fields: int = 0
    needs_translation: int = 0
    skipped_has_target: int = 0
    skipped_no_source: int = 0
    skipped_source_is_target: int = 0
    skipped_bare_is_chinese: int = 0
    """裸字段里**已经是中文**而跳过的数量（实测 Jerez's Arena 有 4,795 处）。

    单列出来是因为它曾经是个 bug：初版只对"语言槽位组"检查
    "是否已有中文"，漏掉了裸字段，于是多出 4,477 条莫须有的翻译任务。
    """
    skipped_bare_punctuation: int = 0
    """纯标点、没有可翻译内容的裸字段（实测有 ``'……'`` / ``'「……」'``）。"""
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def summary(self) -> str:
        return (
            f"包 {self.bundles_seen}（含文本 {self.bundles_with_text}）"
            f"　槽位组 {self.slot_groups}　裸字段 {self.bare_fields}"
            f"　⇒ 需翻译 {self.needs_translation}"
            f"（跳过：已有中文 {self.skipped_has_target}、"
            f"无源文 {self.skipped_no_source}、"
            f"源文即中文 {self.skipped_source_is_target}、"
            f"裸字段已是中文 {self.skipped_bare_is_chinese}、"
            f"纯标点 {self.skipped_bare_punctuation}）"
        )


def find_bundles(game_dir: Path, *, limit: int | None = None) -> list[Path]:
    """找出游戏目录下的 UnityFS 包（只读文件头，很便宜）。

    ``limit`` 只影响**返回数量**：按文件大小**从大到小**排序后截断。
    实测文本多在较大的包里（小的多是贴图/音频），所以从大到小更划算。
    """
    out: list[Path] = []
    data_dirs = [d for d in game_dir.iterdir() if d.is_dir() and d.name.endswith("_Data")]
    if not data_dirs:
        data_dirs = [game_dir]
    for d in data_dirs:
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            try:
                if p.stat().st_size < 4096:
                    continue
                with p.open("rb") as fh:
                    if fh.read(len(UNITYFS_MAGIC)) != UNITYFS_MAGIC:
                        continue
            except OSError:
                continue
            out.append(p)
    out.sort(key=lambda p: p.stat().st_size, reverse=True)
    return out[:limit] if limit else out


def scan_bundle(
    bundle: Path,
    *,
    report: BundleScanReport | None = None,
    max_objects: int | None = None,
) -> list[BundleSlot]:
    """扫描**一个**包，返回所有"需要翻译"的槽位。

    ⚠️ **只读**：不写任何文件。回写请用 `apply_translations`。

    单个包打不开/解析失败**不会**抛异常，而是记进 ``report.errors``
    并返回已找到的部分 —— 一个坏包不该让整条游戏的翻译失败
    （这条来自 #41 的教训：让异常逃出去会中断整轮）。
    """
    try:
        import UnityPy  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 环境相关
        if report is not None:
            report.errors.append(f"UnityPy 不可用：{exc}")
        return []

    rep = report if report is not None else BundleScanReport()
    slots: list[BundleSlot] = []
    try:
        env = UnityPy.load(str(bundle))
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"{bundle.name}: 打开失败 {type(exc).__name__}: {exc}")
        return []

    n = 0
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        n += 1
        if max_objects is not None and n > max_objects:
            break
        try:
            tree = obj.read_typetree()
        except Exception:  # noqa: BLE001
            # 没有 typetree 的 MonoBehaviour（缺脚本定义）很常见，不算错误
            continue
        asset_name = ""
        v = tree.get("m_Name") if isinstance(tree, dict) else None
        if isinstance(v, str):
            asset_name = v

        for fpath, items in _walk_containers(tree):
            variants: dict[str, str] = {}
            for item in items:
                tag = _norm_tag(item)
                text = _slot_text(item)
                if tag is None or text is None:
                    continue
                variants[tag] = text
            if not variants:
                continue
            rep.slot_groups += 1
            slot = BundleSlot(bundle, asset_name, fpath, variants, bare=False)
            if slot_needs_translation(slot):
                rep.needs_translation += 1
                slots.append(slot)
            elif slot.has_target():
                rep.skipped_has_target += 1
            else:
                _t, src = slot.source_text()
                if not src.strip():
                    rep.skipped_no_source += 1
                else:
                    rep.skipped_source_is_target += 1

        for fpath, text in _walk_bare(tree):
            rep.bare_fields += 1
            slot = BundleSlot(bundle, asset_name, fpath, {"__bare__": text}, bare=True)
            if slot_needs_translation(slot):
                rep.needs_translation += 1
                slots.append(slot)
            elif not _MEANINGFUL.search(text):
                rep.skipped_bare_punctuation += 1
            elif _text_is_chinese(text):
                rep.skipped_bare_is_chinese += 1
            else:
                rep.skipped_no_source += 1

    if slots:
        rep.bundles_with_text += 1
    return slots


def scan_game(
    game_dir: Path,
    *,
    limit: int | None = None,
    report: BundleScanReport | None = None,
) -> list[BundleSlot]:
    """扫描整个游戏的包，返回全部"需要翻译"的槽位。

    ⚠️ 只读。``limit`` 建议在"只想探一下"时给一个小值
    （打开大包有成本：实测 188 MB 包保存要 6.83 s，**读取**更快但仍非免费）。
    """
    rep = report if report is not None else BundleScanReport()
    bundles = find_bundles(game_dir, limit=limit)
    out: list[BundleSlot] = []
    for b in bundles:
        rep.bundles_seen += 1
        out.extend(scan_bundle(b, report=rep))
    return out


def _set_path(tree: Any, path: str, value: str) -> bool:
    """把 ``value`` 写到 ``tree`` 的 ``path`` 处；成功返回 True。

    路径格式与 `_parse_path` 对应。会在**写入前校验**目标当前是 str，
    否则返回 False —— 宁可漏写，也不要把非字符串字段改成字符串。
    """
    segs = _parse_path(path)
    if not segs:
        return False
    cur = tree
    for seg in segs[:-1]:
        try:
            cur = cur[seg] if not isinstance(seg, int) else cur[seg]
        except (KeyError, IndexError, TypeError):
            return False
    last = segs[-1]
    if not isinstance(cur, (dict, list)):
        return False
    try:
        existing = cur[last]
    except (KeyError, IndexError, TypeError):
        return False
    if not isinstance(existing, str):
        return False
    cur[last] = value
    return True


@dataclass
class BundleApplyReport:
    """回写结果。``written`` 与 ``skipped`` 都要能核验。"""

    bundles_total: int = 0
    bundles_written: int = 0
    slots_written: int = 0
    slots_skipped: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    backups: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        pct = (
            100.0 * self.bytes_after / self.bytes_before if self.bytes_before else 0.0
        )
        return (
            f"包 {self.bundles_total}（改写 {self.bundles_written}）"
            f"　槽位写入 {self.slots_written}　跳过 {self.slots_skipped}"
            f"　体积 {self.bytes_before}B → {self.bytes_after}B（{pct:.1f}%）"
            f"　备份 {len(self.backups)}"
        )


def apply_translations(
    translations: dict[str, str],
    *,
    asset_names: dict[str, str] | None = None,
    backup_root: Path | None = None,
    pack: str = "lz4",
    report: BundleApplyReport | None = None,
) -> BundleApplyReport:
    """把 ``{pointer: 译文}`` 回写进各自的 UnityFS 包。

    ``pointer`` 就是 `BundleSlot.pointer()` 的格式
    （``<包名>:<资产名>:<字段路径>``）。**按包分组**后，每个包只
    load/save 一次 —— 这一点很关键：实测单个 188 MB 包 save 要 6.83 s，
    逐条 save 会慢到不可接受。

    ## 安全侧（宁可不写）

    * 写之前校验目标字段**当前是 str**（`_set_path`），否则跳过并计数；
    * 译文为空 ⇒ 跳过（不把已有内容清成空）；
    * 每个包写之前先备份到 ``backup_root``（若给了）；
    * `env.save()` 的产物**先写临时文件**，成功后再替换原包 ——
      避免中途失败留下半个包（那会让游戏**彻底打不开**）。

    ⚠️ 本函数**不保证**写出的包能被真实游戏加载。实测只验证到
    "UnityPy 能重新打开、资产数与改动一致"（见 `docs/BUNDLE_SURVEY.md` §4）。
    """
    rep = report if report is not None else BundleApplyReport()
    if not translations:
        return rep

    # pointer -> (包名, 资产名, 字段路径)
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for ptr in translations:
        parts = ptr.split(":", 2)
        if len(parts) != 3:
            rep.errors.append(f"pointer 格式不对，跳过：{ptr!r}")
            continue
        groups.setdefault(parts[0], []).append((ptr, parts[1], parts[2]))
    rep.bundles_total = len(groups)

    try:
        import UnityPy  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 环境相关
        rep.errors.append(f"UnityPy 不可用：{exc}")
        return rep

    # 包名 -> 实际路径（由调用方通过 asset_names 提供，或在本目录里找）
    name_to_path: dict[str, Path] = {}
    for _bname, items in groups.items():
        for _ptr, asset, _fp in items:
            if asset_names and asset in asset_names:
                name_to_path[_bname] = Path(asset_names[asset])

    for bname, items in groups.items():
        bpath = name_to_path.get(bname)
        if bpath is None or not bpath.exists():
            rep.errors.append(f"{bname}: 找不到包文件，跳过 {len(items)} 条")
            rep.slots_skipped += len(items)
            continue
        try:
            env = UnityPy.load(str(bpath))
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(f"{bname}: 打开失败 {type(exc).__name__}: {exc}")
            rep.slots_skipped += len(items)
            continue

        # 资产名 -> 对象（同名时全部尝试）
        by_asset: dict[str, list[Any]] = {}
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tree = obj.read_typetree()
            except Exception:  # noqa: BLE001
                continue
            nm = tree.get("m_Name") if isinstance(tree, dict) else None
            by_asset.setdefault(nm if isinstance(nm, str) else "", []).append(obj)

        touched = 0
        for ptr, asset, fpath in items:
            text = translations.get(ptr, "")
            if not text.strip():
                rep.slots_skipped += 1
                continue
            done = False
            for obj in by_asset.get(asset, []):
                try:
                    tree = obj.read_typetree()
                except Exception:  # noqa: BLE001
                    continue
                if not _set_path(tree, fpath, text):
                    continue
                try:
                    obj.save_typetree(tree)
                except Exception as exc:  # noqa: BLE001
                    rep.errors.append(f"{ptr}: save_typetree 失败 {type(exc).__name__}")
                    continue
                done = True
                break
            if done:
                touched += 1
                rep.slots_written += 1
            else:
                rep.slots_skipped += 1

        if not touched:
            continue

        rep.bytes_before += bpath.stat().st_size
        if backup_root is not None:
            try:
                dst = backup_root / bname
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    shutil.copy2(bpath, dst)
                    rep.backups.append(str(dst))
            except OSError as exc:
                rep.errors.append(f"{bname}: 备份失败 {exc}（已放弃改写该包）")
                rep.slots_written -= touched
                continue

        outdir = bpath.parent / "_novaloc_bundle_out"
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            env.save(pack=pack, out_path=str(outdir))
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(f"{bname}: env.save 失败 {type(exc).__name__}: {exc}")
            rep.slots_written -= touched
            continue
        produced = outdir / bname
        if not produced.exists():
            rep.errors.append(f"{bname}: env.save 没有产出文件")
            rep.slots_written -= touched
            continue
        # ★ 先写临时文件再替换：避免中途失败留下半个包（游戏会打不开）
        tmp = bpath.with_suffix(bpath.suffix + ".novaloc_tmp")
        try:
            shutil.move(str(produced), str(tmp))
            shutil.move(str(tmp), str(bpath))
        except OSError as exc:
            rep.errors.append(f"{bname}: 替换原包失败 {exc}")
            rep.slots_written -= touched
            continue
        rep.bundles_written += 1
        rep.bytes_after += bpath.stat().st_size

    return rep
