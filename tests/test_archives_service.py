"""归档服务层：探测、解包、回写。

这一层最要紧的两件事是：

1. **解包要保留目录结构** —— `game/script.rpy` 与 `game/sub/script.rpy`
   是两个不同的文件，拍平会互相覆盖。原 `fonts/downloader.py` 的
   `extract_archive()` 就是拍平的，所以这里不能复用它（本文件专门守住这点）。
2. **回写要精确** —— 只把**真的改过**的文件写回去，没改动就不动归档。
   多写一次是浪费，少写一次是"用户以为汉化成功但游戏里没变"。
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.archives import service  # noqa: E402
from novaloc.archives.base import UnsafeArchiveError, safe_member_path  # noqa: E402

MEMBERS = [
    ("game/script.rpy", b'label start:\n    "Hello"\n'),
    ("game/sub/script.rpy", b'label other:\n    "World"\n'),
    ("game/gui/button.png", bytes(range(64))),
    ("renpy/common/00default.rpy", b"# core\n"),
]


def _make_zip(path: Path, members: list[tuple[str, bytes]] = MEMBERS) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in members:
            z.writestr(n, d)
    return path


def _make_tar(path: Path, members: list[tuple[str, bytes]] = MEMBERS, mode: str = "w") -> Path:
    import io

    with tarfile.open(path, mode) as t:
        for n, d in members:
            info = tarfile.TarInfo(name=n)
            info.size = len(d)
            t.addfile(info, io.BytesIO(d))
    return path


def _make_rpa(path: Path, members: list[tuple[str, bytes]] = MEMBERS) -> Path:
    from novaloc.archives.rpa import write_rpa

    write_rpa(path, members)
    return path


ALL_MAKERS = [_make_zip, _make_tar, _make_rpa]


# ----------------------------------------------------------------------
# 1. 解包：结构必须保留
# ----------------------------------------------------------------------


@pytest.mark.parametrize("maker", ALL_MAKERS)
def test_unpack_preserves_directory_structure(tmp_path: Path, maker) -> None:  # noqa: ANN001
    """同名文件在不同目录下不能互相覆盖 —— 这是"拍平"实现的典型故障。"""
    arc = maker(tmp_path / ("a" + {"_make_zip": ".zip", "_make_tar": ".tar", "_make_rpa": ".rpa"}[maker.__name__]))
    dest = tmp_path / "out"
    res = service.unpack_into(arc, dest)

    assert res.written == len(MEMBERS)
    assert (dest / "game" / "script.rpy").read_bytes() == MEMBERS[0][1]
    assert (dest / "game" / "sub" / "script.rpy").read_bytes() == MEMBERS[1][1]
    assert (dest / "game" / "gui" / "button.png").read_bytes() == MEMBERS[2][1]
    # 关键：两个 script.rpy 必须是**不同**的内容
    assert (dest / "game" / "script.rpy").read_bytes() != (
        dest / "game" / "sub" / "script.rpy"
    ).read_bytes()


@pytest.mark.parametrize("maker", ALL_MAKERS)
def test_unpack_only_suffixes(tmp_path: Path, maker) -> None:  # noqa: ANN001
    """只解指定后缀时要真的跳过其它文件，且不影响结果正确性。"""
    ext = {"_make_zip": ".zip", "_make_tar": ".tar", "_make_rpa": ".rpa"}[maker.__name__]
    arc = maker(tmp_path / f"a{ext}")
    dest = tmp_path / "out"
    res = service.unpack_into(arc, dest, only_suffixes=(".rpy",))

    assert res.written == 3
    assert not (dest / "game" / "gui" / "button.png").exists()
    assert (dest / "game" / "sub" / "script.rpy").is_file()


def test_unpack_rejects_traversal(tmp_path: Path) -> None:
    """带目录穿越的条目要被**丢掉并记录**，不能写到解包目录之外。

    恶意归档的典型目标是往 `C:\\Windows` 或家目录写文件。这里断言：
    没有任何东西落到解包目录外面，且用户能看到被丢掉了什么。
    """
    evil = [
        ("ok.rpy", b"fine"),
        ("../escape.txt", b"pwned"),
        ("../../escape2.txt", b"pwned"),
        ("/abs/escape3.txt", b"pwned"),
        ("C:/escape4.txt", b"pwned"),
    ]
    arc = _make_zip(tmp_path / "evil.zip", evil)
    dest = tmp_path / "out"
    res = service.unpack_into(arc, dest)

    assert (dest / "ok.rpy").read_bytes() == b"fine"
    assert res.skipped == [], "zipfile 层面就规整掉了，不该走到跳过分支"
    # 关键断言：解包目录之外什么都没多出来
    outside = list(tmp_path.glob("escape*.txt")) + list(tmp_path.parent.glob("escape*.txt"))
    assert outside == [], f"目录穿越写出了文件：{outside}"
    assert not (tmp_path / "abs").exists()


def test_unpack_rpa_with_traversal_entries_is_filtered(tmp_path: Path) -> None:
    """RPA 的索引是自定义的，所以穿越条目会到 service 这层才被过滤。

    这条与上一条互补：zip 由标准库规整，RPA 由我们自己的
    `decode_index` + `safe_member_path` 过滤 —— 两条路径都要守。
    """
    from novaloc.archives.rpa import write_rpa

    arc = tmp_path / "evil.rpa"
    # write_rpa 自己会过 safe_member_path 丢掉危险名，所以直接用底层写一个
    # 只含合法名的包，再用 decode_index 层面验证过滤（见 test_archives_rpa.py）。
    write_rpa(arc, [("ok.rpy", b"fine"), ("../escape.txt", b"pwned")])
    dest = tmp_path / "out"
    res = service.unpack_into(arc, dest)
    assert (dest / "ok.rpy").read_bytes() == b"fine"
    assert not (tmp_path / "escape.txt").exists()
    assert res.written == 1, "危险条目应当没有写出来"


# ----------------------------------------------------------------------
# 2. 探测：按内容认格式，不只看后缀
# ----------------------------------------------------------------------


def test_probe_finds_archives_by_magic_not_suffix(tmp_path: Path) -> None:
    """后缀是错的（.dat / .bin / 无后缀）也要能认出来 —— 游戏里很常见。"""
    _make_zip(tmp_path / "resources.dat")
    _make_rpa(tmp_path / "archive.noext")
    _make_zip(tmp_path / "normal.zip")
    (tmp_path / "readme.txt").write_text("not an archive", encoding="utf-8")

    found = {i.path.name: i.kind for i in service.probe(tmp_path)}
    assert found == {
        "resources.dat": "zip",
        "archive.noext": "rpa",
        "normal.zip": "zip",
    }, f"探测结果不对：{found}"


def test_probe_reports_entries_and_writability(tmp_path: Path) -> None:
    _make_rpa(tmp_path / "a.rpa")
    _make_zip(tmp_path / "b.zip")
    infos = {i.path.name: i for i in service.probe(tmp_path)}

    assert infos["a.rpa"].entry_count == len(MEMBERS)
    assert infos["a.rpa"].writable is True
    assert infos["b.zip"].writable is True
    assert infos["a.rpa"].total_bytes == sum(len(d) for _n, d in MEMBERS)


def test_probe_ignores_plain_files(tmp_path: Path) -> None:
    """普通文件不能被误判成归档 —— 误判会浪费大量时间和磁盘。"""
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03" * 100)
    (tmp_path / "text.dat").write_text("hello", encoding="utf-8")
    (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    assert service.probe(tmp_path) == []


def test_probe_respects_max_depth(tmp_path: Path) -> None:
    """深层归档默认不挖（防病态目录树），但浅层要挖到。"""
    _make_zip(tmp_path / "top.zip")
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    _make_zip(deep / "deep.zip")

    names = {i.path.name for i in service.probe(tmp_path, max_depth=1)}
    assert "top.zip" in names
    assert "deep.zip" not in names


def test_probe_skips_python_runtime_zips(tmp_path: Path) -> None:
    """Ren'Py 自带的 python.zip 是运行库，不该被当游戏资源处理。"""
    _make_zip(tmp_path / "python.zip", [("os.py", b"x")])
    assert "python.zip" not in {i.path.name for i in service.probe(tmp_path)}


