"""`unity_patch` 的测试 —— 锁住"原地等长改写"的安全边界。

这些测试的每一条都对应一个**实测踩过的坑**，不是凭空设计的用例：

* `test_revert_is_byte_exact` —— 改写不可逆曾经真的发生过（38 处只还原 27 处），
  根因是再次读取时从容**变短**的长度前缀推出容量。这条测试是那个 bug 的哨兵。
* `test_does_not_touch_bytes_outside_slots` —— "改序列化资源"最可怕的失效是
  悄悄动了别的字节，然后游戏打不开。
* `test_refuses_when_translation_too_long` —— 空间不足时必须**拒绝**，
  绝不能截断（截断 = 静默丢内容，本项目最危险的失效形态）。
"""

from __future__ import annotations

import struct

from novaloc.engines.unity_patch import (
    align4,
    extract_slots,
    patch_translations,
    slot_at,
    write_slot,
)


def _mk_string(text: str, *, pad_to: int | None = None) -> bytes:
    """按 Unity 的格式造一个字符串：``<4字节长度><UTF-8><零填充>``。"""
    b = text.encode("utf-8")
    cap = align4(len(b)) if pad_to is None else pad_to
    return struct.pack("<I", len(b)) + b + b"\x00" * (cap - len(b))


# ---------------------------------------------------------------------------
# align4 / slot_at
# ---------------------------------------------------------------------------


def test_align4() -> None:
    assert align4(0) == 0
    assert align4(1) == 4
    assert align4(4) == 4
    assert align4(5) == 8


def test_slot_at_reads_length_prefixed_string() -> None:
    data = b"\xff\xff" + _mk_string("Hello there") + b"\xff\xff"
    s = slot_at(data, 2)
    assert s is not None
    assert s.text == "Hello there"
    assert s.capacity == align4(11)
    assert s.offset == 2


def test_slot_at_rejects_nul_inside_text() -> None:
    # 含 NUL 的"字符串"不是文本 —— 拒绝了才不会把二进制当文案改
    data = struct.pack("<I", 8) + b"ab\x00defg" + b"\x00\x00\x00\x00"
    assert slot_at(data, 0) is None


def test_slot_at_rejects_too_short_and_too_long() -> None:
    assert slot_at(struct.pack("<I", 3) + b"abc", 0) is None  # 短于 MIN_LEN
    assert slot_at(struct.pack("<I", 99999) + b"x" * 10, 0) is None  # 超出 MAX_LEN


def test_slot_at_rejects_bad_utf8() -> None:
    data = struct.pack("<I", 4) + b"\xff\xfe\xfd\xfc" + b"\x00\x00\x00\x00"
    assert slot_at(data, 0) is None


# ---------------------------------------------------------------------------
# 容量：核心不变量
# ---------------------------------------------------------------------------


def test_explicit_capacity_overrides_derived_one() -> None:
    """★ 这条是"改写不可逆"那个 bug 的哨兵。

    写过一个**更短**的译文之后，长度前缀变小了。如果再从文件里推导容量，
    推出来的就比真实区间小 —— 原文就再也写不回去了。
    所以调用方必须能把原始容量传进来。
    """
    original = "A fairly long English sentence."
    slot = slot_at(_mk_string(original), 0)
    assert slot is not None
    real_cap = slot.capacity

    # 模拟"已经写入了更短的中文"：
    # 长度前缀从 30 变成 6，后面留 0 —— 真实改写后文件就长这样。
    # （不能用 `_mk_string("中文")`：它把 4 字节长度前缀当成"字符串就这么长"，
    #   长度 4 恰好等于 MIN_LEN，是边界值，读出来的文本会带 NUL。）
    shorter = struct.pack("<I", 6) + "中文".encode() + b"\x00" * 26
    derived = slot_at(shorter, 0)
    assert derived is not None
    assert derived.capacity < real_cap, "前提：短译文的推导容量确实更小"

    fixed = slot_at(shorter, 0, capacity=real_cap)
    assert fixed is not None
    assert fixed.capacity == real_cap, "传了原始容量就必须用它"


