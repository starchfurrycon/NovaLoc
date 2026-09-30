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

    def _options(self, *, temperature: float | None = None) -> dict:
        o = self.cfg.ollama
        opts: dict[str, object] = {
            "temperature": o.temperature if temperature is None else temperature,
            "top_p": o.top_p,
            "num_ctx": o.num_ctx,
            # 关键：不设这个，Ollama 用 1.0（等于关闭），批量翻译必然复读
            "repeat_penalty": o.repeat_penalty,
            "repeat_last_n": o.repeat_last_n,
        }
        return opts
        # num_gpu 交给 Ollama 自己决定；硬设容易撞显存上限

    # ------------------------------------------------------------------
    # 批量切分
    # ------------------------------------------------------------------

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

        for i, item in enumerate(items):
            text = item.unit.source
            kind = item.unit.kind
            long_text = len(text) > 60
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

    def _chat(self, user: str, *, system: str, temperature: float | None = None) -> str:
        result = self.client.chat(
            self._model,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options=self._options(temperature=temperature),
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
        raw = self._chat(user, system=prompts.SYSTEM_PROMPT)
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

        missing = [i for i in expect if i not in mapping]
        if missing:
            log.debug("批次漏了 %d 条：%s", len(missing), missing[:10])
            # 只补漏的那些，**不**重发整批：重发整批既慢又可能再次漏。
            # 逐条调用实测可靠（单条成功率远高于批量），所以用可靠性换速度，
            # 且只对真正缺的条目付出这个代价。
            self.stats["single_fallbacks"] += 1
            for li in missing:
                try:
                    got = self._call_single(batch_items[li], masked[li])
                except _RETRYABLE as exc:
                    log.debug("补漏第 %d 条失败：%s", li, exc)
                    continue
                if got:
                    mapping[li] = got
        return mapping

    def _call_single(self, item: TranslateItem, masked: str) -> str:
        user = prompts.build_single_user_prompt(
            masked,
            kind=item.unit.kind,
            source_lang=self.cfg.translate.source_lang,
            target_lang=self.cfg.translate.target_lang,
            glossary_block=self._collect_glossary([item]),
            extra_context=self._context_hint([item]),
        )
        raw = self._chat(user, system=prompts.SYSTEM_PROMPT)
        mapping, res = parse_translations(raw, expect_indices=[0])
        if 0 in mapping:
            return mapping[0]
        if res.ok and isinstance(res.value, str):
            return res.value
        # 有些模型即使要求 JSON 也只给纯文本，当译文用
        stripped = raw.strip()
        if stripped and not stripped.startswith(("[", "{")):
            return stripped
        raise ProviderError(f"单条翻译失败：{raw[:200]!r}")

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
        out: list[TranslationEntry] = [self._blank(it) for it in items]
        batches = self._make_batches(items)
        ratio = self.cfg.translate.max_chars_ratio
        use_mask = self.cfg.translate.mask_placeholders

        for bi, batch in enumerate(batches):
            self.stats["batches"] += 1
            batch_items = [items[i] for i in batch]
            sources = [it.unit.source for it in batch_items]

            # ---- 1. 屏蔽占位符 ----
            if use_mask:
                masked_all, slots_all = ph.mask_batch(sources)
            else:
                masked_all = list(sources)
                slots_all = [[] for _ in sources]

            # ---- 2. 调用（带重试与逐条降级） ----
            raw_by_index: dict[int, str] = {}
            err: str | None = None
            for attempt in range(3):
                try:
                    if len(batch_items) == 1:
                        got = self._call_single(batch_items[0], masked_all[0])
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
                            try:
                                got = self._call_single(it, masked_all[li])
                                if got:
                                    singles[li] = got
                            except _RETRYABLE as exc2:
                                single_err = str(exc2)
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
                    try:
                        got = self._call_single(batch_items[li], masked_all[li])
                    except _RETRYABLE as exc:
                        log.debug("补空第 %d 条失败：%s", li, exc)
                        continue
                    if got:
                        raw_by_index[li] = got
                        self.stats["empty_recovered"] = (
                            self.stats.get("empty_recovered", 0) + 1
                        )

            # ---- 3. 还原 + 校验 + 守卫 ----
            for local_i, global_i in enumerate(batch):
                item = items[global_i]
                entry = out[global_i]
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
                )
                entry.target = res.text
                entry.warnings = list(res.warnings)
                entry.provider = self.name
                entry.model = self._model
                entry.glossary_hits = [g.source for g in item.glossary]
                entry.retries = 0
                entry.status = EntryStatus.FAILED if res.fatal else EntryStatus.TRANSLATED

        return out

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
            masked_all, slots_all = ph.mask_batch(sources)
            pairs = [(i, masked_all[i], targets[i]) for i in range(len(sources))]
        else:
            slots_all = [[] for _ in sources]
            pairs = [(i, sources[i], targets[i]) for i in range(len(sources))]

        user = prompts.build_review_user_prompt(pairs)
        try:
            raw = self._chat(
                user, system=prompts.REVIEW_SYSTEM_PROMPT, temperature=0.0
            )
            mapping, res = parse_translations(raw, expect_indices=list(range(len(sources))))
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
