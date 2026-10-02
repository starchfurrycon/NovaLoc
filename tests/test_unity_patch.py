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

import pytest

from novaloc.engines.unity_patch import (
    MAX_LEN,
    MIN_LEN,
    _candidate_offsets,
    _extract_slots_legacy,
    _extract_slots_reference,
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


# ---------------------------------------------------------------------------
# ★ 流水线层：换适配器实例后仍然必须回写
# ---------------------------------------------------------------------------


def test_pipeline_apply_patches_assets_after_fresh_adapter(tmp_path, monkeypatch) -> None:
    """★ 端到端回归：`extract` 与 `apply` 用**不同适配器实例**时仍要写出中文。

    这是上面那条适配器测试的**上游**版本，也是我更在意的形态 ——
    因为它走的是真实流水线（`stage_extract` → 手工塞译文 → `stage_apply`），
    而不是直接调适配器。实测那个 bug 就是在这里暴露的：
    `stage_apply` 报告的 `units=3 translations=3` 而回写 0 条，
    最后报 `[回写产物] 没有任何文件被写入`。

    **不调用翻译模型**：译文直接写进 `translations/entries.jsonl`，
    所以这条测试是确定性的、不需要 Ollama。
    """

    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.core.workspace import Workspace
    from novaloc.models import TranslationEntry
    from novaloc.pipeline.stages import Pipeline

    # 每个测试用独立数据根（`Workspace.create` 写在数据根下）
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("NOVALOC_DATA_ROOT", str(data_root))

    game = tmp_path / "MyGame"
    d = game / "MyGame_Data"
    (d / "Managed").mkdir(parents=True)
    (game / "UnityPlayer.dll").write_bytes(b"MZ" + b"\x00" * 32)
    (d / "Managed" / "Assembly-CSharp.dll").write_bytes(b"MZ" + b"\x00" * 32)
    (d / "globalgamemanagers").write_bytes(b"\x00" * 32 + b"2021.3.16f1" + b"\x00" * 16)
    assets = d / "resources.assets"
    assets.write_bytes(
        b"\xAB" * 16
        + _mk_string("The wise king ruled the human world for many years.")
        + b"\xCD" * 16
    )
    before = assets.read_bytes()

    ws = Workspace.create("MyGame", game, target_lang="zh-Hans")
    ws.save()
    ctx = Context(config=Config(), events=EventBus(), workspace=ws, logger=None)
    pipe = Pipeline(ws, ctx)

    r = pipe.stage_extract()
    assert r.ok, r.error
    units = ws.load_units()
    assert units, "前提下：抽取必须能读到 .assets 里的字符串"

    # 手工塞译文（**不调模型**，保持测试确定性）
    entries = [
        TranslationEntry(
            uid=u.uid, source=u.source, target="英明的国王统治了人间很多年。",
            status="translated", engine="unity",
        )
        for u in units
        if isinstance(u.location.byte_offset, int) and u.max_bytes
    ]
    assert entries, "前提：units 必须带 byte_offset 与 max_bytes"
    ws.save_entries(entries)

    r2 = pipe.stage_apply()
    assert r2.ok, f"apply 失败：{r2.error}"

    # ★ 注意写在哪：`stage_apply` **只写** `workspaces/<id>/out`，
    #   原游戏目录这时**一个字节都不该变** —— 覆盖原游戏是 `auto`
    #   写回阶段（备份之后）才做的事。这条纪律正是"游戏不会被工具
    #   悄悄改坏"的保证，所以两个地方都要断言。
    assert assets.read_bytes() == before, "原游戏目录必须原样（只读契约）"

    patched = ws.out_dir / "MyGame_Data" / "resources.assets"
    assert patched.is_file(), "out/ 里必须产出改过的资源"
    after = patched.read_bytes()

    assert len(after) == len(before), "文件大小必须不变"
    assert after[:16] == b"\xAB" * 16, "前哨兵必须原样"
    assert after[-16:] == b"\xCD" * 16, "后哨兵必须原样"
    assert "英明的国王统治了人间很多年。".encode() in after, "中文必须写进资源"
    assert b"The wise king" not in after, "原文应已被替换"

    # 区间外零改动：只有那条字符串的槽位允许变
    off = units[0].location.byte_offset
    cap = units[0].max_bytes
    assert isinstance(off, int) and isinstance(cap, int)
    allowed = set(range(off, off + 4 + cap))
    stray = [i for i in range(len(before)) if before[i] != after[i] and i not in allowed]
    assert not stray, f"槽位外被改动了：{stray[:8]}"


# ---------------------------------------------------------------------------
# ★ 性能优化必须**结果不变**
# ---------------------------------------------------------------------------
#
# `extract_slots` 原本对每个字节都调 `slot_at`，实测：
#
#   * `Robolife2_Data/resources.assets` 前 20 MB → 19,057,391 次调用，
#     其中长度前缀合法的只占 **2.58%**，扫描速度 **2.33 MB/s**；
#   * 按这个速度，`HolyKnightRicca`（9,410 MB 可扫文件）要 **1–2 小时**，
#     实测整轮盘点就卡在这种游戏上（240s 超时）。
#
# 优化成"先挑候选偏移（numpy 向量化），再逐个走原判据"后 **16.7 MB/s**。
#
# ⚠️ 性能优化最怕"快了但结果变了"，尤其是这种**回写游戏文件**的功能 ——
# 少认或多认一个槽位都会影响写回。所以这里对着**逐字节的参考实现**
# 逐项比对，包括偏移、容量、文本三项。


def _synthetic_assets(seed: int = 0, size: int = 20000) -> bytes:
    """造一段"像 .assets"的字节：夹杂真字符串、随机字节、伪长度前缀。

    必须包含会让优化实现出错的几种情况：
    * 合法字符串，且**后面紧跟着**另一个字符串（测推进语义）；
    * 长度前缀合法但内容**不是** UTF-8（decode 会失败）；
    * 长度前缀合法但内容含 NUL（必须被拒）；
    * 长度前缀**超出文件尾部**（越界边界）；
    * 随机字节，制造大量"前缀合法但内容不是文本"的假候选。
    """
    import random

    rnd = random.Random(seed)
    buf = bytearray()
    words = [
        "Hello world",
        "A wise king ruled the land for many years.",
        "Attack +5",
        "こんにちは世界",
        "x",
        "",  # 空串（长度 0，应被 MIN_LEN 拒掉）
        "a" * 40,
        "Mixed 中文 and English",
    ]
    i = 0
    while len(buf) < size:
        i += 1
        pick = rnd.random()
        if pick < 0.25:
            buf += _mk_string(words[i % len(words)])
        elif pick < 0.35:
            # 合法长度前缀但内容不是 UTF-8
            payload = bytes(rnd.randrange(0x80, 0x100) for _ in range(6))
            buf += struct.pack("<I", len(payload)) + payload
        elif pick < 0.45:
            # 合法长度前缀但内容含 NUL
            buf += struct.pack("<I", 6) + b"ab\x00cde"
        elif pick < 0.5:
            # 长度前缀指向文件外（越界）—— 放在最后
            buf += struct.pack("<I", MAX_LEN)
        else:
            buf += bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 12)))
    return bytes(buf[:size])


