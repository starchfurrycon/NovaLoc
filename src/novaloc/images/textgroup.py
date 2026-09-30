"""把 OCR 切碎的文本块按语义合并成"一个逻辑句子"。

**为什么必须做这一步：**

PP-OCRv6 是按视觉行切块的，它不知道哪几块属于同一句话。实测一张
"HP 1250 / 3000    MP 480" 的贴图，OCR 会切成两个块：

    'HP 1250 / 3000'      'MP 480'

如果**逐块翻译**，模型看不到整句话，只能瞎猜：

    'HP 1250 / 3000'  →  "生命 1250 / 3000"    ← 还行
    'MP 480'          →  "MP 480"              ← 没上下文，不知道 MP 是"魔力"
                       或 "480 号议员"         ← 更糟

合并之后再翻译，模型看到完整一句，两个词都能译对。但**不能过度合并**：
如果一行里其实有"按钮文字"和"标题"两个独立 UI 元素，硬合成一句
会得到一段连在一起的译文，贴回去就串行了。

所以策略是**保守合并**：
* 同一视觉行（垂直方向重叠 > 70%）；
* 字高相近（相差 < 35%）；
* 水平相邻且间距不超过字高的 1.8 倍（超过就是两个独立区域）；
* 合并后的整行宽度不超过最长单块的 6 倍（防止把整条横幅都串起来）。

合并只影响**送去翻译的文本**与**回填译文的分配**；每个块的框、颜色、
字号仍然是自己的，所以贴回位置不受影响。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import ImageTextBlock

log = logging.getLogger(__name__)

#: 判定"同一行"的垂直重叠比例下限
SAME_LINE_OVERLAP = 0.70

#: 判定"字高相近"的相对差异上限
SAME_SIZE_TOLERANCE = 0.35

#: 水平间距 / 字高 的上限，超过就认为不是同一句
MAX_GAP_RATIO = 1.8

#: 合并后整行宽度 / 最长单块宽度 的上限
MAX_SPAN_RATIO = 6.0

#: 参与合并的块至少要这么多个字符。**这条很关键**：
#: "Load Save" 这种两三个字符的短标签，中文译文往往比源文还短
#: （"读取存档" 4 字 < "Load Save" 9 字符），合并后根本切不回去，
#: 会出现某个框拿到空字符串。这类块一律各自成组、单独翻译。
MIN_MERGE_CHARS = 4

#: 合并组里块数上限。再多就说明这行本来就是一串独立元素
MAX_MERGE_BLOCKS = 4


@dataclass
class TextGroup:
    """一组属于同一逻辑句子的文本块。"""

    blocks: list[ImageTextBlock] = field(default_factory=list)
    separator: str = " "
    """拼回整句时块之间的连接符。"""

    @property
    def source(self) -> str:
        """拼出来的完整句子（也是送去翻译的文本）。"""
        return self.separator.join(b.source.strip() for b in self.blocks if b.source.strip())

    @property
    def box(self) -> tuple[int, int, int, int]:
        """整组的包围盒。"""
        xs1 = [b.box[0] for b in self.blocks]
        ys1 = [b.box[1] for b in self.blocks]
        xs2 = [b.box[2] for b in self.blocks]
        ys2 = [b.box[3] for b in self.blocks]
        return (min(xs1), min(ys1), max(xs2), max(ys2)) if self.blocks else (0, 0, 0, 0)

    @property
    def is_single(self) -> bool:
        return len(self.blocks) <= 1

    @property
    def avg_height(self) -> float:
        if not self.blocks:
            return 0.0
        return sum(max(1, b.box[3] - b.box[1]) for b in self.blocks) / len(self.blocks)

    def __len__(self) -> int:
        return len(self.blocks)


def _overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """两个框在垂直方向的交叠比例（相对较矮的那个）。"""
    lo = max(a[1], b[1])
    hi = min(a[3], b[3])
    inter = max(0, hi - lo)
    h = min(max(1, a[3] - a[1]), max(1, b[3] - b[1]))
    return inter / h


def _size_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """两个字高的相对差异（0 = 完全一样）。"""
    ha, hb = max(1, a[3] - a[1]), max(1, b[3] - b[1])
    return abs(ha - hb) / max(ha, hb)


def _mergeable(blocks: list[ImageTextBlock]) -> bool:
    """这一组块是否"值得参与合并"。

    太短的标签不合并（见 :data:`MIN_MERGE_CHARS` 的说明）。
    纯数字块也不合并：它没有可翻译的语义，合并只会让它被别人的
    译文挤掉或挤歪。
    """
    for b in blocks:
        t = b.source.strip()
        if len(t) < MIN_MERGE_CHARS:
            return False
        if t.replace(" ", "").isdigit():
            return False
    return True


def group_blocks(
    blocks: list[ImageTextBlock],
    *,
    same_line_overlap: float = SAME_LINE_OVERLAP,
    max_gap_ratio: float = MAX_GAP_RATIO,
    max_span_ratio: float = MAX_SPAN_RATIO,
) -> list[TextGroup]:
    """把文本块合并成逻辑句子组。

    竖排文本**不参与**横向合并（它是竖着读的，横向拼会读错）。
    返回顺序保持与输入一致，方便和译文一一对应。
    """
    if not blocks:
        return []

    # 竖排单独成组，不横向合并
    horizontal = [b for b in blocks if not b.style.vertical]
    vertical = [b for b in blocks if b.style.vertical]

    if not horizontal:
        return [TextGroup(blocks=[b]) for b in blocks]

    # 先按"行"聚类：用垂直重叠把块分行
    rows: list[list[ImageTextBlock]] = []
    for b in sorted(horizontal, key=lambda x: (x.box[1], x.box[0])):
        placed = False
        for row in rows:
            if _overlap_ratio(row[0].box, b.box) >= same_line_overlap:
                row.append(b)
                placed = True
                break
        if not placed:
            rows.append([b])

    groups: list[TextGroup] = []
    for row in rows:
        row.sort(key=lambda x: x.box[0])
        cur: list[ImageTextBlock] = [row[0]]
        for b in row[1:]:
            prev = cur[-1]
            gap = b.box[0] - prev.box[2]
            h = max(1, min(prev.box[3] - prev.box[1], b.box[3] - b.box[1]))
            span = (b.box[2] - cur[0].box[0])
            widest = max(max(1, x.box[2] - x.box[0]) for x in cur)
            ok = (
                gap <= h * max_gap_ratio
                and _size_ratio(prev.box, b.box) <= SAME_SIZE_TOLERANCE
                and span <= widest * max_span_ratio
                and len(cur) < MAX_MERGE_BLOCKS
                and _mergeable(cur) and _mergeable([b])
            )
            if ok:
                cur.append(b)
            else:
                groups.append(TextGroup(blocks=cur))
                cur = [b]
        groups.append(TextGroup(blocks=cur))

    # 竖排块各自成组，按原顺序插回去
    for b in vertical:
        groups.append(TextGroup(blocks=[b]))

    # 恢复成"按 y 再按 x"的稳定顺序，与 OCR 输出顺序一致
    groups.sort(key=lambda g: (g.box[1], g.box[0]))
    merged = sum(1 for g in groups if not g.is_single)
    if merged:
        log.debug("文本分组合并了 %d 组多块句子", merged)
    return groups


def allocate_translation(group: TextGroup, translated: str) -> list[tuple[ImageTextBlock, str]]:
    """把整句译文**按比例**分回各个块。

    合并翻译换来的是准确度，代价是"一整句译文怎么分回两个框"。
    做法是先按源文本的字符占比定目标切点，再在**词边界**上找最接近目标的位置：

        'HP 1250 / 3000' (14 字) + 'MP 480' (6 字) = 20 字
        译文 '生命 1250 / 3000 魔力 480'，目标切点 = round(20 × 14/20) = 14
        位置 14 落在 '3000' 中间 → 不行
        候选词边界：'3000'|'魔力' 在 13，距离最小 ← 选它
        → '生命 1250 / 3000' 和 '魔力 480'

    **绝不丢字**：如果按比例分配会让某个块拿到空串（译文比源文短时很常见），
    就把整句译文给第一个块，其余块留空并标记出来 —— 丢字比排版难看严重得多。
    """
    if group.is_single:
        return [(group.blocks[0], translated)]

    texts = [b.source.strip() for b in group.blocks]
    total = sum(len(t) for t in texts)
    if total <= 0:
        return [(group.blocks[0], translated)]

    cuts = _cut_points(translated, len(group.blocks))

    pieces: list[str] = []
    prev = 0
    for i, _b in enumerate(group.blocks):
        if i == len(group.blocks) - 1:
            nxt = len(translated)
        else:
            cum = sum(len(t) for t in texts[: i + 1]) / total
            target = int(round(len(translated) * cum))
            nxt = _nearest_cut(cuts, target, prev)
        pieces.append(translated[prev:nxt].strip())
        prev = nxt

    # 有块拿到空串 → 分配失败，退化成"整句给第一块"
    if any(not p for p in pieces):
        log.debug(
            "译文分配退化为整句（译文 %d 字 / 源 %d 字，块数 %d）：%r",
            len(translated), total, len(group.blocks), group.source,
        )
        return [
            (b, translated if i == 0 else "")
            for i, b in enumerate(group.blocks)
        ]

    return list(zip(group.blocks, pieces))


def _cut_points(text: str, expected_parts: int) -> list[int]:
    """找出所有"词的边界"位置，供切分时挑选。"""
    if expected_parts <= 1 or not text:
        return [0, len(text)]
    cuts = {0, len(text)}
    for i in range(1, len(text)):
        prev, cur = text[i - 1], text[i]
        # 空白是天然边界
        if prev.isspace() or cur.isspace():
            cuts.add(i)
            continue
        # 数字与字母、数字与汉字的交界（避免把 "3000" 切开，
        # 同时允许 "3000|魔力" 这种边界）
        if prev.isdigit() != cur.isdigit():
            cuts.add(i)
            continue
        # 汉字与拉丁字母的交界
        if _is_han(prev) != _is_han(cur):
            cuts.add(i)
    return sorted(cuts)


def _nearest_cut(cuts: list[int], target: int, low: int) -> int:
    """在 ``low`` 之后挑一个离 ``target`` 最近的切点。"""
    candidates = [c for c in cuts if c > low]
    if not candidates:
        return target
    return min(candidates, key=lambda c: (abs(c - target), c))


def _is_han(ch: str) -> bool:
    return 0x3400 <= ord(ch) <= 0x9FFF or 0xF900 <= ord(ch) <= 0xFAFF


__all__ = ["MAX_GAP_RATIO", "SAME_LINE_OVERLAP", "TextGroup", "allocate_translation", "group_blocks"]
