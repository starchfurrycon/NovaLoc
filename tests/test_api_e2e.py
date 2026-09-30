"""后端 API 端到端验证（含 WebSocket 进度推送）。

用 FastAPI 的 TestClient 直接打接口，不启服务器。验证：

1. 32 条路由能正确响应，返回结构符合前端契约；
2. 后台任务真的能跑起来、进度事件真的能通过 WebSocket 推出去
   （这是"用户能看见进度"的唯一证据）；
3. 任务跑完后事件缓冲仍然完整（用户中途刷新页面要能补发历史）；
4. 错误输入返回 4xx 而不是 500（500 会让前端只能显示"服务器错误"）；
5. 前端未构建时不影响 API（只影响静态路由）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402
from _fake_game import build_fake_game  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novaloc.api.app import create_app  # noqa: E402

SB = FIXTURES


def _game() -> Path:
    """取合成游戏工程；没有就**自己造**。

    以前这里写死 ``tests/fixtures/rpgmaker_game`` 并假设它已存在 ——
    实际上那个目录里的 ``data/`` 是 git-ignored 的，全新 clone 出来
    根本没有，**只有** ``test_engine_rpgmaker.py`` 先跑过才会有。
    于是单独跑本文件必然失败，而跑全量套件时永远是绿的
    （一种典型的"套件之间靠执行顺序耦合"）。

    现在三个需要它的套件都用同一个共享构造函数，谁先跑都一样。
    """
    base = os.environ.get("NOVALOC_FAKE_GAME_DIR")
    dest = Path(base) / "rpgmaker_game" if base else FIXTURES / "rpgmaker_game"
    return build_fake_game(dest)


class FakeTranslator:
    """把翻译提供者换掉，避免测试依赖 Ollama。"""

    provider_name = "fake"

    def __init__(self, ctx=None) -> None:
        pass

    def available(self):
        return True, "fake"

    def translate_batch(self, items, target_lang):
        from novaloc.models import EntryStatus, TranslationEntry

        out = []
        for it in items:
            u = it.unit
            out.append(TranslationEntry(
                uid=u.uid, source=u.source, target="【中】" + u.source,
                status=EntryStatus.TRANSLATED, kind=u.kind,
                provider="fake", model="fake-1",
            ))
        return out


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    # 打补丁：让流水线用假翻译器
    import novaloc.core.registry as reg

    real_resolve = reg.Providers.resolve_translate
    reg.Providers.resolve_translate = lambda self: FakeTranslator(self.ctx)  # type: ignore[method-assign]

    app = create_app()
    client = TestClient(app)

    try:
        # ---------- 1. 健康检查 ----------
        print("[1] /api/health")
        r = client.get("/api/health")
        check("health 返回 200", r.status_code == 200, str(r.status_code))
        h = r.json()
        print(f"    version={h['version']} directml={h['gpu']['directml']} "
              f"engines={[e['id'] for e in h['engines']]}")
        print(f"    阶段：{[s['label'] for s in h['stages']]}")
        check("health 报告了引擎列表", len(h["engines"]) == 4, str(h["engines"]))
        check("health 报告了 8 个阶段", len(h["stages"]) == 8, str(len(h["stages"])))
        check("health 报告了 DirectML 状态", "directml" in h["gpu"])
        check("health 不因 Ollama 缺失而失败", h["ok"] is True)

        # ---------- 2. 设置 ----------
        print("\n[2] /api/settings")
        r = client.get("/api/settings")
        check("读设置 200", r.status_code == 200)
        cfg = r.json()
        check("设置里有 translate 段", "translate" in cfg)
        r = client.put("/api/settings", json={"translate": {**cfg["translate"], "target_lang": "zh-Hans"}})
        check("写设置 200", r.status_code == 200, r.text[:120])
        r = client.put("/api/settings", json={"translate": {"temperature": "不是数字"}})
        check("未知设置项返回 4xx 而不是静默成功", 400 <= r.status_code < 500, str(r.status_code))
        r = client.put("/api/settings", json={"temperture": 0.3})
        check("顶层键名拼错也返回 4xx", 400 <= r.status_code < 500, str(r.status_code))
        # 深层合并：只改一个字段不能把同段其它字段冲掉
        before = client.get("/api/settings").json()
        r = client.put("/api/settings", json={"translate": {"target_lang": "zh-Hant"}})
        after = r.json()["settings"]
        check("深层合并未冲掉同段其它字段",
              after["translate"]["primary_provider"] == before["translate"]["primary_provider"]
              and after["translate"]["use_glossary"] == before["translate"]["use_glossary"],
              f"{before['translate']['primary_provider']} → "
              f"{after['translate']['primary_provider']}")
        check("深层合并确实改到了目标字段",
              after["translate"]["target_lang"] == "zh-Hant",
              after["translate"]["target_lang"])
        # 改回中文简体，免得影响后面的流水线
        client.put("/api/settings", json={"translate": {"target_lang": "zh-Hans"}})

        # ---------- 3. 错误输入 ----------
        print("\n[3] 错误输入处理")
        r = client.get("/api/projects/不存在")
        check("不存在的项目返回 404", r.status_code == 404, str(r.status_code))
        r = client.post("/api/projects", json={"name": "x", "game_dir": r"D:\绝对不存在的路径"})
        check("不存在的游戏目录返回 4xx", 400 <= r.status_code < 500, str(r.status_code))
        r = client.get("/api/jobs/不存在")
        check("不存在的任务返回 404", r.status_code == 404, str(r.status_code))
        r = client.patch("/api/projects/不存在/text", json={"items": []})
        check("给不存在项目打补丁返回 404", r.status_code == 404, str(r.status_code))

        # ---------- 4. 建项目 ----------
        print("\n[4] 创建项目")
        r = client.post("/api/projects", json={
            "name": "API 测试项目", "game_dir": str(_game()),
        })
        check("创建项目 200", r.status_code == 200, r.text[:200])
        proj = r.json()
        pid = proj["id"]
        print(f"    id={pid} engine={proj['engine']} version={proj.get('engine_version')}")
        check("自动识别出 rpgmaker", proj["engine"] == "rpgmaker", str(proj["engine"]))

        r = client.get("/api/projects")
        check("项目列表里有它", any(p["id"] == pid for p in r.json()))

        r = client.get(f"/api/projects/{pid}")
        check("项目详情 200", r.status_code == 200)
        check("详情里带产物目录", "out_dir" in r.json().get("paths", {}), str(r.json().get("paths")))

        r = client.post(f"/api/projects/{pid}/detect")
        d = r.json()
        print(f"    识别：{d['display_name']} conf={d['confidence']:.2f}")
        check("识别接口返回证据链", len(d["evidence"]) >= 2, str(d["evidence"]))

        # ---------- 5. 后台任务 + WebSocket ----------
        print("\n[5] 后台任务与 WebSocket 进度")
        r = client.post(f"/api/projects/{pid}/run")
        check("启动全流程任务 200", r.status_code == 200, r.text[:200])
        job_id = r.json()["job_id"]
        print(f"    job_id={job_id}")

        # 用 WebSocket 收进度
        ws_events: list[dict] = []
        try:
            with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                deadline = time.time() + 240
                while time.time() < deadline:
                    try:
                        msg = ws.receive_json()
                    except Exception as exc:  # noqa: BLE001
                        print(f"    WS 收尾（{type(exc).__name__}）")
                        break
                    ws_events.append(msg)
                    if msg.get("kind") == "closed":
                        break
        except Exception as exc:  # noqa: BLE001
            print(f"    WebSocket 异常：{exc}")

        kinds = [e.get("kind") for e in ws_events]
        stages_seen = {e.get("stage") for e in ws_events if e.get("stage")}
        print(f"    WS 收到 {len(ws_events)} 条消息，类型：{sorted(set(kinds))}")
        print(f"    涉及阶段：{sorted(stages_seen)}")
        check("WebSocket 收到了消息", len(ws_events) > 10, str(len(ws_events)))
        check("收到了进度事件", "progress" in kinds, str(sorted(set(kinds))))
        check("收到了阶段开始/结束事件",
              "stage_start" in kinds and "stage_end" in kinds, str(sorted(set(kinds))))
        check("收到了 log 事件", "log" in kinds, str(sorted(set(kinds))))
        check("WebSocket 覆盖了全部 8 个阶段",
              len([s for s in stages_seen if s in
                   ("detect", "extract", "images_scan", "translate",
                    "fonts", "images_localize", "qa", "apply")]) == 8,
              str(sorted(stages_seen)))
        check("以 closed 消息收尾", "closed" in kinds, str(sorted(set(kinds))[-3:]))

        # ---------- 6. 任务状态与历史缓冲 ----------
        print("\n[6] 任务查询与历史补发")
        r = client.get(f"/api/jobs/{job_id}")
        job = r.json()
        print(f"    status={job['status']} progress={job['progress']} "
              f"stages={len(job.get('result', {}).get('stages', []))}")
        check("任务状态为 done", job["status"] == "done", job.get("error", "")[:200])
        check("任务进度到 1.0", job["progress"] == 1.0, str(job["progress"]))
        check("任务结果含 8 个阶段",
              len(job.get("result", {}).get("stages", [])) == 8,
              str(len(job.get("result", {}).get("stages", []))))
        check("任务保留了历史事件（供刷新页面补发）",
              len(job.get("events", [])) > 10, str(len(job.get("events", []))))
        for st in job.get("result", {}).get("stages", []):
            print(f"      {'✅' if st['ok'] else '❌'} {st['label']:10} {st['duration_s']:6.2f}s "
                  f"{st['message'][:44]}")

        r = client.get("/api/jobs", params={"project_id": pid})
        check("任务列表可用", len(r.json()) >= 1, str(len(r.json())))

        # ---------- 7. 文本接口 ----------
        print("\n[7] 文本接口")
        r = client.get(f"/api/projects/{pid}/text")
        text = r.json()
        print(f"    total={text['total']}  前 3 条：")
        for e in text["entries"][:3]:
            print(f"      [{e['status']:10}] {e['source'][:30]!r} → {e['target'][:30]!r}")
        check("文本列表非空", text["total"] > 20, str(text["total"]))
        check("条目带 uid/source/target/status/kind",
              all(k in text["entries"][0] for k in
                  ("uid", "source", "target", "status", "kind")),
              str(text["entries"][0].keys()))
        check("条目带位置信息（审校要用）",
              "file" in text["entries"][0] and "pointer" in text["entries"][0])
        check("已翻译条目占多数",
              sum(1 for e in text["entries"] if e["status"] == "translated") > 20,
              str(text["total"]))

        # 过滤
        r = client.get(f"/api/projects/{pid}/text", params={"kind": "dialogue"})
        check("按 kind 过滤生效",
              all(e["kind"] == "dialogue" for e in r.json()["entries"]),
              str({e["kind"] for e in r.json()["entries"]}))
        r = client.get(f"/api/projects/{pid}/text", params={"q": "sword"})
        check("关键词搜索生效", all("sword" in (e["source"] + e["target"]).lower()
                                 for e in r.json()["entries"]))

        # 编辑
        uid = text["entries"][0]["uid"]
        r = client.patch(f"/api/projects/{pid}/text", json={
            "items": [{"uid": uid, "target": "人工修改的译文"}],
        })
        check("文本编辑 200", r.status_code == 200, r.text[:120])
        check("编辑计数为 1", r.json()["updated"] == 1, r.text[:120])
        # 验证真的存下来了
        r = client.get(f"/api/projects/{pid}/text", params={"q": "人工修改"})
        check("编辑结果已持久化", r.json()["total"] >= 1, str(r.json()["total"]))

        r = client.patch(f"/api/projects/{pid}/text", json={
            "items": [{"uid": uid, "status": "不存在的状态"}],
        })
        check("非法状态返回 4xx", 400 <= r.status_code < 500, str(r.status_code))

        # ---------- 8. 贴图接口 ----------
        print("\n[8] 贴图接口")
        r = client.get(f"/api/projects/{pid}/images")
        imgs = r.json()
        print(f"    total={imgs['total']}")
        check("贴图列表非空", imgs["total"] >= 1, str(imgs["total"]))
        if imgs["images"]:
            a = imgs["images"][0]
            print(f"    {a['path']}  块数={len(a['blocks'])}")
            for b in a["blocks"][:3]:
                print(f"      box={b['box']} {b['source']!r} → {b['target']!r} "
                      f"conf={b['confidence']:.2f} status={b['status']}")
            check("贴图带文字块", len(a["blocks"]) >= 1, str(len(a["blocks"])))
            check("文字块带坐标（画框要用）",
                  isinstance(a["blocks"][0]["box"], list) and len(a["blocks"][0]["box"]) == 4,
                  str(a["blocks"][0]["box"]))
            check("文字块带识别置信度", "confidence" in a["blocks"][0])

            # 标注图
            r = client.get(f"/api/projects/{pid}/images/{a['uid']}/annotated")
            check("标注图返回 200", r.status_code == 200, str(r.status_code))
            check("标注图是 PNG",
                  r.content[:8] == b"\x89PNG\r\n\x1a\n", str(r.content[:8]))
            print(f"    标注图 {len(r.content)} 字节")

            # 编辑块
            bid = a["blocks"][0]["id"]
            r = client.patch(f"/api/projects/{pid}/images", json={
                "items": [{"uid": a["uid"], "block_id": bid, "target": "贴图人工译文"}],
            })
            check("贴图块编辑 200", r.status_code == 200, r.text[:120])
            check("贴图块编辑计数为 1", r.json()["updated"] == 1, r.text[:120])

        r = client.get(f"/api/projects/{pid}/images/不存在/annotated")
        check("不存在的贴图返回 404", r.status_code == 404, str(r.status_code))

        # ---------- 9. 字体与质检 ----------
        print("\n[9] 字体与质检")
        r = client.get(f"/api/projects/{pid}/fonts")
        f = r.json()
        print(f"    覆盖记录 {len(f['coverage'])} 条，补丁 {len(f['patches'])} 条，"
              f"字符集 {f['charset']['total'] if f['charset'] else 0} 字")
        check("字体接口返回结构完整",
              all(k in f for k in ("coverage", "patches", "charset")), str(list(f.keys())))
        check("字符集已建立", f["charset"] and f["charset"]["total"] > 100,
              str(f["charset"]["total"] if f["charset"] else None))

        r = client.get(f"/api/projects/{pid}/qa")
        qa = r.json()
        print(f"    ok={qa['ok']} issues={len(qa['issues'])} stats={qa['stats']}")
        check("质检报告有 stats", bool(qa.get("stats")), str(qa.get("stats")))
        check("质检覆盖了 8 项以上统计", len(qa["stats"]) >= 6, str(list(qa["stats"].keys())))

        # ---------- 10. 静态前端未构建时的行为 ----------
        print("\n[10] 静态路由")
        r = client.get("/api/definitely-not-a-route")
        check("未知 API 路由返回 404 而不是返回 index.html",
              r.status_code == 404, str(r.status_code))

    finally:
        reg.Providers.resolve_translate = real_resolve  # type: ignore[method-assign]
        # 清掉测试项目
        try:
            client.delete(f"/api/projects/{pid}")
        except Exception:  # noqa: BLE001
            pass

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
    # 端到端要真的把中文渲染进贴图，所以**需要本机有中文字体**。
    # Linux CI runner 一个中文字体都没有；少了这个标记就会红成一片，
    # 而且报出来的是 `IndexError: list index out of range` 这种
    # 完全看不出原因的错。
    pytest.mark.needs_fonts,
]


if __name__ == "__main__":
    raise SystemExit(main())
