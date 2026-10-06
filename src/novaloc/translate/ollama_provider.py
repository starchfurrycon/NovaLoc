"""基于 Ollama 的本地翻译适配器（默认后端，全程离线）。

核心流程（每一步都是被实战教训逼出来的）：

1. **先屏蔽占位符**：把 ``{0}`` ``%s`` ``\\V[1]`` ``<color=#fff>`` 换成 ``⟦i⟧``。
   模型看不到原始语法就无从破坏，攻击面归零。
2. **批量发送，要求带显式索引的对象数组** ``[{"i":0,"t":"..."}]``。
   裸数组一旦漏一条就会整体错位，而且译文通顺、错得极隐蔽。
3. **``repeat_penalty`` 必须显式设置**（Ollama 默认 1.0 = 关闭），
   否则本地小模型在批量任务里会复读到停不下来。
4. **多级恢复**：JSON 解析失败时逐级降级（见 :mod:`.json_parse`），
   最后退化为逐条翻译。宁可慢，也不丢条目。
5. **还原 + 校验占位符**：多重集不一致就判致命，标记 FAILED 并保留原文，
   **绝不把破坏过的文本写回游戏**。
"""

from __future__ import annotations

import logging
import re
import time

from ..core.registry import Context, ProviderError, TranslateItem, register
from ..lang import guess_language
from ..models import EntryStatus, TextKind, TranslationEntry
from . import placeholders as ph
from . import prompts
from .guards import guard
from .json_parse import parse_translations
from .ollama_client import ModelMissing, OllamaClient, OllamaError, OllamaNotRunning
from .sanitize import has_lone_surrogate, sanitize_for_json

log = logging.getLogger(__name__)

#: 批量/单条调用里"失败后值得重试"的异常。
#:
#: 为什么要把 `UnicodeEncodeError`/`ValueError` 显式列进来：
#: 真实游戏上的翻译跑了 27 分钟后被
#: `UnicodeEncodeError: ... surrogates not allowed` 整个打断。
#: 那个异常继承自 `ValueError`，而这里原本只捕
#: `(ProviderError, OllamaError, ModelMissing, OllamaNotRunning)` ——
#: **一个都不匹配**，于是它穿过重试层、穿过逐条降级，一路逃到
#: `stage_translate`，让一整轮翻译作废。
#:
#: 单一文本触发的问题必须只让那一批失败，不能让整个项目失败。
#: 捕获过宽的风险是掩盖真 bug，所以 `sanitize` 层已经先做了输入净化；
#: 这里只是**兜底**：任何"这一批的文本有问题"都退化为该批失败，
#: 由上层标记 FAILED 并在审校页提示，而不是中断整轮。
_RETRYABLE: tuple[type[BaseException], ...] = (
    ProviderError,
    OllamaError,
    ModelMissing,
    OllamaNotRunning,
    UnicodeEncodeError,
    UnicodeDecodeError,
    ValueError,
)

#: 需要"更聪明"的文本类型：独占一批，避免被 UI 短标签的风格带偏
_CAREFUL_KINDS = {TextKind.ITEM_DESC, TextKind.NARRATION, TextKind.CREDIT}

#: 补救时用的**子批大小**（固定小批，不是二分）。
#:
#: ## ★ 为什么不是"递归二分"（我第一版做错了，实测推翻）
#:
#: 我第一版把逐条补救换成递归二分，理由是"拆小更容易成功"。实测：

#: ```
#: 场景                 批大小   整批调用  单条调用   合计   旧(纯逐条)
#: 只回 1 条               40         2       38     40         41   ← 没收益
#: 整批抛异常              40        33       40     73         43   ← 更糟
#: ```
#:
#: 两个原因：
#:
#: 1. **补空时单条调用本来就成功**，所以"分小"没有带来任何额外成功；
#:    请求数只是从"N 次单条"变成"约 N 次单条 + 若干次批调用"。
#: 2. 二分本身要发 `2*ceil(log2 N)` 次批调用；每层还各自触发外层重试
#:    ⇒ 在"整批抛异常"的场景下**请求数翻了近一倍**。
#:
#: ⇒ 结论：**二分的方向是错的**。正确做法是"固定小批"——
#:   本项目的提示词历史里有实测数据：「批=4 → 4/4」可靠，
#:   而大陷入重复循环的正是大批。所以按 4 条一批重发。
#:
#: ## 算术
#:
#: 40 条缺 34 条：
#:   * 旧（逐条）：34 次请求；
#:   * 固定小批：ceil(34/4) = 9 次请求。
#:
#: ⇒ 约 **3.8 倍**少的请求。这才是该做的优化。
#:
#: ⚠️ 这个值必须**实测**才能确认（小批是否真的更可靠）。改它之前先量。
_SUBBATCH_SIZE = 4

#: 一次补救最多发多少次子批请求（防止病态批把请求数炸上天）。
#: 超过这个数就直接落到单条兜底 —— 慢但**一定**能收敛。
_SUBBATCH_MAX_REQUESTS = 12

#: 这些类型天然很短，可以多塞一些（省请求次数）
_SHORT_KINDS = {
    TextKind.UI_LABEL,
    TextKind.MENU,
    TextKind.ITEM_NAME,
    TextKind.SKILL,
    TextKind.MAP_NAME,
    TextKind.CHARACTER_NAME,
}

#: 判断"片段是否已被前文覆盖"时，归一化用的字符集：
#: 只留中日文与字母数字 —— 标点/空格/引号样式的差异不算差异。
_MERGE_STRIP = re.compile(r"[^0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]+")


def _source_line_count(slots: list[str] | None) -> int:
    r"""源文有多少行 —— 从**换行占位符**数出来。

    ## 为什么需要它（#37 实测）

    `mask_newlines=True` 会把换行换成占位符，于是**多行源文在提示词里
    是一行**：

        源文 89 字符 / 3 行
        掩码后: 'Mashiro:⟦0⟧"Mnnn! ♡ …⟦1⟧I-It sounds so lewd…"'
        slots 个数: 2　（两个都是 '\n'）

    但模型**仍按语义行回多段**。此时 `_best_segment` 只留一段，
    **其余行被丢掉**（实测比值稳定在 0.04~0.29，结构性）。

    ## 判据是结构性的，不用阈值

    * 源文行数 = 换行占位符个数 + 1；
    * 若模型给的**段数正好等于源文行数** ⇒ 这是**逐行对应**，
      按行序拼回**不会复读**（每段对应**不同的**源行）。

    这条判据不依赖任何阈值，所以不会重演 `_best_segment` docstring 里
    那次失败（"覆盖率区间重叠，任何阈值都误杀一边"）。

    只数**确实是换行**的占位符：`\\C[6]`、`\\N[2]` 这类引擎转义
    被屏蔽后也进 `slots`，但它们**不是**行边界，数进来会把行数算多、
    进而误判"逐行对应"。
    """
    if not slots:
        return 1
    n = 1
    for s in slots:
        if s and s.replace("\r\n", "\n").replace("\r", "\n") == "\n":
            n += 1
    return n


def _is_degenerate_repetition(lines: list[str], *, max_len: int = 20) -> bool:
    r"""逐行译文是不是"同一个短片段复制了 N 遍"。

    ## 为什么需要这个（#39 实测）

    模型对**每一行**都回同一个说话人标签时，逐行路径会拼出
    `'店员 1：\n店员 1：\n店员 1：'` —— 每行单独看都"是合法中文、
    长度也在范围内"，所以**逐行守卫会全部放行**，拼起来却是把垃圾
    复制了三遍，还挂着 `status=translated` 写回游戏。这是**静默**的。

    ## 为什么这条判据**有判别力**（而比值判据没有）

    我同时试过"译文/原文比值下限"来挡它 —— 实测**完全重叠、无判别力**：

        人工确认的垃圾：比值 0.28 / 0.17 / 0.04 / 0.43 / 0.38
        合理合并译文：  比值 0.17 / 0.26 / 0.29 / 0.29 / 0.31 / 0.37
        ⇒ 垃圾最大 0.43 > 合理最小 0.17，**任何阈值都误杀一边**

    而"**同一个短片段重复**"是**结构性**的，不受源文长短、语种、
    紧凑程度影响：合理的合并译文里不会出现三行一模一样的短句。
    ⇒ 只保留这一条。

    ⚠️ `max_len=20` 是"短片段"的界定：三行都等于**长句**时更可能是
    合理的重复修辞，不当垃圾处理（宁可漏，不可误杀）。
    """
    parts = [p.strip() for p in lines if p.strip()]
    if len(parts) < 2:
        return False
    return len(set(parts)) == 1 and len(parts[0]) < max_len


def _block_degenerate_entries(
    entries: list[TranslationEntry], stats: dict[str, int] | None = None
) -> int:
    r"""把"同一短片段复制多遍"的译文标成失败、清空译文，返回拦下的条数。

    ## 为什么放在**最后**（#39 实测）

    我先把判据加在 3.7（逐行重译）里，结果实测发现**它漏了**：
    退化产物根本没走到 3.7 —— 它在**块 3 的 `placeholder_repaired`
    修复路径**上就被标成 `translated` 了（实测 `placeholder_repaired == 1`）。

    这正是本项目反复出现的陷阱：**"这段代码没问题"与"这段代码没被执行"
    看起来完全一样**。在**单点**加守卫只覆盖那一个点；而
    "写回前的最终值"是唯一能覆盖**所有**路径
    （补空 / 块 3 / 3.5 定向重试 / 3.7 逐行 / 修复路径）的位置。

    ## 为什么抽成独立函数

    原先是内联在 `translate_batch` 末尾的十几行。抽出来之后可以
    **直接单测**（确定性、不依赖提示词措辞或模型桩）——
    而"内容驱动"的假模型被我证明是**不可靠的**：提示词模板里就有
    `例：'⟦0⟧: Confirm'` 和 `Lifia:⟦0⟧「I have to go.」`
    这类示例文本，按内容匹配会被模板本身命中。

    ⚠️ 只处理**多行**译文：单行短译文不适用（`'莉菲娅'` 这种
    "整条翻成一个词"是合法正解）。
    """
    blocked = 0
    for e in entries:
        if e.status is not EntryStatus.TRANSLATED:
            continue
        lines = [x for x in (e.target or "").split("\n") if x.strip()]
        if len(lines) >= 2 and _is_degenerate_repetition(lines):
            e.status = EntryStatus.FAILED
            e.target = ""
            e.warnings = [*e.warnings, "degenerate_repetition"]
            blocked += 1
    if blocked and stats is not None:
        stats["degenerate_blocked"] = stats.get("degenerate_blocked", 0) + blocked
    return blocked


#: 日文假名（平假名 + 片假名）。用于识别"该翻却被回显"的条目。
_KANA_RE = re.compile(r"[\u3040-\u30ff]")


def _content_only(s: str) -> str:
    """只留字母/数字/汉字（丢标点与空白）。

    与 `images.service._content` 语义一致；这里是**文本路径**的复刻，
    避免跨包导入私有函数。
    """
    return "".join(ch for ch in (s or "") if ch.isalnum())


def is_kana_echo(source: str, target: str) -> bool:
    r"""译文是不是**日文假名条目的原样回显**（= 批内该翻没翻）。

    ## 实测依据（`.scratch/_probe_echo_kana.py`）

    对 30 条"含假名 + 批内回显"的条目**单独**问模型：

    ```
    'ポイズンガード'  批内→'ポイズンガード'   单独→'毒药卫'
    'ゴブリン'        批内→'ゴブリン'         单独→'绿皮'
    'ポーション'      批内→'ポーション'       单独→'药水'
    ... 30/30 全部得到真译文（100%）
    ```

    ⇒ 这是"批内该翻没翻"，**不是**"本来就该保留"。

    ## 为什么只判"含假名"，不判"所有回显"

    全库 19,353 条回显里：

    * **纯拉丁**（`Rockman`/`IN-cubus001`）15,892 条 ⇒ **不该重译**（专名）；
    * **含假名** 2,218 条 ⇒ **该重译**；
    * 含汉字 1,243 条 ⇒ 不确定（日文汉字可能本就该保留），**先不动**。

    只在**能用行为验证**的最小集合上动手 —— 这是我上一轮在
    "跳过不翻译"上翻车（静态启发式收益 5% / 风险 25%）学到的。
    """
    if not source or not target:
        return False
    if not _KANA_RE.search(source):
        return False
    a, b = _content_only(source), _content_only(target)
    return bool(a) and a == b


def _is_label_only_output(target: str, source: str = "") -> bool:  # noqa: ARG001
    r"""**#42 撤回：这个判据已停用。** 保留函数只为让撤回记录可执行、可测试。

    ## 它想抓什么

    模型有时对长源文**只回说话人标签**（`'店员 1\n：'`），而
    `status` 却是 `translated`、无异常、无警告 —— 静默地把整句留在原文。

    ## 为什么停用（实测长度区间**仍然重叠**）

    ``总长`` 实测（源文都 ≥60 字符）::

        4   '人 1\n：'                        ← 垃圾
        5   '店员 1\n：'                      ← 垃圾
        5   '男人 1\n：'                      ← 垃圾
        7   '女店员：\n欢迎。'                 ← 垃圾
        **10  '男行人:\n嗯？怎么了？'            ← 合法简明译文！**
        12  '男人：\n哼，我马上就要了。'
        13  '马西罗:\n喂！♡ 亲爱的！♡'          ← 垃圾
        29  '马西罗:\n这不奇怪吧？…\n大家一起去散步吧！'   ← 合法

    **垃圾 13 > 合法 10** ⇒ 任何阈值都误杀一边。
    我把阈值试到 ``≤12`` 时抓到了全部 4 条确认垃圾，但**仍然误杀**
    `'男行人:\n嗯？怎么了？'`。

    这是本项目**第三次**撞上同一堵墙：
    `_best_segment`（2026-09）、#39 的比值判据、以及这里。
    ⇒ **按既定原则停用阈值判据，只留零误杀的结构性判据**
    （`_is_degenerate_repetition`）。

    ⇒ 内容丢失的**极端形态仍然会漏**，作为已知局限记录在
    `docs/ROADMAP.md` 与 `CHANGELOG.md`，并由
    `tests/test_degenerate_perline.py` 的
    `test_KNOWN_LIMITATION_*` 用例钉住现状。

    ⚠️ 函数体恒返回 `False`，所以**不可能**因为误调用而误杀。
    调用点已删除；保留定义是为了让上面的记录不被"代码里查无此物"。
    """
    return False