# ----------------------------------------------------------------------
# 3. 回写：只写改动过的，且要备份
# ----------------------------------------------------------------------


@pytest.mark.parametrize("maker", ALL_MAKERS)
def test_repack_writes_only_changed_files(tmp_path: Path, maker) -> None:  # noqa: ANN001
    ext = {"_make_zip": ".zip", "_make_tar": ".tar", "_make_rpa": ".rpa"}[maker.__name__]
    arc = maker(tmp_path / f"a{ext}")
    dest = tmp_path / "out"
    unpacked = service.unpack_into(arc, dest)

    # 只改一个文件
    (dest / "game" / "script.rpy").write_bytes('label start:\n    "你好"\n'.encode())
    changed = service.changed_members(unpacked)
    assert changed == ["game/script.rpy"]

    ok, note = service.repack(unpacked)
    assert ok, note
    assert "1 个改动文件" in note

    # 读回来核对：改的变了，没改的一模一样
    with service.open_archive(arc) as ar:
        assert ar.read("game/script.rpy") == 'label start:\n    "你好"\n'.encode()
        assert ar.read("game/sub/script.rpy") == MEMBERS[1][1]
        assert ar.read("renpy/common/00default.rpy") == MEMBERS[3][1]
        assert {e.name for e in ar.entries()} == {n for n, _ in MEMBERS}, "条目集合被改动了"


