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


def extract_slots(data: bytes, *, filename: str = "") -> list[StringSlot]:
    """扫出一个文件里所有**可原地替换**的文案区间。

    判据复用 :func:`unity_strings._looks_like_game_text`（保守优先）——
    否则 `Main Texture`、`Hidden/Universal…` 这类资源名会淹没结果。
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
            i = max(slot.region_end, i + 4)
            continue
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