def _slot_tuples(slots: list) -> list[tuple[int, int, str]]:
    return [(s.offset, s.capacity, s.text) for s in slots]


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_optimized_extract_matches_reference(seed: int) -> None:
    r"""★ 候选筛选实现必须与"逐字节"参考实现**逐项一致**。

    比对三项：偏移、容量、文本。任何一项不同都说明优化改变了行为，
    而行为一变就会影响**回写游戏文件**（少认=漏翻，多认=可能改坏别的字节）。
    """
    data = _synthetic_assets(seed)
    new = extract_slots(data, filename="f")
    ref = _extract_slots_reference(data, filename="f")
    assert _slot_tuples(new) == _slot_tuples(ref), (
        f"seed={seed}: 优化实现与参考实现结果不一致\n"
        f"  新 {len(new)} 项，参考 {len(ref)} 项\n"
        f"  新独有 {list(set(_slot_tuples(new)) - set(_slot_tuples(ref)))[:3]}\n"
        f"  参独有 {list(set(_slot_tuples(ref)) - set(_slot_tuples(new)))[:3]}"
    )


def test_candidate_offsets_only_returns_plausible_prefixes() -> None:
    r"""候选偏移必须**只**包含长度前缀落在 `[MIN_LEN, MAX_LEN]` 的位置。

    这是优化的**正确性前提**：`slot_at` 对不合法前缀必然返回 None，
    所以只挑合法前缀不改变结果。若这条断言坏了，等价性就没有依据。
    """
    data = _synthetic_assets(3)
    cands = _candidate_offsets(data)
    assert cands == sorted(cands), "候选必须升序（推进逻辑依赖顺序）"
    assert len(cands) < len(data), "不该把所有偏移都当候选"
    for i in cands[:400]:
        (ln,) = struct.unpack_from("<I", data, i)
        assert MIN_LEN <= ln <= MAX_LEN, f"偏移 {i} 的前缀 {ln} 不在范围内"
    # 反过来：真槽位的位置必须都在候选里（否则会漏）
    for s in extract_slots(data, filename="f"):
        assert s.offset in set(cands), (
            f"真槽位偏移 {s.offset} 不在候选集合里 —— 优化会漏掉它"
        )


