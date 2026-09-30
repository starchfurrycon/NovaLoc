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


def to_translation_map(value: Any, *, expect_indices: list[int] | None = None) -> dict[int, str]:
    """把解析结果规整成 ``{编号: 译文}``。

    支持多种形状：
    * ``[{"i":0,"t":"..."}]``  —— 首选
    * ``{"translations": [...]}`` / ``{"result": [...]}``
    * ``{"0": "...", "1": "..."}``
    * ``["译文0", "译文1"]``   —— 裸数组（按顺序，仅在无索引时使用）
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
        for k, v in value.items():
            idx = _coerce_int(k)
            if idx is None:
                continue
            text = _coerce_str(v)
            if text is None and isinstance(v, dict):
                text = _coerce_str(v.get("t", v.get("text", v.get("translation"))))
            if text is not None:
                out[idx] = text

    elif isinstance(value, str):
        out[0] = value

    return out


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


def _coerce_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        # 模型偶尔把一句拆成数组
        return "".join(str(x) for x in v)
    if isinstance(v, dict):
        for key in ("t", "text", "translation", "译文"):
            if key in v:
                return _coerce_str(v[key])
    return None


def parse_translations(text: str, *, expect_indices: list[int] | None = None) -> tuple[dict[int, str], ParseResult]:
    """一步到位：解析 + 规整。返回 ``(编号→译文, 解析结果)``。"""
    res = parse_json_loose(text)
    return to_translation_map(res.value, expect_indices=expect_indices), res
