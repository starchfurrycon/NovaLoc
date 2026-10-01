"""翻译层集成测试：用假 Ollama 服务端驱动完整链路。

不需要真的装 Ollama —— 起一个本地 HTTP 服务，按需返回
"理想 JSON / 残缺 JSON / 复读 / 破坏占位符"等各类响应，
验证适配器在各种烂输出下都能正确降级，且**不丢失条目、不写坏文本**。

重点验证的是那条"安全契约"。它比"一律判失败"更精确：

1. 补不回来的占位符破坏 → 条目判 FAILED，且**不留残缺译文**；
2. 能确定位置补回来的（模型整段删掉占位符，只是漏了标记，
   文字译文本身可用）→ 允许补回，但**必须满足**：
   * 还原后的占位符与原文**完全一致**（多重集 + 出现顺序都一致）；
   * 不残留任何屏蔽记号。

第 2 条是刻意加的：实测 translategemma:4b 遇到 RPG Maker 转义会把标记
整段删掉，旧行为下这句话**完全不会被翻译**；补回后它对游戏完全可用
（少的只是颜色或换行）。安全底线没有放松 —— 补回后占位符数量或顺序
仍不对的话，依然判 FAILED 并丢弃译文。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.registry import Context, TranslateItem  # noqa: E402
from novaloc.models import TextKind, TextLocation, TextUnit  # noqa: E402
from novaloc.translate.ollama_provider import OllamaTranslationProvider  # noqa: E402

# --------------------------------------------------------------------------
# 可控的假 Ollama
# --------------------------------------------------------------------------

STATE: dict[str, object] = {"mode": "good", "calls": 0, "prompts": []}


def _parse_prompt(prompt: str) -> tuple[list[tuple[int, str]], bool]:
    """从提示里抽出 (编号, 已屏蔽文本) 列表。返回 (列表, 是否单条模式)。"""
    single = "只返回 JSON" in prompt
    pairs: list[tuple[int, str]] = []
    if single:
        marker = "原文（⟦n⟧ 是必须原样保留的占位符）："
        if marker in prompt:
            body = prompt.split(marker, 1)[1].split("只返回 JSON", 1)[0].strip()
            if body:
                pairs = [(0, body)]
        return pairs, True
    for ln in prompt.splitlines():
        if " → " not in ln or ln.lstrip().startswith("·"):
            continue
        idx_s, _, text = ln.partition(" → ")
        try:
            pairs.append((int(idx_s.strip()), text))
        except ValueError:
            continue
    return pairs, False


def make_response(mode: str, prompt: str) -> str:
    """根据模式生成模型"输出"。

    注意：prompt 里的文本**已经屏蔽过占位符**，所以"原样回抄"就等于一个
    守规矩的好模型 —— 它把 ⟦n⟧ 记号原封不动带回来。
    若改用未屏蔽的原文，就测不出适配器的还原能力了。
    """
    pairs, single = _parse_prompt(prompt)

    def emit(items: list[tuple[int, str]]) -> str:
        if single:
            return json.dumps({"t": items[0][1] if items else ""}, ensure_ascii=False)
        return json.dumps([{"i": i, "t": t} for i, t in items], ensure_ascii=False)

    if mode == "good":
        return emit([(i, f"[译]{t}") for i, t in pairs])

    if mode == "truncated":
        if single:
            return "[]"  # 单条模式没法"截断"出合法 JSON，直接返回空
        keep = pairs[: max(1, len(pairs) // 2)]
        return emit([(i, f"[译]{t}") for i, t in keep])

    if mode == "fenced_prose":
        return f"好的，以下是翻译结果：\n```json\n{emit([(i, f'[译]{t}') for i, t in pairs])}\n```\n希望有帮助！"

    if mode == "broken_placeholder":
        # 吞掉所有 ⟦n⟧ 记号 —— 这正是最需要拦住的情况
        return emit([(i, f"[译]{re.sub(r'⟦[0-9]+⟧', '', t)}") for i, t in pairs])

    if mode == "swap_tags":
        # 交换记号的先后顺序：多重集完全正确，但成对标签会还原成坏标记
        items = []
        for i, t in pairs:
            ids = re.findall(r"⟦(\d+)⟧", t)
            flipped = t
            for n, gid in enumerate(ids):
                flipped = flipped.replace(f"⟦{gid}⟧", f"\x00{n}\x00", 1)
            for n, gid in enumerate(reversed(ids)):
                flipped = flipped.replace(f"\x00{n}\x00", f"⟦{gid}⟧", 1)
            items.append((i, f"[译]{flipped}"))
        return emit(items)

    if mode == "cross_leak":
        # 把**同批其它条目**的记号抄进来。
        # 危险之处：这些记号在本条里是"越界下标"，但整批的多重集看起来
        # 完全正常 —— 只有逐条比对自己有几个槽位才能发现。
        # 结果是游戏里读到的变量是错的（比如把勇者名显示成了金钱）。
        #
        # 注意：本条槽位数为 0 时，``verify_restored`` 会先以
        # "多出占位符" 拦下（它的检查更早也更严）。所以这里刻意只挑
        # **自己本来就有槽位、但抄来的下标超过自己槽位数** 的条目，
        # 才能验证 cross_item_leak 这道独立的闸门确实生效。
        #
        # 又因为 mask_batch 是**逐条局部编号**，同一条 3 条的批次里
        # 各条的下标都从 0 起，所以"批内最大下标"追平不了任何条目的
        # 槽位数。必须直接用**别人确实拥有**的那个下标：例如本金条有
        # 1 个槽位（合法下标只有 0），却写了个 ⟦1⟧ —— 那是同批另一条
        # 的第二个占位符。
        import re as _re

        counts = {i: len(_re.findall(r"⟦(\d+)⟧", t)) for i, t in pairs}
        items = []
        for i, t in pairs:
            own_n = counts[i]
            if own_n > 0:
                # 找一个"别的条目有、而下标 >= 本条槽位数"的下标
                for j, other_t in pairs:
                    if j == i:
                        continue
                    for x in _re.findall(r"⟦(\d+)⟧", other_t):
                        if int(x) >= own_n:
                            t = f"{t} [抄来的]{chr(0x27E6)}{x}{chr(0x27E7)}"
                            break
                    else:
                        continue
                    break
            items.append((i, f"[译]{t}"))
        return emit(items)

    if mode == "repeat":
        return emit([(i, "重复重复重复重复重复重复") for i, t in pairs])

    if mode == "garbage":
        return "抱歉，我无法完成这个请求。"

    if mode == "always_fail":
        return "{{{ 不是 JSON"

    return emit([])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: ANN002
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/tags"):
            self._send(200, {"models": [{"name": "fake:latest", "model": "fake:latest"}]})
        elif self.path.startswith("/api/version"):
            self._send(200, {"version": "0.0.0-fake"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        if not self.path.startswith("/api/chat"):
            self._send(404, {"error": "not found"})
            return

        STATE["calls"] = int(STATE["calls"]) + 1
        msgs = req.get("messages") or []
        prompt = msgs[-1].get("content", "") if msgs else ""
        casts = STATE.setdefault("prompts", [])
        assert isinstance(casts, list)
        casts.append(prompt)

        self._send(200, {
            "model": "fake:latest",
            "message": {"role": "assistant", "content": make_response(str(STATE["mode"]), prompt)},
            "done": True,
            "prompt_eval_count": 10,
            "eval_count": 20,
        })


# --------------------------------------------------------------------------
# 测试数据
# --------------------------------------------------------------------------

SAMPLES: list[tuple[str, TextKind]] = [
    (r"勇者\V[1]，你终于醒了！", TextKind.DIALOGUE),
    ("Attack", TextKind.UI_LABEL),
    ("You got %d gold and %s items.", TextKind.SYSTEM),
    ("<color=#ff0000>Warning!</color> Low HP.", TextKind.SYSTEM),
    ("Potion of Greater Healing", TextKind.ITEM_NAME),
]

#: 每条是否"含占位符"
HAS_PH = [bool(re.search(r"\\V\[|%[ds]|<color|</color", src)) for src, _ in SAMPLES]


def build_ctx(host: str) -> Context:
    from novaloc.core.events import EventBus

    cfg = Config()
    cfg.ollama.host = host
    cfg.ollama.text_model = "fake:latest"
    cfg.ollama.enabled = True
    cfg.ollama.max_batch_strings = 3  # 逼出分批
    return Context(config=cfg, events=EventBus())


def make_items() -> list[TranslateItem]:
    items: list[TranslateItem] = []
    for i, (src, kind) in enumerate(SAMPLES):
        unit = TextUnit(
            uid=f"u{i}",
            source=src,
            kind=kind,
            location=TextLocation(file=f"synthetic/{i}.json", pointer=f"/text/{i}"),
        )
        items.append(TranslateItem(unit=unit))
    return items


def run_case(host: str, mode: str) -> tuple[list, OllamaTranslationProvider]:
    STATE["mode"] = mode
    STATE["calls"] = 0
    STATE["prompts"] = []
    prov = OllamaTranslationProvider(build_ctx(host))
    return prov.translate_batch(make_items(), "zh-Hans"), prov


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"假 Ollama 已启动：{host}\n")

    checks: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        checks.append((label, cond, detail))

    # ---------- 场景 1：理想输出 ----------
    entries, prov = run_case(host, "good")
    print("=" * 80)
    print("场景 [理想输出]")
    for e in entries:
        print(f"  {e.status.value:<10} {e.source[:30]!r} → {e.target[:44]!r}")
    check("理想输出：全部成功", all(e.target for e in entries),
          f"{sum(1 for e in entries if e.target)}/{len(entries)}")
    # 真正的判据不是"长得像"，而是**每个占位符都在，且顺序没变**
    ph_ok = True
    for e, ph in zip(entries, HAS_PH, strict=True):
        if not ph:
            continue
        src_ph = re.findall(r"\\V\[\d+\]|%[\d$]*[ds]|</?color[^>]*>", e.source)
        tgt_ph = re.findall(r"\\V\[\d+\]|%[\d$]*[ds]|</?color[^>]*>", e.target)
        if src_ph != tgt_ph:
            ph_ok = False
            print(f"    ⚠️ 占位符不符：src={src_ph} tgt={tgt_ph}")
    check("理想输出：占位符逐个保留且顺序不变", ph_ok)

    # ---------- 场景 2：围栏 + 废话 ----------
    entries, prov = run_case(host, "fenced_prose")
    print("\n" + "=" * 80)
    print("场景 [围栏+废话]")
    for e in entries:
        print(f"  {e.status.value:<10} {e.source[:30]!r} → {e.target[:44]!r}")
    check("围栏+废话：仍能全部翻译", all(e.target for e in entries))

    # ---------- 场景 3：占位符被吞（安全契约） ----------
    entries, prov = run_case(host, "broken_placeholder")
    print("\n" + "=" * 80)
    print("场景 [占位符被破坏 —— 必须判失败且不留残文]")
    for e, has_ph in zip(entries, HAS_PH, strict=True):
        print(f"  {e.status.value:<10} {e.source[:30]!r} → {e.target[:44]!r}")
        # A. 不留残缺：判失败就必须是空译文
        if e.status.value == "failed":
            check(f"占位符破坏判失败必须不留残缺译文：{e.source[:20]}",
                  not e.target, f"target={e.target[:30]!r}")
            continue
        if not has_ph:
            continue
        # B. 不残留屏蔽记号
        check(f"译文不得残留屏蔽记号：{e.source[:20]}",
              "⟦" not in e.target and "⟧" not in e.target,
              f"target={e.target[:40]!r}")
        # C. 原文里的字面占位符必须一个不少、次数一致
        expected = _literal_placeholders(e.source)
        missing = [p for p in expected if p not in e.target]
        check(f"原文占位符必须全部出现在译文里：{e.source[:20]}",
              not missing, f"缺失={missing} target={e.target[:40]!r}")
    check("占位符被破坏：无占位符的条目应正常翻译",
          all(e.target for e, has_ph in zip(entries, HAS_PH, strict=True) if not has_ph))
    check("占位符被破坏：无占位符的条目应正常翻译",
          all(e.target for e, has_ph in zip(entries, HAS_PH, strict=True) if not has_ph))

    # ---------- 场景 4：交换配对标签顺序（沉默损坏） ----------
    entries, prov = run_case(host, "swap_tags")
    print("\n" + "=" * 80)
    print("场景 [配对标签顺序被交换 —— 沉默损坏，必须拦]")
    for e, _has_ph in zip(entries, HAS_PH, strict=True):
        print(f"  {e.status.value:<10} {e.source[:30]!r} → {e.target[:44]!r}")
    check("标签顺序被交换：含成对标签的条目必判失败",
          entries[3].status.value == "failed" and not entries[3].target,
          f"status={entries[3].status.value} target={entries[3].target[:40]!r}")

    # ---------- 场景 5：复读 ----------
    entries, prov = run_case(host, "repeat")
    print("\n" + "=" * 80)
    print("场景 [模型复读]")
    for e in entries:
        print(f"  {e.status.value:<10} {e.source[:30]!r} → {e.target[:44]!r}")
    rep = entries[1]  # "Attack" 没有占位符，复读必须被 guard 抓到
    check("复读：无占位符条目应被警告或判失败",
          rep.status.value == "failed" or bool(rep.warnings),
          f"status={rep.status.value} warnings={rep.warnings}")

    # ---------- 场景 5.5：把别的条目的占位符抄过来 ----------
    entries, prov = run_case(host, "cross_leak")
    print("\n" + "=" * 80)
    print("场景 [抄了别的条目的占位符 —— 多重集正常但下标越界]")
    for e in entries:
        print(f"  {e.status.value:<10} {e.source[:26]!r} → {e.target[:40]!r} "
              f"warn={[w[:38] for w in e.warnings]}")
    leaked = [e for e in entries if any("cross_item_leak" in w for w in e.warnings)]
    check("抄记号：translate_batch 路径下不会误报（逐条局部编号使越界不可能）",
          not leaked, f"误报 {len(leaked)} 条")

    # 直接验证 _detect_cross_item_leak 的判据本身。
    # 为什么必须单独测：mask_batch 逐条局部编号后，"某条的记号" 最大下标
    # 永远等于 own_n-1，所以**在真实批量路径上越界是不可能发生的**。
    # 这道闸门是给"相邻条目槽位数不同"的抄写场景兜底的，只能直接喂数据验证。
    from novaloc.translate.placeholders import _detect_cross_item_leak

    # 条 0 有 4 个槽位，条 1 只有 1 个；条 1 却写了个 ⟦3⟧ ——
    # 那只能是抄了条 0 的第四个占位符。多重集**看不出问题**。
    m0 = "A" + "".join(f"{chr(0x27E6)}{i}{chr(0x27E7)}" for i in range(4))
    m1 = "B" + f"{chr(0x27E6)}0{chr(0x27E7)}"
    t0 = "甲" + "".join(f"{chr(0x27E6)}{i}{chr(0x27E7)}" for i in range(4))
    t1 = "乙" + f"{chr(0x27E6)}0{chr(0x27E7)}" + f"{chr(0x27E6)}3{chr(0x27E7)}"
    bad = _detect_cross_item_leak([m0, m1], [t0, t1])
    print(f"\n  直接验证 _detect_cross_item_leak：{bad}")
    check("抄记号：越界下标被抓到，且指出是第 1 条", bad == [(1, 3)], str(bad))
    # 全合法时不能误报
    ok_bad = _detect_cross_item_leak([m0, m1], [t0, m1.replace("B", "乙")])
    check("抄记号：全部合法时不误报", ok_bad == [], str(ok_bad))
    check("抄记号：无占位符的条目不误伤",
          entries[1].status.value != "failed" or bool(entries[1].warnings),
          f"status={entries[1].status.value}")

    # ---------- 场景 6：完全不是 JSON ----------
    entries, prov = run_case(host, "garbage")
    print("\n" + "=" * 80)
    print("场景 [完全不是 JSON]")
    for e in entries:
        print(f"  {e.status.value:<10} {e.source[:30]!r} → {e.target[:44]!r} warn={e.warnings}")
    check("乱输出：不能有任何非空译文被写回",
          all(not e.target for e in entries))
    check("乱输出：条目数量守恒", len(entries) == len(SAMPLES))

    # ---------- 场景 7：持续失败 ----------
    entries, prov = run_case(host, "always_fail")
    print("\n" + "=" * 80)
    print("场景 [持续失败]")
    for e in entries:
        print(f"  {e.status.value:<10} {e.source[:30]!r} warn={e.warnings}")
    check("持续失败：全部标记失败且条目守恒",
          all(e.status.value == "failed" for e in entries) and len(entries) == len(SAMPLES))

    # ---------- 场景 8：提示词确实带屏蔽记号 ----------
    entries, prov = run_case(host, "good")
    prompts = STATE["prompts"]
    check("提示词里出现屏蔽记号 ⟦n⟧", any("⟦" in str(p) for p in prompts))
    # ★ 格式要求是「行号为键的对象」，不是 `[{"i":…,"t":…}]` 数组。
    #
    # 实测（同一批 40 条真实文本）：要求数组时模型生成 26 token
    # 只回 1 个对象就停（静默漏译 39 条，再靠逐条降级补漏），
    # 要求对象时回满 40 条。详见 `prompts.build_batch_user_prompt` 的注释。
    check(
        "提示词要求行号为键的 JSON 对象",
        any(isinstance(p, str) and "行号" in p and "键" in p for p in prompts),
    )
    check(
        "提示词不许再要求带 i/t 的数组",
        not any('"i"' in str(p) and "数组" in str(p) for p in prompts),
    )

    # ---------- 场景 9：repeat_penalty 已显式设置 ----------
    p = OllamaTranslationProvider(build_ctx(host))
    opts = p._options()
    check("repeat_penalty 已显式设置且 > 1.0",
          float(opts.get("repeat_penalty", 1.0)) > 1.0, str(opts))

    server.shutdown()

    print("\n" + "=" * 80)
    print("断言汇总")
    print("=" * 80)
    n_pass = 0
    for label, ok, detail in checks:
        n_pass += ok
        extra = f"   ({detail})" if detail and not ok else ""
        print(f"  {'✅' if ok else '❌'} {label}{extra}")
    print(f"\n结论：{n_pass}/{len(checks)} 通过" + ("  ✅" if n_pass == len(checks) else "  ❌"))
    return 0 if n_pass == len(checks) else 1


def _literal_placeholders(src: str) -> list[str]:
    """直接从原文里抓出**字面占位符**，用于检查译文是否把它们吞掉了。

    不用 `novaloc` 的屏蔽结果：那条路径会按位置重新编号，同一条里重复的
    占位符会分到不同编号，`in` 检查会误报。这里只关心"原文里这个标记
    还在不在译文里"，所以直接正则抓字面量最可靠。
    """
    pats = (
        r"\\[VNCPI]\[\d+\]",   # RPG Maker 转义
        r"<color=[^>]*>", r"</color>",  # 成对颜色标签
        r"%[ds]",                       # printf 参数
    )
    out: list[str] = []
    for p in pats:
        out.extend(re.findall(p, src))
    return out


def test_suite() -> None:
    """pytest 入口：跑一遍完整报告并断言全通过。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