def test_extract_handles_empty_and_tiny_input() -> None:
    """空/极小输入不能崩（`_candidate_offsets` 有若干边界分支）。"""
    for data in (b"", b"\x01", b"\x04", b"\x04\x00\x00\x00", b"\xff" * 8):
        assert extract_slots(data, filename="f") == []
        assert _extract_slots_reference(data, filename="f") == []


def test_back_to_back_strings_are_both_found() -> None:
    r"""紧挨着的两个字符串必须都找到（测"推进语义"没被优化改坏）。

    原实现找到槽位后把游标推到 `region_end`。优化实现只挑候选位置，
    若推进逻辑写错，第二个紧邻的字符串就会被跳过。
    """
    data = _mk_string("First line") + _mk_string("Second line")
    got = [s.text for s in extract_slots(data, filename="f")]
    assert "First line" in got and "Second line" in got, f"漏了紧邻字符串：{got}"


# ---------------------------------------------------------------------------
# ★★ 旧生产实现**漏文本**（在写等价性用例时才发现）
# ---------------------------------------------------------------------------
#
# 旧实现把"推进到 `region_end`"写在了 `if keep:` **之外**：
#
#     slot = slot_at(data, i)
#     if slot is not None:
#         keep, ... = _looks_like_game_text(slot.text)
#         if keep:
#             out.append(slot)
#         i = max(slot.region_end, i + 4)     # ← keep 为假时也执行
#         continue
#
# 于是"内容不像游戏文案"的候选被拒之后，游标仍然跳过整段 ——
# 而真正要翻译的字符串可能就起始在这段区间**内部**。
#
# 实测 `_synthetic_assets(2)`：偏移 10807 有一个完全合法的字符串
# `'A wise king ruled the land for many years.'`（`keep=True conf=0.85`），
# 却因为偏移 10797 的 'ȳȪΓ' 被判 reject 并跳过整段而**永远检查不到**。
#
# 这是**原本就在丢文案**，不是性能优化引入的差异。


def test_new_impl_is_strict_superset_of_legacy() -> None:
    r"""★ 新实现必须**只多不少**于旧实现 —— "漏文本"不能再回来。

    这条是那个缺陷的哨兵。若有人把"被拒也跳过整段"写回去，
    这里会立刻红。
    """
    for seed in range(6):
        data = _synthetic_assets(seed)
        new = set(_slot_tuples(extract_slots(data, filename="f")))
        old = set(_slot_tuples(_extract_slots_legacy(data, filename="f")))
        missing = old - new
        assert not missing, (
            f"seed={seed}: 新实现比旧实现**少**了 {len(missing)} 项（漏文本回归）\n"
            f"  {sorted(missing)[:3]}"
        )


def test_legacy_impl_really_drops_a_real_string() -> None:
    r"""★ 把那个缺陷**钉成可复现的证据**（而不是只写在注释里）。

    若哪天旧实现被"修好"了（或测试数据变了以至于暴露不出问题），
    这条会失败并提醒复核：说明"严格超集"那条断言已经失去意义。
    """
    data = _synthetic_assets(2)
    new = set(_slot_tuples(extract_slots(data, filename="f")))
    old = set(_slot_tuples(_extract_slots_legacy(data, filename="f")))
    gained = new - old
    assert gained, (
        "这份数据没能暴露旧实现的漏文本缺陷 —— 测试数据需要加强，"
        "否则 `test_new_impl_is_strict_superset_of_legacy` 变成一句空话"
    )
    # 被漏掉的那一项必须真的是游戏文案（含多词/字母），
    # 不能是"多认了一堆垃圾"
    texts = [t for _o, _c, t in gained]
    assert any(" " in t and any(ch.isalpha() for ch in t) for t in texts), (
        f"新实现多认出来的不是正常文案：{texts[:4]}"
    )
    # 而且必须是**参考实现**（逐字节、已修缺陷）也认的
    ref = set(_slot_tuples(_extract_slots_reference(data, filename="f")))
    assert gained <= ref, "多认出来的项连参考实现都不认 —— 说明是误报"
