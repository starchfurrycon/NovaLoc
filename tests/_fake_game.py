"""合成一个结构真实的 RPG Maker MV 工程，供多个测试套件共用。

## 为什么抽成独立模块

这个工程原本是在 ``test_engine_rpgmaker.py`` 里造的，而且写死到
``tests/fixtures/rpgmaker_game``。于是出现了两个真实问题：

1. **套件之间靠执行顺序耦合**：``test_api_e2e.py`` 与
   ``test_pipeline_e2e.py`` 直接读 ``tests/fixtures/rpgmaker_game``，
   自己**不造**。但它们依赖的 ``data/`` 子目录在 ``.gitignore`` 里
   （``data/`` 第 38 行），全新 clone 出来并不存在 —— 只有
   ``test_engine_rpgmaker.py`` 跑过之后才有。
   单独跑 ``pytest tests/test_api_e2e.py`` 就会莫名其妙地失败，
   而在跑过全量套件的机器上永远是绿的。

2. **往仓库里写垃圾**：最小字体写在 ``rpgmaker_game/_testfont.ttf``，
   没被 ignore，``git add -A`` 会把它提交进去。

现在改成：**每个会话把工程造在自己的临时目录里**（见 ``conftest.py``
的 ``fake_rpgmaker_game`` fixture），谁都不再依赖仓库里那份残留，
也不再污染工作区。``build_fake_game(dest)`` 因此要求显式传入目标目录 ——
不提供默认值，正是为了不再出现"不小心写到仓库里"。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# 自造最小字体：测试不允许依赖"本机装了某个字体"（Linux CI 上没有）
from _minimal_font import build_minimal_ttf


def build_fake_game(dest: Path) -> Path:
    """在 ``dest`` 造一个结构真实的 RPG Maker MV 工程，返回 ``dest``。

    ``dest`` 是必填的：以前有个默认值指向仓库内的 fixture 目录，
    结果既造成跨套件的顺序耦合，又被 ``git add -A`` 把生成物收进版本库。
    """
    game = Path(dest)
    if game.exists():
        shutil.rmtree(game)
    data = game / "data"
    data.mkdir(parents=True)

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

    (game / "js").mkdir()
    (game / "js" / "rpg_core.js").write_text("// engine", encoding="utf-8")
    (game / "fonts").mkdir()
    (game / "fonts" / "gamefont.css").write_text("@font-face{}", encoding="utf-8")
    (game / "img" / "system").mkdir(parents=True)

    # 一张有文字、尺寸够大的贴图（只验证"能被发现"，不做 OCR）
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    from novaloc.images.io import imwrite_bgr

    # 用**自己造的**最小字体，不要写死系统字体路径。
    # 以前这里写的是 `C:\Windows\Fonts\arialbd.ttf`，在开发机上一直能跑，
    # 一到 Linux CI 就是 `OSError: cannot open resource` —— 看起来像功能坏了，
    # 其实只是测试把"本机装了某个字体"当成了前提。
    #
    # 字体写在**临时目录**（不放进工程里，免得被当成游戏资源扫描，
    # 也不再有"生成物留在仓库"的问题）。
    font_path = build_minimal_ttf(Path(dest).parent / "_novaloc_testfont.ttf")
    im = Image.new("RGB", (400, 120), (20, 24, 40))
    ImageDraw.Draw(im).text((20, 30), "NEW GAME",
                            font=ImageFont.truetype(str(font_path), 40),
                            fill=(255, 220, 100))
    imwrite_bgr(game / "img" / "system" / "Window.png", np.asarray(im)[:, :, ::-1])
    # 故意放一张极小的图，验证尺寸过滤真的生效
    imwrite_bgr(game / "img" / "system" / "Tiny.png", np.full((40, 40, 3), 50, "uint8"))
    return game
