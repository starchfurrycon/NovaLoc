r"""`unity_bundle` 的测试。

## 测试策略（为什么这样切）

这个模块的关键风险**不是**"读不出来"，而是**判断错**：

* **判错"该翻"** ⇒ 覆盖游戏自带的官方简体译文（质量**倒退**，实测受影响面 48.8%）；
* **判错"往哪写"** ⇒ 把译文写进非字符串字段，**写坏包**（游戏打不开）。

所以测试重点放在：
1. `slot_needs_translation` 的三条判据（**逐条**测，含反例）；
2. `_set_path` 的**拒绝**行为（目标不是 str 时必须返回 False）；
3. pointer 的往返（`pointer()` 生成的串能被 `apply_translations` 解析）；
4. `_parse_path` 对实测路径形态的解析（含 ``[n]`` 下标）。

⚠️ 这里**不**用真包做单测：真包是 188 MB 的外部依赖，单测必须能离线跑。
真包验证是集成实验，写在 `docs/BUNDLE_SURVEY.md`。
"""

from __future__ import annotations

import pytest

from novaloc.engines.unity_bundle import (
    BARE_TEXT_FIELDS,
    SRC_TAGS,
    ZH_TAGS,
    BundleSlot,
    _norm_tag,
    _parse_path,
    _set_path,
    _walk_bare,
    _walk_containers,
    slot_needs_translation,
)


def _slot(variants: dict[str, str], *, bare: bool = False) -> BundleSlot:
    from pathlib import Path

    return BundleSlot(Path("x.bundle"), "asset", ".f", variants, bare=bare)


# --------------------------------------------------------------- tag 归一化


def test_tag_is_stripped() -> None:
    """★ 实测有 275 个 ``'English '``（尾随空格）—— 不 strip 会漏掉它们。"""
    assert _norm_tag({"tag": "English "}) == "english"
    assert _norm_tag({"tag": "  Chinese (Simplified) "}) == "chinese (simplified)"
    assert _norm_tag({}) is None
    assert _norm_tag({"tag": 42}) is None


def test_stripped_english_tag_is_recognized_as_source() -> None:
    """尾随空格的 ``'English '`` 必须能被当成源文（否则那条就漏译了）。"""
    s = _slot({"english": "Hello there, traveler."})
    tag, src = s.source_text()
    assert tag == "english"
    assert src == "Hello there, traveler."


# ---------------------------------------------------------- slot 取值逻辑


def test_source_prefers_japanese() -> None:
    """日文优先：二次元游戏原文多为日文，日→中比英→中更贴近原意。"""
    s = _slot({"english": "Hello", "japanese": "こんにちは"})
    assert s.source_text() == ("japanese", "こんにちは")


def test_source_falls_back_to_english() -> None:
    s = _slot({"english": "Hello", "korean": "안녕"})
    assert s.source_text() == ("english", "Hello")


def test_unknown_tag_still_yields_source() -> None:
    """tag 命名不在已知集合里时，仍要给出源文（靠"已有中文"判据兜底）。"""
    s = _slot({"content_xx": "Bonjour"})
    assert s.source_text() == ("content_xx", "Bonjour")


def test_zh_tags_cover_both_naming_schemes() -> None:
    """两套实测命名都要认：``Chinese (Simplified)`` 与 ``Content_zh-cn``。"""
    assert "chinese (simplified)" in ZH_TAGS
    assert "content_zh-cn" in ZH_TAGS


def test_has_target_detects_existing_chinese() -> None:
    assert _slot({"chinese (simplified)": "你好"}).has_target()
    assert _slot({"content_zh-cn": "你好"}).has_target()
    # 空字符串不算"已有"
    assert not _slot({"chinese (simplified)": "   "}).has_target()
    assert not _slot({"english": "Hi"}).has_target()


# ------------------------------------------------- ★ 核心判据：该不该翻


