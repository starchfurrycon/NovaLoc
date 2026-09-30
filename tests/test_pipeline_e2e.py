"""流水线端到端验证（不依赖 Ollama 与网络）。

用**假翻译器**把整条链路跑通：识别 → 抽取 → 扫图 → 翻译 →
字体 → 贴图 → 质检 → 回写。这样能在没有模型的情况下验证
编排逻辑、阶段依赖顺序、以及"出问题时是否正确拒绝"。

重点验证：
1. 全流程能跑完并产出可读的回写结果；
2. 字体阶段**依赖译文**（没译文时必须拒绝，而不是产出一个缺字的字体）；
3. 占位符坏掉的译文**不会被回写**（这是"游戏内不出乱码"的最后一道闸）；
4. 质检能查出术语不一致、漏翻、字体缺字；
5. 原始游戏目录全程只读。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402
from _fake_game import build_fake_game  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.core.workspace import Workspace  # noqa: E402
from novaloc.models import EntryStatus, Project, TranslationEntry  # noqa: E402
from novaloc.pipeline import Pipeline, PipelineError, run_qa  # noqa: E402

SB = FIXTURES
DATA = Path(r"D:\NovaLocData")  # 中间产物放到 D 盘，别占 C 盘


def _game() -> Path:
    """取合成游戏工程；没有就**自己造**（原因见 ``_fake_game.py``）。

    ``tests/fixtures/rpgmaker_game/data`` 是 git-ignored 的，全新 clone
    出来并不存在，以前只有 ``test_engine_rpgmaker.py`` 跑过之后才有 ——
    也就是说本文件以前隐式依赖另一个套件先执行。
    """
    base = os.environ.get("NOVALOC_FAKE_GAME_DIR")
    dest = Path(base) / "rpgmaker_game" if base else SB / "rpgmaker_game"
    return build_fake_game(dest)


def make_pipeline(ws: Workspace, ctx: Context, fake) -> Pipeline:
    return Pipeline(ws, ctx, translate_fn=fake)


def fake_translate(items, target_lang):
    """把每条文本翻成"中译"前缀形式，保留占位符。"""
    out = []
    for it in items:
        u = it.unit
        # 模拟模型行为：把占位符一字不动地带回来
        from novaloc.translate.placeholders import mask

        m = mask(u.source)
        text = m.text
        # 简单的"翻译"：给非占位符部分加中文前缀
        text = ("【中】" + text) if u.kind.value in ("dialogue", "narration") else ("〔中〕" + text)
        # 还原占位符
        for i, s in enumerate(m.slots):
            text = text.replace(f"\u27e6{i}\u27e7", s)
        out.append(TranslationEntry(
            uid=u.uid, source=u.source, target=text,
            status=EntryStatus.TRANSLATED, kind=u.kind,
            provider="fake", model="fake-1",
        ))
    return out


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    # 自己造工程，不再依赖别的套件先跑过（以前这里会 print 提示后直接 return 1，
    # 于是"单独跑本文件"永远是失败的，还会被误读成流水线坏了）
    game = _game()

    # 每次从干净的工作区开始
    ws_root = DATA / "workspaces"
    pid = "testpipe"
    ws_dir = ws_root / pid
    if ws_dir.exists():
        shutil.rmtree(ws_dir)
    ws_root.mkdir(parents=True, exist_ok=True)

    proj = Project(id=pid, name="流水线测试", game_dir=str(game))
    ws = Workspace(proj, ws_dir)
    ws.save()

    cfg = Config()
    cfg.font.allow_download = False   # 离线；只用手头已有字体
    ctx = Context(config=cfg, events=EventBus())

    events: list[tuple[str, str]] = []
    ctx.events.subscribe(lambda e: events.append((e.kind, e.stage)))

    # ---------------------------------------------------------------
    # 1. 字体阶段必须在翻译之前被拒绝
    # ---------------------------------------------------------------
    print("[1] 阶段依赖：没有译文时字体阶段应拒绝")
    pipe = make_pipeline(ws, ctx, fake_translate)
    try:
        pipe.stage_fonts()
        check("无译文时字体阶段拒绝执行", False, "竟然成功了")
    except PipelineError as exc:
        print(f"    ✅ 正确拒绝：{exc}")
        check("无译文时字体阶段拒绝执行", True)
        check("拒绝原因说明了依赖译文",
              "译文" in str(exc), str(exc))

    # ---------------------------------------------------------------
    # 2. 跑识别 → 抽取 → 扫图
    # ---------------------------------------------------------------
    print("\n[2] 识别 / 抽取 / 扫图")
    r = pipe.stage_detect()
    print(f"    detect: {r.message}  {r.stats}")
    check("识别到 rpgmaker", r.stats.get("engine_id") == "rpgmaker", str(r.stats))
    check("引擎信息写回了项目", ws.project.engine == "rpgmaker")

    r = pipe.stage_extract()
    print(f"    extract: {r.message}")
    check("抽取到文本", r.stats.get("units", 0) > 20, str(r.stats))

    r = pipe.stage_images_scan()
    print(f"    images: {r.message}")
    check("扫到贴图", r.stats.get("images", 0) >= 1, str(r.stats))

    # ---------------------------------------------------------------
    # 3. 翻译
    # ---------------------------------------------------------------
    print("\n[3] 翻译")
    r = pipe.stage_translate()
    print(f"    translate: {r.message}  {r.stats}")
    check("翻译完成", r.ok and r.stats.get("translated", 0) > 20, str(r.stats))
    check("译文确实与原文不同", r.stats.get("changed", 0) > 20, str(r.stats))

    # 幂等：再跑一次应发现"全部已有译文"
    r2 = pipe.stage_translate()
    print(f"    再跑一次: {r2.message}")
    check("翻译阶段幂等（第二次不重复翻译）",
          "无需翻译" in r2.message, r2.message)

    # ---------------------------------------------------------------
    # 4. 字体
    # ---------------------------------------------------------------
    print("\n[4] 字体适配")
    r = pipe.stage_fonts()
    print(f"    fonts: {r.message}  {r.stats}")
    check("字体阶段完成", r.ok, r.error)
    cs = ws.load_charset()
    check("字符集已保存且非空", cs is not None and cs.total > 50,
          str(cs.total if cs else None))
    # 中文字符必须进字符集。
    # 注意 ``all_chars`` 是**字符串**（不是列表），用 ``in`` 做子串判断即可；
    # ``text_chars`` 是列表，整数判断更严格。
    check("中文字符进了字符集",
          cs is not None and "中" in "".join(cs.all_chars),
          "".join(cs.all_chars)[:80] if cs else "")
    # 假翻译器只会加"【中】"前缀，所以译文里出现的汉字就是这三个。
    # 断言"字符集必须完整包含译文用到的每一个字符" —— 这正是
    # "游戏内不出口口口"的定义，比列举某几个字更有意义。
    if cs is not None:
        tx = "".join(e.target for e in ws.load_entries())
        missing_tx = sorted(set(tx) - set("".join(cs.all_chars)))
        check("字符集完整覆盖了所有译文字符", not missing_tx, "".join(missing_tx[:40]))
        check("译文里的汉字（【中】）确实在字符集里",
              {"【", "中", "】"} <= set("".join(cs.all_chars)),
              str(sorted({"【", "中", "】"} - set("".join(cs.all_chars)))))

    # ---------------------------------------------------------------
    # 5. 贴图汉化
    # ---------------------------------------------------------------
    print("\n[5] 贴图汉化")
    r = pipe.stage_images_localize()
    print(f"    images_localize: {r.message}  {r.stats}")
    check("贴图阶段完成", r.ok, r.error)
    # 合成贴图上写的是 "NEW GAME"。
    #
    # ⚠️ 这里以前断言的是 ``blocks >= 2``，注释还写着"OCR 会把它切成
    # 'NEW' 与 'V GAME' 两块"。**那句话从来没被验证过** —— 它是照着一个
    # 旧印象顺手写的；换成测试自造字体后实测是 **1 块**（整串
    # "NEW GAME" 一起识别，见 `.scratch/_check_font_ocr.py` 的输出）。
    # 真正的教训不是"该写几"，而是：这种"具体数量"的断言必须来自实测，
    # 否则它只是在把某个偶然结果固化下来，一旦基础条件变了就变成噪音。
    #
    # 本用例真正要守的是"OCR 认出来了、翻译回调被调用了、块级结果被记下来了"，
    # 所以下面用 >= 1 并配合 ocr_calls 的检查；具体块数由 textgroup 的
    # 专属测试去管（那里才是它的归属）。
    check("贴图被 OCR 识别并送去翻译", r.stats.get("blocks", 0) >= 1, str(r.stats))
    check("翻译回调被调用", r.stats.get("ocr_calls", 0) >= 1, str(r.stats))
    recs = ws.read_json("images/localize.json", []) or []
    all_blocks = [b for rec in recs for b in rec.get("blocks", [])]
    print(f"    识别到的文字块：{[(b['source'], b['target']) for b in all_blocks]}")
    check("块级结果被记录下来（供审校页使用）", len(all_blocks) >= 1, str(len(all_blocks)))
    check("块有译文", any(b.get("target") for b in all_blocks), str(all_blocks[:2]))
    # 识别出的文字必须确实是贴图上那几个字（否则"有块"也可能是噪声）
    joined_src = " ".join(str(b.get("source", "")) for b in all_blocks).upper()
    check("识别出的内容与贴图相符", "NEW" in joined_src and "GAME" in joined_src, joined_src)

    # ---------------------------------------------------------------
    # 6. 质检
    # ---------------------------------------------------------------
    print("\n[6] 质检")
    rep = run_qa(ws, ctx)
    print(f"    ok={rep['ok']} stats={rep['stats']}")
    for i in rep["issues"][:8]:
        print(f"      [{i['severity']:7}] {i['message'][:76]}")
    check("质检跑出报告", "stats" in rep and rep["stats"]["entries"] > 20)
    check("质检统计了译文数", rep["stats"]["translated"] > 20, str(rep["stats"]))
    # 我们的"翻译"是原文加前缀，术语一致，所以不该有大量不一致
    check("没有误报大量术语不一致", rep["stats"]["inconsistent"] <= 2,
          str(rep["stats"]["inconsistent"]))

    # 人为制造问题，验证质检抓得到。
    # 注意顺序：假翻译器把每条译文都做成"前缀+原文"的可逆函数，
    # 所以"同一原文两种译法"**不会**自然出现，必须手工构造。
    ents = ws.load_entries()
    if len(ents) >= 3:
        ents[0].target = ents[0].source          # 漏翻
        ents[1].warnings = ["placeholder_lost"]  # 占位符坏掉
        # 制造术语不一致：把两条**原文相同**的条目的译文改成不同值
        by_src: dict[str, list] = {}
        for e in ents:
            by_src.setdefault(e.source, []).append(e)
        dup = next((v for v in by_src.values() if len(v) >= 2), None)
        if dup is None:
            # 没有重复原文就手工造一对
            ents.append(ents[2].model_copy(update={
                "uid": "dup-check", "source": ents[2].source, "target": "另一种译法",
            }))
        else:
            dup[0].target = "译法甲"
            dup[1].target = "译法乙"
    ws.save_entries(ents)
    rep2 = run_qa(ws, ctx)
    msgs = " ".join(i["message"] for i in rep2["issues"])
    print("\n    人为制造问题后：")
    for i in rep2["issues"][:6]:
        print(f"      [{i['severity']:7}] {i['message'][:76]}")
    check("质检抓到漏翻（译文=原文）", "与原文相同" in msgs, msgs[:200])
    check("质检抓到占位符损坏", "占位符校验未通过" in msgs, msgs[:200])
    check("质检抓到术语不一致", "多种译法" in msgs, msgs[:200])
    check("占位符问题被判为 error 级",
          any(i["severity"] == "error" and i["stage"] == "placeholder"
              for i in rep2["issues"]))
    check("有 error 时 ok=False", rep2["ok"] is False)

    # ---------------------------------------------------------------
    # 7. 回写：坏译文必须被拒绝
    # ---------------------------------------------------------------
    print("\n[7] 回写")

    # 7a. 回归：字体补丁**不能**按 "action == merge" 过滤。
    # 早先 stage_fonts 把 action 硬编码成 "merge"，而 stage_apply 又只认
    # "merge" —— 于是 replace / fallback_only 策略的产物虽然 ok=True、
    # out_path 也有值，却永远不会被回写：文件躺在工作区里，游戏里毫无变化，
    # 而且**不报错**。这种静默丢弃只能靠针对性断言守住。
    print("  [7a] 字体补丁的 action 过滤（静默丢弃产物的回归）")
    from novaloc.pipeline.stages import font_patch_records  # noqa: PLC0415

    real_patches = ws.read_json("fonts/patches.json", []) or []
    print(f"       本项目的补丁 action: {[p.get('action') for p in real_patches]}")
    check("真实补丁记录了实际策略（不是一律 merge）",
          all(p.get("action") in ("merge", "merge_multi", "replace", "fallback_only", "none")
              for p in real_patches),
          str([p.get("action") for p in real_patches]))
    check("真实补丁带上了 strategy 字段",
          all("strategy" in p for p in real_patches),
          str(real_patches[:1]))

    # 直接喂四种 action，断言哪些会被回写。
    # replace / fallback_only 是**必须**回写的两种 —— 它们正是策略配置项
    # 存在的意义（原字体不能改时整份替换）。
    fake_patches = [
        {"font_id": "a.ttf", "ok": True, "action": "merge",
         "out_path": str(ws.p("fonts", "a.ttf"))},
        {"font_id": "b.ttf", "ok": True, "action": "replace",
         "out_path": str(ws.p("fonts", "b.ttf"))},
        {"font_id": "c.ttf", "ok": True, "action": "fallback_only",
         "out_path": str(ws.p("fonts", "c.ttf"))},
        {"font_id": "d.ttf", "ok": True, "action": "none",
         "out_path": str(ws.p("fonts", "d.ttf"))},
        {"font_id": "e.ttf", "ok": False, "action": "merge",
         "out_path": str(ws.p("fonts", "e.ttf"))},
        {"font_id": "f.ttf", "ok": True, "action": "merge", "out_path": ""},
    ]
    ws.write_json("fonts/patches.json", fake_patches)
    got = {r["font_id"] for r in font_patch_records(ws)}
    print(f"       被判定为可回写: {sorted(got)}")
    check("merge 策略会回写", "a.ttf" in got)
    check("replace 策略会回写（曾经被静默丢弃）", "b.ttf" in got, str(sorted(got)))
    check("fallback_only 策略会回写（曾经被静默丢弃）",
          "c.ttf" in got, str(sorted(got)))
    check("action=none 不回写", "d.ttf" not in got)
    check("ok=False 不回写", "e.ttf" not in got)
    check("out_path 为空不回写", "f.ttf" not in got)
    # 还原成真实补丁，免得影响后面的回写步骤
    ws.write_json("fonts/patches.json", real_patches)

    r = pipe.stage_apply()
    print(f"    apply: {r.message}  {r.stats}")
    check("回写完成", r.ok, r.error)
    check("占位符损坏的条目被拒绝回写",
          r.stats.get("refused", 0) >= 1, str(r.stats))
    check("回写了文本", r.stats.get("translations", 0) > 20, str(r.stats))

    out = ws.out_dir
    out_map = out / "data" / "Map001.json"
    check("产物目录里有地图文件", out_map.is_file(), str(out_map))
    if out_map.is_file():
        obj = json.loads(out_map.read_text(encoding="utf-8"))
        lst = obj["events"][1]["pages"][0]["list"]
        texts = [c[2][0] for c in lst if c[0] == 401 and c[2]]
        print(f"    产物中对白：{texts[:2]}")
        check("产物里的对白是中文", any("【中】" in t for t in texts), str(texts[:2]))
        # 脚本必须原样
        check("产物里脚本指令未变",
              any(c[0] == 355 and c[2] == ["$gameParty.gainGold(500);"] for c in lst))
        # 占位符必须完好
        joined = " ".join(texts)
        check("产物里占位符完好", "\\V[1]" in joined or "\\C[6]" in joined, joined[:120])

    # 原始目录只读
    check("原始游戏目录未被修改",
          json.loads((game / "data" / "Map001.json").read_text(encoding="utf-8"))
          ["events"][1]["pages"][0]["list"][1][2][0]
          == "Welcome, traveler.\\V[1] is waiting for you.")

    # ---------------------------------------------------------------
    # 8. 事件广播
    # ---------------------------------------------------------------
    print("\n[8] 事件广播")
    stages_seen = {s for k, s in events if k in ("stage_start", "stage_end")}
    print(f"    收到 {len(events)} 个事件，涉及阶段：{sorted(stages_seen)}")
    for need in ("detect", "extract", "images_scan", "translate",
                 "fonts", "images_localize", "apply"):
        check(f"阶段 {need} 有事件广播", need in stages_seen, str(sorted(stages_seen)))

    # ---------------------------------------------------------------
    # 9. 全流程可重复运行
    # ---------------------------------------------------------------
    print("\n[9] 全流程整体重跑")
    # 上一节故意往条目里塞了坏数据（漏翻 + 占位符损坏），
    # 质检一定会报 error 并中止。这里先修好，才能验证"完整跑通"。
    fixed = ws.load_entries()
    for e in fixed:
        if e.warnings:
            e.warnings = []
        if e.target and e.target == e.source:
            e.target = "【中】" + e.source
        if e.uid == "dup-check":
            e.target = ""
        if e.target in ("译法甲", "译法乙"):
            e.target = "【中】" + e.source
    ws.save_entries([e for e in fixed if e.uid != "dup-check"])
    stale = run_qa(ws, ctx)
    print(f"    修复后质检: ok={stale['ok']} errors={stale['stats']['errors']}")
    check("修好坏数据后质检恢复通过", stale["ok"],
          str([i["message"][:60] for i in stale["issues"] if i["severity"] == "error"][:3]))

    pipe2 = make_pipeline(ws, ctx, fake_translate)
    try:
        results = pipe2.run_all()
        print(f"    {len(results)} 个阶段：")
        for rr in results:
            print(f"      {'✅' if rr.ok else '❌'} {rr.stage:18} {rr.duration_s:6.2f}s "
                  f"{rr.message[:50]}")
        check("run_all 跑完所有阶段", len(results) == 8, str(len(results)))
        check("所有阶段成功", all(x.ok for x in results),
              str([x.stage for x in results if not x.ok]))
    except PipelineError as exc:
        check("run_all 跑完所有阶段", False, str(exc))

    print("\n" + "=" * 78)
    print("断言汇总")
    print("=" * 78)
    n = 0
    for label, ok, detail in checks:
        n += ok
        print(f"  {'✅' if ok else '❌'} {label}" + (f"   ({detail})" if detail and not ok else ""))
    print(f"\n结论：{n}/{len(checks)} 通过" + ("  ✅" if n == len(checks) else "  ❌"))
    return 0 if n == len(checks) else 1


def test_suite() -> None:
    """pytest 入口：跑一遍完整报告并断言全通过。"""
    assert main() == 0

pytestmark = [
    pytest.mark.slow,
    # 整条流水线要真的把中文渲染进贴图，所以**需要本机有中文字体**。
    # Linux CI runner 没有中文字体；少了这个标记就会红，
    # 而且只报 `assert 1 == 0`，看不出是缺字体。
    pytest.mark.needs_fonts,
]


if __name__ == "__main__":
    raise SystemExit(main())
