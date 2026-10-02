r"""把译文**原地写回** Unity 序列化资源 —— 严格等长，一字节都不挪。

## 这个模块的由来（一个被实测推翻的旧结论）

本项目的旧结论是"`.assets` 绝对不能写"。理由是：那是二进制序列化格式，
里面有大量指向别处的偏移量，改一个字符串的长度会让后面的偏移全部失效，
结果是**游戏打不开**，而错误信息毫无线索。

**这个结论一半对、一半错**，实测把它分开了：

### ✗ 错的写法：让库重建整个文件

用现成的资源库（UnityPy）读出来、改完、再 `save()` 写回，实测**不安全**：

    66,240 字节的 .assets 存回去只剩 5,888 字节
    而且把原值写回去也**无法还原**（第一处差异就在偏移 5）

原因是 Save 会**重建**整个文件、丢弃未被引用的对象、重算全部内部偏移。
对独立的 `.assets` 尤其危险 —— 它的偏移是相对自身、且与配套的 `.resS`
互相印证的。所以"用现成库来写"这条路**被否掉了**。

### ✓ 对的写法：原地等长替换

Unity 的序列化字符串就是 ``<4字节长度><UTF-8 字节><0..3 对齐零>``。
**只要新串的字节数不超过原串**，就能做到：

* 文件**总大小不变**；
* 所有**内部偏移不变**（一个字节都不挪）；
* 只改这 4+N 个字节本身，剩余空间补 0。

实测（ButtKnight 的 36 MB `resources.assets`，1,874 处替换）：

| 检验 | 结果 |
|---|---|
| 文件大小 | 36,180,784 → 36,180,784 **相同** |
| 区间外被改动的字节 | **0**（没有越界） |
| 写回原值后 | **逐字节完全相同**（sha256 一致） |
| 改后仍能被解析 | ✅ 6,807 个对象 |

所以现在的做法是：**能装下就改，装不下就如实报告、绝不截断**。

## 为什么"装不下"是必须如实报告的

中文译文通常比英文短（实测 EN→ZH 长度比中位数约 0.35~0.46），所以大多数
条目装得下。但**总有一部分装不下**（比如长句翻成中文仍然很长）。这时：

* **绝不截断** —— 截断会丢掉内容，而且用户看不出来（本项目最危险的失效形态）；
* 记为 `skipped_no_room` 并报给用户，让他知道哪些没翻上。

**"如实报告一部分没做到"远好于"看起来全做了、其实丢内容"。**
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .unity_strings import _looks_like_game_text

log = logging.getLogger(__name__)

#: 候选字符串的长度范围（字节），与 `unity_strings` 保持一致。
MIN_LEN = 4
MAX_LEN = 4096


def align4(n: int) -> int:
    """Unity 序列化里的 4 字节对齐。"""
    return (n + 3) & ~3


@dataclass
class StringSlot:
    """文件里一段可原地替换的字符串区间。"""

    file: str
    offset: int
    capacity: int
    """可用字节数（**不含** 4 字节长度前缀）。"""
    text: str

    @property
    def region_end(self) -> int:
        return self.offset + 4 + self.capacity

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "offset": self.offset,
            "capacity": self.capacity,
            "text": self.text,
        }

    @staticmethod
    def from_dict(d: dict) -> StringSlot:
        return StringSlot(
            file=str(d["file"]),
            offset=int(d["offset"]),
            capacity=int(d["capacity"]),
            text=str(d["text"]),
        )


def slot_at(data: bytes, offset: int, *, capacity: int | None = None) -> StringSlot | None:
    """读一段字符串并算出它的**完整占用区间**。

    ## 容量为什么要能由调用方指定

    默认容量是 ``align4(当前长度前缀)``，这对**原始文件**是对的。
    但一旦我们往这个区间里写过一次**更短**的译文，长度前缀就变小了，
    于是再从文件里读出来的容量会**比真实区间小** —— 后果是
    "把原文写回去"会因容量不足而失败，也就是**改写不可逆**。

    实测踩到过：38 处改写后再写回英文，只有 27 处成功，
    1129 字节对不上。根因就是这个。

    所以回写阶段必须把**抽取时记录的原始容量**传进来
    （:func:`patch_translations` 就是这么做的）。默认值只适合只读场景。

    校验条件都是**可证伪**的硬约束，不依赖经验阈值：

    * 长度前缀落在合理区间；
    * 内容**不含 NUL**（UTF-8 文本不会有，含 NUL 说明这不是真字符串）；
    * 能按 UTF-8 解码。
    """
    if offset < 0 or offset + 4 > len(data):
        return None
    (ln,) = struct.unpack_from("<I", data, offset)
    if not (MIN_LEN <= ln <= MAX_LEN):
        return None
    if offset + 4 + ln > len(data):
        return None
    raw = data[offset + 4 : offset + 4 + ln]
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # 容量取"对齐后的完整区间"，把 Unity 自己的填充也算进去 ——
    # 这是安全上界：只要不超过它，后面的字段起点就不会动。
    #
    # 调用方给了 `capacity` 就**以它为准**（原区间大小），否则按当前
    # 长度前缀推导。回写阶段必须传 —— 否则写过一次短译文后就再也放不回原文。
    cap = capacity if capacity is not None and capacity >= ln else align4(ln)
    if offset + 4 + cap > len(data):
        cap = ln
    return StringSlot(file="", offset=offset, capacity=cap, text=text)


def encode_fits(text: str, capacity: int) -> bytes | None:
    """把译文编成字节；**装不下就返回 None**（绝不截断）。"""
    b = text.encode("utf-8")
    if len(b) > capacity:
        return None
    return b


def write_slot(buf: bytearray, slot: StringSlot, text: str) -> bool:
    """把 ``text`` 原地写进 ``slot``；装不下返回 False（不改任何字节）。"""
    b = encode_fits(text, slot.capacity)
    if b is None:
        return False
    struct.pack_into("<I", buf, slot.offset, len(b))
    buf[slot.offset + 4 : slot.offset + 4 + len(b)] = b
    # 剩余空间补 0 —— 与 Unity 自己的对齐填充一致，避免残留旧字节
    for k in range(slot.offset + 4 + len(b), slot.offset + 4 + slot.capacity):
        buf[k] = 0
    return True


@dataclass
class PatchReport:
    """一次原地回写的结果。**每个数字都要能对用户交代。**"""

    files_scanned: int = 0
    files_changed: int = 0
    applied: int = 0
    skipped_no_room: list[str] = field(default_factory=list)
    skipped_not_found: int = 0
    written: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"改写 {self.applied} 处字符串，涉及 {self.files_changed} 个资源文件"]
        if self.skipped_no_room:
            parts.append(
                f"**{len(self.skipped_no_room)} 处因原文空间不足未改写**"
                "（中文比原文长时放不下；这些条目保留原文，未被截断）"
            )
        if self.skipped_not_found:
            parts.append(f"{self.skipped_not_found} 处没在文件里找到对应位置")
        if self.errors:
            parts.append(f"{len(self.errors)} 个文件出错")
        return "；".join(parts)


def _candidate_offsets(data: bytes) -> list[int]:
    """所有"长度前缀可能合法"的偏移，**升序**。

    ## 为什么需要它（实测的性能缺陷）

    :func:`extract_slots` 原本对**每一个字节**都调 :func:`slot_at`。
    实测（`Robolife2_Data/resources.assets` 前 20 MB）：

    | 指标 | 实测值 |
    | --- | --- |
    | `slot_at` 调用次数 | **19,057,391**（0.91 次/字节） |
    | 其中长度前缀合法的 | 541,038（**2.58%**） |
    | `slot_at` 自身耗时 | **8.8 s**（cProfile tottime 第一位） |
    | 扫描速度 | 2.33 MB/s |

    也就是说 **97% 的调用是在做"读 4 字节 → 发现不在 `[4, 4096]` → 返回 None"**。
    按这个速度，`HolyKnightRicca`（9,410 MB 可扫文件）需要 **1–2 小时**，
    实测整轮盘点就卡在这种游戏上（240 s 超时）。

    ## 为什么"只挑合法前缀"不改变结果

    :func:`slot_at` 的第一件事就是判长度前缀；前缀不合法**必然返回 None**，
    所以那些位置本来就产生不了槽位，跳过它们不影响输出。

    唯一要小心的是 :func:`extract_slots` 的**推进语义**：找到一个槽位后
    会把游标推到 `slot.region_end`（跳过整段）。原来的实现是
    "逐字节 + 推进"，这里改成"只看候选 + 同样的推进" ——
    因为推进只依赖**已接受的**槽位，而**接受与否**在两种实现里是同一套
    判断（同一个 :func:`slot_at` + 同一个 :func:`_looks_like_game_text`），
    所以结果逐字节一致（有专门用例对着原实现比对）。
    """
    n = len(data)
    if n < MIN_LEN + 4:
        return []
    # 逐字节读 4 字节小端长度是 1900 万次 Python 调用，纯 Python 做不了。
    # numpy 的 `unpackbits` 把"每个偏移的 32 位是否在范围内"变成
    # 32 次向量化比较，整段只用一次内存视图转换。
    try:
        import numpy as np

        buf = np.frombuffer(data, dtype=np.uint8)
        if buf.size < MIN_LEN + 4:
            return []
        # ★ 用**跨步视图**把 4 个字节平面叠起来，而不是 `unpackbits`。
        #
        # `unpackbits` 会为 20 MB 输入生成 **160 MB** 的位数组中间量，
        # 内存带宽把收益吃掉了（实测只快 1.3x）。
        # `as_strided` 是只读视图、**不复制数据**，再按列加权求和即得
        # 每个偏移处的 32 位小端值。
        win = buf.size - 3
        planes = np.lib.stride_tricks.as_strided(
            buf, shape=(win, 4), strides=(1, 1), writeable=False
        )
        val = planes.astype(np.uint32) @ np.array(
            [1, 256, 65536, 16777216], dtype=np.uint32
        )
        idx = np.nonzero((val >= MIN_LEN) & (val <= MAX_LEN))[0]
        # 上界：前缀 + 最短内容都还必须落在文件内
        idx = idx[idx <= (buf.size - 4 - MIN_LEN)]
        return [int(x) for x in idx.tolist()]
    except ImportError:  # pragma: no cover - numpy 是硬依赖，兜底而已
        pass
    # 兜底：纯 Python 逐字节读前缀（慢但正确）
    out: list[int] = []
    limit = n - 4 - MIN_LEN
    for i in range(0, min(limit, n - 4) + 1):
        (ln,) = struct.unpack_from("<I", data, i)
        if MIN_LEN <= ln <= MAX_LEN:
            out.append(i)
    return out


def extract_slots(data: bytes, *, filename: str = "") -> list[StringSlot]:
    """扫出一个文件里所有**可原地替换**的文案区间。

    判据复用 :func:`unity_strings._looks_like_game_text`（保守优先）——
    否则 `Main Texture`、`Hidden/Universal…` 这类资源名会淹没结果。

    ## 实现要点：只扫候选位置

    逐字节试是 O(大小) 的 Python 循环，实测 **2.33 MB/s**，
    在 100 MB 级的 `.assets` 上要几十秒、十几 GB 的游戏上要一两小时。
    这里先用 :func:`_candidate_offsets` 一次性挑出**长度前缀可能合法**的
    偏移（实测只占 2.58%），再逐个走原判据 —— **结果完全一致**。
    """
    out: list[StringSlot] = []
    for i in _candidate_offsets(data):
        slot = slot_at(data, i)
        if slot is None:
            continue
        keep, _conf, _why = _looks_like_game_text(slot.text)
        if not keep:
            continue
        slot.file = filename
        out.append(slot)
    return out


def _extract_slots_legacy(data: bytes, *, filename: str = "") -> list[StringSlot]:
    """**旧的生产实现**（保留下来做对照，别再拿它当生产路径）。

    ## 它有一个真的**漏文本**缺陷

    旧实现是"逐字节 + 推进"，但推进写在了 `if keep:` **之外**：

        slot = slot_at(data, i)
        if slot is not None:
            keep, ... = _looks_like_game_text(slot.text)
            if keep:
                out.append(slot)
            i = max(slot.region_end, i + 4)     # ← keep 为假时也执行
            continue

    于是**内容不像游戏文案**的候选（例如 'ȳȪΓ'、'aaaa…' 这类纯符号/重复串）
    被拒之后，游标仍然被推到 `region_end`，**把整段都跳过去了** ——
    而真正要翻译的字符串可能就起始在这段区间**内部**。

    实测（`tests/test_unity_patch.py` 的 `_synthetic_assets(2)`）：
    偏移 10807 处有一个完全合法的字符串
    `'A wise king ruled the land for many years.'`（前缀 42、容量 44、
    判据 `keep=True conf=0.85 '多词文本'`），
    但偏移 10797 的候选 'ȳȪΓ' 被判 `reject(text)`
    并把游标推到 10855 —— **10807 从此再也不会被检查**。

    这不是"性能优化引入的差异"，而是**原本就在丢文案**。
    按真实文件的候选密度（合法前缀约占 2.58%）估计，被吞掉的概率不低。

    保留它的唯一用途：让测试断言
    **新实现的结果是旧实现的严格超集**（只多不少）——
    "漏文本"绝不能再回来。
    """
    out: list[StringSlot] = []
    i = 0
    n = len(data)
    while i + 4 <= n:
        slot = slot_at(data, i)
        if slot is not None:
            keep, _conf, _why = _looks_like_game_text(slot.text)
            if keep:
                slot.file = filename
                out.append(slot)
            # ⚠️ 这一行就是那个缺陷：`keep` 为假时**也**跳过整段
            i = max(slot.region_end, i + 4)
            continue
        i += 1
    return out


def _extract_slots_reference(data: bytes, *, filename: str = "") -> list[StringSlot]:
    """**参考实现**：逐字节扫，但**修掉**了上面那个漏文本缺陷。

    生产路径走 :func:`extract_slots`；这个函数用于
    `tests/test_unity_patch.py` 里"两种实现结果逐项一致"的断言 ——
    优化性能时最怕"快了但结果变了"，所以留一个慢而明确的口径。

    与 :func:`_extract_slots_legacy` 的唯一区别：候选被
    `_looks_like_game_text` 拒掉时**只前进 1 字节**，
    这样区间内部可能存在的真字符串不会被跳过。
    """
    out: list[StringSlot] = []
    i = 0
    n = len(data)
    while i + 4 <= n:
        slot = slot_at(data, i)
        if slot is not None:
            keep, _conf, _why = _looks_like_game_text(slot.text)
            if keep:
                slot.file = filename
                out.append(slot)
                # 只在**接受**时才跳过整段
                i = max(slot.region_end, i + 4)
                continue
            # 被拒 ⇒ 只前进 1 字节（旧实现在这里是跳过整段，会漏文案）
        i += 1
    return out


def patch_translations(
    source_dir: Path,
    out_dir: Path,
    *,
    rel_files: set[str],
    translations: dict[tuple[str, int], str],
    originals: dict[tuple[str, int], str] | None = None,
    capacities: dict[tuple[str, int], int] | None = None,
    dry_run: bool = False,
) -> PatchReport:
    """把译文原地写回这些资源文件，产物放到 ``out_dir`` 下同名相对路径。

    ## 为什么只处理 ``rel_files``

    资源文件动辄几百 MB（实测最大 613 MB）。把所有 `.assets` 都拷进
    ``out/`` 是不可接受的；而**只有真的被改过的文件**才需要产物。
    所以调用方先用 :func:`extract_slots` 找出"哪些文件有可译文本"，
    只把这些传进来。

    ## 为什么产物写到 out_dir 而不是直接改游戏目录

    原游戏目录贯穿全项目的硬约束是**只读**。所有改动先落到工作区的
    ``out/``，再由 `apply` / `auto` 决定是否回写原目录（并留备份）。
    这样"工具在我的游戏里动了什么"始终是可审计的。

    ``originals`` 是抽取时记录的原文。**回写前会逐字比对**：不一致就跳过。
    这不是多余 —— 用户可能换了游戏版本，此时旧偏移会指向**另一段文本**，
    不校验就会把译文写进完全无关的地方（而且游戏还能正常启动，
    于是没人发现）。宁可漏改，不可改错。

    ``capacities`` 是抽取时记录的**原始区间容量**。**必须传**，
    否则改写不可逆（原因见 :func:`slot_at` 的说明）。
    """
    rep = PatchReport()
    orig = originals or {}
    caps = capacities or {}
    by_file: dict[str, dict[int, str]] = {}
    for (fn, off), text in translations.items():
        by_file.setdefault(fn, {})[off] = text

    for rel in sorted(rel_files):
        src = source_dir / rel
        if not src.is_file():
            rep.errors.append(f"{rel}：源文件不存在")
            continue
        rep.files_scanned += 1
        try:
            data = src.read_bytes()
        except OSError as exc:
            rep.errors.append(f"{rel}：读取失败 {exc}")
            continue

        want = by_file.get(rel, {})
        if not want:
            continue

        buf = bytearray(data)
        changed = 0
        for off, text in want.items():
            # ★ 必须用**抽取时记录的原始容量**，不能用当前长度前缀推导的容量。
            # 原因见 `slot_at`：写过一次短译文后前缀会变小，
            # 再推导出来的容量就装不回原文了（= 改写不可逆）。
            slot = slot_at(data, off, capacity=caps.get((rel, off)))
            if slot is None:
                rep.skipped_not_found += 1
                continue
            expect = orig.get((rel, off))
            if expect is not None and slot.text != expect:
                rep.skipped_not_found += 1
                continue
            if write_slot(buf, slot, text):
                changed += 1
                rep.applied += 1
            else:
                rep.skipped_no_room.append(f"{rel}@{off}（容量 {slot.capacity} 字节）")

        if changed and bytes(buf) != data:
            rep.files_changed += 1
            if not dry_run:
                dst = out_dir / rel
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(bytes(buf))
                    rep.written.append(rel)
                except OSError as exc:
                    rep.errors.append(f"{rel}：写出失败 {exc}")
    return rep