def test_needs_translation_when_source_and_no_target() -> None:
    """有源文 + 无中文 ⇒ **该翻**（这是唯一该翻的情形）。"""
    assert slot_needs_translation(_slot({"english": "One day, Ellin knocked."}))


def test_SKIPS_when_chinese_already_exists() -> None:
    """★★ **最重要的一条**：已有中文 ⇒ 绝不翻。

    实测 Jerez's Arena 有 **8533/9392（90.9%）** 槽位**自带官方中文**
    （`.scratch/_reconcile.py`）。翻它们既浪费预算，
    更会**覆盖官方译文**（质量倒退）。

    ⚠️ 初版勘查曾把这批算成 48.8%（4580/9392）并声称有 3653 条"缺中文"
    —— 那是 `.scratch/_jerez_gain.py` 的分组 bug（对遍历中每个 dict
    都递增组号）。仲裁过程见 `docs/BUNDLE_SURVEY.md` §5。
    """
    s = _slot(
        {
            "english": "On a corner of the capital of Honece...",
            "chinese (simplified)": "王都赫雷斯的一角，车水马龙的大道旁矗立着一栋豪华的宅邸。",
        }
    )
    assert s.has_target()
    assert not slot_needs_translation(s), "已有官方简中，不许再翻！"


def test_SKIPS_when_no_source_text() -> None:
    """无源文（空槽位）⇒ 不翻。实测占 911/9392。"""
    assert not slot_needs_translation(_slot({"english": "   ", "japanese": ""}))
    assert not slot_needs_translation(_slot({}))


def test_SKIPS_when_source_is_already_chinese() -> None:
    """源文本身就是中文（tag 映射错乱的游戏）⇒ 不翻。"""
    s = _slot({"english": "王都赫雷斯的一角，车水马龙的大道旁矗立着一栋豪华的宅邸。"})
    assert not slot_needs_translation(s)


def test_japanese_source_with_kanji_is_still_translated() -> None:
    """★ 反例：日文含汉字**不该**被"已是中文"规则误杀（假名是关键区分）。"""
    s = _slot({"japanese": "王都ヘレスの一角、賑やかな大通り沿いに、一棟の豪邸がそびえ立っている。"})
    assert slot_needs_translation(s), "含假名的日文必须仍然要翻"


def test_bare_slot_needs_translation_when_non_empty() -> None:
    assert slot_needs_translation(_slot({"__bare__": "Knockout all enemies."}, bare=True))
    assert not slot_needs_translation(_slot({"__bare__": "  "}, bare=True))


def test_BARE_field_already_chinese_is_skipped() -> None:
    """★ **对账发现的 bug**：裸字段也必须查"是否已是中文"。

    初版 `slot_needs_translation` 只对"语言槽位组"查了"已有中文"，
    对裸字段只看"非空" ⇒ 把 Jerez's Arena 的 **4477 处 `.storyText`
    （繁体字幕）**判成待翻译，凭空多出 4477 条任务。

    这个 bug 是靠"模块计数与手工对账差太多"发现的
    （`.scratch/_verify_bundle2.py`），不是靠读代码看出来的。
    """
    # 实测样本：繁体字幕 ⇒ 不是"待翻成中文"
    assert not slot_needs_translation(
        _slot({"__bare__": "「我知道了，那些錢我會一分不少的還給你。」"}, bare=True)
    )
    assert not slot_needs_translation(
        _slot({"__bare__": "艾琳把酒一飲而盡，格倫拿出一袋沉甸甸的金幣放在她面前。"}, bare=True)
    )


def test_BARE_field_japanese_is_still_translated() -> None:
    """★ 反例：裸字段若是**日文**，必须仍然要翻（假名是关键区分）。"""
    assert slot_needs_translation(
        _slot({"__bare__": "ついにクラウディアが新しい身分となる日が来た。"}, bare=True)
    )


def test_text_is_chinese_logic() -> None:
    from novaloc.engines.unity_bundle import _text_is_chinese

    assert _text_is_chinese("你好，这是一句中文。")
    assert not _text_is_chinese("Hello there, traveler.")
    assert not _text_is_chinese("")  # 空不算
    # 含假名 ⇒ 日文，即使汉字很多
    assert not _text_is_chinese("王都ヘレスの一角、賑やかな大通り沿いに")


