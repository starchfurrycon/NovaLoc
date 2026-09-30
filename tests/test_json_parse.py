"""验证 JSON 恢复阶梯：本地小模型的各种"翻车"输出都要能救回来。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate import json_parse as jp  # noqa: E402

EXPECT = [0, 1, 2]

CASES: list[tuple[str, str, str, list[int]]] = [
    (
        "理想输出",
        '[{"i": 0, "t": "你好"}, {"i": 1, "t": "世界"}, {"i": 2, "t": "再见"}]',
        "raw",
        [0, 1, 2],
    ),
    (
        "带 Markdown 围栏",
        '```json\n[{"i": 0, "t": "你好"}, {"i": 1, "t": "世界"}, {"i": 2, "t": "再见"}]\n```',
        "fence",
        [0, 1, 2],
    ),
    (
        "前后有废话",
        '好的，以下是翻译结果：\n[{"i": 0, "t": "你好"}, {"i": 1, "t": "世界"}, {"i": 2, "t": "再见"}]\n希望有帮助！',
        "balanced[",
        [0, 1, 2],
    ),
    (
        "尾随逗号",
        '[{"i": 0, "t": "你好"}, {"i": 1, "t": "世界"}, {"i": 2, "t": "再见"},]',
        "raw+loose",
        [0, 1, 2],
    ),
    (
        "中文引号",
        '[{“i”: 0, “t”: “你好”}, {“i”: 1, “t”: “世界”}, {“i”: 2, “t”: “再见”}]',
        "raw+loose",
        [0, 1, 2],
    ),
    (
        "译文里含方括号与花括号（括号配平必须跳过字符串内）",
        '[{"i": 0, "t": "使用 [物品] 后 {HP} 恢复"}, '
        '{"i": 1, "t": "数组 [1, 2, 3] 与对象 {a: 1}"}, '
        '{"i": 2, "t": "结束]"}]',
        "raw",
        [0, 1, 2],
    ),
    (
        "被截断（最后一项不完整）→ 应 salvage 出前两条",
        '[{"i": 0, "t": "你好"}, {"i": 1, "t": "世界"}, {"i": 2, "t": "再',
        "salvage",
        [0, 1],
    ),
    (
        "缺逗号（}{ 相邻）",
        '[{"i": 0, "t": "你好"}{"i": 1, "t": "世界"}{"i": 2, "t": "再见"}]',
        "raw+loose",
        [0, 1, 2],
    ),
    (
        "Python 字面量 True/None",
        '[{"i": 0, "t": "你好", "ok": True}, {"i": 1, "t": "世界", "note": None}, '
        '{"i": 2, "t": "再见"}]',
        "raw+loose",
        [0, 1, 2],
    ),
    (
        "裸数组（无索引，按顺序兜底）",
        '["你好", "世界", "再见"]',
        "raw",
        [0, 1, 2],
    ),
    (
        "包在 translations 键里",
        '{"translations": [{"i": 0, "t": "你好"}, {"i": 1, "t": "世界"}, {"i": 2, "t": "再见"}]}',
        "raw",
        [0, 1, 2],
    ),
    (
        "字典形式 编号→译文",
        '{"0": "你好", "1": "世界", "2": "再见"}',
        "raw",
        [0, 1, 2],
    ),
    (
        "换行分隔的纯文本（最后一级降级）",
        "###0### 你好\n###1### 世界\n###2### 再见",
        "delimited",
        [0, 1, 2],
    ),
    (
        "编号点号分隔",
        "0. 你好\n1. 世界\n2. 再见",
        "delimited",
        [0, 1, 2],
    ),
    (
        "索引乱序（必须按 i 归位，不能按顺序）",
        '[{"i": 2, "t": "再见"}, {"i": 0, "t": "你好"}, {"i": 1, "t": "世界"}]',
        "raw",
        [0, 1, 2],
    ),
    (
        "键名用 index/text 而非 i/t",
        '[{"index": 0, "text": "你好"}, {"index": 1, "text": "世界"}, {"index": 2, "text": "再见"}]',
        "raw",
        [0, 1, 2],
    ),
]

def main() -> int:
    print("=" * 88)
    print("JSON 恢复阶梯测试")
    print("=" * 88)

    passed = 0
    for label, raw, want_method, want_idx in CASES:
        mapping, res = jp.parse_translations(raw, expect_indices=EXPECT)
        got_idx = sorted(mapping.keys())
        values_ok = all(isinstance(v, str) and v for v in mapping.values())
        ok = res.ok and got_idx == want_idx and values_ok
        passed += ok
        flag = "✅" if ok else "❌"
        print(f"\n{flag} [{label}]")
        print(f"    恢复级别 {res.method:<14} (期望 {want_method})")
        print(f"    条目 {got_idx}  (期望 {want_idx})")
        for i in sorted(mapping):
            print(f"      {i}: {mapping[i]!r}")
        for note in res.notes:
            print(f"    note: {note[:110]}")

    print()
    print("=" * 88)
    print("极端情况")
    print("=" * 88)
    for label, raw in [
        ("完全空", ""),
        ("纯废话无 JSON", "抱歉，我无法完成这个请求。"),
        ("只有开括号", "[{"),
        ("模型复读故障", '[{"i": 0, "t": "你好你好你好你好你好"}]'),
    ]:
        mapping, res = jp.parse_translations(raw, expect_indices=EXPECT)
        print(f"  [{label}] ok={res.ok} method={res.method} 条目={sorted(mapping.keys())}")

    print()
    print("=" * 88)
    print(f"结论：{passed}/{len(CASES)} 通过" + ("  ✅" if passed == len(CASES) else "  ❌"))
    return 0 if passed == len(CASES) else 1


def test_suite() -> None:
    """pytest 入口：跑一遍完整报告并断言全通过。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
