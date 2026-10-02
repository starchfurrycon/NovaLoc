"""从模型输出里稳健地抽出结构化结果。

本地 4B~8B 模型几乎不可能每次都给出完美 JSON，所以必须有多级恢复阶梯。
按"代价从低到高"依次尝试，任何一级成功就停：

1. 直接 :func:`json.loads`
2. 剥掉 Markdown 代码块围栏 / 前后废话
3. 括号配平扫描，截出第一个完整的 JSON 值（跳过字符串内的括号）
4. 宽松修补：中文引号、尾随逗号、单引号、``True/False/None``
5. JSON5（若装了）
6. 正则逐项 salvage：把 ``"i": 0, "t": "..."`` 一个个抠出来
7. 降级分隔符模式：模型返回 ``###0### 译文`` 这种自定义格式
8. 单条重试（由调用方负责）

第 6、7 级很"脏"但极其重要 —— 宁可拿到 39/40 条也不该整批丢弃。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)

_CURLY_MAP = {
    "\u201c": '"', "\u201d": '"',   # “ ”
    "\u2018": "'", "\u2019": "'",   # ‘ ’
    "\uff02": '"',                  # ＂
}

#: 模型偶尔会输出这些 Python 字面量而不是 JSON 的
_PY_LITERALS = ((r"\bTrue\b", "true"), (r"\bFalse\b", "false"), (r"\bNone\b", "null"))


@dataclass
class ParseResult:
    value: Any = None
    method: str = "none"
    ok: bool = False
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


# --------------------------------------------------------------------------
# 各级恢复
# --------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_balanced(text: str, open_ch: str = "[", close_ch: str = "]") -> str | None:
    """用括号配平扫出第一个完整的 JSON 数组/对象，跳过字符串内的括号。

    比正则可靠得多，因为译文里完全可能出现 ``[`` ``]`` ``{`` ``}``。
    """
    start = text.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _escape_raw_control_chars(text: str) -> str:
    r"""把**字符串字面量内部**的裸换行/制表符转义成 ``\n`` ``\t``。

    ## 为什么需要这一级

    JSON 规范不允许字符串里出现裸控制字符，而模型**经常**吐出来。
    实测（ElfLifia 批 28）模型回吐提示词时的形态：

        {"0": "获得 1 个『额外抽牌』。\n『额外抽牌』：…」} ⟦0⟧"<裸换行>
            <裸换行>    <裸换行>    …确保输出的"}

    整段因此 `json.loads` 失败、`mapping` 为空，上层只能报
    "结果不是字符串"。这一级的收益**不是**救回这条译文
    （那内容是回吐的提示词，护栏应该拦掉），而是让解析能走完、
    于是**护栏拿到文本、能给出真正的原因**（`hint_echo`），
    而不是一个"解析不了"的笼统错误。

    ## 只动字符串内部

    逐字符扫描并跟踪"是否在字符串里"（含反斜杠转义），
    结构性的换行（对象/数组之间）保持原样 —— 那些是合法空白。
    """
    out: list[str] = []
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
                out.append(ch)
                continue
            if ch == "\\":
                escaped = True
                out.append(ch)
                continue
            if ch == '"':
                in_str = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def _loose_fix(text: str) -> str:
    """把模型常见的"近似 JSON"修补成可解析的 JSON。"""
    out = text
    for bad, good in _CURLY_MAP.items():
        out = out.replace(bad, good)
    for pat, rep in _PY_LITERALS:
        out = re.sub(pat, rep, out)
    # 尾随逗号： [1, 2,] / {"a": 1,}
    out = re.sub(r",(\s*[\]}])", r"\1", out)
    # 缺逗号： }{ 或 }" 之间
    out = re.sub(r"}\s*{", "}, {", out)
    out = re.sub(r'"\s*"', '", "', out) if out.count('"') % 2 else out
    # ★ 裸控制字符：放在最后，因为它改的是字符串**内容**，
    #   前面几步（尤其 `"\s*"` → `", "`）需要看到换行来判断结构。
    out = _escape_raw_control_chars(out)
    return out


def _salvage_items(text: str) -> list[dict[str, Any]]:
    """正则逐项抠出 ``{"i": n, "t": "..."}``。

    这是最关键的一级：模型经常在数组末尾截断，或忘了写某个括号，
    但每一项本身是完整的。整批丢弃的代价太大。
    """
    items: list[dict[str, Any]] = []
    # 匹配 i 与 t 的常见书写变体
    pat = re.compile(
        r'["\']?i["\']?\s*[:=]\s*(\d+)\s*,\s*'
        r'["\']?t["\']?\s*[:=]\s*'
        r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')',
        re.DOTALL,
    )
    for m in pat.finditer(text):
        idx = int(m.group(1))
        raw = m.group(2)
        try:
            val = json.loads(raw)
        except Exception:  # noqa: BLE001
            # 单引号字符串：手工去引号
            val = raw[1:-1]
        items.append({"i": idx, "t": val})

    if items:
        return items

    # 退一步：只有 t 没有 i，按出现顺序补索引
    t_pat = re.compile(r'["\']?t["\']?\s*[:=]\s*("(?:[^"\\]|\\.)*")', re.DOTALL)
    for n, m in enumerate(t_pat.finditer(text)):
        try:
            items.append({"i": n, "t": json.loads(m.group(1))})
        except Exception:  # noqa: BLE001
            continue
    return items


#: 降级分隔符格式：``###0### 译文`` 或 ``0. 译文`` 或 ``[0] 译文``
_DELIM_PATTERNS = (
    re.compile(r"^#{2,}\s*(\d+)\s*#{2,}\s*(.+)$", re.MULTILINE),
    re.compile(r"^\[(\d+)\]\s*(.+)$", re.MULTILINE),
    re.compile(r"^(\d+)\s*[.、:：]\s*(.+)$", re.MULTILINE),
)


def _parse_delimited(text: str) -> list[dict[str, Any]]:
    """最后一级：把"编号 → 译文"的纯文本按行切出来。"""
    for pat in _DELIM_PATTERNS:
        items: list[dict[str, Any]] = []
        for m in pat.finditer(text):
            idx = int(m.group(1))
            val = m.group(2).strip().strip('",')
            if val:
                items.append({"i": idx, "t": val})
        if len(items) >= 2:
            return items
    return []


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def _item_text(item: Any) -> str | None:
    """从单个条目里取译文（不借助任何索引）。"""
    if isinstance(item, str):
        return item if item.strip() else None
    if isinstance(item, dict):
        for key in ("t", "text", "translation", "译文"):
            got = _coerce_str(item.get(key))
            if got is not None and got.strip():
                return got
        return None
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return _coerce_str(item[1])
    return None


def _item_index(item: Any) -> int | None:
    if isinstance(item, dict):
        return _coerce_int(item.get("i", item.get("index", item.get("id"))))
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return _coerce_int(item[0])
    return None


def _looks_useful(value: Any) -> bool:
    """判断解析结果是不是"真的含有可用内容"。

    用来挡住括号配平截出的空壳片段。这类片段有两种形态：

    * ``{"i": 2}`` —— 能解析，但没有译文；
    * ``{"t": "b"}`` —— 有译文，但**没有索引**。

    第二种更隐蔽：它有文本，看起来"有用"，但调用方是靠 ``i`` 归位的，
    没有 ``i`` 的条目会被 :func:`to_translation_map` 直接丢弃，
    于是一整批译文全丢。所以这里要求条目**同时**具备译文和索引。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())

    if isinstance(value, dict):
        for key in ("translations", "result", "results", "data", "items", "output"):
            if key in value:
                return _looks_useful(value[key])
        # ★ `t` 的值是**容器**时也当包装键看。
        #
        # 实测：模型把单条的多行译文按"批"的格式裹在 `t` 下面 ——
        #
        #     {"t": {"0": "使用契约与恶魔签订后制作的盔甲。",
        #            "1": "提升所有能力，但恢复效果减半。"}}
        #
        # 这是**语法完全合法**的 JSON，`json.loads` 能解析；但 `_looks_useful`
        # 原先只认数字键/包装键，于是返回 False ⇒ 解析器**丢弃**它 ⇒
        # 掉到正则抢救也救不回来 ⇒ 报"解析得到空映射"、三次重试全败
        # ⇒ 整条失败。**能解析的 JSON 被判成没用**，这是本缺陷的真正断点。
        #
        # ⚠️ 只在值是容器时看 `t`：`{"t": "译文"}` 是**正常形态**
        # （`_item_text` 与单条路径都依赖它），字符串交给下面的
        # "有译文但没索引"判定即可，不能把它当包装键展开。
        inner = value.get("t")
        if isinstance(inner, (list, dict)):
            return _looks_useful(inner)
        k_numeric = [k for k in value if _coerce_int(k) is not None]
        if k_numeric:
            return any(_coerce_str(value[k]) not in (None, "") for k in k_numeric)
        return False

    if isinstance(value, (list, tuple)):
        if not value:
            return False
        # 全是字符串 → 裸数组，调用方会按出现顺序配索引（比整批丢弃好）
        if all(isinstance(x, str) for x in value):
            return any(x.strip() for x in value)
        return any(
            _item_text(x) is not None and _item_index(x) is not None for x in value
        )
    return False