def test_KANA_punctuation_is_not_treated_as_japanese() -> None:
    """★ 回归：``・``（\\u30fb 片假名中点）**不是假名**。

    实测事故：``'「我是克菈蒂雅・奎涅爾。奎涅爾家的長女。」'`` 是**中文**
    人名，因为含 ``・``（也常用于中文译名）被误判成日文，
    于是这条**已是中文**的字幕被当成"待翻译"。

    根因：``[\\u3040-\\u30ff]`` 这个区间里混着 ``\\u30fb`` 与
    ``\\u30fc``（长音符）等**标点**。现在用两个纯字母区间拼。
    """
    from novaloc.engines.unity_bundle import _KANA, _text_is_chinese

    # 这两个是标点，不该被 _KANA 命中
    assert not _KANA.search("・")
    assert not _KANA.search("ー")
    # 真假名仍然命中
    assert _KANA.search("つ")
    assert _KANA.search("ク")
    # 结论：含中文译名中点的句子仍是中文
    assert _text_is_chinese("「我是克菈蒂雅・奎涅爾。奎涅爾家的長女。」")


def test_short_strings_are_not_sent_to_model() -> None:
    """★ 短串保守判为"已是中文" —— 误判的代价不对称。

    实测 ``'喀。'``(2 字) / ``'「哦？」'``(4 字) 这类碎片没法定语言。
    判成"需翻译"= 送垃圾进模型（实测多出 442 条）；
    判成"已是中文"= 少翻一条极短碎片。选后者。
    """
    from novaloc.engines.unity_bundle import _text_is_chinese

    assert _text_is_chinese("喀。")
    assert _text_is_chinese("「哦？」")
    # 但足够长的英文仍要翻
    assert not _text_is_chinese("Character Name")


def test_punctuation_only_is_not_translated() -> None:
    """纯标点没有可翻译内容（实测 ``'……'`` / ``'「……」'``）。"""
    from novaloc.engines.unity_bundle import _MEANINGFUL, _text_is_chinese

    assert not _MEANINGFUL.search("……")
    assert not _MEANINGFUL.search("「……」")
    # 它们同时也不该被判为"需翻译"（_MEANINGFUL 检查在更前面）
    _ = _text_is_chinese
    assert not slot_needs_translation(_slot({"__bare__": "……"}, bare=True))
    assert not slot_needs_translation(_slot({"__bare__": "「……」"}, bare=True))


# --------------------------------------------------------- 路径解析与写入


def test_parse_path_handles_measured_shapes() -> None:
    """解析实测见过的路径形态。"""
    assert _parse_path(".csvLines[12].localizeText[3]") == [
        "csvLines",
        12,
        "localizeText",
        3,
    ]
    assert _parse_path(".m_text") == ["m_text"]
    assert _parse_path(".LocalizeNarratives[0].pages[1].cmds[2].localizeTexts[3]") == [
        "LocalizeNarratives",
        0,
        "pages",
        1,
        "cmds",
        2,
        "localizeTexts",
        3,
    ]


def test_set_path_writes_string_field() -> None:
    tree = {"csvLines": [{"localizeText": [{"tag": "English", "content": "old"}]}]}
    assert _set_path(tree, ".csvLines[0].localizeText[0].content", "新")
    assert tree["csvLines"][0]["localizeText"][0]["content"] == "新"


def test_set_path_REFUSES_non_string_target() -> None:
    """★ 目标不是 str ⇒ **必须拒绝**，否则会把数字字段改成字符串、写坏包。"""
    tree = {"a": {"b": 42}}
    assert not _set_path(tree, ".a.b", "x")
    assert tree["a"]["b"] == 42, "被拒绝了就不许改"


