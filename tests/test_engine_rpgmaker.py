"""RPG Maker MV/MZ 适配器验证：识别 → 抽取 → 翻译 → 回写 → 往返校验。

用**合成**的 RPG Maker 工程来测，因为真实工程体积大且不适合入库。
合成的数据结构严格照抄 MV/MZ 的真实形态（指令二维数组、terms 嵌套、
转义码、插件标签），这样才能真正验证解析与回写。

最关键的一条：**回写后重新读出来的译文必须和写进去的一字不差**，
而且**没有被翻译的字段必须原封不动**（尤其是脚本和数值公式）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines.rpgmaker import RpgMakerAdapter  # noqa: E402

GAME = FIXTURES / "rpgmaker_game"


def build_fake_game() -> Path:
    """造一个结构真实的 RPG Maker MV 工程。"""
    if GAME.exists():
        shutil.rmtree(GAME)
    data = GAME / "data"
    (data).mkdir(parents=True)

    # System.json：含 terms 嵌套 + 数值字段（不该被翻）
    (data / "System.json").write_text(json.dumps({
        "gameTitle": "The Legend of Testing",
        "currencyUnit": "Gold",
        "versionId": 12345,
        "partyMembers": [1, 2],
        "terms": {
            "basic": ["Level", "Lv", "HP", "MP", "TP", "Experience"],
            "commands": ["Fight", "Escape", "Attack", "Guard", "Item", "Skill", "Equip", "Status",
                         "Formation", "Save", "Quit"],
            "params": ["Max HP", "Max MP", "Attack", "Defense", "M.Attack", "M.Defense",
                       "Agility", "Luck"],
            "messages": {
                "actionFailure": "There was no effect on %1!",
                "actorDamage": "%1 took %2 damage!",
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    # Items.json：含 note 插件标签（只翻标签外的自然语言）
    (data / "Items.json").write_text(json.dumps([
        None,
        {
            "id": 1, "name": "Health Potion",
            "description": "Restores 500 HP.\\nTastes faintly of mint.",
            "note": "<CustomEffect:heal:500> A basic healing draught.",
            "price": 100, "consumable": True,
        },
        {
            "id": 2, "name": "Iron Sword",
            "description": "A sturdy blade. Attack +15.",
            "note": "<PassiveSkill:5>",  # 纯标签，不该产生文本单元
            "price": 800, "params": [0, 15, 0, 0, 0, 0, 0, 0],
        },
    ], ensure_ascii=False), encoding="utf-8")

    # MapInfos.json
    (data / "MapInfos.json").write_text(json.dumps([
        None,
        {"id": 1, "name": "Town of Beginnings", "parentId": 0, "order": 1},
        {"id": 2, "name": "Dark Cavern", "parentId": 0, "order": 2},
    ], ensure_ascii=False), encoding="utf-8")

    # Map001.json：事件指令（对白 / 选项 / 脸图名 / 脚本 / 注释）
    (data / "Map001.json").write_text(json.dumps({
        "displayName": "Town of Beginnings",
        "events": [
            None,
            {
                "id": 1, "name": "Village Elder",
                "pages": [{
                    "conditions": {},
                    "list": [
                        [101, 0, ["Face1", 0, 0, 0, "Elder"]],
                        [401, 0, ["Welcome, traveler.\\V[1] is waiting for you."]],
                        [401, 0, ["The sword costs \\C[6]500\\C[0] gold."]],
                        [102, 0, ["Buy the sword", "Ask about the cave", "Leave"]],
                        [402, 0, [0]],
                        [401, 0, ["A wise choice."]],
                        [402, 0, [1]],
                        [401, 0, ["Beware the \\N[2] lurking within."]],
                        [402, 0, [2]],
                        [0, 0, []],
                        # 脚本指令：绝不能被翻译
                        [355, 0, ["$gameParty.gainGold(500);"]],
                        [655, 0, ["console.log('debug message here');"]],
                        # 注释：不翻
                        [108, 0, ["This event handles the shop intro. Do not translate."]],
                    ],
                }],
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")

    (GAME / "js").mkdir()
    (GAME / "js" / "rpg_core.js").write_text("// engine", encoding="utf-8")
    (GAME / "fonts").mkdir()
    (GAME / "fonts" / "gamefont.css").write_text("@font-face{}", encoding="utf-8")
    (GAME / "img" / "system").mkdir(parents=True)
    # 一张有文字、尺寸够大的贴图（只验证"能被发现"，不做 OCR）
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    from novaloc.images.io import imwrite_bgr
    im = Image.new("RGB", (400, 120), (20, 24, 40))
    ImageDraw.Draw(im).text((20, 30), "NEW GAME",
                            font=ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 40),
                            fill=(255, 220, 100))
    imwrite_bgr(GAME / "img" / "system" / "Window.png", np.asarray(im)[:, :, ::-1])
    # 故意放一张极小的图，验证尺寸过滤真的生效
    imwrite_bgr(GAME / "img" / "system" / "Tiny.png", np.full((40, 40, 3), 50, "uint8"))
    return GAME


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    game = build_fake_game()
    ctx = Context(config=Config(), events=EventBus())
    ad = RpgMakerAdapter(ctx)

    # ---------- 1. 识别 ----------
    info = ad.detect(game)
    print(f"[1] detect → {info.display_name} v{info.version} conf={info.confidence:.2f}")
    for e in info.evidence:
        print(f"      · {e}")
    check("识别为 RPG Maker", info.engine_id == "rpgmaker")
    check("置信度高", info.confidence >= 0.8, f"{info.confidence}")
    check("判定为 MV", info.version == "MV", info.version)
    check("识别出游戏标题", any("Legend of Testing" in e for e in info.evidence))

    # 非 RPG Maker 目录不该误判
    other = FIXTURES / "not_a_game"
    other.mkdir(parents=True, exist_ok=True)
    check("非 RPG Maker 目录不被误判", not ad.detect(other).ok,
          f"conf={ad.detect(other).confidence}")

    # ---------- 2. 抽取 ----------
    units, report = ad.extract_text(game)
    print(f"\n[2] 抽取 {len(units)} 条  扫描 {report.files_scanned} 文件  "
          f"命中 {report.files_matched}")
    print("    文本位置与内容：")
    for u in units:
        print(f"      {u.kind.value:16} {u.location.file}{u.location.pointer:38} {u.source[:44]!r}")
    check("抽到足够多的文本", len(units) >= 25, f"{len(units)}")

    kinds = {u.kind.value for u in units}
    check("识别出对白类型", "dialogue" in kinds, str(kinds))
    check("识别出菜单类型", "menu" in kinds or "ui_label" in kinds, str(kinds))
    check("识别出角色名类型", "character_name" in kinds, str(kinds))
    check("识别出地图名类型", "map_name" in kinds, str(kinds))

    srcs = {u.source for u in units}
    # 脚本 / 注释绝不能被抽出来
    check("脚本指令未被抽取（355/655）",
          not any("gainGold" in s or "console.log" in s for s in srcs),
          str([s for s in srcs if "gainGold" in s or "console" in s]))
    check("注释指令未被抽取（108）",
          not any("Do not translate" in s for s in srcs))
    # 数值字段不该被抽
    check("数值/结构字段未被抽取",
          not any(s in ("12345", "100", "800", "Face1") for s in srcs),
          str([s for s in srcs if s in ("12345", "100", "800", "Face1")]))
    # 纯插件标签不该产生单元
    check("纯插件标签不产生文本单元", "<PassiveSkill:5>" not in srcs)

    # 转义码必须原样保留在源串里
    escape_units = [u for u in units if "\\V[" in u.source or "\\N[" in u.source or "\\C[" in u.source]
    check("含转义码的文本被正确抽取", len(escape_units) >= 2,
          str([u.source for u in escape_units]))
    print(f"    含转义码的条目 {len(escape_units)} 条")

    # ---------- 3. 贴图发现 ----------
    imgs, irep = ad.extract_images(game)
    print(f"\n[3] 发现贴图 {len(imgs)} 张：{[i.path for i in imgs]}")
    check("发现 img/ 下的候选贴图", len(imgs) >= 1, f"{len(imgs)}")
    check("过小的图被尺寸过滤掉", not any("Tiny" in i.path for i in imgs),
          str([i.path for i in imgs]))

    # ---------- 4. 字体发现 ----------
    fonts = ad.discover_fonts(game)
    print(f"[4] 发现游戏自带字体 {len(fonts)} 个")
    check("字体发现接口可用（无字体时不报错）", isinstance(fonts, list))

    # ---------- 5. 回写 ----------
    # 译文用"【译】+ 原文"构造，但**不能截断** —— 早先用 source[:20] 切片
    # 把插件标签 `<CustomEffect:heal:500>` 截成了 `...:5`，
    # 于是测试自己造出一个坏数据然后又去断言它没坏。真实翻译层是靠
    # 占位符掩码保证标签不变的，所以这里保持原文完整。
    translations = {u.uid: f"【译】{u.source}" for u in units}
    out = FIXTURES / "rpgmaker_out"
    res = ad.apply(game, out, units, translations)
    print(f"\n[5] apply → ok={res.ok} 写入 {res.files_written} 文件 "
          f"跳过 {res.files_skipped}  警告 {len(res.warnings)}")
    for w in res.warnings[:5]:
        print(f"      ⚠️ {w}")
    check("回写成功", res.ok, res.error)
    check("写入了多个 JSON 文件", res.files_written >= 3, f"{res.files_written}")

    # ---------- 6. 往返校验（最关键） ----------
    ad2 = RpgMakerAdapter(ctx)
    units2, _ = ad2.extract_text(out)
    got = {u.uid: u.source for u in units2}
    print(f"\n[6] 往返校验：原始 {len(units)} 条 → 回写后读出 {len(units2)} 条")
    mismatch = []
    for u in units:
        want = translations[u.uid]
        have = got.get(u.uid)
        if have != want:
            mismatch.append((u.uid, want, have))
    check("所有译文一字不差地写回并可读出", not mismatch,
          f"{len(mismatch)} 处不符，例如 {mismatch[:3]}")
    check("条目数一致", len(units2) == len(units), f"{len(units2)} vs {len(units)}")

    # 未被翻译的内容必须原封不动
    orig_map = json.loads((game / "data" / "Map001.json").read_text(encoding="utf-8"))
    out_map = json.loads((out / "data" / "Map001.json").read_text(encoding="utf-8"))
    olist = orig_map["events"][1]["pages"][0]["list"]
    nlist = out_map["events"][1]["pages"][0]["list"]
    script_ok = any(c[0] == 355 and c[2] == ["$gameParty.gainGold(500);"] for c in nlist)
    check("脚本指令原封不动", script_ok)
    comment_ok = any(c[0] == 108 and "Do not translate" in c[2][0] for c in nlist)
    check("注释原封不动", comment_ok)
    check("指令条数未变", len(olist) == len(nlist), f"{len(olist)} vs {len(nlist)}")

    # note 里的插件标签必须保留
    orig_items = json.loads((game / "data" / "Items.json").read_text(encoding="utf-8"))
    out_items = json.loads((out / "data" / "Items.json").read_text(encoding="utf-8"))
    check("note 里的插件标签未被破坏",
          "<CustomEffect:heal:500>" in out_items[1]["note"],
          out_items[1]["note"])

    # 插件标签在**翻译层**也必须被保护住（掩码 → 还原，标签一字不动）
    from novaloc.translate.placeholders import mask, verify_restored
    note = "<CustomEffect:heal:500> A basic healing draught."
    m = mask(note)
    print(f"\n[6b] 插件标签掩码：{note!r}\n      → {m.text!r}")
    check("插件标签被识别为占位符", "<CustomEffect:heal:500>" in m.slots,
          str(m.slots))
    # 模拟模型把自然语言译成中文、标签原样带回来
    simulated = m.text.replace("A basic healing draught.", "一瓶基础的疗伤药剂")
    restored, chk = verify_restored(note, simulated, m.slots, masked_source=m.text)
    check("标签原样返回时校验通过", not chk.fatal, str(chk))
    check("还原后得到正确译文",
          restored == "<CustomEffect:heal:500> 一瓶基础的疗伤药剂", restored)
    print(f"      还原 → {restored!r}  fatal={chk.fatal}")

    # 标签被模型改坏时必须报错（而不是静默产出坏数据）
    broken = m.text.replace("\u27e60\u27e7", "")
    _r2, chk2 = verify_restored(note, broken, m.slots, masked_source=m.text)
    check("标签丢失时校验报错（不静默放过）", chk2.fatal, str(chk2))
    # 顺序交换也必须被抓到
    two = "<A:1> foo <B:2>"
    m2 = mask(two)
    if len(m2.slots) >= 2:
        swapped = m2.text.replace("\u27e60\u27e7", "\u0001").replace(
            "\u27e61\u27e7", "\u27e60\u27e7").replace("\u0001", "\u27e61\u27e7")
        _r3, chk3 = verify_restored(two, swapped, m2.slots, masked_source=m2.text)
        check("两个标签顺序被交换时报错", chk3.fatal, str(chk3))
    check("数值字段（价格/参数）未被改动",
          out_items[1]["price"] == orig_items[1]["price"]
          and out_items[2]["params"] == orig_items[2]["params"])

    # ---------- 7. 原目录只读 ----------
    check("原游戏目录未被修改",
          json.loads((game / "data" / "Items.json").read_text(encoding="utf-8"))[1]["name"]
          == "Health Potion")

    # ---------- 8. 指针定位失败要能被发现 ----------
    from novaloc.models import TextLocation, TextUnit
    bad = TextUnit(uid="bad", source="x",
                   location=TextLocation(file="Items.json", pointer="/999/name"))
    ok = RpgMakerAdapter._set_pointer({"a": 1}, bad.location.pointer, "y")
    check("非法指针定位返回 False（不静默改错位置）", ok is False)

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


if __name__ == "__main__":
    raise SystemExit(main())