def parse_json_loose(text: str) -> ParseResult:
    """尽最大努力从 ``text`` 里解析出 JSON。"""
    if not text or not text.strip():
        return ParseResult(notes=["模型返回空内容"])

    res = ParseResult()
    base_candidates: list[tuple[str, str]] = [("raw", text.strip())]
    stripped = _strip_fences(text)
    if stripped != text.strip():
        base_candidates.append(("fence", stripped))

    # 尝试 1~5 级：不同候选文本 × 不同解析器
    # 注意：必须先快照 base_candidates 再收集派生候选。
    # 直接在 `for label, cand in candidates` 里 append 到 candidates
    # 会让循环永远走不完（每轮又生成新的 balanced 候选）。
    candidates: list[tuple[str, str]] = list(base_candidates)
    for _label, cand in base_candidates:
        for arr_open, arr_close in (("[", "]"), ("{", "}")):
            balanced = _extract_balanced(cand, arr_open, arr_close)
            if balanced and balanced != cand:
                candidates.append((f"balanced{arr_open}", balanced))

    # 候选文本 × 解析器（原文 / 修补后）
    seen: set[str] = set()
    for label, cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        for final_label, payload in ((label, cand), (f"{label}+loose", _loose_fix(cand))):
            try:
                value = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            # 关键：括号配平可能截出一个"语法合法但内容残缺"的片段。
            # 例如 '[{"i":0,"t":"a"},{"i":1,"t":"b' 会截出 '{"i":1,"t":"b'
            # （不合法而跳过），但更狡猾的情况是截出 '{"i": 2}' 这种能解析、
            # 却没有 t 字段的空壳。若直接返回它，调用方会以为成功，
            # 实际上整批译文全丢了。所以这里先验证它确实能产出条目。
            if _looks_useful(value):
                res.value = value
                res.method = final_label
                res.ok = True
                return res

    # JSON5（可选依赖）
    try:
        import json5  # type: ignore[import-not-found]

        for label, cand in candidates[:4]:
            try:
                res.value = json5.loads(cand)
                res.method = f"{label}+json5"
                res.ok = True
                res.notes.append("使用了 JSON5 回退")
                return res
            except Exception:  # noqa: BLE001
                continue
    except ImportError:
        pass

    # 级别 6：逐项 salvage
    salvaged = _salvage_items(text)
    if salvaged:
        res.value = salvaged
        res.method = "salvage"
        res.ok = True
        res.notes.append(f"JSON 解析失败，正则抢救出 {len(salvaged)} 项")
        return res

    # 级别 7：分隔符模式
    delimited = _parse_delimited(text)
    if delimited:
        res.value = delimited
        res.method = "delimited"
        res.ok = True
        res.notes.append(f"JSON 解析失败，按分隔符解析出 {len(delimited)} 项")
        return res

    res.notes.append(f"所有解析策略均失败，原始输出前 200 字符：{text[:200]!r}")
    res.ok = False
    return res