def test_revert_is_byte_exact() -> None:
    """★ 写中文再写回英文，必须**逐字节**等于原文。

    实测曾经不成立（38 处只还原 27 处，1129 字节对不上），
    根因就是容量被重新推导。这条测试守住它。
    """
    parts = [
        _mk_string("The wise king and the beautiful queen."),
        b"\x01\x02\x03\x04",
        _mk_string("I wonder what history this piece might hold!"),
        b"\xde\xad\xbe\xef",
        # 注意：长度前缀必须 ≥ MIN_LEN(4)，且 `_looks_like_game_text` 对
        # 太短的串会拒掉，所以这里用一个明确像文本的短句。
        _mk_string("Alright then"),
    ]
    orig = b"".join(parts)

    slots = extract_slots(orig, filename="t.assets")
    assert len(slots) == 3

    caps = {("t.assets", s.offset): s.capacity for s in slots}

    # ① 全部换成中文（保证比英文短，才放得下）
    buf = bytearray(orig)
    done = 0
    for s in slots:
        if write_slot(buf, s, "中文译文"):
            done += 1
    assert done == 3
    mid = bytes(buf)
    assert len(mid) == len(orig)

    # ② 从**改后**的缓冲区读，带原始容量，写回英文
    buf2 = bytearray(mid)
    for s in slots:
        again = slot_at(mid, s.offset, capacity=caps[("t.assets", s.offset)])
        assert again is not None
        assert write_slot(buf2, again, s.text)

    assert bytes(buf2) == orig, "写回原文必须逐字节还原"


def test_without_original_capacity_revert_fails() -> None:
    """★ 反向哨兵：**不**传原始容量时，还原就该失败。

    这条测试把"容量必须由调用方给出"这个约束钉死 ——
    如果哪天有人"顺手"把 `capacities` 参数去掉并让测试仍然通过，
    说明这个约束已经被悄悄破坏，改写又变得不可逆了。
    """
    orig = _mk_string("The wise king and the beautiful queen.")
    slot = extract_slots(orig, filename="t.assets")[0]
    real_cap = slot.capacity

    buf = bytearray(orig)
    assert write_slot(buf, slot, "国王")  # 写入更短的译文
    mid = bytes(buf)

    # 不带原始容量 → 推导出的容量更小 → 装不回英文
    derived = slot_at(mid, slot.offset)
    assert derived is not None
    assert derived.capacity < real_cap

    buf2 = bytearray(mid)
    assert write_slot(buf2, derived, slot.text) is False
    assert bytes(buf2) != orig


# ---------------------------------------------------------------------------
# write_slot：绝不截断
# ---------------------------------------------------------------------------


def test_refuses_when_translation_too_long() -> None:
    """装不下就**拒绝**并返回 False —— 绝不截断。

    截断会让用户看到"翻译成功了"，而内容其实少了一半。
    这是本项目最危险的失效形态，必须有测试挡住。
    """
    # 长度前缀必须 ≥ MIN_LEN(4)，否则读不出 slot
    orig = _mk_string("Hello there")
    slot = slot_at(orig, 0)
    assert slot is not None
    buf = bytearray(orig)
    before = bytes(buf)

    too_long = "这是一个非常非常长的译文" * 3
    assert len(too_long.encode()) > slot.capacity, "前提：确实放不下"
    assert write_slot(buf, slot, too_long) is False
    assert bytes(buf) == before, "拒绝时不能改动任何字节"


def test_write_fills_remainder_with_zeros() -> None:
    """剩余空间补 0 —— 否则会残留旧字符串的尾巴，游戏读到脏数据。"""
    slot = slot_at(_mk_string("Hello World!"), 0)
    assert slot is not None
    buf = bytearray(_mk_string("Hello World!"))
    assert write_slot(buf, slot, "短")
    tail = bytes(buf[slot.offset + 4 + 3 : slot.region_end])
    assert tail == b"\x00" * len(tail)