def test_set_path_refuses_missing_path() -> None:
    tree: dict = {"a": {}}
    assert not _set_path(tree, ".a.nope", "x")
    assert not _set_path(tree, ".nope.deeper", "x")


def test_set_path_refuses_out_of_range_index() -> None:
    tree = {"a": [{"t": "x"}]}
    assert not _set_path(tree, ".a[5].t", "y")


# ------------------------------------------------------------------ 遍历


def test_walk_containers_finds_language_slot_groups() -> None:
    """识别"一组语言槽位"的形态（实测自 Jerez's Arena）。"""
    tree = {
        "csvLines": [
            {
                "localizeText": [
                    {"tag": "English", "content": "Hello"},
                    {"tag": "Chinese (Simplified)", "content": "你好"},
                ]
            }
        ]
    }
    found = _walk_containers(tree)
    assert len(found) == 1
    path, items = found[0]
    assert path == ".csvLines[0].localizeText"
    assert len(items) == 2


def test_walk_containers_ignores_plain_dict_lists() -> None:
    """首元素不含 ``tag`` 的 list **不是**槽位组（避免误判）。"""
    tree = {"selectedBlocks": [{"m_FileID": 0, "m_PathID": 1}]}
    assert _walk_containers(tree) == []


def test_walk_containers_ignores_empty_list() -> None:
    assert _walk_containers({"x": []}) == []


def test_walk_bare_only_accepts_allowlisted_fields() -> None:
    """★ 只认白名单 —— ``m_Name``/``m_FamilyName`` **不许**被当成文本。"""
    tree = {
        "m_text": "Real dialogue here.",
        "m_Name": "SomeAssetName",
        "m_FaceInfo": {"m_FamilyName": "Noto Sans CJK TC"},
        "MotionName": "チンコしまいAG_1074.fade",
    }
    found = dict(_walk_bare(tree))
    assert list(found) == [".m_text"]
    assert found[".m_text"] == "Real dialogue here."
    # 反例必须被挡住（这些是实测真实出现的"假文本"）
    assert ".m_Name" not in found
    assert ".m_FaceInfo.m_FamilyName" not in found


def test_allowlist_excludes_measured_noise_fields() -> None:
    """把"实测证明是噪音"的字段名钉住，防止有人好心加进白名单。"""
    for bad in ("m_name", "motionname", "m_familyname", "pagename", "blogname"):
        assert bad not in BARE_TEXT_FIELDS, f"{bad} 实测是资产/字体/动画名，不该翻"


# ------------------------------------------------------------------ pointer


def test_pointer_round_trips_through_parsing() -> None:
    """`pointer()` 生成的串必须能被 `apply_translations` 的解析逻辑切开。"""
    from pathlib import Path

    s = BundleSlot(
        Path("scriptableobjects_assets_all.bundle"),
        "MyAsset",
        ".csvLines[0].localizeText[3]",
    )
    ptr = s.pointer()
    assert ptr == "scriptableobjects_assets_all.bundle:MyAsset:.csvLines[0].localizeText[3]"
    parts = ptr.split(":", 2)
    assert len(parts) == 3
    assert parts[0] == "scriptableobjects_assets_all.bundle"
    assert parts[1] == "MyAsset"
    assert parts[2] == ".csvLines[0].localizeText[3]"


def test_pointer_tolerates_empty_asset_name() -> None:
    from pathlib import Path

    s = BundleSlot(Path("a.bundle"), "", ".m_text")
    assert s.pointer() == "a.bundle:-:.m_text"


def test_src_tags_are_lowercase() -> None:
    """tag 比较前会 lower()，常量表必须也是小写，否则永不命中。"""
    assert all(t == t.lower() for t in SRC_TAGS)
    assert all(t == t.lower() for t in ZH_TAGS)


@pytest.mark.parametrize("tag", ["japanese", "english", "korean"])
def test_each_src_tag_can_supply_source(tag: str) -> None:
    s = _slot({tag: "some text here"})
    got_tag, src = s.source_text()
    assert got_tag == tag
    assert src == "some text here"
