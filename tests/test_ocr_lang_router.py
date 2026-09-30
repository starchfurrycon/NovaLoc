"""OCR 分语种识别路由。

守住一个**真实的静默失败**：PP-OCRv6 的识别器不含韩语与西里尔文字，
对这类语言它不是"读得差"而是**读成空字符串** —— 与"这张图本来没有
文字"在结果上完全一样，所以流水线不会报错，用户只会觉得"汉化不了"。

本文件分两部分：

* **纯路由逻辑**（不需要模型/GPU，CI 里跑）：语种 → 模型的映射表；
* **真实识别对比**（标 `needs_models` + `needs_gpu`）：实测"默认读到空、
  路由后读对"。这部分不进 CI —— 它需要 13.5 MB 的韩语模型，
  但它是**唯一能证明改动有意义**的证据，所以必须存在。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.images import rec_models  # noqa: E402

# ----------------------------------------------------------------------
# 1. 路由逻辑（CI 里跑）
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "want"),
    [
        # v6 覆盖的语言 → 保持默认模型（None）
        ("zh", None), ("zh-Hans", None), ("zh-Hant", None), ("chinese", None),
        ("ja", None), ("japanese", None), ("jp", None),
        ("en", None), ("english", None),
        ("auto", None), ("", None), (None, None),
        # v6 **不覆盖**的语种 → 必须路由到 v5 分语种模型
        ("ko", "KOREAN"), ("ko-KR", "KOREAN"), ("korean", "KOREAN"),
        ("ru", "ESLAV"), ("russian", "ESLAV"),
        ("be", "ESLAV"), ("uk", "ESLAV"),
        ("bg", "CYRILLIC"), ("sr", "CYRILLIC"), ("cyrillic", "CYRILLIC"),
    ],
)
def test_language_routing_table(src, want) -> None:  # noqa: ANN001
    got = rec_models.resolve(src)
    assert (got.lang if got else None) == want, (
        f"{src!r} 应路由到 {want!r}，实际 {(got.lang if got else None)!r}"
    )


def test_korean_and_russian_are_not_left_on_v6() -> None:
    """这条是本次改动的**存在理由**，单独强化一次。

    如果哪天有人"简化"了路由表把 ko/ru 归回默认，这条会红 ——
    而症状（韩语识别成空）在别处很难被发现。
    """
    for src in ("ko", "ko-KR", "ru", "ru-RU"):
        model = rec_models.resolve(src)
        assert model is not None, f"{src} 必须路由到 v5 分语种模型，不能用 v6"


def test_router_does_not_regress_japanese_to_v5() -> None:
    """日文**必须**留在 v6：官方数据 90.5% vs v5 73.7%，差了 16.8 个点。

    很容易犯的错是"既然有 v5 分语种模型，那日文也换上吧" ——
    那会让日文识别明显变差。
    """
    for src in ("ja", "japanese", "jp", "ja-JP"):
        assert rec_models.resolve(src) is None, (
            f"{src} 应该用 PP-OCRv6（日文在 v6 上准确率高 16.8 个点）"
        )


@pytest.mark.parametrize(
    "src",
    ["KO", "ko_KR", "Ko-Kr", "  ko  ", "ko-KR-x-private"],
)
def test_routing_is_case_and_separator_insensitive(src: str) -> None:
    """真实配置里的写法很杂（大小写、下划线、带方言子标签）。

    这条守的是"用户填 `KO` 就不生效"这类问题 —— 功能在，但要用对写法才行，
    等于没做。
    """
    got = rec_models.resolve(src)
    assert got is not None and got.lang == "KOREAN", f"{src!r} 没被识别为韩语"


def test_downloadable_models_have_urls_and_hashes() -> None:
    """可下载的模型必须带 URL；有哈希的必须格式正确。

    没 URL 会在离线环境下变成一个"不知道怎么修"的错误。
    """
    for m in rec_models.DOWNLOADABLE:
        assert m.url.startswith("https://"), f"{m.lang} 没有可用的下载地址"
        assert m.filename.endswith(".onnx"), f"{m.lang} 文件名不像 ONNX"
        assert m.size > 0, f"{m.lang} 没记录大小，无法校验完整性"
        if m.sha256:
            assert len(m.sha256) == 64, f"{m.lang} 的 SHA256 长度不对"
            int(m.sha256, 16)  # 必须是合法十六进制


def _manifest_entries() -> dict[str, dict[str, str]]:
    """把 RapidOCR 的模型清单摊平成 ``{模型名: {model_dir, SHA256}}``。

    清单是三层嵌套（引擎 → 版本 → 任务 → 语种 → 模型 → 字段），
    直接按固定路径取会写错，所以这里递归收集任何带 `model_dir` 的字典。
    """
    import rapidocr
    import yaml

    manifest = yaml.safe_load(
        (Path(rapidocr.__file__).parent / "default_models.yaml").read_text(encoding="utf-8")
    )
    out: dict[str, dict[str, str]] = {}

    def walk(node) -> None:  # noqa: ANN001
        if isinstance(node, dict):
            if "model_dir" in node:
                name = Path(str(node["model_dir"])).name
                out[name] = {k: str(v) for k, v in node.items()}
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(manifest)
    return out


def test_model_sizes_match_the_published_manifest() -> None:
    """URL / SHA256 / 大小必须与 RapidOCR 官方清单一致。

    这是完整性校验的依据。写错会两头出问题：
    大小写大了会一直认为"文件不完整"而反复重下；
    校验和写错会在下载**成功**之后判定失败，把一个好文件删掉。
    """
    entries = _manifest_entries()
    checked = 0
    for m in rec_models.DOWNLOADABLE:
        assert m.filename in entries, (
            f"{m.filename} 不在 RapidOCR 的模型清单里 —— 文件名或版本写错了"
        )
        official = entries[m.filename]
        assert official["model_dir"] == m.url, (
            f"{m.filename} 的 URL 与官方清单不一致：\n"
            f"  我们的 = {m.url}\n  清单 = {official['model_dir']}"
        )
        if "SHA256" in official:
            assert official["SHA256"].lower() == m.sha256.lower(), (
                f"{m.filename} 的 SHA256 与官方清单不一致"
            )
        checked += 1
    assert checked == len(rec_models.DOWNLOADABLE) > 0


def test_is_present_rejects_truncated_file(tmp_path: Path) -> None:
    """半截文件必须被判定为"不在" —— 否则 RapidOCR 会抛一个
    与"文件不完整"毫无关系的加载错误，排查方向全错。
    """
    m = rec_models.KOREAN
    p = rec_models.model_path(m, tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * 100)  # 大小明显不对
    assert rec_models.is_present(m, tmp_path) is False

    p.write_bytes(b"\x00" * m.size)  # 大小对了（内容假，但 is_present 只查大小）
    assert rec_models.is_present(m, tmp_path) is True


def test_verify_sha256(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello")
    good = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert rec_models.verify_sha256(p, good) is True
    assert rec_models.verify_sha256(p, "00" * 32) is False
    # 没记录哈希时跳过校验，而不是一律判失败
    assert rec_models.verify_sha256(p, "") is True


def test_missing_model_error_is_actionable(tmp_path: Path) -> None:
    """离线缺模型时的错误必须**能照着做**：说清缺什么、放哪、去哪下。

    只说"模型缺失"等于把问题丢回给用户。
    """
    err = rec_models.ModelMissingError(rec_models.KOREAN, tmp_path / "x.onnx")
    msg = str(err)
    assert rec_models.KOREAN.filename in msg, "错误信息里没有模型文件名"
    assert str(tmp_path / "x.onnx") in msg, "错误信息里没有目标路径"
    assert "https://" in msg, "错误信息里没有下载地址"
    assert "韩语" in msg, "错误信息里没说清是哪个语种的问题"
    # 要解释**为什么**需要单独下，而不是让用户以为是 bug
    assert "PP-OCRv6" in msg


def test_ensure_model_refuses_download_when_disabled(tmp_path: Path) -> None:
    """`allow_download=False` 时必须**不去联网**，直接给可执行的错误。

    离线承诺必须在断网时也成立；"反正会失败所以先试试"会让断网环境
    卡在一个超时上，而不是立刻给出修复方法。
    """
    with pytest.raises(rec_models.ModelMissingError) as ei:
        rec_models.ensure_model(rec_models.KOREAN, tmp_path, allow_download=False)
    assert "下载地址" in str(ei.value)


# ----------------------------------------------------------------------
# 2. 真实识别对比（不进 CI）
# ----------------------------------------------------------------------


def _find_korean_font() -> Path | None:
    for c in (
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/gulim.ttc"),
        Path("C:/Windows/Fonts/batang.ttc"),
    ):
        if c.is_file():
            return c
    return None


@pytest.mark.needs_models
@pytest.mark.needs_gpu
def test_korean_is_unreadable_by_default_but_readable_when_routed(tmp_path: Path) -> None:
    """**本次改动的核心证据**：同一张韩语图，默认读到空，路由后读对。

    不断言"默认一定读空"（模型将来可能更新），但断言"路由后能读对"
    —— 后者才是我们承诺的能力。默认读空会作为附加信息打印出来。
    """
    font = _find_korean_font()
    if font is None:
        pytest.skip("没有含韩文字形的字体，无法造测试图")

    from PIL import Image, ImageDraw, ImageFont

    text = "게임 시작"
    img = Image.new("RGB", (420, 130), (250, 250, 250))
    ImageDraw.Draw(img).text((20, 25), text, font=ImageFont.truetype(str(font), 64),
                             fill=(20, 20, 20))
    img_path = tmp_path / "ko.png"
    img.save(img_path)

    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.images.ocr_ppocrv6 import PPOcrV6Engine

    def read_with(lang: str) -> list[str]:
        cfg = Config()
        cfg.ocr.lang = lang
        cfg.ocr.allow_model_download = True
        eng = PPOcrV6Engine(Context(config=cfg, events=EventBus()))
        page = eng.read(img_path)
        assert not page.error, f"{lang}: {page.error}"
        return [b.source for b in page.blocks]

    routed = read_with("ko")
    print(f"\n  默认（v6）读到：{read_with('')!r}")
    print(f"  路由（v5 korean）读到：{routed!r}")

    joined = " ".join(routed)
    for word in text.split():
        assert word in joined, (
            f"韩语路由后仍没读出 {word!r}（实际读到 {joined!r}）；"
            "说明语种路由没生效"
        )