# ---------------------------------------------------------------------------
# extract_slots：只收真文案
# ---------------------------------------------------------------------------


def test_extract_slots_skips_code_and_shader_paths() -> None:
    """像代码/着色器路径的东西必须被排除。

    ## 这条测试**不**声称"资源名都被排除了"

    实测：`Main Texture`、`Normal Map`、`Stadium Atlas Material` 这类
    **会**通过筛选（判据是"多词文本"，它们确实像）。
    这是**已知并有意的取舍**：宁可多收一点噪声，也不要漏掉真台词 ——
    "清单不全"是这份功能唯一价值所在，而多出来的条目用户能一眼扫掉。

    真正**必须**挡掉的是路径式/代码式的东西（`Hidden/Universal...`），
    因为它们在数量上能淹没清单。这条测试守的就是后者。
    """
    data = b"".join(
        [
            _mk_string("Hidden/Universal Render Pipeline/Sampling"),
            _mk_string("_MainTex"),
            _mk_string("UnityEngine.Rendering.Universal"),
            _mk_string("The wise king ruled the human world for many years."),
        ]
    )
    slots = extract_slots(data)
    texts = [s.text for s in slots]
    assert "The wise king ruled the human world for many years." in texts
    assert "Hidden/Universal Render Pipeline/Sampling" not in texts
    assert "_MainTex" not in texts
    assert "UnityEngine.Rendering.Universal" not in texts


# ---------------------------------------------------------------------------
# patch_translations：文件级行为
# ---------------------------------------------------------------------------


def test_patch_preserves_size_and_only_touches_slots(tmp_path) -> None:
    """★ 大小不变 + 区间外零改动。这是"游戏还能启动"的核心保证。"""
    good = _mk_string("The wise king ruled the human world for many years.")
    junk = b"\xde\xad\xbe\xef" * 4
    orig = junk + good + junk
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.assets").write_bytes(orig)

    slots = extract_slots(orig, filename="x.assets")
    assert slots
    s = slots[0]
    cap = s.capacity
    allowed = set(range(s.offset, s.region_end))

    rep = patch_translations(
        src,
        tmp_path / "out",
        rel_files={"x.assets"},
        translations={("x.assets", s.offset): "英明的国王统治人间多年。"},
        originals={("x.assets", s.offset): s.text},
        capacities={("x.assets", s.offset): cap},
    )
    assert rep.applied == 1
    assert not rep.errors

    new = (tmp_path / "out" / "x.assets").read_bytes()
    assert len(new) == len(orig), "文件大小必须不变"
    stray = [i for i in range(len(orig)) if orig[i] != new[i] and i not in allowed]
    assert stray == [], f"区间外被改动了：{stray[:5]}"


def test_patch_skips_when_original_mismatch(tmp_path) -> None:
    """偏移对不上抽取时的原文 → 跳过。

    用户换了游戏版本时，旧偏移会指向**另一段文本**。
    不校验就会把译文写进完全无关的地方，而游戏还能启动 ——
    于是没人发现。
    """
    orig = _mk_string("The wise king ruled the human world for many years.")
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.assets").write_bytes(orig)
    slots = extract_slots(orig, filename="x.assets")
    s = slots[0]

    rep = patch_translations(
        src,
        tmp_path / "out",
        rel_files={"x.assets"},
        translations={("x.assets", s.offset): "译文"},
        originals={("x.assets", s.offset): "完全不同的原文，说明版本变了"},
        capacities={("x.assets", s.offset): s.capacity},
    )
    assert rep.applied == 0
    assert rep.skipped_not_found == 1
    assert not (tmp_path / "out" / "x.assets").exists()