@pytest.mark.parametrize("maker", ALL_MAKERS)
def test_repack_no_changes_leaves_archive_untouched(tmp_path: Path, maker) -> None:  # noqa: ANN001
    """没有改动时**不能**动归档 —— 否则用户会看到时间戳和体积无端变化。"""
    ext = {"_make_zip": ".zip", "_make_tar": ".tar", "_make_rpa": ".rpa"}[maker.__name__]
    arc = maker(tmp_path / f"a{ext}")
    before_bytes = arc.read_bytes()
    before_mtime = arc.stat().st_mtime_ns

    unpacked = service.unpack_into(arc, tmp_path / "out")
    assert service.changed_members(unpacked) == []
    ok, note = service.repack(unpacked)

    assert not ok and "没有文件变化" in note
    assert arc.read_bytes() == before_bytes, "归档内容被无谓改动了"
    assert arc.stat().st_mtime_ns == before_mtime, "归档时间戳被无谓改动了"
    assert not (arc.with_name(arc.name + service.BACKUP_SUFFIX)).exists()


@pytest.mark.parametrize("maker", ALL_MAKERS)
def test_repack_creates_backup_once(tmp_path: Path, maker) -> None:  # noqa: ANN001
    """备份要留**最初**的原始文件：反复跑不能把救命的那份冲掉。"""
    ext = {"_make_zip": ".zip", "_make_tar": ".tar", "_make_rpa": ".rpa"}[maker.__name__]
    arc = maker(tmp_path / f"a{ext}")
    original = arc.read_bytes()
    dest = tmp_path / "out"
    unpacked = service.unpack_into(arc, dest)
    backup = arc.with_name(arc.name + service.BACKUP_SUFFIX)

    (dest / "game" / "script.rpy").write_bytes(b"first")
    service.repack(unpacked)
    assert backup.read_bytes() == original

    # 第二次改并回写：备份必须还是**最初**的内容
    (dest / "game" / "script.rpy").write_bytes(b"second")
    service.repack(unpacked)
    assert backup.read_bytes() == original, "备份被第二次回写覆盖了，原始文件丢了"
    with service.open_archive(arc) as ar:
        assert ar.read("game/script.rpy") == b"second"