def to_translation_map(
    value: Any,
    *,
    expect_indices: list[int] | None = None,
    sources: list[str] | None = None,
) -> dict[int, str]:
    """把解析结果规整成 ``{编号: 译文}``。

    支持多种形状：
    * ``[{"i":0,"t":"..."}]``  —— 首选
    * ``{"translations": [...]}`` / ``{"result": [...]}``
    * ``{"0": "...", "1": "..."}``
    * ``["译文0", "译文1"]``   —— 裸数组（按顺序，仅在无索引时使用）
    * ``{"原文": "译文", ...}`` —— **以原文为键的对象**

    最后一种不是我们要求的格式，但**实测 `translategemma:4b` 就是会这么回**：
    要求"返回 6 个对象的 JSON 数组"时它只回 ``{"i": 0, "t": "生命值"}``，
    而要求"每行 `编号<TAB>原文`"时它回
    ``{"HP": "生命值", "MP": "魔法值", ...}`` —— 完整且正确。

    所以这一层必须认原文键，否则一个 4B 专用翻译模型会被判成"翻译失败"。
    传 ``sources`` 后按"原文 → 编号"反查；查不到的键直接丢弃。
    """
    out: dict[int, str] = {}

    if value is None:
        return out

    # 解开常见的包装键
    if isinstance(value, dict):
        for key in ("translations", "result", "results", "data", "items", "output"):
            inner = value.get(key)
            if isinstance(inner, (list, dict)):
                value = inner
                break
        else:
            # ★ `{"t": {…}}` —— 模型把单条的多行译文按"批"的格式裹在 `t` 下。
            #
            # 实测形态（真实 E2E，`Dungeon And Darkness-Steam` 的盔甲说明）：
            #
            #     {"t": {"0": "使用契约与恶魔签订后制作的盔甲。",
            #            "1": "提升所有能力，但恢复效果减半。"}}
            #
            # 两处都得认它：
            #   * `_looks_useful` 认了 ⇒ 解析器才**不丢弃**这段合法 JSON；
            #   * 这里认了 ⇒ 才真的把它拆成 `{0: …, 1: …}`。
            # 只改一处都还是空映射（实测：只改 `_looks_useful` 仍是 `{}`）。
            #
            # ⚠️ **只在值是容器时**展开：`{"t": "译文"}` 是**正常形态**
            # （`_item_text` 与单条路径都依赖它），把字符串也当包装键展开
            # 会让正常译文变成 `{}` —— 这个错我踩过一次，见提交信息。
            inner = value.get("t")
            if isinstance(inner, (list, dict)):
                value = inner

    if isinstance(value, list):
        for pos, item in enumerate(value):
            if isinstance(item, dict):
                idx = _coerce_int(item.get("i", item.get("index", item.get("id"))))
                text = _coerce_str(
                    item.get("t", item.get("text", item.get("translation", item.get("译文"))))
                )
                if text is None:
                    continue
                if idx is None:
                    idx = expect_indices[pos] if expect_indices and pos < len(expect_indices) else pos
                out[idx] = text
            elif isinstance(item, str):
                idx = expect_indices[pos] if expect_indices and pos < len(expect_indices) else pos
                out[idx] = item
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                idx = _coerce_int(item[0])
                text = _coerce_str(item[1])
                if idx is not None and text is not None:
                    out[idx] = text

    elif isinstance(value, dict):
        src_index = _build_source_index(sources)
        for k, v in value.items():
            idx = _coerce_int(k)
            # ★ 把**这条的原文**传下去：数字键 map 的合成需要知道源文有几行
            #   （见 `_join_by_source_lines`）。`sources` 与 `expect_indices`
            #   是平行列表，所以用同一个下标取。
            src_text: str | None = None
            if sources and expect_indices and idx is not None:
                try:
                    src_text = sources[expect_indices.index(idx)]
                except ValueError:
                    src_text = None
            text = _coerce_str(v, source=src_text)
            if text is None and isinstance(v, dict):
                text = _coerce_str(
                    v.get("t", v.get("text", v.get("translation"))), source=src_text
                )
            if text is None:
                continue
            if idx is None:
                # 数字键失败 → 当成"原文作键"反查
                idx = _match_source(str(k), src_index)
            if idx is not None:
                out[idx] = text

    elif isinstance(value, str):
        out[0] = value

    return out


