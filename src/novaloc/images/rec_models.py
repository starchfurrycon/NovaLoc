"""OCR 分语种识别模型的路由与按需下载。

## 要解决的问题（一个真实的"静默失败"）

本工具原先只用一个识别器：**PP-OCRv6 的 `multi` 模型**。它能认
简体中文、繁体、英文、日文和 46 种拉丁字母语言 —— 但
**完全不含韩语，也不含任何西里尔字母文字（俄语等）**。

这不是我们的 bug，是模型的覆盖边界，而且两边官方文档都写明了：

* PaddleOCR 的 PP-OCRv6 文档：识别支持 50 种语言 = 中/繁/英/日 + 46 种拉丁；
* RapidOCR 的模型清单对 v6 识别器的原话是"**不包括韩语**、阿拉伯语、
  藏语、彝族等语言"。

后果是最坏的一类：韩语/俄语游戏的贴图**识别结果为空**，流水线不报错
（"这张图没有文字"和"这张图有文字但读不出来"在结果上长得一样），
用户只会觉得"这游戏汉化不了"。所以必须按语种路由到正确的模型。

## 补法：检测用 v6，识别按语种换 v5

* **检测（定位文字框）**仍然用 PP-OCRv6 medium —— 它在旋转文本上
  Hmean 93.8，明显领先，没有理由动；
* **识别**按语种选：中日英拉丁继续用 v6；韩语/西里尔换成
  PP-OCRv5 的对应分语种模型。

本机 RapidOCR 的清单已核实（见 `.scratch/_probe_rapidocr_langs.py`）：

| 语族 | 模型 | 大小 |
| --- | --- | --- |
| 中/日/英/拉丁 | `multi_PP-OCRv6_rec_{tiny,small,medium}` | 4.5 / 21 / 77 MB |
| 韩语 | `korean_PP-OCRv5_rec_mobile` | 13.5 MB |
| 西里尔（含俄语） | `cyrillic_PP-OCRv5_rec_mobile` | 8.1 MB |
| 东斯拉夫（ru/be/uk） | `eslav_PP-OCRv5_rec_mobile` | 7.9 MB |
| 拉丁（备选） | `latin_PP-OCRv5_rec_mobile` | 7.9 MB |
| 英文（备选） | `en_PP-OCRv5_rec_mobile` | 7.9 MB |

补齐全部分语种只要 **约 45 MB** —— 相对识别质量的提升，这个代价可以忽略。

## 为什么"按需下载"而不是全部预置

默认只装中日英拉丁（一个 v6 模型）。用户是韩语游戏时才下 13.5 MB 的
韩语识别器。这样裸装体积不膨胀，且**离线可用性不受影响**（不下就还是
原来的能力，只是韩国游戏要下一次）。

下载走 `data_root` 下的模型目录，带 SHA256 校验 —— 校验失败会删掉重下，
避免"下了个半截文件然后一直报奇怪的错"。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: 模型下载源（RapidOCR 官方在 ModelScope 的固定版本目录）
_BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx"


@dataclass(frozen=True)
class RecModel:
    """一个识别模型：在哪、多大、校验和是多少。"""

    lang: str
    """RapidOCR 的 `LangRec` 成员名（如 `KOREAN`）。"""

    ocr_version: str
    """`PPOCRV5` / `PPOCRV6`。"""

    filename: str
    url: str
    sha256: str
    size: int

    @property
    def mb(self) -> float:
        return self.size / 1024 / 1024


#: 韩语：v6 完全没有，必须用 v5
KOREAN = RecModel(
    lang="KOREAN", ocr_version="PPOCRV5",
    filename="korean_PP-OCRv5_rec_mobile.onnx",
    url=f"{_BASE}/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx",
    sha256="cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b",
    size=13_488_748,
)

#: 西里尔（覆盖范围更广，含俄语、保加利亚语、塞尔维亚语等）
CYRILLIC = RecModel(
    lang="CYRILLIC", ocr_version="PPOCRV5",
    filename="cyrillic_PP-OCRv5_rec_mobile.onnx",
    url=f"{_BASE}/PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile.onnx",
    sha256="90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711",
    size=8_074_092,
)

#: 东斯拉夫（俄语/白俄/乌克兰）—— PaddleOCR 官方公布它比泛西里尔更准
ESLAV = RecModel(
    lang="ESLAV", ocr_version="PPOCRV5",
    filename="eslav_PP-OCRv5_rec_mobile.onnx",
    url=f"{_BASE}/PP-OCRv5/rec/eslav_PP-OCRv5_rec_mobile.onnx",
    sha256="08705d6721849b1347d26187f15a5e362c431963a2a62bfff4feac578c489aab",
    size=7_911_802,
)

JAPAN = RecModel(
    lang="JAPAN", ocr_version="PPOCRV5",
    filename="japan_PP-OCRv5_rec_mobile.onnx",
    url=f"{_BASE}/PP-OCRv5/rec/japan_PP-OCRv5_rec_mobile.onnx",
    sha256="",
    size=0,
)  # 占位：日文用 v6 更好（90.5% vs v5 73.7%），刻意不用 v5

LATIN = RecModel(
    lang="LATIN", ocr_version="PPOCRV5",
    filename="latin_PP-OCRv5_rec_mobile.onnx",
    url=f"{_BASE}/PP-OCRv5/rec/latin_PP-OCRv5_rec_mobile.onnx",
    sha256="b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a",
    size=7_904_513,
)

EN = RecModel(
    lang="EN", ocr_version="PPOCRV5",
    filename="en_PP-OCRv5_rec_mobile.onnx",
    url=f"{_BASE}/PP-OCRv5/rec/en_PP-OCRv5_rec_mobile.onnx",
    sha256="c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8",
    size=7_872_351,
)


#: 源语言 → 该用哪个识别模型。``None`` 表示"用默认的 PP-OCRv6 multi"。
#:
#: 键兼容 BCP-47（`ko`、`ko-KR`）与常见别名（`korean`、`ru`），
#: 因为用户填的和配置里存的写法都不统一。
LANG_TO_MODEL: dict[str, RecModel | None] = {
    # 中日英拉丁 → v6（日文在 v6 上强很多：90.5% vs 73.7%）
    "zh": None, "zh-hans": None, "zh-hant": None, "zh-cn": None, "zh-tw": None,
    "chinese": None, "cn": None,
    "ja": None, "japanese": None, "jp": None,
    "en": None, "english": None,
    "auto": None,
    # 韩语 → v5 korean
    "ko": KOREAN, "ko-kr": KOREAN, "korean": KOREAN, "kr": KOREAN,
    # 俄语 → v5 eslav（官方数据 ru/be/uk 更准）
    "ru": ESLAV, "russian": ESLAV, "ru-ru": ESLAV,
    "be": ESLAV, "belarusian": ESLAV,
    "uk": ESLAV, "ukrainian": ESLAV,
    # 其它西里尔 → v5 cyrillic
    "bg": CYRILLIC, "bulgarian": CYRILLIC,
    "sr": CYRILLIC, "serbian": CYRILLIC,
    "mk": CYRILLIC, "macedonian": CYRILLIC,
    "cyrillic": CYRILLIC,
    # 拉丁语族想更专精时可选（一般 v6 已够）
    "latin": LATIN,
    # 明确点名英文模型
    "en-strict": EN,
}

#: 需要按需下载的模型（去重后的集合）
DOWNLOADABLE: tuple[RecModel, ...] = (KOREAN, CYRILLIC, ESLAV, LATIN, EN)


def resolve(src_lang: str | None) -> RecModel | None:
    """把源语言映射到识别模型；``None`` 表示用默认的 PP-OCRv6 multi。"""
    if not src_lang:
        return None
    key = str(src_lang).strip().lower().replace("_", "-")
    if key in LANG_TO_MODEL:
        return LANG_TO_MODEL[key]
    # 取主语言子标签再试一次（`ko-KR-x-foo` → `ko`）
    head = key.split("-")[0]
    return LANG_TO_MODEL.get(head)


def models_dir(models_root: Path) -> Path:
    """``models_root`` 是 `<data_root>/models`（即 `paths.models_dir()`）。"""
    return Path(models_root) / "rapidocr" / "rec"


def model_path(model: RecModel, models_root: Path) -> Path:
    return models_dir(models_root) / model.filename


def is_present(model: RecModel, models_root: Path) -> bool:
    p = model_path(model, models_root)
    if not p.is_file():
        return False
    # 大小对不上说明是半截文件 —— 宁可重下，也不要让 RapidOCR 抛一个
    # 与"文件不完整"毫无关系的加载错误
    return not (model.size and p.stat().st_size != model.size)


def verify_sha256(path: Path, want: str) -> bool:
    """校验文件摘要。``want`` 为空表示该模型没记录摘要（跳过校验）。"""
    if not want:
        return True
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower() == want.lower()


def ensure_model(model: RecModel, models_root: Path, *, allow_download: bool = False) -> Path:
    """确保模型文件就位，返回它的路径。

    下载失败/校验失败会抛 :class:`ModelMissingError`，消息里带**可直接执行
    的修复指令**（下载链接），而不是只说"模型没装"。
    """
    path = model_path(model, models_root)
    if path.is_file() and model.size and path.stat().st_size == model.size:
        if verify_sha256(path, model.sha256):
            return path
        log.warning("识别模型 %s 校验不通过，将重新下载", path.name)
        path.unlink(missing_ok=True)

    if not allow_download:
        raise ModelMissingError(model, path)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        _download(model.url, tmp)
        if model.size and tmp.stat().st_size != model.size:
            raise ModelMissingError(
                model, path,
                f"下载不完整（期望 {model.size} 字节，实际 {tmp.stat().st_size}）",
            )
        if not verify_sha256(tmp, model.sha256):
            raise ModelMissingError(model, path, "下载后 SHA256 校验不通过")
        tmp.replace(path)
    except ModelMissingError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise ModelMissingError(model, path, str(exc)) from exc
    return path


def _download(url: str, dest: Path) -> None:
    """流式下载。用 httpx（项目已有依赖），带进度日志。"""
    import httpx

    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
                done += len(chunk)
        log.info("已下载 %s（%.1f MB）", dest.name, done / 1024 / 1024)
        if total and done != total:
            raise RuntimeError(f"下载被截断：{done}/{total} 字节")


class ModelMissingError(RuntimeError):
    """识别模型缺失，需要下载。"""

    def __init__(self, model: RecModel, path: Path, detail: str = "") -> None:
        self.model = model
        self.path = path
        msg = (
            f"缺少「{model.lang}」语种的 OCR 识别模型（{model.filename}）。\n"
            f"原因：PP-OCRv6 的识别器**不含韩语与西里尔文字**，"
            f"这类语言必须换成 PP-OCRv5 的分语种模型。\n"
            f"需要放到：{path}\n"
            f"下载地址：{model.url}"
        )
        if detail:
            msg += f"\n详情：{detail}"
        msg += (
            "\n\n修复方式（任选其一）：\n"
            f"  1. 放到上面那个路径（约 {model.mb:.1f} MB）；\n"
            "  2. 让工具自己下载：把 `ocr.allow_model_download` 设为 true 后重跑；\n"
            "  3. 如果是识别错误，可在设置里把 `ocr.lang` 改回 `auto` 用默认模型。"
        )
        super().__init__(msg)


__all__ = [
    "CYRILLIC",
    "DOWNLOADABLE",
    "EN",
    "ESLAV",
    "KOREAN",
    "LATIN",
    "LANG_TO_MODEL",
    "ModelMissingError",
    "RecModel",
    "ensure_model",
    "is_present",
    "model_path",
    "models_dir",
    "resolve",
    "verify_sha256",
]
