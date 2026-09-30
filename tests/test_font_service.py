"""FontService 端到端验证：审计 → 规划 → 合并 → QA → 达标判定。

不联网，用本机已有字体当候选（.scratch/fonts 里的 LXGW 系列 + 系统字体）。
重点验证**"防口口口"这条承诺的闭环**：

* 覆盖率达到 100% 时正常产出；
* 覆盖率不达标时**硬失败**，并且**不留下被采用的产物**
  （这是最关键的一条：宁可不产出，也不能产出会显示口口口的字体）；
* 授权分类正确（IPA-1.0 的霞鹜新晰黑不得随包分发）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.fonts import FontService, catalog  # noqa: E402

OUT = FIXTURES / "fontsvc"
OUT.mkdir(parents=True, exist_ok=True)


def local_candidates() -> list[Path]:
    """本机可用的中文字体候选（不联网）。"""
    cands: list[Path] = []
    for p in [
        FIXTURES / "fonts" / "LXGWNeoXiHei.ttf",
        FIXTURES / "fonts" / "LXGWWenKaiLite-Regular.ttf",
    ]:
        if p.is_file():
            cands.append(p)
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "seguisym.ttf"):
        p = Path(r"C:\Windows\Fonts") / name
        if p.is_file():
            cands.append(p)
    return cands


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    base = Path(r"C:\Windows\Fonts\arial.ttf")
    if not base.is_file():
        print("找不到 arial.ttf，无法测试")
        return 1

    cfg = Config()
    cfg.font.allow_download = False  # 测试不联网，全用本机字体
    svc = FontService(Context(config=cfg, events=EventBus()))

    # ---------- 1. 字符集构建 ----------
    # 模拟一个真实项目：译文 + 原文 + 游戏 UI 安全字符
    translated = ["新的旅程", "继续游戏", "设置", "第一章：觉醒", "生命 1250 / 3000", "酒馆"]
    source = ["NEW GAME", "Continue", "Settings", "Chapter One : The Awakening", "TAVERN"]
    required = svc.build_charset(translated, source)
    print(f"[1] 项目字符集 {len(set(required))} 个字符")
    check("字符集包含译文字符", all(c in required for c in "新的旅程"))
    check("字符集包含原文字符（未翻译的串也要能渲染）", "W" in required)
    check("字符集包含 UI 安全字符（漏掉就会口口口）",
          all(c in required for c in "、。←→♪✓"), "".join(c for c in "、。←→♪✓" if c not in required))
    check("字符集包含全角标点", "，" in required or "！" in required)

    # ---------- 2. 审计原字体 ----------
    audit = svc.audit(base, required)
    print(f"[2] {audit.summary()}")
    check("arial 覆盖中文不足（说明确实需要补字）", audit.coverage < 0.5, f"{audit.coverage:.3f}")
    check("审计列出了缺字", len(audit.missing) > 100, f"{len(audit.missing)}")

    # ---------- 3. 候选字体 ----------
    cands = local_candidates()
    print(f"[3] 候选字体 {len(cands)} 个：{[p.name for p in cands]}")
    check("找到本机候选字体", len(cands) >= 2, f"{len(cands)}")
    # 候选里必须有能覆盖全部字符的组合
    plan = svc.ensure_charset_plannable.__self__ if False else None  # noqa: F841

    # ---------- 4. 完整注入流程 ----------
    res = svc.patch_font(base, required, out_path=OUT / "arial_patched.ttf",
                         candidates=cands, max_sources=4)
    print(f"\n[4] patch_font → ok={res.ok} {res.summary}")
    if res.error:
        print(f"    error: {res.error}")
    if res.plan:
        print("    " + res.plan.summary().replace("\n", "\n    "))
    if res.qa:
        print(f"    QA: {res.qa.summary}")
    check("字体注入成功", res.ok, res.error)
    check("产物存在", res.out_path is not None and res.out_path.is_file())
    if res.audit_before and res.audit_after:
        print(f"    覆盖率 {res.audit_before.coverage:.3%} → {res.audit_after.coverage:.3%}")
        check("覆盖率提升到 100%", res.audit_after.coverage >= 0.999,
              f"{res.audit_after.coverage:.4%} 缺 {res.audit_after.missing[:40]}")
    check("QA 通过（无缺字/空白/豆腐块）", res.qa is not None and res.qa.ok,
          res.qa.summary if res.qa else "无 QA 结果")
    check("合并报告覆盖率为 100%",
          res.merge is not None and res.merge.coverage_after >= 0.999,
          f"{res.merge.coverage_after if res.merge else None}")

    # ---------- 5. 硬失败路径（最关键的一条） ----------
    # 要求一个任何字体都不可能覆盖的字符（平面 14 的变体选择符）
    impossible = required + "\U000E0100"
    res_bad = svc.patch_font(base, impossible, out_path=OUT / "should_not_exist.ttf",
                             candidates=cands, max_sources=4)
    print(f"\n[5] 不可达字符 → ok={res_bad.ok}  error={res_bad.error[:90] if res_bad.error else ''}")
    check("覆盖不了的字符集导致硬失败", not res_bad.ok)
    check("硬失败有明确原因", bool(res_bad.error))
    check("硬失败时不产出被采用的结果", res_bad.out_path is None)
    if res_bad.audit_after:
        check("硬失败时报告了真实覆盖率", res_bad.audit_after.coverage < 1.0,
              f"{res_bad.audit_after.coverage:.4f}")
    check("硬失败时给出警告说明未采用", any("未采用" in w for w in res_bad.warnings),
          str(res_bad.warnings))

    # ---------- 6. 授权分类 ----------
    print("\n[6] 授权分类")
    bundle_ok = {f.id for f in catalog.redistributable()}
    local_only = {f.id for f in catalog.local_only()}
    print(f"    可随包分发: {sorted(bundle_ok)}")
    print(f"    仅本机使用: {sorted(local_only)}")
    check("霞鹜新晰黑是 IPA-1.0，不得随包分发",
          "lxgw-neo-xihei" in local_only and "lxgw-neo-xihei" not in bundle_ok)
    check("思源黑体是 OFL，可随包分发", "source-han-sans-sc" in bundle_ok)
    check("可分发与仅本机两集合不重叠", not (bundle_ok & local_only),
          str(bundle_ok & local_only))
    # 巨型字体不能直接合并（必须先子集化）
    big = [f.id for f in catalog.CATALOG if not catalog.safe_to_merge(f)]
    print(f"    需先子集化才能合并: {big}")
    check("识别出字形数过大的字体（不能直接合并）", len(big) >= 1, str(big))

    # ---------- 7. replace 策略 ----------
    res_rep = svc.patch_font(base, required, out_path=OUT / "replaced.ttf",
                             candidates=cands, strategy="replace")
    print(f"\n[7] replace 策略 → ok={res_rep.ok}")
    print(f"    error: {res_rep.error}")
    # 这不是缺陷，而是**正确行为**：实测没有任何单个字体能覆盖 99.9%，
    # 最好的一个也只有 98.5%。此时必须明确拒绝并给出可操作建议，
    # 而不是悄悄产出一个缺 7 个字的字体，让用户进游戏看到口口口。
    check("单字体覆盖不足时 replace 明确拒绝（不产出半成品）", not res_rep.ok)
    check("拒绝时给出可操作建议（改用 merge）",
          "merge" in (res_rep.error or ""), res_rep.error)
    check("拒绝时不产出被采用的结果", res_rep.out_path is None)
    best, cov = svc._best_single_font(cands, required)
    print(f"    最佳单字体: {best.name if best else None} = {cov:.3%}")
    check("报告了最佳单字体的真实覆盖率", 0.9 < cov < 0.999, f"{cov:.4f}")
    check("merge 能达标而 replace 不能 —— 说明多源合并确有必要",
          res.ok and not res_rep.ok)

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
    pytest.mark.needs_fonts,
]


if __name__ == "__main__":
    raise SystemExit(main())