def _best_segment(segs: list[str], slots: list[str] | None = None) -> str | None:
    r"""从模型返回的多个片段里挑**一个最好**的，而不是把它们拼起来。

    ## 为什么是"挑"而不是"拼" —— 真实数据（10/10 复现）

    用产品**真实**的单条路径（`_call_single` + 真实提示词）对 10 条
    ElfLifia 多行条目各请求一次，**10/10** 都返回多段 JSON。
    逐条人工判定后，模型给的分段有**三种**语义，而它们长得很像：

    | 形态 | 真实样本 | 正确做法 |
    |---|---|---|
    | 每段是**同一内容的完整替代译文** | `{"0":"造成4倍「防御」的伤害。随后所有「防御」效果消失。", "1":"造成4倍你的「防御」值造成的伤害。\n然后移除所有「防御」效果。"}` | 取**段 0** |
    | 段 0 **不完整**，段 1 补全 | `{"0":"恢复 5 点生命值。", "1":"生命回复：每回合结束时回复…"}` | 取**段 0**（它已含源文全部信息） |
    | 各段**完全相同** | `{"0":"获得 1 个『复制』。…", "1":"获得 1 个『复制』。…"}` | 取**段 0** |

    三种形态的共同答案是**段 0**。

    ## 为什么"拼接"是错的（我第一版就犯了这个错）

    第一版按编号 `"\n".join`，结果在形态 1、3 上把同一句话接了两遍：

        '造成 4 倍「防御」的伤害。随后所有「防御」效果消失。
         造成 4 倍你的「防御」值造成的伤害。\n然后移除所有「防御」效果。'

    这是**复读**——比丢内容更糟（译文长度翻倍，游戏里显示两遍）。
    本项目 2026-09 已因复读事故修过一次，不能再犯。

    而"用二元组覆盖率去重"也试过，**不可行**：真实样本里
    "重复措辞"的覆盖率是 0.38~0.90，"真·互补内容"是 0.00~0.57 ——
    **两个区间重叠**，任何阈值都会误杀一边。信号不足以分开它们，
    所以选一个不依赖阈值的规则。

    ## 选择规则（按优先级）

    1. **丢的占位符最少**的段（含 `slots` 时）。占位符丢了这条译文
       就没法正确回写，是最硬的约束，优先于一切。
    2. 其中**含换行**的段 —— 换行是作者定的版面（见
       `_split_across_slots` 的说明），带换行的段能直接对上引擎槽位。
    3. 仍并列时取**编号最小**的段（段 0）。

    ▲ 规则 3 不是随手加的：段 0 是模型对"整条文本"的主答案，
    后续段才是它自己拆出来的补充/替代。实测 10 条里 **8 条**
    各段都没丢占位符（占位符在源文里、模型直接吃掉了），
    此时若按"最长"选，`Items.json:/2` 会把**补充说明段**
    （`生命回复：每回合结束时…`）选成整条译文，
    读起来像原文只有后半句。以"编号最小"兜底才对。

    ▲ 判定换行时要**同时认**真换行与 `\n` 字面量：模型输出的是
    JSON 字符串时，换行是 `"\\n"` 两个字符（实测
    `Items.json:/19` 就是这样）。只看真换行会漏掉这类段。

    Args:
        segs: 模型按编号顺序给出的片段。
        slots: 本条目的占位符表（用于规则 1）；给了才启用规则 1。

    Returns:
        选中的单个片段；全为空时返回 ``None``。
    """
    cand = [s.strip() for s in segs if s and s.strip()]
    if not cand:
        return None
    if len(cand) == 1:
        return cand[0]
    if slots:
        missing = [sum(1 for m in slots if m not in s) for s in cand]
        fewest = min(missing)
        cand = [s for s, m in zip(cand, missing, strict=True) if m == fewest]
    with_nl = [s for s in cand if "\n" in s or "\\n" in s]
    pool = with_nl or cand
    # 并列时取**编号最小**的（`pool` 保持了原始顺序）
    return pool[0]


