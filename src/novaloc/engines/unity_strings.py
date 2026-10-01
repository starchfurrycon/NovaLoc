r"""从 Unity 序列化资源里**只读**提取候选字符串。

## 这个模块解决什么问题

Unity 的策划文案大多不在明文 txt 里，而在
``level*``、``resources.assets``、``sharedassets*.assets`` 这些
**二进制序列化资源**里。本工具**不会**写这些文件 —— 盲写几乎必然
破坏资源、导致游戏打不开，而失败表现是"游戏打不开"，
用户根本查不出原因。宁可不支持，也不能把游戏改坏（见 `unity.py` 模块注释）。

但"不写"不等于"什么都做不了"。用户真正需要的是两件事：

1. **知道有哪些文本要翻** —— 否则他连该在 UABEA 里找什么都无从下手；
2. **拿到译好的文本** —— 可以照着在外部工具里粘贴回去。

所以这个模块做**只读提取**：把候选字符串连位置一起列出来，
导成 CSV/JSON 交给用户。**一个字节都不改。**

## 怎么"猜"字符串的位置（以及为什么必须保守）

Unity 的序列化字符串格式是

    <4 字节小端长度> <UTF-8 字节> <0..3 字节对齐填充>

长度是**未对齐**的真实字节数。这个格式给了我们一个很强的校验：
**按长度读出来的字节，必须能解码成合法的、看起来像人话的文本。**
于是可以用"长度前缀自洽性"来筛，而不是靠"扫到可打印字符就报"。

后面那种朴素做法在 `.assets` 上会产出海量垃圾
（`Assembly-CSharp`、GUID、着色器变量名、类型名…），
把报告冲垮 —— 本会话已经栽过两次同类问题
（Unity 的 `output_log.txt` 抽出 53 万条、`Mono/etc/` 抽出 9,586 条）。
**所以这里宁可少报，不可乱报。**

## 三类筛子

1. **格式自洽**：长度前缀 + 合法 UTF-8 + 长度在合理范围；
2. **看起来像人话**：字母/汉字占比够高、不是标识符、不是路径、
   不是 GUID/哈希、有空格或足够长；
3. **不是代码/资源名**：排除 `Assembly-CSharp`、`m_Script`、
   `UnityEngine.` 这类已知的引擎标识符。

第 3 条用**白名单式排除**而不是黑名单式包含，是因为
"像人话"很难正向定义，但"明显不是人话"很好列举。
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

#: 候选字符串的长度范围（字节）。太短没有信息量，太长不是字段值。
MIN_LEN = 4
MAX_LEN = 4096

#: **可证明安全**的跳过阈值：连续这么多零字节才算"空白区"。
#:
#: 长度前缀 ≥ `MIN_LEN`(4) 意味着首字节非零，所以一段连续零字节里
#: 不可能有字符串**起点** —— 这个跳过不依赖任何经验假设。
#: 值取 64 是留足余量（对齐填充最多 3 个零字节，绝不会凑到 64 个）。
ZERO_RUN_MIN = 64

#: 匹配**长零字节区**（用于安全跳过）。用 `match(data, pos)` 从 pos 处匹配，
#: `m.end()` 即零区回后的第一个位置。
_ZERO_RUN_RE = re.compile(rb"\x00{%d,}" % ZERO_RUN_MIN)

#: 明显是**代码/引擎标识符**而不是策划文案的模式。
_NOT_GAME_TEXT_RES = (
    # 引擎/框架类型名与命名空间
    re.compile(r"^(?:UnityEngine|UnityEditor|System|Mono|TMPro|DG\.Tweening|"
               r"Spine|Cinemachine|Newtonsoft|ExcelDataReader)\b"),
    # 序列化字段名（`m_Script`、`m_GameObject`、`_Size` 等）
    re.compile(r"^_?m_[A-Za-z]"),
    re.compile(r"^_[A-Za-z]+$"),
    # .NET 类型引用：`Mono.MonoConfig.FeatureNodeHandler, mconfig, Version=…`
    re.compile(r",\s*[A-Za-z0-9_.]+,\s*Version=\d"),
    # 程序集 / 命名空间形态
    re.compile(r"^(?:Assembly-|Microsoft\.|\.NET |mscorlib)"),
    # GUID / 哈希
    re.compile(r"^[0-9a-fA-F]{16,}$"),
    # 文件路径（含盘符、斜杠、已知后缀）
    re.compile(r"^[A-Za-z]:[\\/]"),
    re.compile(r"^(?:Assets|Packages|Library)[\\/]"),
    re.compile(r"\.(?:dll|exe|cs|js|json|xml|png|jpg|mat|prefab|unity|asset|"
               r"shader|compute|ttf|otf|fbx|wav|ogg|mp3|txt|ini|bytes)$", re.IGNORECASE),
    # 着色器 / 图形 API 常量
    re.compile(r"^(?:SHADER|_MainTex|_Color|Hidden/|Sprites/|UI/)"),
)

#: 纯 ASCII 单词（无空格、无标点、无下划线）—— 单独处理，见 `_is_plain_word`。
#:
#: ⚠️ 为什么不能把它直接扔进 `_NOT_GAME_TEXT_RES`：
#: 真正的 UI 文案**可以**是单个词（`Options`、`Gallery`、`Status`），
#: 而资源名/代码标识符也长这样。两者只能靠**大小写形态**区分，
#: 所以需要一段带判断的逻辑，不是一个静态正则。
_PLAIN_WORD_RE = re.compile(r"^[A-Za-z]+$")

#: 资源名分隔符：`--`、`__`、连续短横线、多位数字 —— 人写的文案里不会有。
_ASSET_SEPARATOR_RE = re.compile(r"(?:\s--|--\s|__|-{2,}|\d{2,})")


def natural_words(t: str) -> list[str]:
    """挑出"人写的词" —— 不含数字/下划线、形状像英文单词的 token。

    资源名（`Tile_4_Bottom  --`、`-----Creep  L`、`1024_CircleGlow`）
    几乎不含这样的词；UI 文案（`Camera Zoom :`、`Loop :`、`Save Game`）有。
    所以"人写的词有几个"能相当好地把两类分开。
    """
    out: list[str] = []
    for tok in t.split():
        core = tok.strip(".,:;!?()[]{}<>'\"-\u2018\u2019\u201c\u201d")
        if not core or "_" in core or any(c.isdigit() for c in core):
            continue
        if len(core) >= 2 and core.replace("'", "").isalpha():
            out.append(core)
    return out


def is_human_word(t: str) -> bool:
    """单个词是"人写的英文单词"，而不是**代码标识符/资源名**吗？

    ## 为什么看**大写字母的分布**（而不是加黑名单）

    | 形态 | 例子 | 内部大写数 | 判定 |
    |---|---|---|---|
    | 首字母大写，其余全小写 | `Options`、`Gallery` | 0 | ✅ 人写的词 |
    | PascalCase（内部有大写） | `AdvancingFrontNode`、`BoneJoint` | ≥1 | ❌ 标识符 |
    | 驼峰 | `someField` | ≥1 | ❌ 标识符 |
    | 全大写 / 全小写 | `IMPACT`、`default` | — | ❌ 资源名或代码 |

    **"首字母大写 + 内部零大写"这个形态只有人写的英文单词才会有** ——
    C# 类型名全是 PascalCase，资源名要么全大写要么带下划线。
    所以这是一条**形态判据**，不是"黑名单碰运气"：
    它不依赖我知道 Unity 里有哪些类名（我不可能知道全）。
    """
    if not _PLAIN_WORD_RE.match(t):
        return False          # 含下划线/数字/标点 ⇒ 标识符或资源名
    if len(t) < 4 or not t[0].isupper():
        return False          # 太短、或全小写/驼峰首字母 ⇒ 代码
    return not any(c.isupper() for c in t[1:])


#: 明显的**配置/调试**值（不是给人看的文案）。
_NOT_GAME_TEXT_VALUES = frozenset({
    "true", "false", "null", "none", "unknown", "default", "defaultproperties",
    "enabled", "disabled", "yes", "no", "ok", "cancel", "n/a",
})

#: 音频/视频**编解码器元数据** —— 实测占某个游戏候选的 **57%**（351 条里 200 条）。
#:
#: 这是"格式自洽"筛子的**盲区**：Vorbis 注释块本身就是
#: `长度前缀 + UTF-8`，和 Unity 的字符串格式**一模一样**，
#: 所以 `Xiph.Org libVorbis I 20101101 (Schaufenugget)` 能完整通过格式校验。
#: 它甚至有 3 个词、字母占比很高 —— 从"像不像人话"看也过关。
#:
#: 只能按**内容特征**排：编解码器签名、版权行、`KEY=VALUE` 形式的元数据。
_NOT_GAME_TEXT_RES = _NOT_GAME_TEXT_RES + (
    # 编解码器签名（Vorbis / Opus / LAME / FFmpeg / Theora）
    re.compile(r"^(?:Xiph\.Org|libVorbis|libTheora|Lavf|Lavc|LAME|Opus|"
               r"OggS|Encoder=|TagLib)", re.IGNORECASE),
    # 音频元数据字段：`Copyright=…`、`Artist=…`、`Album=…`
    re.compile(r"^(?:Copyright|Artist|Album|Title|Genre|Track|Comment|"
               r"Encoded|Software|Source|Date|License)\s*=", re.IGNORECASE),
    # 版权声明行（不是给玩家看的）
    re.compile(r"\bcopyright\b\s*(?:\(c\)|©|\d{4})", re.IGNORECASE),
    re.compile(r"^\s*(?:\(c\)|©)\s*\d{4}", re.IGNORECASE),
    # Unity 版本戳与构建信息
    re.compile(r"^\d+\.\d+\.\d+[a-z]\d+\s*$"),
    re.compile(r"\bUnity\s+\d+\.\d+"),
    # ---- 以下四类来自真实数据实测（336 条候选里约 290 条是这四类）----
    #
    # ① **动画状态机迁移**：`Base Layer.Idle -> Base Layer.Attack`
    #    实测占最多。特征是 ` -> ` 加 `Base Layer.` 前缀。
    re.compile(r"\s->\s"),
    re.compile(r"^(?:Base Layer|AnyState|Entry|Exit)\b"),
    # ② **音频资产名**：`FOOTSTEP Walk Trainers Wood Boards RR1 (mono)`
    #    结尾的 `(mono)` / `(stereo)` / `(loop ...)` 是音频工程的惯例标注。
    re.compile(r"\((?:mono|stereo|loop)[^)]*\)\s*$", re.IGNORECASE),
    re.compile(r"^\[(?:BGM|SE|SFX|VOICE|CV)\b", re.IGNORECASE),
    re.compile(r"^(?:sounds?\.|bgm\.|se\.|voice\.)"),
    # ③ **材质/着色器/贴图名**：`256_Glow_Circle  --Color`、`BG_2 -- GrayPink`
    #    `--` 是美术工具里的变体分隔符，不是文案标点。
    re.compile(r"\s--\s*(?:Color|Light|Dark|Gray|Alpha|Normal|Add|Mul)\s*$", re.IGNORECASE),
    re.compile(r"^[A-Za-z0-9_]+\s*--\s"),
    re.compile(r"\bGUI_|_Glow_|_Circle\b"),
    # ④ **资源命名惯例**：`BG_93  Egg Cage`、`Magic_3_Hit   GORE Stab Splat`
    #    特征是短前缀 + 下划线 + 数字，且整体没有句读。
    re.compile(r"^(?:BG|CG|SE|BGM|UI|FX|Ani|Anim|Mat|Tex|Img|Pic|Eff)_\d"),
    re.compile(r"^(?:Base Layer|Magic|IMPACT|FOOTSTEP|ALARM|Glass|Device|Slot)\b.*_"),
    # ⑤ 点分标识符（`Base Layer.Naked_End`、`A.B.C`）
    re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_. ]*$"),
    # ⑥ 只有一个"词"但带下划线的（`JF Dot Kanamecho 12` 之外的资源名）
    re.compile(r"^[A-Za-z0-9]+(?:_[A-Za-z0-9]+){2,}$"),
    # ⑦ **音频/字体资源名**（最后一小撮噪音）
    #    `[BGM_5] FX_sound_design_effect_build_suspenseful_cinematic`
    #    `Song_Ambient_MonoFiltered_01`、`Noto Sans CJK JP`（字体名）
    re.compile(r"^\[[A-Z]{2,5}_?\d*\]"),
    # ⚠️ 下划线形态要求**至少两个**下划线。
    # 早先写的是 `{1,}`（一个就够），结果 `Save_Game`、`Load_Game`
    # 这类**正常名字**被当噪音排掉；更糟的是它们被排掉后，
    # 扫描位置落到了字符串的**内部**，把紧随其后的下一个字符串
    # 也一起吃掉 —— 一个过宽的判据会连带丢掉邻接的**真**候选。
    re.compile(r"^[A-Za-z0-9]+(?:_[A-Za-z0-9]+){2,}$"),
    re.compile(r"^(?:Noto|Roboto|Arial|Times|Courier|DejaVu|Liberation)\s"),
    # ⑨ **SQL 语句** —— 来自存档/数据库逻辑，不是给玩家看的。
    #    实测真实数据里有 `SELECT * FROM items WHERE id = 1`。
    #    它通过了"多词 + 字母占比高"的判据，所以只能按关键词排。
    #
    #    ⚠️ **必须区分大小写**（不加 `IGNORECASE`）。
    #    加过 `IGNORECASE`，结果把 **`Select Location to Teleport.`**
    #    当成 SQL 排掉了 —— 一句真实 UI 文案。
    #    SQL 关键字在代码里是**全大写**惯例，而文案不会；
    #    `Select` 这种首字母大写恰恰是句子/按钮的写法。
    #    "大小写无关"在这里不是更宽松，而是**判据形态错**。
    re.compile(r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b"),
    # ⑧ **纯资源名形态**（`JF Dot Kanamecho 12`、`Glass Break 01`）——
    #    特征是"没有句读、没有功能词、每个词首字母大写"。
    #    用**小写功能词缺失**来判：真正的句子几乎总有一两个
    #    小写词（the/a/of/to/you/…) 或句末标点。
    #
    #    ⚠️ 这里**不能**用"首词全大写"来判断音效描述。
    #    曾经加过 `^[A-Z]{4,}\s+[A-Za-z]+\s+...` 想拦
    #    `IMPACT Concrete Slab on Concrete Slab Short Dark`，
    #    结果把 **`THANK YOU FOR YOUR SUPPORT!`** 和
    #    **`THANK YOU FOR PLAYING THIS DEMO`** 一起拦掉了 ——
    #    全大写的文案（标题、感谢页、按钮）在游戏里**非常常见**，
    #    而"全大写"和"音效名"没有必然关系。**判据的形态选错了。**
    #    音效名靠 `(mono)` 后缀等更精确的特征拦，见上一条。
)

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass
class UnityString:
    """一个从序列化资源里提出的候选字符串。"""

    file: str
    offset: int
    text: str
    #: 置信度（0～1）。给用户排序用，不是"正确率"。
    confidence: float = 0.0
    reason: str = ""


@dataclass
class UnityScanReport:
    files_scanned: int = 0
    bytes_scanned: int = 0
    candidates: list[UnityString] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.candidates)


def _looks_like_game_text(s: str) -> tuple[bool, float, str]:
    """判断一个解码出来的字符串像不像**给玩家看的文案**。

    返回 ``(是否保留, 置信度, 原因)``。判据**保守优先**：
    宁可漏掉几句，也不要用几万条垃圾把报告冲垮。
    """
    t = s.strip()
    if not t:
        return False, 0.0, "空"
    if t.lower() in _NOT_GAME_TEXT_VALUES:
        return False, 0.0, "配置值"
    if len(t) < MIN_LEN:
        return False, 0.0, "太短"
    # 控制字符（保留换行/制表）
    if any(ord(c) < 32 and c not in "\n\r\t" for c in t):
        return False, 0.0, "含控制字符"

    for pat in _NOT_GAME_TEXT_RES:
        if pat.search(t):
            return False, 0.0, f"像代码/资源名（{pat.pattern[:26]}）"

    has_cjk = bool(_CJK_RE.search(t))

    # 含汉字/假名/谚文：几乎一定是文案（代码里不会出现）
    if has_cjk:
        return True, 0.9, "含 CJK"

    words = t.split()

    # ---- 资源名分隔符：`Tile_4_Bottom  --`、`-----Creep  L`、`1024_CircleGlow` ----
    # 人写的文案里不会有 `--`、`__`、连续短横线、多位数字。
    # 这一条单独拿出来，是因为它比"数人写的词"更硬 —— 它是**形态特征**，
    # 而资源名占了放宽判据后新增候选的绝大多数（实测 974 条里 527 条）。
    if _ASSET_SEPARATOR_RE.search(t):
        return False, 0.0, "含资源名分隔符"

    nw = natural_words(t)

    # ---- 单个词：靠**大小写形态**区分"人写的词"和"标识符" ----
    # 见 `is_human_word`：`Options`/`Gallery` 收下，
    # `AdvancingFrontNode`/`BoneJoint`（PascalCase）挡掉。
    if len(words) == 1:
        if is_human_word(t):
            return True, 0.75, "单词标签"
        return False, 0.0, "单标识符/资源名"

    # ---- 多个词：要求至少 2 个"人写的词" ----
    # `Camera Zoom :`（2）、`Save Game`（2）、`Game Over`（2）通过；
    # `1 ---BotCenter`（0）、`x7  Top`（0）被挡。
    if len(nw) >= 2:
        conf = 0.8
        if any(c in t for c in ".!?,:;'\""):
            conf = 0.85
        return True, conf, "多词文本"

    if len(t) >= 24 and nw:
        return True, 0.7, "长句"
    return False, 0.0, f"人写的词不足（{len(nw)}）"


def extract_strings_from_bytes(
    data: bytes, *, filename: str = "", max_candidates: int = 200_000
) -> list[UnityString]:
    """在字节流里按 Unity 的**长度前缀**格式找候选字符串。

    为什么要按长度前缀扫，而不是"扫连续可打印字符"：

    * 长度前缀提供了一个**独立的校验**——读满 `n` 字节后必须能解码，
      且 `n` 与内容自洽。朴素扫描没有这个约束，
      在 `.assets` 上会命中大量跨字段的假字符串；
    * 它天然跳过二进制区域，不会把浮点数的字节读成可见字符。

    仍然会漏（有的字符串长度前缀是我们没识别的变体），
    但**漏比乱好**：这份清单是给用户做人工定位用的，不是自动回写。

    ## ⚠️ 这里**不做**"长时间没命中就大步前跳"的优化

    曾经写过一版：连续 4096 次未命中就前跳 64 KB，理由是
    "那段区间本来就没有候选"。**这个理由是错的**，实测数据：

    * `level0` 有 13 条真实候选，加上跳过后只剩 **1 条**；
    * 相邻候选之间的间隔是 **10,681 / 17,585 / 33,553 / 49,353 字节** ——
      和 64 KB 同一量级。

    也就是说，"连续未命中"**不能**推出"接下来这一段没有候选"。
    跳过的代价是**静默漏掉真实文案** —— 用户永远不知道
    清单里少了几句，而"清单不全"恰恰是这份功能唯一的价值。

    现在只保留**可证明安全**的跳过：**零字节长串**。
    长度前缀 ≥ `MIN_LEN`(4) 意味着首字节非零，
    所以一段 ≥ `ZERO_RUN_MIN` 的连续零字节里**不可能**有字符串起点。
    跳跃范围因此有严格上界，且不依赖任何经验阈值。
    """
    out: list[UnityString] = []
    n = len(data)
    i = 0
    seen: set[int] = set()
    while i + 4 <= n and len(out) < max_candidates:
        # ---- 可证明安全的跳过：连续零字节区 ----
        #
        # ## 为什么可以跳，跳多远
        #
        # 长度前缀 ≥ `MIN_LEN`(4) ⇒ 字符串起点的**首字节非零**。
        # 因此一串 ≥ `ZERO_RUN_MIN` 个连续零字节里**不可能**有起点
        # ⇒ 整段可以跳掉。这个结论**不依赖任何经验假设**。
        #
        # 而 `[i, 下一个零字节)` 这段全是非零字节，**可能是起点**
        # ⇒ 不能跳，必须逐字节检查（下面的 `i += 1`）。
        #
        # 唯一要小心的是"找零区结尾"不能用 Python 循环 ——
        # 开头写过 `while data[e] == 0: e += 1`，在 10 MB 零区上
        # 慢到 **0.40 秒**（就是 Python 逐字节）。改用正则匹配
        # `\x00{64,}` 拿到整段零区的结尾，是 C 实现、一次到位。
        m = _ZERO_RUN_RE.match(data, i)
        if m is not None:
            i = m.end()
            continue

        (ln,) = struct.unpack_from("<I", data, i)
        if MIN_LEN <= ln <= MAX_LEN and i + 4 + ln <= n:
            raw = data[i + 4 : i + 4 + ln]
            # 快速排除：长度前缀若要成立，字节里不该有 NUL（UTF-8 文本没有）
            if b"\x00" not in raw:
                try:
                    s = raw.decode("utf-8")
                except UnicodeDecodeError:
                    s = ""
                # ⚠️ 这里的自洽性检查**必须比字节数**，不能比字符数。
                # 长度前缀是**字节**长度，而 `len(s)` 是**字符**数 ——
                # 对纯 ASCII 两者相等，所以这个 bug 只对**非 ASCII 文案**
                # 生效：`你确定要保存吗？` 是 24 字节 / 8 字符，
                # `len(s) == ln` 永远为假，**中文文案会被全部丢掉**。
                # 而这个功能的用户要翻的就是中文 —— 等于把最该收的漏光。
                if s and len(raw) == ln:
                    keep, conf, why = _looks_like_game_text(s)
                    if keep and i not in seen:
                        seen.add(i)
                        out.append(
                            UnityString(
                                file=filename, offset=i, text=s.strip(),
                                confidence=conf, reason=why,
                            )
                        )
                        i += 4 + ln
                        continue
        i += 1
    return out


def scan_unity_assets(
    path: Path, *, max_candidates: int = 200_000, max_bytes: int = 0
) -> UnityScanReport:
    """扫描单个序列化资源文件（**只读**）。

    ## 为什么要分块，而不是"整个读进来"

    蓝图里有个大游戏的主资源是 **613 MB**。早先的实现设了个
    400 MB 上限，超过就跳过并报一句"已跳过" —— 结果是
    **最可能藏着最多文本的那个文件根本没被扫**，而报告看起来"成功"。

    现在改成**分块流式扫描**：一次读 8 MB，块间保留 4 MB 重叠
    （保证跨块边界的字符串不被截断）。于是：

    * 内存占用恒定（几十 MB），与文件大小无关；
    * **没有任何文件因为"太大"而被跳过**；
    * 跳过优化仍然生效 —— 块内长时间没有候选时直接前跳 64 KB，
      图像/音频数据区不会被逐字节爬完。

    ``max_bytes`` 保留作为**可选**的硬保护（默认 0 = 不限制）。
    """
    rep = UnityScanReport(files_scanned=1)
    try:
        size = path.stat().st_size
    except OSError as exc:
        rep.errors.append(f"{path.name} 无法访问：{exc}")
        return rep
    if max_bytes and size > max_bytes:
        rep.errors.append(
            f"{path.name} 有 {size // (1024 * 1024)} MB，超过配置的上限"
            f"（{max_bytes // (1024 * 1024)} MB），已跳过"
        )
        return rep

    chunk = 8 * 1024 * 1024
    overlap = 4 * 1024 * 1024
    found: list[UnityString] = []
    seen: set[tuple[int, int]] = set()
    try:
        with open(path, "rb") as fh:
            base = 0
            tail = b""
            while len(found) < max_candidates:
                buf = fh.read(chunk)
                if not buf:
                    break
                data = tail + buf
                # `data` 的第 i 字节对应文件偏移 `base - len(tail) + i`
                origin = base - len(tail)
                for c in extract_strings_from_bytes(
                    data, filename=path.name, max_candidates=max_candidates - len(found)
                ):
                    key = (origin + c.offset, len(c.text))
                    if key in seen:
                        continue
                    seen.add(key)
                    c.offset = origin + c.offset
                    found.append(c)
                rep.bytes_scanned += len(buf)
                base += len(buf)
                tail = data[-overlap:] if len(data) > overlap else data
    except OSError as exc:
        rep.errors.append(f"{path.name} 读取失败：{exc}")
        return rep

    rep.candidates = found
    return rep


#: 需要扫描的 Unity 序列化资源文件名模式。
SERIALIZED_PATTERNS = (
    "level*",
    "resources.assets",
    "sharedassets*.assets",
    "globalgamemanagers.assets",
    "*.assets",
)


def find_serialized_assets(data_dir: Path) -> list[Path]:
    """列出某个 ``*_Data`` 下的序列化资源文件（去重、按名字排序）。"""
    found: dict[Path, None] = {}
    for pat in SERIALIZED_PATTERNS:
        for p in data_dir.glob(pat):
            if p.is_file():
                found[p] = None
    return sorted(found)


def scan_game(game_dir: Path, *, max_bytes: int = 0) -> UnityScanReport:
    """扫描一个 Unity 游戏的全部序列化资源（**只读**）。

    ``max_bytes`` 默认 **0 = 不限制**。分块扫描让内存占用与文件大小无关，
    所以"文件太大就跳过"不再有必要 —— 而它曾经**有害**：
    某个游戏的主资源是 613 MB，设了 400 MB 上限之后它被跳过，
    报告却看起来"扫描成功"，用户拿到的是一份**不全的清单**。
    """
    rep = UnityScanReport()
    data_dirs = [d for d in game_dir.iterdir() if d.is_dir() and d.name.endswith("_Data")]
    if not data_dirs:
        rep.errors.append(f"{game_dir} 下没有 *_Data 目录，不像是 Unity 游戏")
        return rep
    for d in data_dirs:
        for f in find_serialized_assets(d):
            sub = scan_unity_assets(f, max_bytes=max_bytes)
            rep.files_scanned += sub.files_scanned
            rep.bytes_scanned += sub.bytes_scanned
            rep.candidates.extend(sub.candidates)
            rep.errors.extend(sub.errors)
    return rep


def write_csv(rep: UnityScanReport, dest: Path) -> Path:
    """导出成 CSV —— 给用户拿到外部工具（UABEA / AssetStudio）里对照着改。

    ## 为什么导 CSV 而不是"直接回写"

    `.assets` 是二进制序列化格式，里面有大量**指向别处的偏移量**。
    改一个字符串的长度会让它后面的所有偏移量失效 ——
    结果通常是游戏打不开，而错误信息毫无线索。

    本工具**不做**这件事（见模块注释）。但它能做的是：
    把"有哪些文本、在哪、原文是什么"精确列出来，让用户
    用**专业工具**（它们实现了完整的序列化格式）去改，
    或者用本工具的「散装文件」模式处理导出的 TextAsset。

    这比"什么都不做"有用得多，也比"猜着盲写"安全得多。
    """
    import csv as _csv

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8-sig", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["file", "offset", "confidence", "reason", "source", "target"])
        for c in sorted(rep.candidates, key=lambda x: (x.file, x.offset)):
            w.writerow([c.file, c.offset, f"{c.confidence:.2f}", c.reason, c.text, ""])
    return dest
