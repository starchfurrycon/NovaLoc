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

            limit = min(o.max_batch_strings, 50 if short else 25)
            if cur and (len(cur) >= limit or cur_chars + len(text) > o.max_batch_chars):
                flush()
            cur.append(i)
            cur_chars += len(text)
        flush()
        return batches

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------

    def _chat(
        self,
        user: str,
        *,
        system: str,
        temperature: float | None = None,
        n_items: int = 1,
        src_chars: int = 0,
    ) -> str:
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
        mapping, res = parse_translations(raw, expect_indices=expect, sources=list(masked))
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
            user, system=prompts.SYSTEM_PROMPT, n_items=1, src_chars=len(masked or "")
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
        mapping, res = parse_translations(
            raw, expect_indices=[0], sources=[masked]
        )
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

    def translate_batch(
        self, items: list[TranslateItem], target_lang: str
    ) -> list[TranslationEntry]:
        if not items:
            return []

        ok, why = self.available()
        if not ok:
            raise ProviderError(why)

        self.stats["items"] += len(items)

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

        for bi, batch in enumerate(batches):
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

                    # 批量反复失败 → 退化为逐条，用可靠性换速度
                    if attempt >= 1 and len(batch_items) > 1:
                        self.stats["single_fallbacks"] += 1
                        singles: dict[int, str] = {}
                        single_err: str | None = None
                        for li, it in enumerate(batch_items):
                            if li in per_item_failed:
                                continue
                            # ★ 已经逐条试过且失败的条目**不再重试**。
                            #
                            # 实测浪费（一次真实端到端跑，`Dungeon And
                            # Darkness-Steam` + `Midnight Exhibitionist DX`）：
                            #
                            #   "批 N 条里只回 M 条" 出现 48 次
                            #   缺的条数合计 **403** ⇒ 额外 403 次单条请求
                            #   "批 N（M 条）第 K 次失败" 24 次，
                            #   其中 K≥2 的 8 次
                            #
                            # 第 2 次失败（attempt=1）会进这个逐条循环；
                            # 若它没救回任何条目，外层会**再循环一次**
                            # （attempt=2），于是同一批的每一条又被逐条问一遍。
                            # 那些刚刚失败的条目基本不会因为"再问一次"而变好
                            # —— 触发源是内容（见下），不是偶发抖动。
                            try:
                                got = self._call_single(it, masked_all[li], slots=slots_all[li])
                                if got:
                                    singles[li] = got
                                else:
                                    per_item_failed.add(li)
                            except _RETRYABLE as exc2:
                                single_err = str(exc2)
                                per_item_failed.add(li)
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

            # ---- 3. 还原 + 校验 + 守卫 ----
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
                    continue

                if use_mask:
                    restored, ph_check = ph.verify_restored(
                        item.unit.source,
                        raw_masked,
                        slots_all[local_i],
                        masked_source=masked_all[local_i],
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
                    # ★ **逐行**过守卫 —— 见上面"代价与安全"的说明
                    r_one = guard(
                        one,
                        got_one,
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