def test_patch_reports_no_room_instead_of_truncating(tmp_path) -> None:
    """空间不足要**如实上报**，让用户知道哪些没翻上。"""
    orig = _mk_string("Hi there!")
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.assets").write_bytes(orig)
    slots = extract_slots(orig, filename="x.assets")
    if not slots:
        # "Hi there!" 可能被判为不够像文案；换一个明确像句子的
        orig = _mk_string("Hello there, my friend!")
        (src / "x.assets").write_bytes(orig)
        slots = extract_slots(orig, filename="x.assets")
    assert slots
    s = slots[0]

    rep = patch_translations(
        src,
        tmp_path / "out",
        rel_files={"x.assets"},
        translations={("x.assets", s.offset): "这是一个明显太长的中文译文，肯定放不下" * 2},
        originals={("x.assets", s.offset): s.text},
        capacities={("x.assets", s.offset): s.capacity},
    )
    assert rep.applied == 0
    assert len(rep.skipped_no_room) == 1
    assert "容量" in rep.skipped_no_room[0]
    # 原文必须原封不动（没被截断）
    assert (src / "x.assets").read_bytes() == orig


def test_patch_dry_run_writes_nothing(tmp_path) -> None:
    orig = _mk_string("The wise king ruled the human world for many years.")
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.assets").write_bytes(orig)
    s = extract_slots(orig, filename="x.assets")[0]

    rep = patch_translations(
        src,
        tmp_path / "out",
        rel_files={"x.assets"},
        translations={("x.assets", s.offset): "国王统治人间。"},
        originals={("x.assets", s.offset): s.text},
        capacities={("x.assets", s.offset): s.capacity},
        dry_run=True,
    )
    assert rep.applied == 1  # 报告说"会改"
    assert rep.written == []  # 但没落盘
    assert not (tmp_path / "out" / "x.assets").exists()


# ---------------------------------------------------------------------------
# ★ 适配器层：偏移绝不能只靠实例状态传
# ---------------------------------------------------------------------------


def test_adapter_apply_works_without_instance_state(tmp_path) -> None:
    """★ 回归：`apply` 必须靠**传进来的 units**，不能只靠 `self._last_units`。

    实测踩过的坑：`stage_apply` 拿到的适配器实例与 `stage_extract` 的
    **不是同一个对象**，于是 `self._last_units` 是空的 —— 表现是
    "抽取到 3 条、翻译 3 条、回写 0 条"，最后报 `没有任何文件被写入`。

    这个坑很隐蔽：**直接调用永远是对的**（那里复用了同一个适配器对象），
    只有走完整流水线才暴露。所以这里刻意用一个**没跑过 extract 的新
    适配器**去调 `apply` —— 如果实现退回依赖实例状态，这条测试会红。
    """
    import struct

    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.engines.unity import UnityAdapter

    def mk(t: str) -> bytes:
        b = t.encode()
        cap = (len(b) + 3) & ~3
        return struct.pack("<I", len(b)) + b + b"\x00" * (cap - len(b))

    game = tmp_path / "MyGame"
    d = game / "MyGame_Data"
    (d / "Managed").mkdir(parents=True)
    (game / "UnityPlayer.dll").write_bytes(b"MZ" + b"\x00" * 32)
    (d / "Managed" / "Assembly-CSharp.dll").write_bytes(b"MZ" + b"\x00" * 32)
    (d / "globalgamemanagers").write_bytes(b"\x00" * 32 + b"2021.3.16f1" + b"\x00" * 16)
    (d / "resources.assets").write_bytes(
        b"\xAB" * 8 + mk("The wise king ruled the human world.") + b"\xCD" * 8
    )

    ctx = Context(config=Config(), events=EventBus(), logger=None)
    # ① 用一个适配器抽取
    extractor = UnityAdapter(ctx)
    units, _ = extractor.extract_text(game)
    assert units

    # ② 用**另一个全新**适配器回写（模拟 stage_apply 的真实情形）
    applier = UnityAdapter(ctx)
    assert not getattr(applier, "_last_units", None), "前提：新实例没有缓存"

    tr = {units[0].uid: "英明的国王统治人间。"}
    out = tmp_path / "out"
    res = applier.apply(game, out, units, tr)
    assert res.ok, res.error
    nb = (out / "MyGame_Data" / "resources.assets").read_bytes()
    assert "英明的国王统治人间。".encode() in nb, "新实例也必须能回写"