def _build_source_index(sources: list[str] | None) -> dict[str, list[int]]:
    """把 ``sources`` 做成 ``{原文: [编号...]}``。

    值是**列表**：同一批次里两条原文完全相同时（游戏里很常见），
    它们理应拿到同一份译文，所以一个键要能映射到多个编号，
    而不是后者覆盖前者。
    """
    idx: dict[str, list[int]] = {}
    if not sources:
        return idx
    for i, s in enumerate(sources):
        if not s:
            continue
        idx.setdefault(s, []).append(i)
    return idx


def _match_source(key: str, src_index: dict[str, list[int]]) -> int | None:
    """按原文反查编号：先精确匹配，再规范化匹配（空白折叠）。"""
    if not src_index:
        return None
    if key in src_index:
        return src_index[key][0]
    norm = " ".join(key.split())
    if not norm:
        return None
    for src, idxs in src_index.items():
        if " ".join(src.split()) == norm:
            return idxs[0]
    return None


def _coerce_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        if m:
            return int(m.group(0))
    return None


def _join_by_source_lines(values: list[str], source: str | None) -> str:
    r"""把多段译文合成一条，换行**只在源文确实有那么多行时**才插入。

    ## 为什么必须看源文（实测的坑）

    模型有时把**单条**的多行译文按"批"的格式回答：

        {"t": {"0": "使用契约与恶魔签订后制作的盔甲。",
               "1": "提升所有能力，但恢复效果减半。"}}

    最自然的修法是"把值用 `\n` 拼起来"。但 `guard()` 对行数的判定是
    **不对称**的（实测，`.scratch/_nested_guard.py`）：

    | 源→译文行数 | guard 结果 |
    | --- | --- |
    | 2 → 2 | 正常 |
    | 2 → 1 | **warn** `sentence_drop:2->1` |
    | **1 → 2** | **无警告（fatal=False）** |

    也就是说：源文**单行**时用 `\n` 拼，会**悄悄多出一行**且无人报警 ——
    比"整条失败并保留原文"更糟。所以：

    * 源文行数 == 段数 ⇒ 用 `\n` 拼（还原原本的换行结构）；
    * 否则 ⇒ 用**空串**拼（绝不凭空引入换行）。

    `source` 为 `None`（拿不到源文）时一律用空串 —— **保守优先**。
    """
    if not values:
        return ""
    if source is None:
        return "".join(values)
    src_lines = source.count("\n") + 1
    return ("\n" if src_lines == len(values) else "").join(values)