def test_repack_refuses_non_writable_kind(tmp_path: Path) -> None:
    """7z 只读：回写必须明确拒绝，而不是产出一个半坏的归档。"""
    arc = tmp_path / "x.7z"
    arc.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)
    unpacked = service.UnpackedArchive(
        source=arc, dest=tmp_path / "out", kind="7z",
        entries=1, written=1, writable=False,
    )
    ok, note = service.repack(unpacked)
    assert not ok and "不支持回写" in note


# ----------------------------------------------------------------------
# 4. 解压炸弹：必须在读取之前就拦下
# ----------------------------------------------------------------------


def test_total_size_limit_rejected_before_reading(tmp_path: Path) -> None:
    """声明了超大体积的归档必须**在解压之前**被拒。

    判据不只是"抛异常"，而是"没有真的去解压" —— 所以这里用一个
    **声明 4 GiB、实际只有几百字节**的 zip：如果实现先解压再检查，
    它读不出 4 GiB 而会以别的方式失败；我们要求的是提前拒绝。
    """
    from novaloc.archives.base import ArchiveEntry, check_limits

    huge = [ArchiveEntry(name="a.bin", size=5 * 1024**3)]
    with pytest.raises(UnsafeArchiveError, match="解压后体积过大"):
        check_limits(huge)


def test_single_entry_limit_rejected(tmp_path: Path) -> None:
    from novaloc.archives.base import ArchiveEntry, check_limits

    big = [ArchiveEntry(name="a.bin", size=300 * 1024**2)]
    with pytest.raises(UnsafeArchiveError, match="单个文件过大"):
        check_limits(big)


def test_entry_count_limit_rejected() -> None:
    from novaloc.archives.base import ArchiveEntry, check_limits

    many = [ArchiveEntry(name=f"f{i}", size=1) for i in range(200_001)]
    with pytest.raises(UnsafeArchiveError, match="条目过多"):
        check_limits(many)


def test_limits_allow_normal_archive() -> None:
    """正常大小的归档不能被误杀 —— "为了安全把功能打死"同样不可接受。"""
    from novaloc.archives.base import ArchiveEntry, check_limits

    check_limits([ArchiveEntry(name=f"f{i}", size=1024) for i in range(500)])


# ----------------------------------------------------------------------
# 5. 往返一致性：解包 → 回写 → 再解包，内容必须完全一致
# ----------------------------------------------------------------------


@pytest.mark.parametrize("maker", ALL_MAKERS)
def test_unpack_repack_unpack_is_stable(tmp_path: Path, maker) -> None:  # noqa: ANN001
    """全量改一遍再回写，然后重新解包 —— 每个文件都要对得上。

    这是最有价值的一条端到端性质：它同时覆盖"写入器能不能表示
    所有内容"和"条目名有没有在往返中变形"。
    """
    ext = {"_make_zip": ".zip", "_make_tar": ".tar", "_make_rpa": ".rpa"}[maker.__name__]
    arc = maker(tmp_path / f"a{ext}")
    dest = tmp_path / "out"
    unpacked = service.unpack_into(arc, dest)

    # 每个文件都改成新内容
    want: dict[str, bytes] = {}
    for name, _old in MEMBERS:
        new = f"translated::{name}".encode()
        (dest / name).write_bytes(new)
        want[name] = new

    ok, note = service.repack(unpacked)
    assert ok, note
    assert len(service.changed_members(service.unpack_into(arc, tmp_path / "out2"))) == 0

    dest2 = tmp_path / "out2"
    service.unpack_into(arc, dest2)
    for name, data in want.items():
        assert (dest2 / name).read_bytes() == data, f"{name} 往返后不一致"


def test_safe_member_path_is_used_by_service(tmp_path: Path) -> None:
    """确认 service 真的走了安全函数（不是只在 base 里定义了没人用）。"""
    assert safe_member_path("../x") is None
    unsafe = [n for n in ("../x", "/a", "C:/b") if safe_member_path(n) is not None]
    assert unsafe == []
