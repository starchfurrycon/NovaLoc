"""字符集解析：决定用哪些字体覆盖项目实际会用到的每一个字符。

为什么要专门做这件事
--------------------
实测（本机 Windows 11）：

* SimHei 缺 ``¶†‡•‹›↔↕♪♫✓✔✕✖❤``
* SimSun / 微软雅黑 / 等线 缺 ``↔↕♪♫✓✔✕✖❤``
* Segoe UI Symbol 覆盖 92.6% 的符号但**一个汉字都没有**
* 霞鹜新晰黑（OFL/IPA）缺 ``✔✕✖❤``

结论：**没有任何单一字体能覆盖游戏里会出现的全部字符**。
所以必须做"贪心集合覆盖"：从候选字体里挑最少的一组，
把项目字符集完整覆盖掉，再按优先级依次合并进游戏原字体。

这里也承担"防口口口"的最终判定：如果所有候选加起来仍缺字符，
就把缺字列表作为**硬错误**抛出来，绝不静默写入半成品。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .coverage import FontInfo, load_font_info
from .textutil import NON_RENDERING
from .textutil import is_ignorable as _is_ignorable_impl

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 游戏 UI 安全字符集
# --------------------------------------------------------------------------

#: 除了译文本身，游戏界面**一定**会渲染到的字符。
#: 漏掉它们的后果：界面上出现口口口，而译文本身完全正常。
UI_SAFE_CHARS = (
    # ASCII 全量
    "".join(chr(c) for c in range(0x20, 0x7F))
    # 中日文标点
    + "、。〈〉《》「」『』【】〔〕…‥・ー～"
    # 全角形式
    + "".join(chr(c) for c in range(0xFF01, 0xFF5F))
    + "￥￠￡"
    # 常用标点/符号
    + "–—‘’“”…†‡•‰′″‹›※§¶"
    # 货币/度量
    + "¥¢°µ×÷±"
    # 数学
    + "≈≠≤≥∞∑√∏∫∅∴∵"
    # 箭头
    + "←↑→↓↔↕⇧⇩"
    # 几何图形
    + "■□▲△▼▽◆◇○●◎"
    # 杂项符号
    + "☀☁☂☎♀♂♪♫♥♦♣♠"
    # 装饰
    + "✓✔✕✖✗✘❤"
    # 带圈字母数字
    + "①②③④⑤⑥⑦⑧⑨⑩"
    # 罗马数字
    + "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"
    # 制表/方块
    + "─│┌┐└┘├┤┬┴┼█▓▒░"
    # 空格类
    + "\u00a0\u3000"
)

#: 中文常用字（GB2312 一级的抽样，用于快速判断一个字体"能不能当中文主字体"）
CJK_CORE_SAMPLE = (
    "的一是不了在人有我他这个们中来上大为和国地到以说时要就出会可也你对生能而子那得于着下自之年过发后作里用道行所然家种事成方多经么去法学如都同现当没动面起看定天分还进好小部其些主样理心她本前开但因只从想实日军者意无力它与长把机十民第公此已工使情明性知全三又关点正业外将两高间由问很最重并物手应战向头文体政美相见被利什二等产或新己制身果加西斯月话合回特代内信表化老给世位次度门任常先海通教儿原东声提立及比员解水名真论处走义各入几口认条平系气题活尔更别打女变四神总何电数安少报才结反受目太量再感建务做接必场件计管期市直德资命山金指克许统区保至队形社便空决治展马科司五基眼书非则听白却界达光放强即像难且权思王象完设式色路记南品住告类求据程北边死张该交规万取拉格望觉术领共确传师观清今切院让识候带导争运笑飞风步改收根干造言联持组每济车亲极林服快办议往元英士证近失转夫令准布始怎呢存未远叫台单影具罗字爱击流备兵连调深商算质团集百需价花党华城石级整府离况亚请技际约示复病息究线似官火断精满支视消越器容照须九增研写称企八功吗包片史委乎查轻易早曾除农找装广显吧阿李标谈吃图念六引历首医局突专费号尽另周较注语仅考落青随选列武红响虽推势参希古众构房半节土投某案黑维革划敌致陈律足态护七兴派孩验责营星够章音跟志底站严巴例防族供效续施留讲型料终答紧黄绝奇察母京段依批群项故按河米围江织害斗双境客纪采举杀攻父苏密低朝友诉止细愿千值仍男钱破网热助倒育属坐帝限船脸职速刻乐否刚威毛状率甚独球般普怕弹校苦创假久错承印晚兰试股拿脑预谁益阳若哪微尼继送急血惊伤素药适波夜省初喜卫源食险待述陆习置居劳财环排福纳欢雷警获模充负云停木游龙树疑层冷洲冲射略范竟句室异激汉村哈策演简卡罪判担州静退既衣您宗积余痛检差富灵协角占配征修皮挥胜降阶审沉坚善妈刘读啊超免压银买皇养伊怀执副乱抗犯追帮宣佛岁航优怪香著田铁控税左右份穿艺背阵草脚概恶块顿敢守酒岛托央户烈洋哥索胡款靠评版宝座释景顾弟登货互付伯慢欧换闻危忙核暗姐介坏讨丽良序升监临亮露永呼味野架域沙掉括舰鱼杂误湾吉减编楚肯测败屋跑梦散温困剑渐封救贵枪缺楼县尚毫移娘朋画班智亦耳恩短掌恐遗固席松秘谢鲁遇康虑幸均销钟诗藏赶剧票损忽巨炮旧端探湖录叶春乡附吸予礼港雨呀板庭妇归睛饭额含顺输摇招婚脱补谓督毒油疗旅泽材灭逐莫笔亡鲜词圣择寻厂睡博勒烟授诺伦岸奥唐卖俄炸载洛健堂旁宫喝借君禁阴园谋宋避抓荣姑孙逃牙束跳顶玉镇雪午练迫爷篇肉嘴馆遍凡础洞卷坦牛宁纸诸训私庄祖丝翻暴森塔默握戏隐熟骨访弱蒙歌店鬼软典欲萨伙遭盘爸扩盖弄雄稳忘亿刺拥徒姆杨齐赛趣曲刀床迎冰虚玩析窗醒妻透购替塞努休虎扬途侵刑绿兄迅套贸毕唯谷轮库迹尤竞街促延震弃甲伟麻川申缓潜闪售灯针哲络抵朱埃抱鼓植纯夏忍页杰筑折郑贝尊吴秀混臣雅振染盛怒舞圆搞狂措姓残秋培迷诚宽宇猛摆梅毁伸摩盟末乃悲拍丁赵坚揭韦魔素鸣"
)


# --------------------------------------------------------------------------
# 结果模型
# --------------------------------------------------------------------------


@dataclass
class SourcePick:
    """一个被选中的补充字体，以及它负责覆盖的字符。"""

    path: Path
    covers: list[str] = field(default_factory=list)
    num_glyphs: int = 0
    units_per_em: int = 0
    license_note: str = ""

    @property
    def covers_str(self) -> str:
        return "".join(self.covers)

    def __str__(self) -> str:
        return f"{self.path.name}: {len(self.covers)} 字"


@dataclass
class CharsetPlan:
    """完整的字符集覆盖方案。"""

    required: str = ""
    """项目需要的全部字符（去重后）。"""

    covered_by_base: list[str] = field(default_factory=list)
    sources: list[SourcePick] = field(default_factory=list)
    still_missing: list[str] = field(default_factory=list)
    base_family: str = ""

    optional_missing: list[str] = field(default_factory=list)
    """**可放弃**的字符里没被覆盖的部分（不导致失败）。

    这些字符是工具**猜**游戏界面可能会渲染的（见 `UI_SAFE_CHARS`），
    不是任何一条真实文本里的字符。它们缺了只是"少几个装饰符号"，
    而不是"玩家看到口口口"。

    为什么必须和 `still_missing` 分开 —— 真实事故：

    某些符号（`※ ‥ ′ ″ ‰`）**本机任何一个 CJK 字体都没有**。
    它们出现在 `UI_SAFE_CHARS` 里，于是一整轮字体适配直接
    **硬失败**：

        ❌ ok=False 错误=字符集里有 4 个字符没有任何候选字体能提供，
           无法生成完整字体：ინშ️

    而这 4 个字符在真实游戏文本里出现 **0 次**（数过：
    981 个场景、65570 条文本里一次都没有）。
    也就是说，**一句都用不到的装饰符号把整个流程拦死了**。
    """

    @property
    def coverage(self) -> float:
        total = len(set(self.required))
        if not total:
            return 1.0
        missing = len(set(self.still_missing))
        return (total - missing) / total

    @property
    def ok(self) -> bool:
        return not self.still_missing

    @property
    def source_paths(self) -> list[Path]:
        return [s.path for s in self.sources]

    def summary(self) -> str:
        lines = [
            f"字符集共 {len(set(self.required))} 个字符",
            f"  原字体已覆盖   {len(self.covered_by_base)}",
        ]
        for s in self.sources:
            lines.append(
                f"  + {s.path.name:<28} 补 {len(s.covers):>5} 字  "
                f"(UPEM {s.units_per_em}, {s.num_glyphs} 字形)"
            )
        if self.still_missing:
            lines.append(f"  ❌ 仍缺 {len(self.still_missing)} 字：{''.join(self.still_missing[:80])}")
        else:
            lines.append("  ✅ 可以完整覆盖，不会出现口口口")
        if self.optional_missing:
            lines.append(
                f"  ⚠️ 另有 {len(self.optional_missing)} 个**猜测用**的装饰符号本机字体没有"
                f"（不影响任何真实文本）：{''.join(self.optional_missing[:40])}"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 规划
# --------------------------------------------------------------------------


def build_required_charset(
    translated_texts: list[str] | None = None,
    source_texts: list[str] | None = None,
    extra: str = "",
    *,
    include_ui_safe: bool = True,
) -> str:
    """汇总项目真正需要渲染的全部字符。"""
    return "".join(sorted(build_charset_tiers(translated_texts, source_texts, extra,
                                              include_ui_safe=include_ui_safe)[0]))


def build_charset_tiers(
    translated_texts: list[str] | None = None,
    source_texts: list[str] | None = None,
    extra: str = "",
    *,
    include_ui_safe: bool = True,
) -> tuple[str, str]:
    """返回 ``(全部字符, 其中真实文本需要的字符)``。

    第二个值是**硬失败**判据用的：只有它缺字才算"玩家会看到口口口"。
    第一个值与第二个值之差就是工具**猜**的那些装饰符号
    （见 `UI_SAFE_CHARS`）—— 能补就补，补不上只记警告。

    ⚠️ **换行/制表/零宽字符在这里就被剔掉**（见 :func:`is_ignorable`）。
    以前这里不过滤，而下游 `merge.py` 会把译文里的 ``\\n`` 当成
    "字体缺这个字形"，导致覆盖率永远差一点点、补丁判定失败、
    整个字体阶段中止。真实游戏上表现就是"字体适配失败，流程走不下去"，
    而报错信息完全指不到真正的原因。

    在**源头**过滤（而不是只在下游容忍）是刻意的：字符集是这个模块的
    对外产物，会落盘到 `fonts/charset.json` 供审校与报告用，
    里面不该出现"永远不可能有字形"的字符。
    """
    # 真实文本需要的字符（硬失败判据）
    text_parts: list[str] = []
    for group in (translated_texts or [], source_texts or []):
        text_parts.extend(group)
    text_parts.append(extra)
    strict = {c for c in "".join(text_parts) if not is_ignorable(c)}

    # 猜的字符（可放弃）
    guessed: set[str] = set()
    if include_ui_safe:
        guessed = {c for c in UI_SAFE_CHARS if not is_ignorable(c)}

    allv = strict | guessed
    return "".join(sorted(allv)), "".join(sorted(strict))


def plan_charset(
    required: str,
    base_font: Path | None,
    candidates: list[Path],
    *,
    base_face_index: int = 0,
    prioritize: list[str] | None = None,
    max_sources: int = 4,
    optional: str = "",
) -> CharsetPlan:
    """贪心集合覆盖：挑最少的字体把 ``required`` 全部覆盖。

    ``prioritize`` 是字体文件名的优先顺序（靠前的先被考虑），
    用来保证"风格最匹配的字体"优先承担中文字形。

    ## ``optional``：**可放弃**的字符

    这些字符是工具**猜**界面会渲染的（见 `UI_SAFE_CHARS`），
    不是任何真实文本里的字符。它们**参与规划**（能补就补），
    但补不上时**不算失败** —— 只记进 :attr:`CharsetPlan.optional_missing`。

    为什么必须分开（真实事故）：`※ ‥ ′ ″ ‰` 这几个符号
    **本机任何一个 CJK 字体都没有**，而它们在 `UI_SAFE_CHARS` 里，
    于是整轮字体适配硬失败 —— 尽管它们在真实游戏文本里
    出现 **0 次**（981 个场景、65570 条文本数过）。
    一句都用不到的装饰符号把整个流程拦死，这是判据用错了地方：
    "硬失败"应当只针对**真的会被渲染**的字符。
    """
    plan = CharsetPlan(required=required)
    remaining = {c for c in set(required) if not _is_ignorable(c)}
    # 可放弃的字符并进同一个待覆盖集合 —— 它们一样值得去补，
    # 只是最后结算时分开记账。
    optional_set = {c for c in set(optional) if not _is_ignorable(c)}
    remaining |= optional_set

    if base_font is not None and base_font.exists():
        info = load_font_info(base_font, base_face_index)
        if info is not None and info.cmap:
            plan.base_family = info.family
            covered = {c for c in remaining if info.has_char(c)}
            plan.covered_by_base = sorted(covered)
            remaining -= covered

    if not remaining:
        return plan

    # 读入所有候选的覆盖信息
    infos: list[tuple[Path, FontInfo]] = []
    for p in candidates:
        if base_font is not None and p.resolve() == base_font.resolve():
            continue
        info = load_font_info(p)
        if info is None or not info.cmap:
            continue
        infos.append((p, info))

    # 应用优先顺序
    if prioritize:
        order = {name.lower(): i for i, name in enumerate(prioritize)}
        infos.sort(key=lambda t: order.get(t[0].name.lower(), len(order)))

    used_names: set[str] = set()
    for p, info in infos:
        if not remaining or len(plan.sources) >= max_sources:
            break
        if p.name in used_names:
            continue
        # 这个字体此刻还能贡献多少字符？
        takes = {c for c in remaining if info.has_char(c)}
        if not takes:
            continue
        used_names.add(p.name)
        plan.sources.append(
            SourcePick(
                path=p,
                covers=sorted(takes),
                num_glyphs=info.num_glyphs,
                units_per_em=info.units_per_em,
            )
        )
        remaining -= takes

    # 分开结算：真的需要的缺了才是失败，猜的缺了只是警告。
    plan.still_missing = sorted(c for c in remaining if c not in optional_set)
    plan.optional_missing = sorted(c for c in remaining if c in optional_set)
    return plan


_NON_RENDERING = NON_RENDERING


def is_ignorable(ch: str) -> bool:
    """这个字符是不是**不需要字形**的（控制/换行/零宽）。

    实现在 :mod:`novaloc.fonts.textutil` —— 放在那里是因为
    `coverage.py` 也要用同一个判定，而它不能再从本模块导入（会循环）。

    保留这个别名是为了让 `charset.is_ignorable` 这个既有调用点继续可用。
    """
    return _is_ignorable_impl(ch)


def _is_ignorable(ch: str) -> bool:
    """``is_ignorable`` 的旧名，保留以兼容既有调用。"""
    return _is_ignorable_impl(ch)


def assert_plannable(plan: CharsetPlan) -> None:
    """**真实文本**里的字缺了就硬失败。

    这是"保证不出现口口口"契约的最后一关：
    宁可整个流程停下来报错，也不要写回一个会显示方框的字体。

    ⚠️ 只管 :attr:`CharsetPlan.still_missing`（真实文本需要的字符）。
    :attr:`CharsetPlan.optional_missing`（工具猜的装饰符号）**不导致失败** ——
    那些字符在游戏里可能一次都不出现，为它们中止整个流程
    是判据用错了地方。
    """
    if plan.ok:
        return
    raise CharsetCoverageError(plan)


class CharsetCoverageError(RuntimeError):
    def __init__(self, plan: CharsetPlan) -> None:
        self.plan = plan
        miss = "".join(plan.still_missing[:120])
        more = f"（还有 {len(plan.still_missing) - 120} 个）" if len(plan.still_missing) > 120 else ""
        super().__init__(
            f"有 {len(plan.still_missing)} 个字符没有任何候选字体能覆盖，"
            f"继续写入会导致游戏内显示口口口：{miss}{more}\n"
            f"请在设置里增加字体来源，或把包含这些字符的文本加进忽略列表。"
        )


def default_supplement_candidates(
    bundled_dir: Path | None = None, extra_dirs: list[Path] | None = None
) -> list[Path]:
    """默认的补充字体候选顺序。

    优先用**可再分发的 OFL 字体**（体积小、覆盖好、许可干净），
    其次才用系统字体（不能打包，但本机渲染完全合法）。
    """
    out: list[Path] = []
    if bundled_dir and bundled_dir.exists():
        out.extend(sorted(bundled_dir.glob("*.tt[fc]")))
        out.extend(sorted(bundled_dir.glob("*.ot[fc]")))
    for d in extra_dirs or []:
        if d.exists():
            out.extend(sorted(d.glob("*.tt[fc]")))
    # 系统字体兜底：这些能覆盖 OFL 字体缺的个别符号
    from .coverage import system_font_dirs

    for d in system_font_dirs():
        for name in ("simhei.ttf", "msyh.ttc", "Deng.ttf", "simsun.ttc", "seguisym.ttf"):
            p = d / name
            if p.exists() and p not in out:
                out.append(p)
    return out
