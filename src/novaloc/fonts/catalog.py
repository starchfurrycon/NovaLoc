"""中文字体目录：只收录**可再分发**的开源字体。

许可红线（务必遵守）：

* ✅ SIL OFL 1.1 / Apache-2.0 —— 可随本工具分发、可修改、可嵌入产物。
* ❌ 微软雅黑 / 方正 / 汉仪 / 造字工房 / MiSans / HarmonyOS Sans 等
  —— 许可是"免费使用"，不是"免费再分发"，**不得打包进发行版**。
  本目录里只登记 ``bundled=False`` 且需要用户自行确认的条目，或者干脆不登记。

字体文件一律下载到 ``<data_root>/fonts``，不放进仓库（避免仓库体积爆炸）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

License = Literal["OFL-1.1", "Apache-2.0", "MIT", "IPA-1.0", "Public-Domain"]

#: 允许随发行版一起打包的许可。IPA-1.0 不在其中（其条款不允许再许可，
#: 只做"本地渲染/嵌入产物"是安全的，但把字体文件打进安装包有风险）。
BUNDLE_OK_LICENSES: frozenset[str] = frozenset(
    {"OFL-1.1", "Apache-2.0", "MIT", "Public-Domain"}
)


@dataclass(frozen=True)
class FontSpec:
    id: str
    family: str
    """字体内部家族名（name table 里的 family）。"""

    display_zh: str
    license: License
    homepage: str
    # 直接可用的 TTF/OTF 直链；为空表示只能整包下载
    direct_url: str = ""
    # 压缩包下载地址与包内需要提取的文件名
    archive_url: str = ""
    archive_member: str = ""
    archive_kind: Literal["zip", "7z", "tar.gz"] = "zip"
    approx_mb: float = 0.0
    weights: tuple[str, ...] = ("Regular",)
    coverage: Literal["full", "ext", "subset", "artistic"] = "full"
    """full=GB18030 级全量；ext=含 CJK 扩展；subset=仅常用字；artistic=展示用。"""

    use_cases: tuple[str, ...] = ()
    """body/ui/display/dialogue/pixel/artistic —— 供字体匹配器打分。"""

    note: str = ""
    verified: bool = True
    """URL 是否经过实际校验。"""

    reserved_font_names: tuple[str, ...] = ()
    """OFL 保留字体名（RFN）。子集化 = Modified Version，产物**不得**再用这些名字。"""

    bundle_ok: bool = True
    """是否允许随安装包分发字体文件本身。"""

    glyph_count: int = 0
    """实测（或上游公布）的字形数。接近 65535 时不能再合并，必须先子集化。"""


CATALOG: tuple[FontSpec, ...] = (
    # ---------------- 黑体（正文/界面首选） ----------------
    FontSpec(
        id="source-han-sans-sc",
        family="Source Han Sans SC",
        display_zh="思源黑体 SC",
        license="OFL-1.1",
        homepage="https://github.com/adobe-fonts/source-han-sans",
        archive_url="https://github.com/adobe-fonts/source-han-sans/releases/download/2.005R/05_SourceHanSansSubsetOTF.zip",
        archive_kind="zip",
        approx_mb=173.0,
        weights=("ExtraLight", "Light", "Normal", "Regular", "Medium", "Bold", "Heavy"),
        coverage="full",
        use_cases=("body", "ui", "display"),
        note="字重齐全（7 档），是复刻原字体粗细的主力。整包较大，只解压需要的字重。",
    ),
    FontSpec(
        id="lxgw-neo-xihei",
        family="LXGW Neo XiHei",
        display_zh="霞鹜新晰黑",
        # 注意：不是 OFL，而是 IPA Font License 1.0（不可再许可）。
        license="IPA-1.0",
        homepage="https://github.com/lxgw/LxgwNeoXiHei",
        direct_url="https://github.com/lxgw/LxgwNeoXiHei/releases/download/v1.305/LXGWNeoXiHeiPlus.ttf",
        approx_mb=9.0,
        glyph_count=28000,
        coverage="full",
        use_cases=("ui", "body"),
        note=(
            "体积小（9 MB）但覆盖常用字，适合做轻量默认值。"
            "许可为 IPA-1.0：可本地使用与渲染，但把字体文件打进安装包有风险，"
            "默认标记为不可捆绑。"
        ),
        bundle_ok=False,
    ),
    FontSpec(
        id="sarasa-gothic-sc",
        family="Sarasa Gothic SC",
        display_zh="更纱黑体 SC",
        license="OFL-1.1",
        homepage="https://github.com/be5invis/Sarasa-Gothic",
        archive_url="https://github.com/be5invis/Sarasa-Gothic/releases/download/v1.0.42/SarasaGothicSC-TTF-Unhinted-1.0.42.7z",
        archive_kind="7z",
        approx_mb=48.0,
        weights=("ExtraLight", "Light", "Regular", "SemiBold", "Bold"),
        coverage="full",
        use_cases=("ui",),
        note="等宽拉丁 + 全宽中文对齐，界面表格类文本最整齐。需要 7z 解压。",
    ),
    # ---------------- 楷/宋（对白、旁白） ----------------
    FontSpec(
        id="lxgw-wenkai-screen",
        family="LXGW WenKai Screen",
        display_zh="霞鹜文楷 屏幕阅读版",
        license="OFL-1.1",
        homepage="https://github.com/lxgw/LxgwWenKai-Screen",
        direct_url="https://github.com/lxgw/LxgwWenKai-Screen/releases/download/v1.522/LXGWWenKaiGBScreen.ttf",
        approx_mb=26.0,
        coverage="full",
        use_cases=("dialogue", "body"),
        note="专为屏幕小字号调优，比原版文楷在 12~16px 下更清晰。",
    ),
    FontSpec(
        id="lxgw-wenkai",
        family="LXGW WenKai",
        display_zh="霞鹜文楷",
        license="OFL-1.1",
        homepage="https://github.com/lxgw/LxgwWenKai",
        direct_url="https://github.com/lxgw/LxgwWenKai/releases/download/v1.522/LXGWWenKai-Regular.ttf",
        approx_mb=25.0,
        weights=("Light", "Regular", "Medium"),
        coverage="full",
        use_cases=("dialogue", "body"),
    ),
    FontSpec(
        id="source-han-serif-sc",
        family="Source Han Serif SC",
        display_zh="思源宋体 SC",
        license="OFL-1.1",
        homepage="https://github.com/adobe-fonts/source-han-serif",
        archive_url="https://github.com/adobe-fonts/source-han-serif/releases/download/2.003R/09_SourceHanSerifSC.zip",
        archive_kind="zip",
        approx_mb=139.0,
        weights=("ExtraLight", "Light", "Regular", "Medium", "SemiBold", "Bold", "Heavy"),
        coverage="full",
        use_cases=("display", "body"),
        note="衬线体，适合标题与需要古典感的旁白。",
    ),
    # ---------------- 展示/艺术（标题、Logo 位） ----------------
    FontSpec(
        id="smiley-sans",
        family="Smiley Sans",
        display_zh="得意黑",
        license="OFL-1.1",
        homepage="https://github.com/atelier-anchor/smiley-sans",
        archive_url="https://github.com/atelier-anchor/smiley-sans/releases/download/v2.0.1/smiley-sans-v2.0.1.zip",
        archive_kind="zip",
        approx_mb=5.8,
        coverage="artistic",
        use_cases=("display", "artistic"),
        note="倾斜粗体展示字体，适合替换游戏标题大字；正文不可用（字形太少）。",
    ),
    FontSpec(
        id="noto-sans-sc",
        family="Noto Sans SC",
        display_zh="Noto Sans SC",
        license="OFL-1.1",
        homepage="https://github.com/notofonts/noto-cjk",
        archive_url="https://github.com/notofonts/noto-cjk/releases/download/Sans2.004/18_NotoSansSC.zip",
        archive_kind="zip",
        approx_mb=50.0,
        weights=("Thin", "Light", "Regular", "Medium", "Bold", "Black"),
        coverage="full",
        use_cases=("body", "ui"),
        note="与思源黑体同源，字重命名更贴近拉丁字体习惯。仅含 SC 子集，50 MB。",
        reserved_font_names=("Source",),
        glyph_count=65535,
    ),
    # ---------------- 可变字体（一个文件覆盖全部字重） ----------------
    FontSpec(
        id="source-han-sans-sc-vf",
        family="Source Han Sans SC VF",
        display_zh="思源黑体 SC 可变字体",
        license="OFL-1.1",
        homepage="https://github.com/adobe-fonts/source-han-sans",
        direct_url=(
            "https://github.com/adobe-fonts/source-han-sans/releases/download/2.005R/"
            "SourceHanSansSC-VF.ttf"
        ),
        approx_mb=10.2,
        weights=("ExtraLight", "Light", "Normal", "Regular", "Medium", "Bold", "Heavy"),
        coverage="full",
        use_cases=("body", "ui", "display"),
        note=(
            "用户要复刻任意字重时最省体积的选择：10 MB 覆盖全部 7 档。"
            "注意很多游戏引擎不吃可变字体，最终产物要先用 "
            "font.set_variation_by_axes() 实例化成静态面。"
        ),
        reserved_font_names=("Source",),
        glyph_count=65535,
    ),
    # ---------------- 像素字体（复古 / 低分辨率游戏） ----------------
    FontSpec(
        id="fusion-pixel-12-monospaced",
        family="Fusion Pixel 12px Monospaced SC",
        display_zh="缝合像素字体 12px 等宽",
        # 上游为 OFL-1.1 与 MIT 双许可
        license="OFL-1.1",
        homepage="https://github.com/TakWolf/fusion-pixel-font",
        archive_url=(
            "https://github.com/TakWolf/fusion-pixel-font/releases/download/"
            "2025.03.30/fusion-pixel-12px-monospaced-zh_hans.zip"
        ),
        archive_kind="zip",
        approx_mb=25.6,
        coverage="full",
        use_cases=("pixel", "ui"),
        note=(
            "按像素网格设计的等宽字体，拉丁半宽 + 汉字全宽正好 2 倍格。"
            "复古游戏 / 低分辨率 UI 应该用它，而不是把矢量字体硬渲染到 12px。"
        ),
    ),
    FontSpec(
        id="ark-pixel-12-monospaced",
        family="Ark Pixel 12px Monospaced SC",
        display_zh="方舟像素字体 12px 等宽",
        license="MIT",
        homepage="https://github.com/TakWolf/ark-pixel-font",
        archive_url=(
            "https://github.com/TakWolf/ark-pixel-font/releases/download/"
            "2025.03.30/ark-pixel-font-12px-monospaced-zh_cn.zip"
        ),
        archive_kind="zip",
        approx_mb=20.0,
        coverage="full",
        use_cases=("pixel", "ui"),
        note="MIT 许可的像素字体，条款比 OFL 更宽松。",
    ),
)

BY_ID: dict[str, FontSpec] = {f.id: f for f in CATALOG}


def get(font_id: str) -> FontSpec | None:
    return BY_ID.get(font_id)


def find_by_family(family: str) -> FontSpec | None:
    fam = family.strip().lower()
    for f in CATALOG:
        if f.family.lower() == fam or f.display_zh.lower() == fam or f.id == fam:
            return f
    return None


def by_use_case(use: str) -> list[FontSpec]:
    return [f for f in CATALOG if use in f.use_cases]


def redistributable() -> list[FontSpec]:
    """所有可以随发行版一起打包的字体。"""
    return [f for f in CATALOG if f.bundle_ok and f.license in BUNDLE_OK_LICENSES]


def local_only() -> list[FontSpec]:
    """只能在本机下载/渲染、**不要**打进安装包的字体。"""
    return [f for f in CATALOG if not f.bundle_ok or f.license not in BUNDLE_OK_LICENSES]


def safe_to_merge(font: FontSpec) -> bool:
    """能否直接作为合并基底。

    字节数接近 65535 的字体（思源/Noto 全量面）已经顶到 OpenType 上限，
    **不能**再往里塞字形 —— 必须先 pyftsubset 裁到项目实际字符集。
    """
    return font.glyph_count < 60000


def license_summary() -> list[dict[str, object]]:
    return [
        {
            "id": f.id,
            "family": f.family,
            "display_zh": f.display_zh,
            "license": f.license,
            "homepage": f.homepage,
            "bundle_ok": f.bundle_ok,
            "approx_mb": f.approx_mb,
            "reserved_font_names": list(f.reserved_font_names),
            "note": f.note,
        }
        for f in CATALOG
    ]


#: 子集化后的产物是 OFL 定义下的 "Modified Version"，**必须**避开这些保留名。
ALL_RESERVED_FONT_NAMES: tuple[str, ...] = tuple(
    sorted({n for f in CATALOG for n in f.reserved_font_names})
)


def safe_subset_family_name(original_family: str) -> str:
    """给子集化产物生成一个不含保留名的家族名。

    例如 ``Source Han Sans SC`` → ``NovaLoc Sans SC``。
    """
    name = original_family
    for rfn in ALL_RESERVED_FONT_NAMES:
        name = name.replace(rfn, "NovaLoc")
    return name.strip() or "NovaLoc Font"


# 明确"不要打包、也不要自动下载"的商用字体（仅用于识别用户已安装的字体）
NON_REDISTRIBUTABLE_HINTS: tuple[str, ...] = (
    "Microsoft YaHei", "微软雅黑", "SimHei", "黑体", "SimSun", "宋体",
    "NSimSun", "FangSong", "仿宋", "KaiTi", "楷体", "DengXian", "等线",
    "MiSans", "HarmonyOS Sans SC", "Alibaba PuHuiTi",
    "Zpix", "全字庫", "TW-Kai", "TW-Sung", "KingHwa", "京華老宋",
)