def _coerce_str(v: Any, *, source: str | None = None) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        # ★ 字符串里**还是一个数字键 map** 的情形。
        #
        # 为什么会有这一层：模型返回 `{"t": {"0": …, "1": …}}` 时
        # `parse_json_loose` 整体解析失败（嵌套结构 + 正则抢救），
        # 抢救出来的形态是 `{"t": '{"0": …, "1": …}'}` ——
        # **内层 map 变成了一个字符串**。实测（`.scratch/_nested_trace.py`）：
        #
        #     parse_json_loose('{"t": {"0": "甲。", "1": "乙。"}}')
        #       -> ok=False, value=None          ← 整体失败
        #     to_translation_map({"t": {"0":…,"1":…}})
        #       -> {}                            ← 丢空
        #
        # 只在"确实像个数字键 map"时才再解析一次，避免对正常译文动手。
        s = v.strip()
        if s.startswith("{") and s.endswith("}") and re.search(r'"\s*\d+\s*"\s*:', s):
            try:
                inner = json.loads(s)
            except (ValueError, TypeError):
                inner = None
            if isinstance(inner, dict):
                got = _coerce_str(inner, source=source)
                if got is not None:
                    return got
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        # 模型偶尔把一句拆成数组
        return "".join(str(x) for x in v)
    if isinstance(v, dict):
        for key in ("t", "text", "translation", "译文"):
            if key in v:
                inner = _coerce_str(v[key], source=source)
                if inner is not None:
                    return inner
        # ★ 数字键的 map：模型把**一条**的多行文本按"批"的格式回答。
        #
        # 实测形态（真实 E2E，`Dungeon And Darkness-Steam` 的盔甲说明）：
        #     {"t": {"0": "使用契约与恶魔签订后制作的盔甲。",
        #            "1": "提升所有能力，但恢复效果减半。"}}
        # 旧实现走到这里就返回 `None` ⇒ `to_translation_map` 丢弃该条
        # ⇒ `mapping` 为空 ⇒ 报"解析得到空映射"、三次重试全败
        # ⇒ 整条失败（保留原文）。
        #
        # 修的时候**必须**配合源文行数，理由见 `_join_by_source_lines`。
        #
        # ⚠️ 按**数字**排序，不能按字符串：`"10" < "2"` 在字符串序下成立，
        #   段数上两位时会把顺序弄乱。
        numeric = [k for k in v if _coerce_int(k) is not None]
        if numeric:
            numeric.sort(key=lambda k: _coerce_int(k))  # type: ignore[arg-type,return-value]
            strs = [
                p
                for p in (_coerce_str(v[k], source=None) for k in numeric)
                if isinstance(p, str)
            ]
            if strs:
                return _join_by_source_lines(strs, source)
        return None
    return None


def parse_translations(
    text: str,
    *,
    expect_indices: list[int] | None = None,
    sources: list[str] | None = None,
) -> tuple[dict[int, str], ParseResult]:
    """一步到位：解析 + 规整。返回 ``(编号→译文, 解析结果)``。

    ``sources`` 传本批次**屏蔽后的原文**（顺序即编号顺序），
    用于识别"模型以原文为键"的返回形态（见 :func:`to_translation_map`）。
    """
    res = parse_json_loose(text)
    return to_translation_map(res.value, expect_indices=expect_indices, sources=sources), res