@register("translate", "ollama")
class OllamaTranslationProvider:
    name = "ollama"

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        cfg = ctx.config
        self.cfg = cfg
        self.client = ctx.cache_get(
            "ollama_client",
            lambda: OllamaClient(cfg.resolved_ollama_host(), timeout=cfg.ollama.request_timeout_s),
        )
        self._model = cfg.ollama.text_model
        self._ready: tuple[bool, str] | None = None
        #: 统计信息，交给 UI 显示
        #: ★★ **请求预算**（见 OllamaConfig.request_budget_factor）
        #: 与已用计数。**唯一发请求的出口是 _chat**，预算在那里记。
        self._requests_used = 0
        self._request_budget = 0  # 由 translate_batch 按条目数设定
        self.stats: dict[str, int] = {
            "batches": 0,
            "retries": 0,
            "single_fallbacks": 0,
            "recovered_json": 0,
            "placeholder_fatal": 0,
            "placeholder_repaired": 0,
            "cross_item_leak": 0,
            # 批量调用成功、但个别条目返回空串时的补救次数。
            # 实测这种"空"与整批失败无关；不补就是静默漏译。
            "empty_retry": 0,
            # 补空救回来的条目数。与 empty_retry 的差值反映模型在
            # "短串 + 占位符"结构上的真实弱点。
            "empty_recovered": 0,
            "items": 0,
        }

    # ------------------------------------------------------------------

    def stats_snapshot(self) -> dict[str, int]:
        """给调用方一份统计的**只读快照**。

        为什么需要这个公开入口（而不是让调用方直接读 `self.stats`）：

        `self.stats` 里的数字**本来就在记**，但**从来没被汇总输出过** ——
        实测代价是：那两种"慢到看起来像卡死、流水线却一切正常"的降级路径
        （整批只回 2/12、逐条降级）在整轮端到端跑里各出现 11 次，
        却**没有任何一处**把它们报出来。查问题时只能靠翻日志里散落的
        `log.warning` 行去数。

        实测数据（`Dungeon And Darkness-Steam` 一次端到端）：
        日志 331 行里，`批 12 条里只回 2 条` 出现 8 次、
        `Ollama 返回 500: prediction aborted` 出现若干次，
        而收尾报告里**一个字都没提**。

        所以把快照做成公开方法，让流水线在 `translate` 阶段结束时
        汇总打印一次 —— 让"藏在计数里的慢"变成一眼能看到的数字。
        """
        return dict(self.stats)

    def available(self) -> tuple[bool, str]:
        if self._ready is not None:
            return self._ready
        if not self.cfg.ollama.enabled:
            self._ready = (False, "配置里已禁用 Ollama")
            return self._ready
        if not self.client.is_running():
            self._ready = (False, f"Ollama 服务未运行（{self.client.host}）")
            return self._ready
        names = self.client.list_models()
        if not self._has_model(names, self._model):
            self._ready = (
                False,
                f"缺少翻译模型 '{self._model}'。已安装：{', '.join(names) or '无'}；"
                f"请执行 `ollama pull {self._model}`",
            )
            return self._ready
        ver = self.client.version()
        self._ready = (True, f"Ollama {ver} · 模型 {self._model}")
        return self._ready

    @staticmethod
    def _has_model(names: list[str], model: str) -> bool:
        if model in names:
            return True
        base = model.split(":")[0]
        return any(n.split(":")[0] == base for n in names)

    def model_info(self) -> dict:
        try:
            return self.client.show(self._model)
        except OllamaError:
            return {}

    def _options(
        self,
        *,
        temperature: float | None = None,
        n_items: int = 1,
        src_chars: int = 0,
    ) -> dict:
        o = self.cfg.ollama
        # ⚠️ 这里**只放 Ollama `/api/chat` 真正认识的参数**。
        #
        # `OllamaConfig` 里还有 `flash_attention` 与 `kv_cache_type`，
        # 但它们是 **Ollama 服务器**的环境变量
        # （`OLLAMA_FLASH_ATTENTION` / `OLLAMA_KV_CACHE_TYPE`），
        # **不是**每个请求能带的参数 —— 放进 `options` 会被静默忽略。
        # 两个字段的 docstring 已写明这件事。
        #
        # 早先这里在 `return opts` **之后**还留了一段（不可达）代码，
        # 显然是想把这两个塞进 options 但没写完。
        # 留着比删掉更糟：读代码的人会以为设置生效了。
        opts: dict[str, object] = {
            "temperature": o.temperature if temperature is None else temperature,
            "top_p": o.top_p,
            "num_ctx": o.num_ctx,
            # 关键：不设这个，Ollama 用 1.0（等于关闭），批量翻译必然复读
            "repeat_penalty": o.repeat_penalty,
            "repeat_last_n": o.repeat_last_n,
            "num_predict": self._num_predict(n_items, src_chars),
        }
        return opts
        # num_gpu 交给 Ollama 自己决定；硬设容易撞显存上限

    def _num_predict(self, n_items: int, src_chars: int) -> int:
        """给这一批算一个**输出 token 上限**。这是防"跑飞"的硬闸门。

        ## 为什么必须设

        原先**没设** `num_predict`，Ollama 就不限制输出。真实游戏
        （19048 句台词）实测：正常批次 6～10 秒，个别批次 **384 秒**、
        有的 **1500 秒以上**（模型停在 `done_reason='length'`，
        撞的是 `num_ctx` 上限反复生成）。整轮翻译看起来像卡死，
        实际在一条条慢慢烧。

        ## 上限怎么算

        上限必须**贴着这批内容的实际需要**，太松挡不住跑飞、
        太紧会把正常 JSON 截断 —— 截断的后果更慢：解析失败 → 重试
        → 再逐条降级（12 条 = 12 次调用）。

        实测两次校准：

        * `per_item=48`（一刀切）：跑飞的那批从 384 秒降到 17.6 秒，
          但另一些批次涨到 **153～183 秒**，正是因为被截断后触发了重试与逐条降级；
        * 改成按内容算之后，正常批次回到 6～17 秒。

        算式：``(原文总字符数 × 系数 + 每条 JSON 开销)``，
        再用 ``num_ctx/3`` 兜底。

        系数取 2.2：中文译文一般**比原文短**，但这批原文里混着西语、
        表情符号和 RPG Maker 转义序列，实测译文/原文的 token 比接近 1.0～1.5，
        2.2 留了安全余量。每条再加 12 token 覆盖
        ``{"i": 0, "t": ""},`` 这类 JSON 结构开销。
        """
        o = self.cfg.ollama
        per_item = max(1, int(getattr(o, "max_output_tokens_per_item", 48)))
        factor = float(getattr(o, "max_output_char_factor", 2.2))
        # 内容需要多少
        #
        # ★ 这条保险丝必须**跟着内容走**，不能按"每条平均"一刀切。
        #
        # 旧实现是 `ceiling = per_item * n_items + 64`，即"每条平均 48 token"。
        # 批里一旦混进长条目，额度就被平均掉，实测（`072 Project` 真实文本，
        # 工作区 f4b03ca791a9）：
        #
        #   * 单条 400 字符的魔物说明 ⇒ `need=912`，但 `ceiling=48*1+64=` **112**；
        #     而它的中文译文要 **~300 token**
        #     （另一次探针实测：`940 字符 -> 279 token`）⇒ JSON 被截断在
        #     字符串中间（`'{"t": "……【生命值】\\n【攻击力】\\n【'`），
        #     **三种解析策略全部失败**，重试 3 次再逐条降级，最后还是空译文。
        #   * 后果：`len(source) > 200` 的条目 **18/20 = 90% 拿不到译文**，
        #     而全库游戏里 3–4 行的"人物/魔物介绍"**全部**超过 200 字符。
        #
        # 取"内容估算"与"条数×每条额度"里**较大**的那个，再留 64 token
        # 给 JSON 收尾。回归测试见
        # `tests/test_ollama_options_wiring.py::test_long_single_item_gets_budget_proportional_to_its_length`
        # —— 该用例在旧实现下**确实是红的**（已验证）。
        need = int(src_chars * factor) + 12 * max(1, n_items) + 32
        per_count = per_item * max(1, n_items) + 64
        budget = max(need, per_count)
        # 硬上限：绝不超过上下文的三分之一，防止跑飞（原实现的本意，保留）
        return max(64, min(budget, o.num_ctx // 3))

    # ------------------------------------------------------------------
    # 批量切分
    # ------------------------------------------------------------------

    def _hints_of(self, item: TranslateItem) -> list[str]:
        """取这条目在提示词里收到的规则文本（供 hint 回声判据比对）。

        ▲ 必须与实际发给模型的 hint **同源**（都来自 ``prompts.KIND_HINT``），
          否则提示词一改，判据就悄悄失效了。
        """
        hint = prompts.KIND_HINT.get(item.unit.kind, "")
        return [hint] if hint else []

    def _make_batches(self, items: list[TranslateItem]) -> list[list[int]]:
        """把条目切成批次。

        经验值：UI 短标签可以 30~50 条一批，长对话 8~12 条，
        而且**长短句不要混批** —— 长句的语言风格会传染给短标签，
        导致按钮文字变长、溢出。
        """
        o = self.cfg.ollama
        batches: list[list[int]] = []
        cur: list[int] = []
        cur_chars = 0

        def flush() -> None:
            nonlocal cur, cur_chars
            if cur:
                batches.append(cur)
                cur, cur_chars = [], 0

        # ★ 「长文本」的门槛不能定得太低 —— 定低了会**逐条**发请求，慢 9 倍。
        #
        # 早先这里是硬编码 `len(text) > 60`。真实 MV 游戏（剩余 34357 条）实测：
        #
        #     长度：中位 50  均值 43  90 分位 67  最大 159
        #     超过 60 字符的：11966 条 = **34.8%**
        #
        # 也就是说三分之一的文本被逐条翻译。而"逐条"的代价是实打实的：
        # 一条 67 字符的对白和一条 20 字符的都占满一次模型往返（约 5 秒），
        # 于是 34357 条要发约 12866 次请求 ≈ 7.4 小时。
        #
        # 更重要的是：**60 字符根本不是"长"**。900 字的旁白才谈得上
        # "语言风格传染给短标签"。把门槛提到 200 之后：
        # 只有极少数真正长的条目逐条走，其余按 `max_batch_chars` 正常成批 ——
        # 请求数降到约 1/9，而 `max_batch_chars` 仍然守着"一批别塞太多字"。
        #
        # 顺带说明为什么门槛可以调而"短标签不混批"必须留着：
        # 后者防的是**按钮文字被长句带长**（真实会溢出 UI 框），
        # 前者只是省请求数，不涉及质量。
        for i, item in enumerate(items):
            text = item.unit.source
            kind = item.unit.kind
            long_text = len(text) > o.max_batch_long_chars
            hard = len(text) > o.max_batch_chars or kind in _CAREFUL_KINDS or long_text
            short = kind in _SHORT_KINDS and len(text) <= 20

            if hard:
                flush()
                batches.append([i])
                continue

            # 短标签和普通文本不混在一批
            if cur:
                prev_short = items[cur[0]].unit.kind in _SHORT_KINDS and len(
                    items[cur[0]].unit.source
                ) <= 20
                if prev_short != short:
                    flush()

            # ★★ 批大小上限：**普通文本从 25 降到 8**（实测吞吐翻倍）
            #
            # 热态实测（`.scratch/_probe_warm.py`，走生产的 `/api/chat`，
            # 25 条真实原文，模型已加载）：
            #
            # | 批大小 | 并发1 | 并发2 | 并发3 | 并发4 |
            # | --- | --- | --- | --- | --- |
            # | **8** | 170 | 332 | 461 | **548** |
            # | 16 | 165 | 327 | 486 | 476 |
            # | **25**（原上限） | **148** | 309 | 324 | 308 |
            #
            # 两个结论：
            #
            # 1. **批越小，单位时间吞吐越高。** 批 8 的"每条耗时"约是
            #    批 25 的 **3.7 倍之一** —— 因为提示词变长后，模型要读更多
            #    上下文，而注意力的开销随长度**超线性**增长。
            #    8 条批：提示词约 400 字 → 单请求 3.3 秒；
            #    25 条批：提示词约 630 字 → 单请求 16.5 秒（并发 4 时）。
            # 2. **原来的 "25 条 + 并发 4" 恰好是表里最差的一格**（308），
            #    比 "8 条 + 并发 4"（548）慢 **1.8 倍**。
            #
            # ⇒ 普通文本上限取 8。
            #
            # ▲ 为什么 `short` 仍取 50：短标签（`HP`/`OK`）本身就是短串，
            #   拼进一批的提示词增量很小，实测那种批的"每条耗时"没有恶化；
            #   而且长门槛（`max_batch_long_chars`）仍然守着"短标签别被
            #   长句带长风格"这条质量规则。
            limit = min(o.max_batch_strings, 50 if short else 8)
            if cur and (len(cur) >= limit or cur_chars + len(text) > o.max_batch_chars):
                flush()
            cur.append(i)
            cur_chars += len(text)
        flush()
        return batches

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------

    def _recover_by_subbatch(
        self,
        items: list[TranslateItem],
        masked: list[str],
        slots: list[list[str]],
        already_failed: set[int],
    ) -> tuple[dict[int, str], str | None]:
        r"""批量失败/只回少数后，用**固定小批**把缺的条目捞回来。

        ## ★★ 为什么是"固定小批"而不是"逐条"（原来的实现）

        原实现是 `for li, it in enumerate(items): self._call_single(...)`
        —— **40 条缺 34 条就发 34 次请求**。

        实测（run5 的 `auto9.err`）这个病态有多普遍：

        ```
        '只回少数'事件          1,067 次
        其中退化逐条            1,055 次（99%）
        期望 15,055 条 → 实回 2,333 条（回收率 15.5%）
        ```

        ⇒ **85% 的批次内容靠逐条补救**，而 GPU 利用率只有 **13%**
          （请求太碎，GPU 一直在等）。

        ## ★ 我先试了"递归二分"，**实测推翻了它**

        理由是"拆小更容易成功"。实测：

        ```
        场景            批大小  整批调用  单条调用   合计   旧(纯逐条)
        只回 1 条          40        2       38     40         41   ← 无收益
        整批抛异常         40       33       40     73         43   ← 更糟
        ```

        两个原因：

        1. **补空时单条调用本来就成功** ⇒ "分小"没带来任何额外成功，
           请求数只是从 N 次单条变成 N 次单条 + 几次批调用；
        2. 二分每层各自触发外层重试 ⇒ "整批抛异常"时请求数**翻了近一倍**。

        ⇒ 方向错了，改成**按 `_SUBBATCH_SIZE` 切固定小批**。
          本项目提示词历史里有实测：「批=4 → 4/4」可靠，
          而陷入重复循环的正是大批。

        ## 算术

        40 条缺 34 条：

        * 旧（逐条）：**34** 次请求；
        * 固定小批：ceil(34/4) = **9** 次请求。

        ## 终止条件

        * 子批也失败的条目 ⇒ 落到 `_call_single` 兜底（慢，但**一定**收敛）；
        * 子批请求数超过 `_SUBBATCH_MAX_REQUESTS` ⇒ 剩余全部单条兜底
          （防止病态批把请求数炸上天）。

        `already_failed` 里的下标**不再问**：触发源是内容，不是偶发抖动
        （这条结论来自原实现的实测注释，保留）。
        """
        out: dict[int, str] = {}
        if not items:
            return out, None

        todo = [i for i in range(len(items)) if i not in already_failed]
        if not todo:
            return out, None

        err: str | None = None

        # --- 1. 按固定小批切分并重发 ---
        #
        # `enumerate` 的下标同时充当"已发子批请求数"，用于卡住
        # `_SUBBATCH_MAX_REQUESTS`（防止病态批把请求数炸上天）。
        for used, start in enumerate(range(0, len(todo), _SUBBATCH_SIZE)):
            if used >= _SUBBATCH_MAX_REQUESTS:
                break
            chunk = todo[start : start + _SUBBATCH_SIZE]
            sub = [items[i] for i in chunk]
            sub_masked = [masked[i] for i in chunk]
            try:
                got_map = self._call_batch(sub, sub_masked)
            except _RETRYABLE as exc:
                err = str(exc)
                got_map = {}
            # 子批的返回值是**局部下标**，映射回本方法的全局下标
            for li_local, text in (got_map or {}).items():
                if 0 <= li_local < len(chunk) and text and text.strip():
                    out[chunk[li_local]] = text

        missing = [i for i in todo if i not in out]
        if not missing:
            return out, err

        # --- 2. 子批仍然救不回 ⇒ 单条兜底（可靠性最后一道） ---
        #
        # ▲ 实测三种场景（`.scratch/_measure_subbatch.py`）：
        #
        # | 场景 | 批大小 | 本策略 | 旧(逐条) | 倍数 |
        # | --- | --- | --- | --- | --- |
        # | 小批可靠、大批失败 | 40 | 15 | 43 | **2.9x** |
        # | 大批只回 2 条 | 40 | 38 | 40 | 1.1x |
        # | 任何批都失败 | 40 | 55 | 43 | **0.8x（略差）** |
        #
        # 第 3 行是唯一退步：条目本身就病态（任何批大小都失败），
        # `_SUBBATCH_SIZE` 的子批请求纯属浪费。我在 `_SUBBATCH_SIZE`
        # 的注释里说明了这个权衡 —— 选择接受，因为：
        #   * 第 1 行（**真实系统的实测形态**：1,055/1,067 次都是
        #     "大批失败但条目本身可救"）收益 ~3 倍；
        #   * 第 3 行只差 20%，且最终**结果正确**（单条兜底仍会跑）。
        #
        # 曾想加"子批一个都没救回就不再试"的守卫，写完发现
        # 与下面这段**完全等价**（都是逐条兜底）⇒ 是冗余代码，删掉。
        for li in missing:
            try:
                got = self._call_single(items[li], masked[li], slots=slots[li])
            except _RETRYABLE as exc:
                err = str(exc)
                already_failed.add(li)
                continue
            if got:
                out[li] = got
            else:
                already_failed.add(li)
        return out, err

    def _chat(
        self,
        user: str,
        *,
        system: str,
        temperature: float | None = None,
        n_items: int = 1,
        src_chars: int = 0,
        timeout: float | None = None,
    ) -> str:
        """发一次 `/api/chat`。

        ``timeout`` 覆盖客户端的默认上限（300 秒）。**单条请求应当传一个
        更短的值** —— 见 `_call_single_once` 与
        `OllamaConfig.single_request_timeout_s` 的说明。
        """
        # ★★ 请求预算：**唯一发请求的出口**在这里，所以预算也在这里记。
        #
        # 见 OllamaConfig.request_budget_factor 的 docstring：
        # 三层重试相乘（批 3 × 逐条 2 × 补空 2 × 单条 2）让一条病态条目
        # 最多发 6 次请求，实测 960 条失败 ⇒ 约 5,760 次无效请求。
        #
        # 预算是**纯行为**控制（数请求数），**不判断内容** ——
        # 这是本轮两次否决内容判据（假名长度、汉字占比）后学到的：
        # 字符层面分不出「'ポイズンガード'该译」与「'一の型・焔斬'不译」。
        self._requests_used += 1
        if self._requests_used > self._request_budget:
            # 只在**第一次**用尽时记（避免刷屏），并且要能一眼看出来。
            self.stats["request_budget_exhausted"] = (
                self.stats.get("request_budget_exhausted", 0) + 1
            )
            if self.stats["request_budget_exhausted"] == 1:
                log.error(
                    "★ 请求预算用尽（%d 次 > 上限 %d）：本次运行不再发新请求，"
                    "剩余条目留作未翻译（下次运行会重试）。"
                    "常见原因：模型对某类短专名/技能名不给译文，"
                    "而三层重试相乘导致请求数爆炸。",
                    self._requests_used, self._request_budget,
                )
            raise ProviderError(
                f"请求预算已用尽（{self._requests_used} > {self._request_budget}）"
                f"：本次运行不再发新请求，剩余条目留作未翻译"
            )
        result = self.client.chat(
            self._model,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options=self._options(
                temperature=temperature, n_items=n_items, src_chars=src_chars
            ),
            fmt="json",
            keep_alive=self.cfg.ollama.keep_alive,
            timeout=timeout,
        )
        return result.text

    def _collect_glossary(self, items: list[TranslateItem]) -> str:
        seen: dict[str, str] = {}
        for it in items:
            for g in it.glossary:
                seen.setdefault(g.source, g.target)
        if not seen:
            return ""
        return prompts.build_glossary_block(list(seen.items()))

    def _respect_gate(self) -> None:
        """动态资源占用的闸门：如果 `_pause` 存在就**睡到它消失**。

        ## 为什么是"睡"而不是"退出"

        退出会丢掉这一轮已经完成的进度吗？其实不会（有断点续跑），
        但**退出意味着模型被卸载**，下次要重新加载（实测冷启动十几秒），
        而且 `auto` 的主循环会把当前游戏判成"失败"或"未完成"，
        下次要重新抽取。所以"挂着睡"的代价比"退出重来"小得多。

        ## 为什么每 5 秒看一次文件

        用户可能**手动**创建 `_pause` 来临时让路（有意保留的口子），
        也可能由忙闲监视器创建。文件很小，5 秒一次读的开销可以忽略，
        而 5 秒的响应延迟对人来说就是"立刻"。

        ## 醒来后为什么还要再确认一次

        因为文件可能在"读到不存在"之后、真正开始推理之前被创建。
        再确认一次把窗口缩到一次文件读的时间。做不到零窗口 ——
        真要严格就得让监视器去网关 Ollama，那会引入更多故障点。
        这里选择"最多多跑一批"，一批只有几秒。
        """
        import time as _time

        from . import busy

        root = getattr(self.cfg, "data_root", "") or ""
        if not root or not busy.is_paused(root):
            return
        waited = 0.0
        while busy.is_paused(root):
            _time.sleep(5.0)
            waited += 5.0
            if waited % 60.0 < 5.0:
                # 每分钟报一次，别刷屏
                import logging as _logging

                _logging.getLogger(__name__).info(
                    "机器忙/用户在用，暂停翻译（已让路 %.0f 秒）", waited
                )
        self.stats["paused_s"] = self.stats.get("paused_s", 0.0) + waited

    def _call_batch(self, batch_items: list[TranslateItem], masked: list[str]) -> dict[int, str]:
        """一次批量调用。返回 ``{批次内下标: 译文}``。"""
        user = prompts.build_batch_user_prompt(
            masked,
            kinds=[it.unit.kind for it in batch_items],
            target_lang=self.cfg.translate.target_lang,
            source_lang=self.cfg.translate.source_lang,
            glossary_block=self._collect_glossary(batch_items),
            extra_context=self._context_hint(batch_items),
        )
                # n_items 与 src_chars 共同决定输出上限（截断会让 JSON 解析失败，
        # 反而更慢），所以这里必须把这一批的真实长度告诉它
        raw = self._chat(
            user,
            system=prompts.SYSTEM_PROMPT,
            n_items=len(batch_items),
            src_chars=sum(len(s or "") for s in masked),
        )
        expect = list(range(len(batch_items)))
        # 传 masked 作为 sources：模型有时不按"对象数组"回，而是回
        # "原文作键的对象"（实测 translategemma:4b 就是这样），
        # 解析层靠这个反查编号。
        #
        # ★★ 同时接受**未掩码原文**形态：`mask_newlines=True` 时换行在
        #    `masked` 里是 ``⟦0⟧``，而模型经常回吐字面 ``\n`` 形态
        #    （单条路径实测连续 3 次全败，见 `_call_single` 的注释）。
        #
        # ⚠️ **不能**把两种形态塞进同一个 `sources`：`_build_source_index`
        #    按**位置**分配编号，于是"原文形态"会落在编号 len(masked) 上
        #    ⇒ 反查"原文作键"得到的是那个越界编号，而不是它真正对应的
        #    0..n-1。实测踩到：单条传 `[掩码, 原文]` 得到 `{1: ...}`
        #    而不是 `{0: ...}` ⇒ 取不到译文。
        #
        #    所以做**两次独立解析**：先用模型实际看到的掩码形态，
        #    取不到再用原文形态。两次的 `sources` 都与 `expect` 严格平行。
        mapping, res = parse_translations(
            raw, expect_indices=expect, sources=list(masked)
        )
        if not mapping:
            raw_sources = [it.unit.source or "" for it in batch_items]
            if any(
                s and s != m
                for s, m in zip(raw_sources, masked, strict=False)
            ):
                mapping2, res2 = parse_translations(
                    raw, expect_indices=expect, sources=raw_sources
                )
                if mapping2:
                    mapping, res = mapping2, res2
        if not res.ok:
            raise ProviderError(
                f"JSON 解析失败（{'; '.join(res.notes)[:160]}）：{raw[:180]!r}"
            )
        if res.method not in ("raw", "fence"):
            self.stats["recovered_json"] += 1
            log.debug("批 %d 通过 %s 级恢复解析", len(batch_items), res.method)

        # ★ 越界编号必须删掉，**而且**要把"越界"当成"这条没译"。
        #
        # 实测模型会在批响应里给出**本批不存在的编号**
        # （例如本批 2 条却回了 `{"0": ..., "2": ...}`）。
        # 早先只做 `missing = [i for i in expect if i not in mapping]`，
        # 越界编号根本不参与判断；而它又占着一个键，于是
        # 真正缺的那条不会被发现 —— 补漏逻辑**静默跳过**。
        #
        # 现在把越界编号直接剔除，让对应的真实编号落进 `missing`。
        n_expect = len(batch_items)
        out_of_range = [i for i in mapping if not (0 <= i < n_expect)]
        for i in out_of_range:
            log.debug("批响应里有越界编号 %r（本批只有 %d 条），丢弃", i, n_expect)
            del mapping[i]

        missing = [i for i in expect if i not in mapping]
        if missing:
            # ★ 大比例缺失 = **提示词格式没被遵守**，不是偶发漏条。
            #
            # 真实事故：提示词要求 `[{"i":…,"t":…}]` 数组时，
            # translategemma:4b 把示例里的 `{"i": 0, "t": "…"}` 当成
            # "要产出的那**一个**对象"，输出完就 `done_reason=stop`。
            # 无论批大小（4/8/16/24/40）都只回 1 条，缺 3~39 条。
            #
            # 只靠"逐条降级补漏"兜住的后果是**每批发 1+N 次请求** ——
            # 慢到看起来像卡死（每批 200 秒以上），但流水线一切"正常"。
            # 这种"整批系统性残缺"必须显式记一笔，否则没人会去看
            # `single_fallbacks` 这个数字（它本来就有，只是没人盯）。
            if len(missing) >= max(2, len(expect) // 2):
                self.stats["batch_mostly_missing"] = (
                    self.stats.get("batch_mostly_missing", 0) + 1
                )
                log.warning(
                    "批 %d 条里只回 %d 条（缺 %d 条）—— 提示词格式可能没被遵守；"
                    "本批改用逐条翻译。若这条反复出现，先查 prompts 里的输出格式说明。",
                    len(expect),
                    len(mapping),
                    len(missing),
                )
            log.debug("批次漏了 %d 条：%s", len(missing), missing[:10])
            # 只补漏的那些，**不**重发整批：重发整批既慢又可能再次漏。
            # 逐条调用实测可靠（单条成功率远高于批量），所以用可靠性换速度，
            # 且只对真正缺的条目付出这个代价。
            self.stats["single_fallbacks"] += 1
            for li in missing:
                try:
                    # 这个兜底路径没有 `slots_all`（它是 `_call_batch` 内部的
                    # 返回值），从掩码文本本身抠出记号 —— 段选择只关心
                    # "哪些记号该在"，`⟦n⟧` 就是全部信息。
                    got = self._call_single(
                        batch_items[li],
                        masked[li],
                        slots=ph.remaining_masks(masked[li]),
                    )
                except _RETRYABLE as exc:
                    log.debug("补漏第 %d 条失败：%s", li, exc)
                    continue
                if got:
                    mapping[li] = got
        # 最后再筛一遍类型：单条补漏也可能带回非字符串
        # （实测模型会把某一条回成 `["译文", ""]`）。
        # 非字符串在这里**丢掉**比带下去好 —— 带下去会让
        # `verify_restored` 抛异常，而异常信息完全指不到真正的原因。
        bad = [i for i, v in mapping.items() if not isinstance(v, str)]
        for i in bad:
            log.debug("批内第 %d 条返回了非字符串（%r），丢弃", i, type(mapping[i]).__name__)
            del mapping[i]
        return mapping

    def _call_single(
        self,
        item: TranslateItem,
        masked: str,
        *,
        marker_warning: str = "",
        slots: list[str] | None = None,
    ) -> str:
        """单条翻译，**对"模型回空 JSON"做一次额外重试**。

        ## 为什么要多一层（实测，两次跑结果不同 ⇒ 偶发）

        `translategemma:4b` 偶尔对**完全正常**的单行文本回一个空对象 `{}`：

            原始输出前 200 字符：'{}'）：解析得到空映射：'{}'

        同一输入重复 5 次的实测（`.scratch/_brace_retry.py`）：

        | 输入 | 成功 |
        | --- | --- |
        | `'Wayland:'` | **5/5** |
        | `'Boy:'` | **5/5** |
        | `'.............'`（纯标点、无可译内容） | **0/5** |

        ⇒ 分两类：**真内容**的空回是**偶发**（再问一次就好），
        **无内容**的空回是**必然**（浪费一次请求也无妨，反正它本来译不出）。

        ⚠️ 不要为 `'.............'` 这类做特殊处理：它**确实没有可译内容**，
        判失败是正确行为（只是原因不显眼，见 ROADMAP）。

        重试只加 **1 次**：实测偶发类的两次内成功率已接近 100%，
        加更多次数只是拖慢"必然失败"的那些。
        """
        last: Exception | None = None
        for attempt in range(2):
            try:
                return self._call_single_once(
                    item, masked, marker_warning=marker_warning, slots=slots
                )
            except ProviderError as exc:
                last = exc
                # 只对"空映射"重试：其它 ProviderError 是内容/格式问题，
                # 再问一次不会变好（那正是 `per_item_failed` 记录的教训）。
                if "解析得到空映射" not in str(exc):
                    raise
                if attempt == 0:
                    self.stats["empty_mapping_retry"] = (
                        self.stats.get("empty_mapping_retry", 0) + 1
                    )
                    log.debug("单条回空 JSON，重试一次：%s", str(exc)[:120])
                    continue
                raise
        raise last if last else ProviderError("单条翻译失败")

    def _call_single_once(
        self,
        item: TranslateItem,
        masked: str,
        *,
        marker_warning: str = "",
        slots: list[str] | None = None,
    ) -> str:
        """单条翻译。

        ``marker_warning`` 会**追加到用户提示词末尾**，用于"上一次把
        掩码记号弄丢了，重来一次"的定向重试 —— 见 `_retry_lost_markers`。

        ``slots`` 是这条的占位符表（原样记号）。多段返回时用它挑段：
        占位符丢得最少的段优先 —— 多行条目的段落边界就在占位符处，
        丢了占位符的段没法正确回写（见 :func:`_best_segment`）。
        """
        user = prompts.build_single_user_prompt(
            masked,
            kind=item.unit.kind,
            source_lang=self.cfg.translate.source_lang,
            target_lang=self.cfg.translate.target_lang,
            glossary_block=self._collect_glossary([item]),
            extra_context=self._context_hint([item]),
        )
        if marker_warning:
            user = f"{user}\n\n{marker_warning}"
        raw = self._chat(
            user,
            system=prompts.SYSTEM_PROMPT,
            n_items=1,
            src_chars=len(masked or ""),
            # ★ 单条用**更短**的超时。正常单条 3~10 秒，而 300 秒的余量
            #   一旦遇上重复循环就是纯浪费：实测一条病态条目烧满
            #   300 秒 × 3 次重试 = **15 分钟**，把整个队列拖成 0 条/分钟
            #   （`CrossdresserKiller`，582 条里 6 条超长韩文多行条目）。
            timeout=float(getattr(self.cfg.ollama, "single_request_timeout_s", 90.0)),
        )
        # ★ **必须传 `sources=[masked]`**（2026-10 修的第二个 bug）。
        #
        # `to_translation_map` 支持"以原文为键"的返回形态
        # （``{"vigilance requirements": "警戒要求"}``），但那要靠 `sources`
        # 反查"原文 → 编号"。这里原来**没传** `sources`，于是键是
        # ``"vigilance requirements"``、而查找用的是 ``0`` ⇒ 查不到
        # ⇒ `mapping` 为空 ⇒ 报"解析得到空映射"。
        #
        # 实测现场（真实游戏库 ``auto``，单条重试路径连续 3 次全败）：
        #
        #     批 0（1 条）第 1/2/3 次失败：单条翻译失败（无法解析为 JSON）
        #         ：解析得到空映射：'{"vigilance requirements": "警戒要求"}'
        #
        # ⚠️ 单条请求的 `sources` 就是 **`masked`**（屏蔽换行后的原文）——
        # 模型看到的就是它，所以它回吐的键也应该是它。
        #
        # ★★ 但实测**还要再试一次**（2026-10 第二次修这里）：
        #    `mask_newlines=True` 时 `masked` 里换行是记号 ``⟦0⟧``，
        #    而模型经常回吐**原文形态**（字面 ``\n``）而不是记号形态。
        #    真实日志（``auto5.err``，单条路径连续 3 次全败）：
        #
        #        第 1 次：{"t": {"火山を主な生息地とする竜種。\n首の長さで…": "火山是主要栖息地…"}}
        #        第 3 次：{"t": {"火山是主要栖息地…": ""}}
        #
        #    键里是 ``\n``、而 `sources=[masked]` 里是 ``⟦0⟧`` ⇒
        #    `to_translation_map` 反查不到 ⇒ `mapping` 为空 ⇒ 报
        #    "解析得到空映射"。**而译文明明就在值里。**
        #
        #    实测判据（`.scratch/_reject_probe.py`）：
        #
        #        parse_translations(原文作键, sources=[掩码形态]) → keys=None ❌
        #        parse_translations(原文作键, sources=[原文形态]) → keys=[0]  ✅ 取出 58 字符
        #
        # ⚠️ **不能**把两个形态塞进同一个 `sources` 列表：那会让
        #    "原文形态" 落在下标 1 ⇒ 反查回来是**编号 1**，
        #    而单条只有编号 0 ⇒ 取不到。实测踩到：
        #
        #        sources=[掩码, 原文] → {1: '火山是主要栖息地…'}   ← 编号错了
        #
        #    正确做法是**两次独立解析**：先用模型实际看到的形态，
        #    取不到再用原文形态。两次的 `sources` 都只有一个元素
        #    ⇒ 反查结果必然是编号 0。
        mapping, res = parse_translations(
            raw, expect_indices=[0], sources=[masked]
        )
        if not mapping and item.unit.source and item.unit.source != masked:
            mapping2, res2 = parse_translations(
                raw, expect_indices=[0], sources=[item.unit.source]
            )
            if mapping2:
                mapping, res = mapping2, res2
        # ⚠️ 这里的处理**必须**认得出"模型按批格式回答单条请求"这一形态。
        #
        # ## 实测形态（真实游戏，ITEM_DESC）
        #
        #     源文  'A girl who grew up in the Kingdom of Bohelos. Not very athletic.\n
        #             Weapon Type: \C[6]Sword\C[0]'
        #     输出  {"t": [{"i": 0, "t": "来自博赫洛斯王国的女孩。并不擅长运动。"},
        #                  {"i": 1, "t": "武器类型：剑"}]}
        #
        # 模型把**原文里的换行**当成了"两条独立文本"，于是按**批**的格式
        # 编号回答（`{"i": n, "t": ...}`），而 `parse_translations` 会把它
        # 摊成 `{0: ..., 1: ...}`。
        #
        # 这时候：
        #   * `mapping[0]` 是**合法字符串**（类型检查抓不到）；
        #   * 若只取 `mapping[0]` ⇒ **静默丢掉第二行**；
        #   * 若像早先那样看到 list 就带下去 ⇒ 下游抛 `AttributeError`，
        #     被 `except ProviderError` 漏掉 ⇒ **重试静默失效**。
        #
        # ## 正确处理
        #
        # **挑一个最好的片段**，而不是拼起来 —— 真实数据实测
        # 10/10 都是多段返回，而其中 2/3 的形态拼接会造成复读。
        # 判定依据与实测样本见 :func:`_best_segment` 的说明。
        #
        # ⚠️ **必须传 `slots`**：占位符丢得最少的段优先。多行条目的
        #   段落边界就在占位符处，丢了占位符的段没法正确回写。
        #
        # ⚠️ 规则 1（占位符丢得最少）**优先于**"取段 0"：段 0 有时
        #   不完整（`{"0": "恢复 5 点生命值。", "1": "生命回复：…"}`），
        #   靠它筛。筛完仍并列才回落到段 0。
        #
        # ⚠️ 段选择**修不了**"换行占位符丢失"：实测 76 条里 5 条
        #   （7%）模型把 `⟦n⟧` 换行记号吞了，此时选哪一段都会丢行，
        #   guard 会判死 → **保留原文**。这是安全侧，但不是完整译文。
        #   想再进一步需要"退回不屏蔽换行重译"的路径，见 ROADMAP。
        # ⚠️ 键的类型：`parse_translations` 给的是 **`int`** 键
        #   （`{0: …, 1: …}`）—— 实测确认过。写成 `mapping.get("0")`
        #   会永远取到 `None`，把整个多段分支废掉。别改键类型。
        #
        # ⚠️ 这个分支**进不来**的两种真实情形（都会掉到下面的
        #   "结果不是字符串"）：
        #
        #   1. **模型回吐提示词**。实测形态（ElfLifia 批 28）：
        #      `{"0": "获得 1 个『额外抽牌』。\n『额外抽牌』：…」} ⟦0⟧ 保持
        #      原文格式和数值。 调整句子结构…确保输出的"}`
        #      —— 它把提示词里的规则一条条接在译文后面吐出来，
        #      中间还夹着**未转义的换行**，于是 `parse_json_loose`
        #      直接失败、`mapping` 为空。这不是"选段"能救的，
        #      要靠护栏（hint_echo 已经在拦，见 `guards.py`）。
        #   2. 模型只给纯文本（没有 JSON），由下面 `stripped` 那条接住。
        keys = sorted(mapping)
        # ---- ★ 把"从 1 开始编号"的响应折成从 0 开始 ----
        #
        # ## 实测（Round 7，读到 stderr 才发现的）
        #
        # `.scratch/_e2e_auto3.err` 里同一批连报三次同一个症状：
        #
        #     批 0（1 条）第 1 次失败：单条翻译失败（无法解析为 JSON）：
        #         解析结果不是字符串：'{"1": "在 1 个回合内，自动保护生命值较低的队友。",
        #                              "2": "在 1 回合内，自动保护生命值较少的角色。"}'
        #     批 0（1 条）第 2 次失败：…（同样的东西，只是译文措辞不同）
        #     批 0（1 条）第 3 次失败：…
        #
        # **1 条输入、3 次重试、3 次全废 ⇒ 这条内容丢失。**
        #
        # 关键在于模型给的编号是 **`1, 2`**，不是 `0, 1`。
        # 而下面这行判定要求**必须有键 0**：
        #
        #     joins = isinstance(mapping.get(0), str)      # {1:…, 2:…} ⇒ False
        #
        # 于是多段分支**整段被跳过**，`keys` 非空 ⇒ 报"解析结果不是字符串"。
        # **这不是模型给了坏数据，是我们的形状假设太窄**：
        # 响应形状本身完全合理（连续编号的字符串片段），只是基准是 1。
        #
        # ## 为什么"平移"是安全的（而不是放宽判据）
        #
        # 平移之后交给**同一段**既有逻辑处理，等价于"模型当初回的就是 0 基准"：
        # * 仍然走 `_best_segment` **选段**，绝不拼接（拼接会让两行粘一起）；
        # * 连续性仍然被检查 —— 有洞就**原样不动**掉到下面的报错，
        #   所以"编号不连续"这条安全性**没有被放宽**；
        # * 段选择优先看占位符完整度，因此多行条目的段落边界照样守住。
        #
        # 只认**从 1 开始且连续**这一种形态：`min(keys) != 1` 时不动
        # （比如键是 `[2,3]`，那更像真的编号错乱，宁可报错也不要猜）。
        if keys and keys[0] == 1 and all(isinstance(mapping[k], str) for k in keys):
            contiguous = keys == list(range(1, len(keys) + 1))
            if contiguous:
                mapping = {k - 1: v for k, v in mapping.items()}
                keys = sorted(mapping)
                log.debug(
                    "单条请求的响应从 1 开始编号，已平移为 0 基准：%d 段",
                    len(keys),
                )
        joins = isinstance(mapping.get(0), str)
        if joins and len(keys) > 1:
            if keys != list(range(len(keys))):
                raise ProviderError(
                    f"单条翻译返回的编号不连续（{keys}）：段落边界不明，拒绝拼接。"
                    f"原文：{masked[:80]!r}"
                )
            if not all(isinstance(mapping[k], str) for k in keys):
                raise ProviderError(
                    f"单条翻译返回了非字符串片段：{ {k: type(mapping[k]).__name__ for k in keys} }"
                )
            # ---- ★ 段数 == 源文行数 ⇒ 按行序拼回，而不是只选一段 ----
            #
            # ## 实测（#36 / #37）
            #
            # `mask_newlines=True` 把多行源文**压成一行**（换行 → 占位符），
            # 但模型**仍按语义行回多段**。此时只选一段会**丢掉其余行**，
            # 且是**结构性**的（同一源文重复 4 次，比值极差中位只有 0.02）：
            #
            #     源文 89 字符 / 3 行
            #     整条 ⇒ '玛希罗：'        ← 只剩角色名，比值 0.04
            #     拆行 ⇒ 3/3 行全有，比值 0.51
            #
            # `status` 还是 `translated`、**没有任何 warning** ——
            # "非空"不等于"完整"，这是最难发现的一类损失。
            #
            # ## 为什么这里可以拼（而 `_best_segment` 说拼接是错的）
            #
            # `_best_segment` 反对拼接，针对的是**段数 ≠ 行数**的三种形态
            # （同一内容的替代译文 / 段 0 不完整 / 各段完全相同）——
            # 那些情况拼接确实会复读。
            #
            # 而**段数正好等于源文行数**时，各段对应**不同的**源行，
            # 拼回**不可能**复读。判据是**结构性**的（数占位符），
            # 不依赖任何阈值 —— 所以不会重演 docstring 里那次
            # "覆盖率区间重叠、任何阈值都误杀一边"的失败。
            #
            # 段数 ≠ 行数时**保持原行为**，宁可少救也不要赌。
            segs = [mapping[k] for k in keys]
            n_src_lines = _source_line_count(slots)
            if (
                len(segs) > 1
                and n_src_lines > 1
                and len(segs) == n_src_lines
                and all(s.strip() for s in segs)
            ):
                joined = "\n".join(s.strip() for s in segs)
                log.debug(
                    "单条请求：段数(%d) == 源文行数(%d)，按行序拼回（避免只留一段）",
                    len(segs),
                    n_src_lines,
                )
                return joined
            picked = _best_segment(segs, slots)
            if picked is not None:
                log.debug(
                    "单条请求收到批格式 %d 段响应，按占位符完整度选段：%r",
                    len(keys),
                    {k: v[:30] for k, v in mapping.items()},
                )
                return picked
        parts = [
            v for v in mapping.values() if isinstance(v, str) and v.strip()
        ]
        if len(parts) == 1 and all(isinstance(v, str) for v in mapping.values()):
            return parts[0]
        if res.ok and isinstance(res.value, str):
            return res.value
        # 有些模型即使要求 JSON 也只给纯文本，当译文用
        stripped = raw.strip()
        if stripped and not stripped.startswith(("[", "{")):
            return stripped
        # ---- 报错要说**真正的**原因 ----
        #
        # 早先这里只有一句"结果不是字符串"，而它描述的其实是症状。
        # 实测踩过的真实原因（ElfLifia 批 28）是**模型回吐提示词**：
        # 它把规则一条条接在译文后面吐出来，中间还夹着裸换行，
        # 于是 `parse_json_loose` 整个失败（`res.ok=False`）、
        # `mapping` 为空。日志上看到的是"结果不是字符串"，
        # 让人以为是选段逻辑坏了 —— 查错方向被带偏。
        # 现在把解析层的诊断（`res.notes` / `res.method`）带出来。
        why = "；".join(res.notes) if res.notes else "无法解析为 JSON"
        raise ProviderError(
            f"单条翻译失败（{why}）："
            f"{'解析得到空映射' if not mapping else '解析结果不是字符串'}："
            f"{raw[:200]!r}"
        )

    @staticmethod
    def _context_hint(items: list[TranslateItem]) -> str:
        lines: list[str] = []
        for it in items[:6]:
            if it.context_lines:
                lines.extend(it.context_lines[:3])
        if not lines:
            return ""
        return " / ".join(dict.fromkeys(lines))[:400]

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------


    def begin_run(self, total_items: int) -> None:
        r"""开始翻**一个游戏**：按该游戏的全部条数设定请求预算。

        ## 为什么必须有这个方法（实测事故）

        我第一版把预算写在 `translate_batch` 里按**每次调用**的条数设，
        而那个方法是被**按批**调用的（每批 8~25 条）⇒ 预算退化成
        "单批允许几次请求"⇒ 正常批次重试几次就撞上限 ⇒ **整批被拒**。

        实测 `Battle Demon Kirsten`（31,263 条）报"25 次 > 上限 24"。

        ⇒ 由调用方（`stage_translate`）在开始游戏前调用一次，
        用**全部条数**设预算；`translate_batch` 只负责递增。

        ## ★★ 预算**只增不减**（第二个实测事故）

        贴图管线的回调拿到的 items 是**一张图里的文字块**
        （常常 1~4 条），它也会走这里。若允许把预算**缩小**，
        一次 `begin_run(1)` 就会把上限压到 `max(1+8, 3)=9`
        并**不再恢复** ⇒ 实测连续拒绝几百批：

        ```
        批 0（1 条）第 1 次失败：请求预算已用尽（10 > 9）
        …（100 > 9）…（300 > 9）…（600 > 9）…
        ```

        ⇒ 只在**新预算更大**时才更新；缩小的请求直接忽略。

        ⚠️ 计的是**整个游戏**的请求数，所以它是"病态游戏的止血阀"，
        不是"单批保护"。正常游戏实际约 1 次请求/条，
        远低于 `factor`（默认 3.0）。
        """
        o = self.cfg.ollama
        factor = float(getattr(o, "request_budget_factor", 3.0))
        floor = int(getattr(o, "request_budget_floor", 8))
        total_items = max(0, int(total_items))
        new_budget = max(total_items + floor, int(total_items * factor))
        if new_budget <= self._request_budget:
            # ★★ **只增不减** —— 见 docstring 的实测事故：
            #   贴图管线会用 1~4 条的小 items 调这里，
            #   若允许缩小就会把预算压到 `max(1+8, 3)=9` 并**不再恢复**
            #   ⇒ 连续拒绝几百批（实测日志里 10>9 … 600>9）。
            return
        self._requests_used = 0
        self._request_budget = new_budget
        log.info(
            "请求预算（本作用域）：%d 条 × %.1f = **%d 次请求**",
            total_items, factor, self._request_budget,
        )

    def translate_batch(
        self, items: list[TranslateItem], target_lang: str
    ) -> list[TranslationEntry]:
        if not items:
            return []

        ok, why = self.available()
        if not ok:
            raise ProviderError(why)

        self.stats["items"] += len(items)

        # ★★ 设定**请求预算**（见 `OllamaConfig.request_budget_factor`）
        #
        # 三层重试相乘让一条病态条目最多发 6 次请求：
        #
        #     translate_batch   for attempt in range(3)   ← 批重试 3 次
        #       ├─ _recover_by_subbatch → _call_single    ← 每缺条 2 次
        #       └─ 补空 → _call_single                    ← 又 2 次
        #     _call_single      for attempt in range(2)   ← 单条再 2 次
        #
        # 实测 `Battle Demon Kirsten`：960 条失败 × 约 6 次 ≈ **5,760 次**
        # 无效请求，把吞吐拖到 **0~2 条/分钟**。独立重试只需 960 次 ⇒ 可省 83%。
        #
        # 预算按条目数给（每条允许 `factor` 次），所以**正常游戏永远碰不到**
        # —— 正常几乎无失败，实际约 1 次/条；只有病态游戏才会撞上。
        #
        # ⚠️ 这是**纯行为**控制（数请求数），**不判断内容**。这是本轮
        #    **两次否决内容判据**后学到的：字符层面分不出
        #    「`'ポイズンガード'`(7 字) 该译」与「`'一の型・焔斬'`(6 字) 不译」。
        # ★★ 预算的作用域是**整个游戏**，不是**这一批**。
        #
        # ## 为什么（实测事故）
        #
        # `stage_translate` 是**按批**调用 `translate_batch` 的：
        #
        #     for idxs in batch_indices:
        #         provider.translate_batch(items, ...)   # ← 每批一次
        #
        # 而我第一版在这里**每批重置**计数器与上限：
        #
        #     self._requests_used = 0
        #     self._request_budget = max(len(items) + _floor, ...)
        #
        # ⇒ 预算退化成"**单批**允许几次请求"。
        #   一个 16 条的批只要重试几次（`_call_batch` 3 次 + 逐条 + 补空）
        #   就会撞到 24 ⇒ **整批被拒** ⇒ 条目被判 `FAILED`。
        #   实测 `Battle Demon Kirsten`（31,263 条，预算本该 93,789）
        #   报的是"25 次 > 上限 24"。
        #
        # ## 修法
        #
        # * **`begin_run(total_items)`**（新增）：`stage_translate` 在开始
        #   翻一个游戏前调用一次，用**该游戏全部条数**设预算；
        # * **`translate_batch` 不再重置** —— 只按批递增计数器；
        # * 没调用 `begin_run` 时（贴图管线的小批量回调）退回
        #   "按本次调用条数"设预算，保持原有的安全网。
        _factor = float(getattr(self.cfg.ollama, "request_budget_factor", 3.0))
        # 下限余量**可配置**：见 `request_budget_floor` 的 docstring。
        _floor = int(getattr(self.cfg.ollama, "request_budget_floor", 8))
        if self._request_budget <= 0:
            # 没有 `begin_run` ⇒ 按本次调用的条数设（安全网）
            self._requests_used = 0
            self._request_budget = max(len(items) + _floor, int(len(items) * _factor))

        # ---- 0. 同批内去重 ----
        # 真实数据里冗余极高：某个角色的**说话人名**有 4334 条待翻，
        # 而不同原文只有 **33** 种（99.2% 是重复的 `'<right>  Ulula  </right>'`
        # 这类）；整体待办里 26.3% 是重复原文。
        # 不去重就是"同一句话问模型几千次"，纯浪费。
        # 去重后只在 `unique` 上跑模型，再把结果按原文映射回去。
        #
        # 只在**同一批**内去重（不是全局缓存）：批内重复已经覆盖了
        # 这类高度重复的场景，而且不需要维护跨批状态与失效逻辑。
        uniq_items, first_of, owners = self._dedupe(items)
        if len(uniq_items) < len(items):
            self.stats["deduped"] = self.stats.get("deduped", 0) + (
                len(items) - len(uniq_items)
            )

        out: list[TranslationEntry] = [self._blank(it) for it in items]
        batches = self._make_batches(uniq_items)
        ratio = self.cfg.translate.max_chars_ratio
        use_mask = self.cfg.translate.mask_placeholders
        # 换行也屏蔽（多行条目的完整性问题，见 placeholders.mask 的 newlines 参数）
        mask_nl = self.cfg.translate.mask_newlines

        def _one_batch(
            bi: int,
            batch: list[int],
        ) -> tuple[dict[int, str], str | None, list[str], list[list[str]]]:
            r"""发**一个**批次的请求：屏蔽 + 重试 + 固定小批补救 + 补空。
            返回 ``(局部下标→屏蔽态译文, 错误串, masked_all, slots_all)``。
            `masked_all` / `slots_all` 一并带出，因为外层的校验段要用它们
            还原占位符。

            ## 为什么是**嵌套函数**而不是方法

            我前两次把它抽成**方法**，都因为「漏了外层变量」而搞坏
            （35 个 `F821 Undefined name`）。嵌套函数用**闭包**
            自动捕获 `uniq_items` / `use_mask` / `mask_nl` / `self`
            / `log` / `time` 等一切外层名字 —— 从根上消除那一类错误。

            ## 两个 `break` 为什么不用改

            它们跳的是**内层** `for attempt in range(3)`，不是外层循环。
            我一开始误以为要改成 `return`，那是错的 —— 会提前跳过
            「补空」和「还原」两段。实测确认它们都在内层。

            ## 线程安全

            本函数**不碰**任何跨批状态（`out` / `first_of` 留在外层），
            只读 `self.cfg`。`httpx.Client` 官方支持多线程共享。
            `self.stats` 的「读-改-写」在多线程下**可能丢计数** ——
            可接受（统计不是判据），**不影响任何译文**。
            """
            # ★ 动态资源占用：每批之前问一次"现在该让路吗"。
            #
            # 为什么放在**批次边界**而不是"随时打断"：
            # 一批就是一次 Ollama 往返，打断它会浪费已经算出来的 token；
            # 而批次之间的延迟最多几秒，用户感觉不到。
            self._respect_gate()
            self.stats["batches"] += 1
            batch_items = [uniq_items[i] for i in batch]
            sources = [it.unit.source for it in batch_items]

            # ---- 1. 屏蔽占位符 ----
            if use_mask:
                masked_all, slots_all = ph.mask_batch(sources, newlines=mask_nl)
            else:
                masked_all = list(sources)
                slots_all = [[] for _ in sources]

            # ---- 2. 调用（带重试与逐条降级） ----
            raw_by_index: dict[int, str] = {}
            err: str | None = None
            #: 本批里**已经逐条试过且失败**的下标。
            #: 跨重试轮次保留 —— 否则每次退化成逐条时都会把这些条目
            #: 再问一遍（实测浪费见下面 `if li in per_item_failed` 处的注释）。
            per_item_failed: set[int] = set()
            for attempt in range(3):
                try:
                    if len(batch_items) == 1:
                        got = self._call_single(
                            batch_items[0], masked_all[0], slots=slots_all[0]
                        )
                        raw_by_index = {0: got} if got else {}
                    else:
                        raw_by_index = self._call_batch(batch_items, masked_all)
                    err = None
                    break
                except _RETRYABLE as exc:
                    # `_RETRYABLE` 里额外含 `UnicodeEncodeError`/`ValueError`：
                    # 它们继承自 ValueError，不属于 ProviderError/OllamaError，
                    # 早先的 `except (ProviderError, OllamaError, ModelMissing,
                    # OllamaNotRunning)` **一个都不匹配**，于是异常穿过整个
                    # 重试层逃到 stage_translate，让一轮跑了 27 分钟的翻译
                    # 全部作废（实测）。
                    err = str(exc)
                    self.stats["retries"] += 1
                    log.warning("批 %d（%d 条）第 %d 次失败：%s", bi, len(batch_items), attempt + 1, err)
                    time.sleep(0.4 * (attempt + 1))

                    # 批量反复失败 → 退化为**递归二分重批**（不是逐条！）
                    if attempt >= 1 and len(batch_items) > 1:
                        self.stats["single_fallbacks"] += 1
                        singles, single_err = self._recover_by_subbatch(
                            batch_items, masked_all, slots_all, per_item_failed
                        )
                        if singles:
                            raw_by_index, err = singles, single_err
                            break

            # ---- 2.5 补空 ----
            # _call_batch 只重试"整批失败"和"漏掉的条目"，但**批量调用成功、
            # 个别条目却返回空串**时它不重试 —— 那种情况在它看来批处理是好的。
            # 实测：`MP: {mp}`、`Gold: {gold}` 这类"短词 + 占位符"的结构
            # 会在批量里被返回成空串（3 次运行里 3 次复现，与术语表无关）。
            #
            # 不补的后果是**静默漏译**：条目以 FAILED/空译文写回游戏，
            # 玩家看到的是一个没被翻译的 UI 元素，而质检只会说"有条目未翻译"。
            # 逐条重试对这类短串几乎总能救回来。
            empty = [
                li for li, gi in enumerate(batch)
                if not raw_by_index.get(li, "").strip()
            ]
            if empty and len(batch_items) > 1:
                self.stats["empty_retry"] = self.stats.get("empty_retry", 0) + 1
                for li in empty:
                    if raw_by_index.get(li, "").strip():
                        continue
                    # ★ 刚刚在逐条降级里试过并失败的条目，这里不再补。
                    #
                    # 为什么要跳过：走完逐条降级后 `raw_by_index` 只装了
                    # "救回来的"那些，所以**失败过的条目也在这个 `empty` 里**。
                    # 不跳过的话，它们会立刻被单独再问一次 —— 而它们刚刚
                    # 就是单独问过才失败的（触发源是内容，不是偶发抖动）。
                    # 实测这类重复请求在两次真实端到端跑里量到 400+ 次。
                    #
                    # 保留对**其余**空条目的补空：那些是"批量调用成功、
                    # 但这条返回空串"的情况（`MP: {mp}` 这类短词 + 占位符），
                    # 它们**没有**被逐条试过，补空对它们确实有效
                    # （3 次运行 3 次复现，见上方注释）。
                    if li in per_item_failed:
                        continue
                    try:
                        got = self._call_single(
                            batch_items[li], masked_all[li], slots=slots_all[li]
                        )
                    except _RETRYABLE as exc:
                        log.debug("补空第 %d 条失败：%s", li, exc)
                        per_item_failed.add(li)
                        continue
                    if got:
                        raw_by_index[li] = got
                        self.stats["empty_recovered"] = (
                            self.stats.get("empty_recovered", 0) + 1
                        )
                    else:
                        per_item_failed.add(li)

            # ---- 2.7 【已删除】"疑似假成功"检测（#39） ----
            #
            # 我在这里加过一个检测：源文多行、译文塌成一行且比值过低
            # ⇒ 判为"假成功"，标失败并交给 3.7 逐行重译。
            #
            # **实测证明它没有判别力，所以删掉了**（不是"调阈值"，是删）：
            #
            # | 类别 | 译文/原文比值 |
            # | --- | --- |
            # | 人工确认的垃圾（只回说话人标签） | 0.28 / 0.17 / **0.04** / 0.43 / 0.38 |
            # | 合理合并译文（中文把三行合成一句） | **0.17** / 0.26 / 0.29 / 0.29 / 0.31 / 0.37 |
            #
            # 垃圾**最大** 0.43 > 合理译文**最小** 0.17 ⇒ **完全重叠**，
            # 任何阈值都必然误杀一边。我先后取过三个阈值（`len>=40`、
            # 拉丁字母>=25、拉丁字母>=60），**三次都在误伤**：
            #
            # * `len>=40` 把 `'Lifia: I have something to say\n「I have to go.」'`
            #   （41 字符，其中 `「」` 只贡献长度不贡献信息）判成"足够长"；
            # * 拉丁字母>=25 同样命中它（26 个字母）；
            # * 每次误触发都**抢走 3.7 的工作** —— 而 3.7 是**量过**的
            #   （30 条真实多行条目救回 7 条、原通过的 17 条一条未改）。
            #
            # 这是本项目**第四次**"看起来合理 ≠ 量过"。教训与
            # `_best_segment` docstring 里那次一模一样：**区间重叠的
            # 判据不能用阈值救，只能换成结构性判据**。
            #
            # ## 真正有判别力的那条判据在哪
            #
            # "逐行译文是不是同一个短片段重复" —— 见
            # :func:`_is_degenerate_repetition`，它用在 **3.7** 那里。
            # 它是**结构性**的（不受语种/紧凑程度影响），
            # 实测两个方向都成立：
            #   * 合理合并译文 ⇒ 不触发；
            #   * `'店员 1：' × N` ⇒ 拦下。
            #
            # ## 另外三条实测结论（避免以后重复投入）
            #
            # 1. 真实工作区里 `translated` 且"源文多行 + 译文单行"的**只有
            #    6 条**，逐条看过**全是合理的合并译文**，不含垃圾。
            # 2. 真正只回标签的那条（`'店员 1\n：'`，比 0.04）**不是**
            #    靠这条判据抓的 —— 它带 `sentence_drop:6->1`，
            #    而 `sentence_drop` 是**故意只警告、不判死**的：
            #    实测开火率 10.7%，人工抽查**大部分是假阳性**
            #    （语气词被合并），且"真丢内容"与"风格性合并"的
            #    len_ratio 区间**重叠**，分不开（见 `guards.py` 的说明）。
            #    ⇒ 那条垃圾的**唯一现有信号就是那个警告**，
            #      本轮**没有**修好它，作为已知局限记在 ROADMAP。
            # 3. 所以"假成功"在这个语料上**不是**可判定的主要缺口；
            #    48.6% 的 `failed` 才是（其中大量是 500
            #    `token repeat limit reached`）。


            return raw_by_index, err, masked_all, slots_all

        # ---- 2a. 并发派发批次（**窗口式**，带背压） ---- ★ 吞吐关键
        #
        # 原实现把「发请求」和「校验」写在同一个 `for` 里 ⇒ 每批都要
        # 等 Ollama 往返结束才发下一批 ⇒ `OLLAMA_NUM_PARALLEL` 的
        # 多个槽位**永远用不满**。
        #
        # 实测（`.scratch/_probe_concurrency.py`，25 条批）：
        #
        # | 并发 | 吞吐(条/分) |
        # | --- | --- |
        # | 1 | 289 |
        # | 4 | **655** |
        # | 5 | 416（超出槽位，排队） |
        #
        # ## ★★ 为什么必须是"窗口式"而不是"一次性 submit 全部"
        #
        # 我第一版写的是 `ThreadPoolExecutor` + **一次性把所有批次
        # submit 进去**（`{_bi: pool.submit(...) for _bi, _bt in enumerate(batches)}`）。
        # 那等于**同时**向 Ollama 发几百个请求（一个 49,733 条的游戏
        # 会切出上千个批）。后果实测：
        #
        # ```
        # [err] Ollama 请求过慢：POST /api/chat 用了 300.0s（失败）超时
        # [err] 批 0（1 条）第 1 次失败：请求 Ollama 超时
        # ```
        #
        # **每个请求的 300 秒超时从"提交时刻"开始算**，而 Ollama 只有
        # `-np N` 个槽 ⇒ 排在队尾的请求**必然超时**（还没轮到它就到期了）。
        # 表现出来就是"连 1 条的批都超时"、worker 攒到 **102 个线程**、
        # 而 `llama-server` 的 CPU 很低（它在慢慢处理队首）。
        #
        # ⇒ 需要的是**背压**：最多 `_workers` 个在飞，回来一个补一个。
        #   这样任何请求的等待时间都被限制在 O(_workers) 个批次的时长内。
        #
        # ⚠️ 教训：`ThreadPoolExecutor.map` / 一次性 submit 都会把
        #    **全部**任务立即开始等待 —— 对"下游有并发上限"的场景，
        #    必须自己控制提交节奏。
        _workers = max(1, int(getattr(self.cfg.ollama, "concurrency", 1) or 1))
        _results: dict[int, tuple[dict[int, str], str | None, list[str], list[list[str]]]] = {}

        # ---- ★ 派发层的连续失败熔断 ----
        #
        # 条目层的熔断（下面 2b）要等**全部批次跑完**才能生效，所以它挡不住
        # `_one_batch` 内部的重试放大（批 3 次 × 逐条 2 次 × 补空 2 次）。
        # 实测：60 条全失败时条目层虽然熔断了，但**已经发出 159 次请求**。
        #
        # ⇒ 这里在**每次派发前**再查一次：一旦某批的 `err` 显示"整批失败"，
        #   就把连续失败计数按该批条数推进，超阈值立刻停止派发后续批次。
        #
        # ⚠️ 这是**近似**计数（以批为单位而不是以条目为单位），
        #    保守方向是**更容易触发**（每批按满条数计），这对"止血"是安全的。
        _cb_limit = int(getattr(self.cfg.ollama, "fail_circuit_breaker", 40))
        _dispatch_fail = 0
        _dispatch_tripped = False

        def _note_batch_result(_bi: int, _res: tuple) -> None:
            """按批结果推进派发层计数；整批失败则累加，有产出则清零。"""
            nonlocal _dispatch_fail, _dispatch_tripped
            _raw = _res[0] if _res else {}
            if _raw:
                _dispatch_fail = 0
                return
            _batch_len = len(batches[_bi]) if _bi < len(batches) else 1
            _dispatch_fail += max(1, _batch_len)
            log.debug(
                "派发层熔断计数：批次 %d 无产出（%d 条），连续累计 %d / %d",
                _bi, _batch_len, _dispatch_fail, _cb_limit,
            )
            if not _dispatch_tripped and _dispatch_fail >= _cb_limit:
                _dispatch_tripped = True
                log.error(
                    "★ 熔断（派发层）：连续约 %d 条无任何译文，停止派发后续批次"
                    "（剩余条目留作未翻译，下次运行会重试）。"
                    "常见原因：模型对某类短专名/技能名不给译文。",
                    _dispatch_fail,
                )
                self.stats["dispatch_circuit_tripped"] = 1

        if _workers > 1 and len(batches) > 1:
            log.debug(
                "窗口式并发派发 %d 个批次（concurrency=%d）", len(batches), _workers
            )
            from collections import deque
            from concurrent.futures import ThreadPoolExecutor, wait

            def _take(_bi: int, _fut: object) -> None:
                try:
                    _results[_bi] = _fut.result()  # type: ignore[attr-defined]
                except _RETRYABLE as _exc:
                    _results[_bi] = ({}, str(_exc), [], [])
                except Exception as _exc:  # noqa: BLE001
                    # 单批的意外异常不该毁掉整轮翻译
                    log.warning("批次 %d 派发异常：%s", _bi, _exc)
                    _results[_bi] = ({}, str(_exc), [], [])
                _note_batch_result(_bi, _results[_bi])

            _queue = deque(enumerate(batches))
            with ThreadPoolExecutor(max_workers=_workers) as _pool:
                # 先填满窗口
                _inflight: dict[object, int] = {}
                while _queue and len(_inflight) < _workers and not _dispatch_tripped:
                    _bi, _bt = _queue.popleft()
                    _inflight[_pool.submit(_one_batch, _bi, _bt)] = _bi
                # 回来一个补一个
                while _inflight:
                    _done, _ = wait(list(_inflight), return_when="FIRST_COMPLETED")
                    for _fut in _done:
                        _bi = _inflight.pop(_fut)
                        _take(_bi, _fut)
                        if _queue and not _dispatch_tripped:
                            _nbi, _nbt = _queue.popleft()
                            _inflight[_pool.submit(_one_batch, _nbi, _nbt)] = _nbi
        else:
            for _bi, _bt in enumerate(batches):
                if _dispatch_tripped:
                    break
                _results[_bi] = _one_batch(_bi, _bt)
                _note_batch_result(_bi, _results[_bi])

        # ---- 2b. 顺序跑校验（**内容一字不改**，数据改从 _results 取） ----
        #
        # ★★ 连续失败熔断器（治 `Battle Demon Kirsten` 卡死 44 分钟的根）。
        #
        # 实测：该游戏 1,016 条失败条目在**无限重试**，全库吞吐掉到
        # **2 条/分钟**（预期 150）。失败内容是短日文技能名
        # （`'一の型・焔斬'`/`'終の型・煌々一閃'`/`'チンゲリオン'`），
        # 模型确实不给译文（回显原文）。
        #
        # 重试放大链（已测绘）：
        #
        #     translate_batch   for attempt in range(3)     ← 批重试 3 次
        #       ├─ _recover_by_subbatch → _call_single      ← 每缺条 2 次
        #       └─ 补空 → _call_single                      ← 又 2 次
        #     _call_single      for attempt in range(2)     ← 单条再 2 次
        #
        # ⇒ 一条病态条目最多 **6 次请求**。
        #
        # 为什么 `fail_streak` 救不了：它**只在整轮结束时累加**，
        # 而这一轮永远结束不了（一直在重试）⇒ streak 停在 0/1。
        #
        # 为什么用"行为"而不是"内容"判据：试过给假名回显守卫加长度
        # 阈值，**被测试否决** —— `ポイズンガード`(7 字，已验证能译)
        # 与 `'一の型・焔斬'`(6 字，不译) **长度分不开**。
        #
        # 熔断**不丢任何已成功的译文**，只是不再为病态条目烧算力；
        # 剩余条目留作未翻译，下次运行会重试（那时它们已有 fail_streak）。
        _cb_limit = int(getattr(self.cfg.ollama, "fail_circuit_breaker", 40))
        _consec_fail = 0
        _cb_tripped = False
        for bi, batch in enumerate(batches):
            if _cb_tripped:
                # 已熔断：把这一批及**后面所有**条目留作未翻译。
                #
                # 为什么不继续跑：熔断的意义就是"别再为病态内容烧算力"。
                # 继续跑只会让症状（吞吐掉到 2 条/分钟）延续下去。
                #
                # 这些条目会被写成 FAILED，`stages.py` 的跨轮次
                # `fail_streak` 会在本轮结束时 +1 —— 所以下次运行
                # 它们就有机会被跳过，或（若内容其实可翻）被重试成功。
                for _local_i, global_i in enumerate(batch):
                    entry = out[first_of[global_i]]
                    if entry.status != EntryStatus.TRANSLATED:
                        entry.status = EntryStatus.FAILED
                        entry.target = ""
                        entry.warnings = [
                            "circuit_breaker: 连续失败过多，本次运行已停止尝试"
                        ]
                continue
            batch_items = [uniq_items[i] for i in batch]
            raw_by_index, err, masked_all, slots_all = _results[bi]
            for local_i, global_i in enumerate(batch):
                item = uniq_items[global_i]
                entry = out[first_of[global_i]]
                raw_masked = raw_by_index.get(local_i, "")
                # 出站净化：模型偶尔把 `\uddd1` 这类**孤立代理项**当字面量
                # 吐出来，`json.loads` 会忠实还原成一个非法字符。留着它
                # 会污染条目（并且被当作下一批的输入反复触发），
                # 写入 JSON 时也会炸。换成 U+FFFD 保留长度与占位符位置。
                if has_lone_surrogate(raw_masked):
                    self.stats["surrogate_sanitized"] = (
                        self.stats.get("surrogate_sanitized", 0) + 1
                    )
                    raw_masked = sanitize_for_json(raw_masked)

                if not raw_masked:
                    entry.status = EntryStatus.FAILED
                    entry.target = ""
                    entry.warnings = [f"provider_error: {err or '空响应'}"]
                    _consec_fail += 1
                    if not _cb_tripped and _consec_fail >= _cb_limit:
                        _cb_tripped = True
                        log.error(
                            "★ 熔断：已连续 %d 条翻译失败，停止为本次运行继续"
                            "发请求（剩余条目留作未翻译，下次运行会重试）。"
                            "常见原因：模型对某类短专名/技能名不给译文。",
                            _consec_fail,
                        )
                        self.stats["circuit_breaker_tripped"] = 1
                    continue
                _consec_fail = 0

                if use_mask:
                    restored, ph_check = ph.verify_restored(
                        item.unit.source,
                        raw_masked,
                        slots_all[local_i],
                        masked_source=masked_all[local_i],
                    )
                    # ★★★ 换行补回必须**独立于 `ph_check.fatal` 判断**。
                    #
                    # 这里有一个**真正**的漏洞（实测发现）：
                    # `verify_restored` 的占位符计数是拿"还原后文本里的记号"
                    # 与 **`masked_source`** 比的；而模型丢掉 `⟦n⟧` 之后，
                    # 还原函数找不到记号可换，**还原后文本里一个记号都没有**
                    # ⇒ 计数 `0 == 0` 两侧相等、**顺序校验也被跳过**
                    # ⇒ `ph_check.fatal` 是 **False**。
                    #
                    # 实测（`Lifia: I have something to say\n「I have to go.」`，
                    # 模型只回第一行）：
                    #
                    #     verify_restored(masked_source=掩码) → fatal=True  ✅ 抓到
                    #     verify_restored(masked_source=原文) → fatal=False ❌ 漏掉
                    #
                    # 也就是说：**"丢了整行"这件事只在一种调用口径下可见**。
                    # 早先我把补回逻辑放在 `if ph_check.fatal:` 里面，
                    # 于是"该抓的情形恰好不 fatal"⇒ 补回**从不执行**。
                    #
                    # 所以下面**无条件**试补换行，再看结果变好还是变坏。
                    _nl_fixed = ph.repair_missing_newlines(
                        masked_all[local_i], raw_masked, slots_all[local_i]
                    )
                    if _nl_fixed is not None:
                        _nl_restored, _nl_check = ph.verify_restored(
                            item.unit.source,
                            _nl_fixed,
                            slots_all[local_i],
                            masked_source=masked_all[local_i],
                        )
                        # ⚠️ 两道门槛，缺一不可：
                        #
                        # ① **原本必须 fatal** —— 原本就通过的情况不能动
                        #    （那是既有正确路径，尤其不能丢掉 `_call_single`
                        #    已经拼好的多段结果）；
                        # ② **补后必须真的更完整** —— 见下。
                        #
                        # 为什么需要 ②：`verify_restored` 对"内容整段丢失"
                        # **看不见**。实测：
                        #
                        #     源  'Lifia: I have something to say\n「I have to go.」'
                        #     模型 '莉菲娅：我有话要说。'          ← 第二行整段没了
                        #     补后 '莉菲娅：我有话要说。⟦0⟧'      ← 记号补上了
                        #     verify_restored(补后) → fatal=False  ❌ 它只看记号
                        #
                        # 也就是说"补上记号"会让原本 fatal 的结果变成不 fatal，
                        # 但**第二行依然是丢的**。若直接采用，就把一个
                        # 静默丢行写进了游戏 —— 比保留原文更糟。
                        #
                        # ⚠️ 判据用**结构性**口径：补回后是否**真的补进了内容**。
                        #
                        # 用"和原文比"当口径是错的 —— 实测一条**完全正确**的
                        # 多行译文被它挡住：
                        #
                        #     源     'Lifia: I have something to say\n「I have to go.」'
                        #     模型   '{"0": "莉菲娅：我有话要说。", "1": "我得走了。"}'  ← 两行都在
                        #     拼回   '莉菲娅：我有话要说。⟦0⟧我得走了。'                ← 正确
                        #     非空白 17 字符 vs 原文 32 字符 ⇒ 比值 0.53
                        #
                        # 中文本来就比英文短（这个项目的既定事实，`num_predict`
                        # 的系数 2.2 就是为此而设），拿"和原文比"会把好译文误杀。
                        #
                        # 改用**"补回后比模型原始输出多了多少"**：补回只加记号，
                        # 所以若补后还原出的**文字**明显多于模型原始输出，
                        # 就说明原本的 fatal 是"记号缺失"而非"内容缺失"。
                        # 反之（`'莉菲娅：我有话要说。'` 补成 `'…。⟦0⟧'`，
                        # 文字量几乎没变）说明第二行**根本没被翻译**，
                        # 补记号只是把丢行**掩盖**掉 —— 必须拒绝。
                        _raw_n = len("".join((raw_masked or "").split()))
                        _tgt_n = len("".join((_nl_restored or "").split()))
                        _complete = _tgt_n > _raw_n
                        if ph_check.fatal and not _nl_check.fatal and _complete:
                            self.stats["newline_repaired"] = (
                                self.stats.get("newline_repaired", 0) + 1
                            )
                            log.debug(
                                "补回换行占位符：%s → %r",
                                ph_check.describe(),
                                _nl_fixed[:80],
                            )
                            restored, ph_check = _nl_restored, _nl_check
                            raw_masked = _nl_fixed
                        elif ph_check.fatal and not _nl_check.fatal:
                            # 记号补上了但内容明显不全 ⇒ 明确记一笔，
                            # 否则这种"看起来修好了"的情形无从追查。
                            self.stats["newline_repair_rejected_incomplete"] = (
                                self.stats.get(
                                    "newline_repair_rejected_incomplete", 0
                                )
                                + 1
                            )
                            log.debug(
                                "补回换行会掩盖丢内容（模型原始输出 %d 字符 → "
                                "补后还原 %d 字符，文字量没有增加），已拒绝：%r",
                                _raw_n,
                                _tgt_n,
                                _nl_fixed[:60],
                            )
                    if ph_check.fatal:
                        # 占位符被破坏。先试**补回**再决定是否放弃：
                        # 实测 translategemma:4b 会把 `\C[6]`、`\N[2]`、`\n`
                        # 这类转义整段删掉只译文字。原本直接判失败，
                        # 结果是"这句没翻译"，而它其实完全可用（少的只是
                        # 颜色或换行）。补回只调整记号位置，"数量与内容是否
                        # 齐全"仍由 verify_restored 复查。
                        repaired = ph.repair_dropped_masks(
                            masked_all[local_i], raw_masked, slots_all[local_i]
                        )
                        if repaired is not None:
                            restored2, check2 = ph.verify_restored(
                                item.unit.source,
                                repaired,
                                slots_all[local_i],
                                masked_source=masked_all[local_i],
                            )
                            if not check2.fatal:
                                self.stats["placeholder_repaired"] = (
                                    self.stats.get("placeholder_repaired", 0) + 1
                                )
                                log.debug(
                                    "补回占位符：%s → %s",
                                    ph_check.describe(),
                                    repaired,
                                )
                                restored, ph_check = restored2, check2
                                raw_masked = repaired
                    if ph_check.fatal:
                        # 再试一种**安全的**修复：删掉**凭空多出来的**屏蔽记号。
                        #
                        # 与"丢失占位符"不同 —— 丢失的是真信息，必须拒绝；
                        # 多出来的是模型幻觉，删掉不丢任何东西。
                        # 真实记录：源文根本没有占位符，模型却回了
                        # `'啊…乌鲁拉，别急，我还没瞄准呢！”} ⟦0⟧'`，
                        # 于是整条被判 fatal、译文清空，玩家看到空白对话框。
                        # 实测 14 条这类失败全部可救。
                        cleaned = ph.strip_unknown_masks(
                            raw_masked, len(slots_all[local_i])
                        )
                        cleaned2, check3 = ph.verify_restored(
                            item.unit.source,
                            cleaned,
                            slots_all[local_i],
                            masked_source=masked_all[local_i],
                        )
                        if cleaned != raw_masked and not check3.fatal:
                            self.stats["stray_mask_stripped"] = (
                                self.stats.get("stray_mask_stripped", 0) + 1
                            )
                            log.debug(
                                "删掉凭空记号：%s → %r",
                                ph_check.describe(),
                                cleaned,
                            )
                            restored, ph_check = cleaned2, check3
                            raw_masked = cleaned
                    # ★★ 这里**原本还有第二段**换行补回（无任何完整性门槛，
                    # 直接采纳），它会把上面那段带门槛的版本**覆盖掉** ——
                    # 于是"补上记号但第二行整段丢失"的结果照样被写回游戏。
                    #
                    # 实测（`_rescue_trace2.py`）：
                    #
                    #     源     'Lifia: I have something to say\n「I have to go.」'
                    #     模型   '莉菲娅：我有话要说。'      ← 第二行整段没了
                    #     补后   '莉菲娅：我有话要说。⟦0⟧'  ← 记号补上
                    #     verify_restored(补后) → fatal=False  ❌ 它只数记号
                    #     ⇒ 条目被判为"已翻译"，**丢行静默写回**
                    #
                    # 两段重复代码里**只有一段**能生效，而生效的是错的那段。
                    # 这类"看似都在工作、实则互相抵消"的重复是本项目
                    # 反复踩到的坑（见 ROADMAP §10 的"我自己错了两次"）。
                    # ⇒ 只保留上面那一份带完整性判据的实现，此处不再重复。
                    if ph_check.fatal:
                        # 补不回来 —— 硬错误，绝不能写回游戏
                        self.stats["placeholder_fatal"] += 1
                        entry.status = EntryStatus.FAILED
                        entry.target = ""
                        entry.warnings = [f"placeholder_broken: {ph_check.describe()}"]
                        entry.meta["raw_model_output"] = raw_masked
                        continue
                    candidate = restored
                else:
                    candidate = raw_masked

                # 抄记号检测：mask_batch 是**逐条局部编号**，所以"把上一条的
                # ⟦1⟧ 抄进这一条"用越界检查抓不到（这一条自己也有 ⟦0⟧）。
                # 某条译文里出现超出自身槽位数的记号下标，就是抄错了。
                # 这类错误很危险：还原后占位符多重集仍然"正确"，
                # 但游戏里读到的变量是错的（比如把名字显示成了金钱）。
                if use_mask:
                    leaked = [
                        idx for idx in ph.mask_indices(raw_masked)
                        if idx >= len(slots_all[local_i])
                    ]
                    if leaked:
                        self.stats["cross_item_leak"] = (
                            self.stats.get("cross_item_leak", 0) + 1
                        )
                        entry.status = EntryStatus.FAILED
                        entry.target = ""
                        entry.warnings = [
                            f"cross_item_leak: 译文里出现了本条目不存在的占位符编号 "
                            f"{sorted(set(leaked))}（本条只有 {len(slots_all[local_i])} 个占位符），"
                            "疑似把相邻条目的占位符抄了过来"
                        ]
                        entry.meta["raw_model_output"] = raw_masked
                        continue

                res = guard(
                    item.unit.source,
                    candidate,
                    max_chars=item.unit.max_chars,
                    length_ratio=ratio,
                    target_lang=target_lang,
                    hints=self._hints_of(item),
                )
                # ★ 判死的条目**不留译文**。
                #
                # ## 为什么这一行很关键（真实事故）
                #
                # 早先是 `entry.target = res.text`（无条件赋值）。于是一条被
                # `foreign_script` 守卫判坏的译文，虽然状态是 FAILED，
                # **文字还留着** —— 而下游好几处只看"target 非空"：
                #
                # * `fonts` 的 `_collect_texts()` 把它当译文收进字符集 ⇒
                #   俄文/格鲁吉亚文字符进了**硬失败**判据 ⇒ 本机任何 CJK
                #   字体都没有它们 ⇒ 整轮字体适配中止 ⇒ `apply` 把原字体
                #   原样拷过去 ⇒ **游戏里满屏口口口**；
                # * 翻译记忆（`memory.py`）虽然按 status 过滤了，
                #   但那是它自己额外加的保险，不该依赖。
                #
                # 三条被**正确拒绝**的坏译文，差点让整个游戏的全部中文
                # 变成方块 —— 判据没错，错在拒绝之后没把垃圾清掉。
                #
                # 清空也让语义自洽：`FAILED` 的定义就是"没有可用的译文"。
                # `_invalidate_entries()` 一直是这么做的（清空 + 标 FAILED），
                # 这里当初漏了。
                entry.target = "" if res.fatal else res.text
                entry.warnings = list(res.warnings)
                entry.provider = self.name
                entry.model = self._model
                entry.glossary_hits = [g.source for g in item.glossary]
                entry.retries = 0
                entry.status = EntryStatus.FAILED if res.fatal else EntryStatus.TRANSLATED

                # ---- 3.4 ★ 假名回显 ⇒ 判失败（交给逐条重译） ----
                #
                # ## 缺陷（实测，`.scratch/_probe_echo_kana.py`）
                #
                # 批处理时模型会把**纯假名条目原样回显**，而流水线把它
                # 记成"已译"：
                #
                #     批内：'ポイズンガード' → 'ポイズンガード'
                #     单条：'ポイズンガード' → '毒药卫'      ← 单独问就对
                #
                # 对 30 条"含假名 + 回显"条目**单独复测：30/30 全部得到
                # 真译文（100%）** ⇒ 是"批内该翻没翻"，不是"该保留"。
                #
                # 规模：已译条目里 **2,218 条**（全库 19,353 条回显的 11.5%）。
                #
                # ## 为什么只处理"含假名"的回显
                #
                # 19,353 条回显里：纯拉丁 15,892 条（专名，**不该**重译）、
                # 含假名 2,218 条（**该**重译）、含汉字 1,243 条（不确定，先不动）。
                # ⇒ 只在**能用行为验证**的最小集合上动手。
                #
                # ## 判失败而不是就地重译
                #
                # * 判失败 ⇒ 条目进 `only_pending` 队列，下一轮被**逐条**
                #   处理（单条路径实测 30/30 成功）；
                # * 就地重译要给本函数加递归，侵入性大；
                # * 而且"失败"是**诚实**的状态 —— 它确实还没翻好。
                #
                # ⚠️ 必须**同时清空 target**：`FAILED` 的定义就是"没有可用
                #   译文"（见上面那段事故注释 —— 留着垃圾译文会让字体阶段
                #   把非中文字符收进字符集，最终满屏口口口）。
                if (
                    entry.status is EntryStatus.TRANSLATED
                    and is_kana_echo(item.unit.source, entry.target)
                ):
                    self.stats["kana_echo_rejected"] = (
                        self.stats.get("kana_echo_rejected", 0) + 1
                    )
                    entry.status = EntryStatus.FAILED
                    entry.target = ""
                    entry.warnings = [
                        *entry.warnings,
                        "批内原样回显了日文假名（单独重译通常可得译文）",
                    ]

            # ---- 3.5 定向重试：把"整条掩码记号弄丢"的条目重问一次 ----
            #
            # ## 为什么值得单独重试
            #
            # 实测 `translategemma:4b` 对 Ren'Py 的成对文本标签
            # （`{color=#ffd700}…{/color}`）会**整条丢掉两个记号**只译文字：
            #
            #     源文  {color=#ffd700}The lamp was lit at dawn.{/color}
            #     掩码  ⟦0⟧The lamp was lit at dawn.⟦1⟧
            #     输出  '灯在黎明时被点亮了。'          ← 两个 ⟦⟧ 都没了
            #
            # `repair_dropped_masks` 对**这种**情况帮不上忙：它擅长补回
            # 单侧记号（`\C[6]` 只能放开头、`\n` 只能放中间），
            # 而 `{color…}` / `{/color}` 是**成对**的 —— 丢了开标签时
            # 谁也不知道作者想让哪几个字变色。硬补一个位置是**猜**，
            # 猜错了就是"颜色标错"，比不翻更糟。所以它返回 None 是对的。
            #
            # 但**不翻**并不是唯一出路：模型丢记号是"没听清要求"，
            # 不是"做不到"。单独重问一次、并在提示词里明确点出
            # "必须原样保留 ⟦数字⟧ 记号"，实测能把大部分救回来。
            #
            # 代价可控：只对**已经失败的**条目多打一次请求
            # （失败的本来结果就是空，重试没有下行风险）。
            retried = 0
            for local_i, _global_i in enumerate(batch):
                entry = out[_global_i]
                if entry.status != EntryStatus.FAILED or not entry.warnings:
                    continue
                if not any("placeholder" in w for w in entry.warnings):
                    continue
                if not slots_all[local_i]:
                    continue  # 本来就没有记号，不是这个病因
                try:
                    again = self._call_single(
                        batch_items[local_i],
                        masked_all[local_i],
                        marker_warning=prompts.marker_retry_hint(
                            masked_all[local_i], slots_all[local_i]
                        ),
                        slots=slots_all[local_i],
                    )
                except _RETRYABLE:
                    continue
                if not again or again == entry.meta.get("raw_model_output"):
                    continue
                restored2, check2 = ph.verify_restored(
                    batch_items[local_i].unit.source,
                    again,
                    slots_all[local_i],
                    masked_source=masked_all[local_i],
                )
                if check2.fatal:
                    continue  # 还是不行 —— 保持失败，绝不写回坏译文
                res2 = guard(
                    batch_items[local_i].unit.source,
                    restored2,
                    max_chars=batch_items[local_i].unit.max_chars,
                    # ▲ 这里原来是 `self.cfg.translate.max_output_chars_factor` ——
                    #   配置里**根本没有这个字段**（只有 `max_chars_ratio`），
                    #   所以一旦走到"记号重试"这条路就 `AttributeError`，
                    #   整批的补救全部作废。
                    #
                    #   极隐蔽：正常路径不经过这里，单元测试与历史实测都没碰到，
                    #   直到换了模型（HY-MT 更容易让首轮解析失败 ⇒ 更常走重试）
                    #   才暴露出来。
                    length_ratio=self.cfg.translate.max_chars_ratio,
                    target_lang=target_lang,
                    hints=self._hints_of(batch_items[local_i]),
                )
                if res2.fatal:
                    continue
                entry.target = res2.text
                entry.warnings = list(res2.warnings)
                entry.status = EntryStatus.TRANSLATED
                entry.meta["marker_retry"] = True
                entry.meta["raw_model_output"] = again
                entry.retries = 1
                retried += 1
            if retried:
                self.stats["marker_retry_recovered"] = (
                    self.stats.get("marker_retry_recovered", 0) + retried
                )
                log.info("定向重试救回 %d 条丢失掩码记号的译文", retried)

            # ---- 3.6 【已撤回】换行记号丢失时改用不屏蔽换行重问 ----
            #
            # 这里曾实现过一条重试路径：换行记号被模型吞掉时，改用
            # `mask_newlines=False` 重问一次。**实测确认它不可达，已撤回。**
            #
            # 撤回原因：`repair_dropped_masks` 会把丢掉的记号**重新插回去**
            # （包括换行记号 —— 它插在数字/量词后面），所以走到这一步时
            # `slots` 里永远不缺换行记号。准入条件 `换行 in slots` 与实际
            # 失败原因（还丢了别的记号）互相排斥，路径永不触发。
            #
            # 实测 60 条真实多行条目：`placeholder_fatal` 27 条，其中
            # 命中这条路径的 **0 条**（加之前与加之后 `stats` 完全一样）。
            #
            # ★ 教训：**加分支前先用真实数据确认它可达**。不可达的修复
            #   和没修一样，但会让人以为已经修了 —— 这比不修更危险。
            #   真实失败机理与后续方向见 docs/ROADMAP.md §3.21。

            # ---- 3.7 多行条目失败 ⇒ **逐行重译**（实测救回约 23%）----
            #
            # ## 为什么是"逐行"，而不是再改判据或换模型
            #
            # ROADMAP §3.21 记录了两个已实测否掉的方向：
            #
            # * **改判据**（把"丢换行记号"降级为警告）—— 不行：丢换行记号里
            #   混着"两行并成一行、内容完整"和"真的丢了第二行"两种，
            #   而 `compare_restored` 对**两者都返回 ok**（它比内容重叠，
            #   丢掉的第二行在还原前后都没有，于是"相等"）。
            #   换行记号是目前**唯一**能抓住"少翻一行"的信号。
            # * **换模型** —— 不行：同一批 24 条真实多行条目上，
            #   `translategemma:4b` 成功 16/24，两个专精翻译的 7B
            #   （含腾讯混元翻译）都只有 14/24 且慢 5~12 倍。
            #
            # 那剩下的就是**改任务形态**：不要让模型一次面对多行。
            # 逐行送翻时每行都是独立条文，模型**没有"合并/截断"的机会**。
            #
            # ## 实测收益（30 条真实多行条目）
            #
            # 合并送翻通过 17、判死 13；对判死的 13 条逐行重译
            # **救回 7 条**（占全部抽样 **23%**），且原通过的 17 条
            # **一条都没被改动**。样例：
            #
            #     Lifia ↵ 「I have to go.」   →  莉菲娅 ↵ 我得走了。
            #     Please enter a number between 1 and 15. ↵ (Standard difficulty is 5)
            #       → 请输入一个 1 到 15 之间的数字。↵（标准难度为 5）
            #
            # ## 代价与安全
            #
            # * 代价：一条 n 行 → n 次请求。**只对已经失败的条目**做，
            #   那本来就没有译文，重试没有下行风险。
            # * **逐行过守卫**（不是把拼好的整条过一次）：
            #   这样每一行都要自己满足长度/重复/占位符判据。
            #   实测必须逐行，因为整条判据会漏掉行内的坏产物 ——
            #   例如 `「邪恶之 bane」`（原词泄漏）在整条校验里看不出来。
            # * ⚠️ **已知残留限制**：`guards.guard` 抓不住"中文里混进一个
            #   原文拉丁词"这种泄漏（它既不是 `foreign_script`，也不会
            #   触发重复/长度判据）。实测 `「邪恶之 bane」` 就是漏过的。
            #   这属于**质量瑕疵而非静默丢内容**，比"整条保留原文"好，
            #   但仍需人工审校；后续方向见 ROADMAP §3.21。
            perline_saved = 0
            for local_i, _global_i in enumerate(batch):
                entry = out[_global_i]
                if entry.status != EntryStatus.FAILED:
                    continue
                unit = batch_items[local_i].unit
                src_lines = [x for x in (unit.source or "").split("\n") if x.strip()]
                if len(src_lines) < 2:
                    continue  # 单行条目不走这条路
                got_lines: list[str] = []
                bad = False
                # 逐行救援的**失败原因**统计。
                #
                # 为什么值得常驻：这条路径以前有真缺陷（传裸源码行、
                # 不传 slots），修好之后**仍然**只救回一小部分条目，
                # 而"为什么没救回"必须能一眼看出来 —— 否则又会变成
                # "看起来在工作、实际没效果"。分类计数能直接指向
                # 下一步该改哪一层（提示词 / 守卫 / 分批）。
                why = ""
                for one in src_lines:
                    # ★ 这里必须**先屏蔽这一行**，再把掩码文本交给 `_call_single`。
                    #
                    #   `_call_single(item, masked, *, slots=...)` 的第二个参数是
                    #   **掩码后**的文本，`slots` 是它对应的槽位表 ——
                    #   返回时靠 `slots` 把 `⟦n⟧` 还原成原样记号。
                    #
                    #   原先这里传的是**裸源码行** `one`、且**不传 `slots`**，
                    #   于是：
                    #     * `_call_single` 内部拿 `one` 当"掩码文本"，
                    #       而它其实没被掩码，`slots` 为空 ⇒ 模型回什么都不会被还原；
                    #     * 更糟的是 `slots` 缺失后，内部的
                    #       `if ph.remaining_masks(got_one)` 之类的判据失去依据。
                    #   结果这条"逐行救援"路径**几乎救不回任何条目**
                    #   （实测长条目 `perline_recovered = 0`）。
                    #
                    #   逐行送翻时每行**自己就没有换行**了，所以这里
                    #   `newlines=False`（行内没有换行可屏蔽）；
                    #   行内该保护的占位符（`\V[1]`、`\C[0]` 等）仍然照样屏蔽。
                    line_mask = ph.mask(one, newlines=False)
                    try:
                        got_one = self._call_single(
                            batch_items[local_i],
                            line_mask.text,
                            slots=line_mask.slots,
                        )
                    except _RETRYABLE as exc:
                        bad = True
                        why = f"call:{type(exc).__name__}"
                        break
                    if not got_one:
                        bad = True
                        why = "empty"
                        break
                    if ph.remaining_masks(got_one):
                        bad = True
                        why = "mask_left"
                        break
                    # ★★ 补回被模型**整段丢掉**的样式记号（颜色/停顿等）。
                    #
                    # ## 为什么这里必须补（实测）
                    #
                    # `_call_single` **不做** `repair_dropped_masks` ——
                    # 那是整条路径（§3 的 `if ph_check.fatal` 分支）才有的步骤。
                    # 于是同一个输入在两条路径上结果不同（实测
                    # `.scratch/_single_vs_batch_color.py`，3/3 轮一致）：
                    #
                    #     输入        '\c[0]Suck it.'
                    #     模型原样    '{"0": "吃不了就别来!"}'   ← 压根没回 ⟦0⟧
                    #     _call_single → '吃不了就别过来。'       ← 颜色码没了
                    #     translate_batch → '\c[0]吃不了就别过来。' ← 颜色码**保住了**
                    #
                    # 差别就在整条路径调了 `repair_dropped_masks`：
                    # 它按"记号在原文里的左邻字符/相对位置"把 `⟦0⟧` 插回去，
                    # 位置**确定**时才算成功（见它的 docstring），
                    # 所以不是猜。
                    #
                    # ⇒ 逐行救援路径漏了这一步，导致救回的译文**丢配色**。
                    #    补上它，救回的译文才与整条路径同样完整。
                    #
                    # ⚠️ 只在**能确定位置**时补（函数内部保证，否则返回 None）；
                    #    补完仍要过下面的 `guard`，数量/内容是否齐全照样复查。
                    repaired_one = ph.repair_dropped_masks(
                        line_mask.text, got_one, line_mask.slots
                    )
                    if repaired_one is not None:
                        # 采用前必须确认**所有**槽位都真的补回来了。
                        #
                        # ⚠️ `_call_single` 返回的是**掩码形态**（带 `⟦i⟧`），
                        #    不是还原后的文本 —— 这一点我从它的返回路径确认过
                        #    （`return parts[0]` 等分支返回的都是 `mapping` 里的
                        #    值，而 `mapping` 来自模型原始输出）。
                        #    所以这里比的是"记号是否齐全"，与整条路径一致。
                        restored_one = ph.unmask(repaired_one, line_mask.slots)
                        if not ph.remaining_masks(restored_one) and all(
                            s in restored_one for s in line_mask.slots
                        ):
                            got_one = repaired_one
                            self.stats["perline_style_repaired"] = (
                                self.stats.get("perline_style_repaired", 0) + 1
                            )
                    # ★ **逐行**过守卫 —— 见上面"代价与安全"的说明
                    r_one = guard(
                        one,
                        ph.unmask(got_one, line_mask.slots)
                        if line_mask.slots
                        else got_one,
                        max_chars=None,
                        length_ratio=self.cfg.translate.max_chars_ratio,
                        target_lang=target_lang,
                        hints=self._hints_of(batch_items[local_i]),
                    )
                    if r_one.fatal:
                        bad = True
                        why = f"guard:{','.join(r_one.warnings)[:40]}"
                        break
                    got_lines.append(r_one.text.strip())
                if bad or len(got_lines) != len(src_lines):
                    if why:
                        self.stats[f"perline_why_{why.split(':')[0]}"] = (
                            self.stats.get(f"perline_why_{why.split(':')[0]}", 0) + 1
                        )
                    continue
                # ★ 逐行全过守卫**仍然可能是垃圾**：实测模型对每一行都回
                # 同一个说话人标签（`'店员 1：'` × 3）。每行单独看都
                # "是合法中文、长度也在范围内"，守卫于是放行，拼起来却是
                # 把垃圾复制了三遍 —— 还挂着 `status=translated` 写回游戏。
                #
                # ## 判据只有"重复"这一条 —— 理由见 `_is_degenerate_repetition`
                #
                # 我先加过比值下限（`_is_acceptable_recovery`）。撤掉是因为
                # **实测证明比值没有判别力**（垃圾 0.04~0.43 与合理译文
                # 0.17~0.37 完全重叠），而且它当场把一条**已量过**的正解
                # 拒掉了：`'Lifia\n「I have to go.」'` 的正解就是人名
                # `'莉菲娅'`（比值 0.125、不含换行），见
                # `tests/test_marker_retry.py`（实测 30 条救回 7 条、
                # 原通过的 17 条一条未改）。
                #
                # ⇒ 只留结构性判据，两个方向都成立：
                #   * `'莉菲娅'`（单段，不重复）⇒ 放行 ✅
                #   * `'店员 1：' × 3`（重复短段）⇒ 拦下 ✅
                if _is_degenerate_repetition(got_lines):
                    self.stats["perline_why_degenerate"] = (
                        self.stats.get("perline_why_degenerate", 0) + 1
                    )
                    continue
                entry.target = "\n".join(got_lines)
                entry.warnings = []
                entry.status = EntryStatus.TRANSLATED
                entry.meta["perline_fallback"] = True
                entry.meta["raw_model_output"] = entry.target
                entry.retries = 1
                perline_saved += 1
            if perline_saved:
                self.stats["perline_recovered"] = (
                    self.stats.get("perline_recovered", 0) + perline_saved
                )
                log.info("逐行重译救回 %d 条多行译文", perline_saved)

        # ---- 4. 把去重后的结果摊回重复项 ----
        # 重复项的 `uid` 各不相同（同一句台词出现在多个事件里，
        # 引擎用 `location.pointer` 区分），所以每个 uid 都要有自己的条目 ——
        # 否则回写时只会改到一处，游戏里其它地方还是原文。
        # 译文/状态照抄，但 `uid`/`source`/`kind` 必须是这一条自己的。
        first_positions = set(first_of.values())
        for pos, entry in enumerate(out):
            if pos in first_positions:
                continue  # 首次出现，本身就是被翻译的那条
            canonical = out[first_of[owners[pos]]]
            entry.target = canonical.target
            entry.status = canonical.status
            entry.warnings = list(canonical.warnings)
            entry.provider = canonical.provider
            entry.model = canonical.model
            entry.glossary_hits = list(canonical.glossary_hits)
            entry.meta = {**canonical.meta, "deduped_from_uid": canonical.uid}

        # ---- 3.9 ★ 最后一道闸：任何路径都不得留下"退化产物"（#39）----
        #
        # 判据、实测与"为什么要放在最后"见
        # :func:`_block_degenerate_entries`。
        degenerate = _block_degenerate_entries(out, self.stats)
        if degenerate:
            log.warning("拦下 %d 条退化译文（同一短片段复制多遍）", degenerate)

        return out

    @staticmethod
    def _dedupe(
        items: list[TranslateItem],
    ) -> tuple[list[TranslateItem], dict[int, int], list[int]]:
        """按 `source` 去重，返回 ``(唯一条目, 唯一序号→首次出现序号, 每条对应的唯一序号)``。

        ## 为什么值得做

        真实游戏待办 22087 条里，**26.3% 是重复原文**。极端例子是说话人名：
        4334 条待翻、不同原文只有 **33** 种（99.2% 重复）——
        就是 `'<right>  Ulula  </right>'` 这种被复制到几百个事件里的名字。
        不去重等于同一句话问模型几千次。

        ## 去重的键为什么只用 `source`

        加上 `kind` 会更"安全"，但实测同一文本在不同 `kind` 下（比如
        某个词既是道具名又是说话人）译文应该一致；用 `source` 能多省一些。
        `max_chars` 这类每条的约束由后续的守卫单独检查，
        不会因为共用一条译文而漏检。

        注意**不能**按 `uid` 去重 —— 引擎给每处出现都分配了不同的
        `uid`（`location.pointer` 不同），它们是不同的回写目标。
        """
        first_of: dict[int, int] = {}
        owners: list[int] = []
        uniq_items: list[TranslateItem] = []
        seen: dict[str, int] = {}
        for i, it in enumerate(items):
            key = it.unit.source
            if key in seen:
                owners.append(seen[key])
                continue
            seen[key] = len(uniq_items)
            owners.append(len(uniq_items))
            first_of[len(uniq_items)] = i
            uniq_items.append(it)
        return uniq_items, first_of, owners

    def _blank(self, item: TranslateItem) -> TranslationEntry:
        return TranslationEntry(
            uid=item.unit.uid,
            source=item.unit.source,
            target="",
            status=EntryStatus.PENDING,
            kind=item.unit.kind,
            provider=self.name,
            model=self._model,
            meta={"detected_lang": guess_language(item.unit.source)},
        )

    # ------------------------------------------------------------------
    # 自检轮
    # ------------------------------------------------------------------

    def review_batch(self, sources: list[str], targets: list[str], target_lang: str) -> list[str]:
        """对一批译文做审校，返回修正后的译文。失败时原样返回。

        原则：**只允许"修"，不允许把好译文改坏**。若新译文引入了致命问题
        （占位符丢失）或警告变多，就保留原译文。
        """
        if not sources:
            return []
        use_mask = self.cfg.translate.mask_placeholders
        if use_mask:
            masked_all, slots_all = ph.mask_batch(
                sources, newlines=self.cfg.translate.mask_newlines
            )
            pairs = [(i, masked_all[i], targets[i]) for i in range(len(sources))]
        else:
            slots_all = [[] for _ in sources]
            pairs = [(i, sources[i], targets[i]) for i in range(len(sources))]

        user = prompts.build_review_user_prompt(pairs)
        try:
            raw = self._chat(
                user,
                system=prompts.REVIEW_SYSTEM_PROMPT,
                temperature=0.0,
                n_items=len(sources),
                src_chars=sum(len(s or "") for s in sources),
            )
            # ★ 同样要传 `sources`：模型可能以**原文为键**回答，
            # 不传就查不到编号 ⇒ 整批被当成"没解析出东西"而静默丢弃。
            # 这里传的是它**实际看到**的文本（开了掩码就是掩码后的）。
            seen_sources = masked_all if use_mask else sources
            mapping, res = parse_translations(
                raw,
                expect_indices=list(range(len(sources))),
                sources=list(seen_sources),
            )
            if not mapping:
                return targets

            out: list[str] = []
            for i, cur in enumerate(targets):
                cand = mapping.get(i, "")
                if not cand.strip():
                    out.append(cur)
                    continue
                if use_mask:
                    restored, check = ph.verify_restored(
                        sources[i], cand, slots_all[i], masked_source=masked_all[i]
                    )
                    if check.fatal:
                        out.append(cur)
                        continue
                    cand = restored
                g_old = guard(sources[i], cur, length_ratio=self.cfg.translate.max_chars_ratio, target_lang=target_lang)
                g_new = guard(sources[i], cand, length_ratio=self.cfg.translate.max_chars_ratio, target_lang=target_lang)
                if g_new.fatal and not g_old.fatal or len(g_new.warnings) > len(g_old.warnings):
                    out.append(cur)
                else:
                    out.append(g_new.text)
            return out
        except (ProviderError, OllamaError) as exc:
            log.warning("审校轮失败，保留原译文：%s", exc)
            return targets
