# 字体机制：为什么不会出现口口口

本文档是本项目最重要的一篇。它解释 `src/novaloc/fonts/` 全部代码为什么存在，
以及"保证游戏内不出现口口口"这个承诺具体是怎么被兑现的。

---

## 1. 口口口的真正原因

**口口口（豆腐块）不是编码问题，是字体问题。**

游戏引擎渲染文本时做的是这样一件事：

```
字符 → 查 Unicode 码点 → 在字体 cmap 里找这个码点 → 拿到 glyph id → 画出轮廓
```

如果在字体的 cmap 里**找不到这个码点**，引擎只能退到 `.notdef` 字形。
`.notdef` 的约定外观就是一个空心矩形 —— 也就是玩家看到的 `口`。

由此可以推出几个重要结论，它们直接决定了本工具的设计：

| 现象 | 真正原因 | 说明 |
|---|---|---|
| 英文游戏换成中文后满屏口口 | 游戏自带字体没有中文字形 | 最常见的情况 |
| 大部分字正常，个别符号是口口 | 字体缺**那几个**符号 | 中文全的字体往往缺 `✔✕❤` 这类符号 |
| 编码/UTF-8 没设对？ | **几乎从不** | 引擎读的是二进制码元，不涉及"编码猜测" |
| 同一个游戏，有的界面正常有的不正常 | 不同界面用了**不同字体** | 每个字体都要单独审计 |
| 只在某个分辨率下出现口口 | 位图/像素字体只有固定字号 | 缩放后没有覆盖的字形 |

所以本工具从不"猜编码"。它做的是：**把项目实际会用到的每一个字符列出来，
逐个确认目标字体能不能画出来。**

---

## 2. `CharsetCoverageError`：宁可失败，不写半成品

多数工具的策略是"尽力而为然后交付"。用户装完游戏跑起来才发现某个界面全是方块，
而且因为不知道是哪一步出的问题，几乎无法排查。

本工具的选择是相反的方向，实现在 `src/novaloc/fonts/charset.py`：

```python
def assert_plannable(plan: CharsetPlan) -> None:
    """缺字就硬失败。"""
    if plan.ok:
        return
    raise CharsetCoverageError(plan)


class CharsetCoverageError(RuntimeError):
    def __init__(self, plan: CharsetPlan) -> None:
        self.plan = plan
        miss = "".join(plan.still_missing[:120])
        ...
        super().__init__(
            f"有 {len(plan.still_missing)} 个字符没有任何候选字体能覆盖，"
            f"继续写入会导致游戏内显示口口口：{miss}{more}\n"
            f"请在设置里增加字体来源，或把包含这些字符的文本加进忽略列表。"
        )
```

这个异常在链路上有三处会被触发/上报：

1. **规划阶段**（`assert_plannable`）：所有候选字体加起来仍缺字符 → 立刻抛。
2. **合并阶段**（`FontService.patch_font`）：合并后覆盖率低于 `min_glyph_coverage`
   （默认 `0.999`）→ `res.error` 置位，并追加一句
   *"为避免游戏内出现口口口，本次结果**未采用**"*。
3. **QA 阶段**（`pipeline/qa.py`）：把缺字标成 `severity=ERROR`，
   让 `report["ok"] = False`，`novaloc qa` 以退出码 1 结束。

`stage_fonts` 还有一层兜底：如果**所有需要补丁的字体都失败**，直接让整个阶段失败：

```python
needed = [p for p in patches if p["action"] != "none"]
hard_fail = [p for p in needed if not p["ok"]]
if needed and len(hard_fail) == len(needed):
    return StageResult(stage="fonts", ok=False, error=(
        f"{len(hard_fail)} 个字体补丁全部失败。"
        "为避免游戏内出现口口口，已中止流程。"))
```

**设计意图**：一个缺字的字体是**不可用**的，不是"可用但略有瑕疵"。
把它交付出去只会让用户浪费时间去游戏里找哪个界面坏了。

---

## 3. 实测覆盖表：没有任何单一字体够用

这是整个设计的技术基础。数据来自本机（Windows 11）实测，
同时写进了 `charset.py` 与 `service.py` 的 docstring：

| 字体 | 特点 | 缺失字符 |
|---|---|---|
| **SimHei（黑体）** | 中文字形全 | `¶†‡•‹›↔↕♪♫✓✔✕✖❤` |
| **SimSun（宋体）** | 中文字形全 | `↔↕♪♫✓✔✕✖❤` |
| **微软雅黑（Microsoft YaHei）** | 中文字形全 | `↔↕♪♫✓✔✕✖❤` |
| **等线（DengXian）** | 中文字形全 | `↔↕♪♫✓✔✕✖❤` |
| **seguisym.ttf（Segoe UI Symbol）** | **符号 92.6%** | **一个汉字都没有**（0% CJK） |
| **LXGW Neo XiHei（霞鹜新晰黑）** | **符号 92.6% + CJK 100%** | `✔✕✖❤` |

### 3.1 这张表说明了什么

1. **每一条路都堵着。** 中文全的字体缺符号；符号全的字体没有中文；
   两者都好的那个还差四个常见符号（勾、叉、重叉、心）。
2. **所以多字体贪心合并是必需的，不是优化项。**
   `charset.py` 的 docstring 把结论写得很直接：
   *"没有任何单一字体能覆盖游戏里会出现的全部字符。所以必须做'贪心集合覆盖'"*。
3. **游戏 UI 用的符号恰好就是缺得最厉害的那一批。**
   确认框要 `✓`/`✕`，设置页要 `↔`，音乐开关要 `♪`，生命值要 `❤`。
   这些不是生僻字，是**必然出现**的字符。

### 3.2 字符集是怎么算出来的

`build_required_charset()` 汇总三部分，缺一不可：

```python
parts = []
for group in (translated_texts or [], source_texts or []):
    parts.extend(group)
if include_ui_safe:
    parts.append(UI_SAFE_CHARS)
parts.append(extra)
return "".join(sorted(set("".join(parts))))
```

- **译文**：当然要覆盖。
- **原文**：游戏里总有没被翻译的串（人名、型号、版本号、代码），
  它们仍然要用这个字体渲染。漏掉 = 界面口口口。
  `stage_fonts` 的 `_collect_texts()` 还会把**没翻译的条目的原文**额外补进 `translated` 列表，
  并且在贴图块里同时收 `b.target` 与 `b.source`。
- **`UI_SAFE_CHARS`**：游戏界面**一定**会渲染到的字符。它包含：
  ASCII 全量（0x20–0x7E）、中日文标点、全角形式（0xFF01–0xFF5E）、
  货币/度量、数学符号、箭头、几何图形、杂项符号、装饰符号（含 `✓✔✕✖✗✘❤`）、
  带圈数字、罗马数字、制表与方块字符、不间断空格与全角空格。

**贴图译文也参与字符集**：`_collect_texts()` 遍历 `ws.load_images()`，
因为贴图重绘用的是真实字体，缺字会**直接画在图上**。

### 3.3 单个归档字体的覆盖率上限

目录里两个字体的字形数顶到 OpenType 上限：

| 字体 | `glyph_count` |
|---|---|
| `noto-sans-sc` | 65535 |
| `source-han-sans-sc-vf` | 65535 |

`catalog.safe_to_merge()` 因此有一道判断：

```python
return font.glyph_count < 60000
```

接近 65535 的字体**不能再往里塞字形** —— 必须先 `pyftsubset` 裁到项目实际字符集。
本工具的合并流程第一步就是子集化（见下一节），所以这一点是自动满足的。

---

## 4. 合并算法

实现集中在 `src/novaloc/fonts/merge.py`。核心函数是
`merge_fonts_multi(base_path, sources, out_path, required_chars)`。

### 4.1 步骤

```
1. 读基础字体的 cmap
   needed = 项目字符集 − 基础字体已有码点
   若 needed 为空 → 只把原字体导出成独立 TTF，直接成功返回

2. 基础字体子集化：保留「原有全部码点 ∪ 待新增码点」
   subset_font(base, have | needed, retain_gids=True, drop_layout=True)

3. 逐个补充字体，按 sources 顺序（顺序即优先级）：
     takes = 仍未覆盖的字符 ∩ 该字体能提供的字符
     若 takes 为空 → 跳过这个字体
     子集化到 takes ∪ {0x20}
     UPEM 对齐：scale_upem(extra, base_upem)
     字形重命名：rename_glyphs(extra, f"{prefix}{idx}_")
     剩余集合 -= takes
   若一个补充字体都没用上 → raise MergeError("没有任何补充字体提供缺失字形")

4. 表集合归一化：normalize_tables(fonts)
   （Merger 的硬性要求，见 4.3）

5. Merger().merge(paths) —— 逐表合并

6. 收尾：再子集化一次、修正垂直度量、设置 OS/2 CJK 位、
   重算 usFirstCharIndex/usLastCharIndex、调整 name 表

7. 审计：重新读产物，算 coverage_after
   ok = (coverage_after >= 0.999) and (没有 still-missing)
```

### 4.2 cmap 重映射

`rename_glyphs(font, prefix)` 是合并能成立的前提。它做两件事：

**（a）把除 `.notdef` 外的所有字形改成 `<prefix><五位十六进制序号>`。**

基础字体的字形名（如 `uni4E00`）和补充字体的字形名极可能冲突。
若不改名，`Merger` 会把两个字形的数据混在一起，产物看起来正常、实际画错。

改名必须**同步所有引用字形名的地方**，漏一个就会在 save 时 `KeyError`
（这一点在 `merge.py` 的模块 docstring 里被列为"四个真实坑"之一）：

| 表/结构 | 处理 |
|---|---|
| `glyf` | 重建 `glyphs` 字典，并修复合字（composite）的 `components[].glyphName` |
| `hmtx` | 重建 `metrics` 字典 |
| `vmtx` / `hdmx` / `VORG` | 同样重映射（存在才处理） |
| `CFF ` / `CFF2` | 重建 `CharStrings.glyphOrder`、`topDict.charset`、各 `FDArray` 的 `charset` |
| `post` | 重映射 `extraNames` 与 `mapping` |
| glyph order | **最后**才 `setGlyphOrder()` |

顺序很关键：**先把 glyph order 落定，再重建 cmap**，
否则 fontTools 内部的反向映射缓存会指向已经改名的字形。

**（b）cmap 整体重建为单一 format 12 子表。**

```python
font["cmap"].tableVersion = 0
font["cmap"].tables = []
sub = CmapSubtable.newSubtable(12)
sub.platformID = 3
sub.platEncID = 10      # Unicode full repertoire
sub.language = 0
sub.cmap = {cp: n for cp, n in new_cmap.items() if cp != 0}
font["cmap"].tables.append(sub)
```

用 format 12 而不是 format 4，是为了支持 BMP 之外的字符（CJK 扩展 B 及以上）。
最后清掉 `_reverseGlyphMap` 与 `_glyphOrder` 两个内部缓存。

### 4.3 UPEM 缩放

`fontTools.merge.Merger` 有一条硬性检查：**两个字体的 `unitsPerEm` 必须一致**，
否则直接抛 `Cannot merge fonts with different unitsPerEm`。

这是个真实问题，不是理论问题：**中文点阵/像素字体常见 UPEM=256，而拉丁字体常见 2048。**
`_match_upem()` 用 `fontTools.ttLib.scaleUpem.scale_upem()` 把补充字体缩放到基础字体的 UPEM。
缩放同时会正确缩放 advance width 与 bearing，所以不需要额外修正度量。

注意快照时机：`_snapshot_metrics(cjk)` 必须在子集化**之前**调用 ——
`Merger` 会把两边的 `hhea`/`OS/2` 度量取**最大值**，而我们需要的原始值只在合并前存在。

### 4.4 被丢弃的表

`_DROP_ALWAYS` 分成三类，理由不同：

**（a）签名与布局表** —— 合并后签名必然失效，布局表 `Merger` 根本不合：

```
DSIG                                      签名，合并后一定失效
GSUB, GPOS, GDEF, morx, kerx, BASE, JSTF, gasp, kern
```

其中 `GSUB`/`GPOS` 的缺失已在 `merge.py` 的 docstring 里说明：
*"Merger 无法合并 GSUB/GPOS，合并前后的 subset 必须把布局表剥掉，
否则产物在 Unity/FreeType 里可能加载失败。"*

**（b）明确列出的三张表**（也是被用户最常问到的三条日志）：

```
LTSH   线性阈值表（Linear Threshold）—— 只对 GDI 的灰度渲染有影响，现代渲染器忽略
PCLT   PCL 5 打印机度量表 —— 打印机时代的产物
meta   元数据表 —— 与字形渲染无关
```

这三张表被丢弃时会打印 `LTSH NOT subset; don't know how to subset; dropped` 这类日志。
**这是正常的，不是错误**（详见 `docs/TROUBLESHOOTING.md` 的"可以忽略的日志"一节）。

**（c）两边几乎必然不对称的表** —— 这一组的存在纯属工程经验：

```
VDMX, hdmx, vhea, vmtx, VORG, FFTM, prop
EBDT, EBLC, EBSC, CBDT, CBLC, sbix      位图字形表
SVG , COLR, CPAL                        彩色字形表
MATH                                    数学排版表
```

理由是注释原文：*"Merger 是按'表集合长度'逐表比较的，只要一边有一边没有就会抛
`Expected all items to be equal: [NotImplemented, 0]` / TypeError。
它们对 CJK 字形显示都没有必要，统一剥掉最安全。"*

`normalize_tables()` 把多个字体裁剪到**相同的表集合**（取交集），
并检查 `_REQUIRED_TABLES` 是否齐全：

```python
_REQUIRED_TABLES = {"cmap", "glyf", "head", "hhea", "hmtx",
                    "loca", "maxp", "name", "post", "OS/2"}
```

缺任何一个就直接 `MergeError`，不做任何尝试。

### 4.5 合并后的垂直度量修正

`Merger` 对 `hhea.ascent/descent/lineGap` 与 `OS/2.sTypo*`/`usWin*` 取**两边最大值**。
拉丁字体的 ascent/descent 常比中文字体大，**合并后行高会暴涨**，
游戏里对话框、列表框的行距全部错位。

`_apply_metrics(merged, metrics_snap)` 把快照（来自**第一个提供字形的补充字体**，
即优先级最高的那个 CJK 字体）写回去。原则是：
**中文文本的行高应由中文字体主导。**

另外 `_mark_cjk_ranges()` 会设置 OS/2 的 Unicode/CodePage range 位：

- `ulUnicodeRange2` bit 48 = CJK Unified Ideographs
- `ulUnicodeRange1` bit 0 = Basic Latin
- `ulCodePageRange1` bit 18 = GB2312 (CP936)、bit 19 = Big5

不设这些位，老式 GDI 引擎可能拒绝把这个字体用于中文，
表现为"字体装上了但中文变成别的字体/方框"。

`_fix_char_index_bounds()` 重算 `usFirstCharIndex` / `usLastCharIndex`
（含非 BMP 字符时取 0xFFFF）。

### 4.6 名字表

`_adjust_names()` 保留原 family 名（引擎常常按 family 名查找字体文件），
把 full name 设为 `"<family> NovaLoc"`，并删掉 nameID 18/20/21/22
（那些是 typographic family/subfamily，与合并后的实际内容不再匹配）。

`Merger` 总是产出 TTF 风格的 sfnt，所以 `_as_ttf_path()` 会把 `.ttc`/`.otc`
后缀纠正为 `.ttf` —— 否则 Pillow 与 Unity 会按字体集合去打开，
报 `specify a font number between 0 and N`。

---

## 5. QA 不变量：为什么 IoU 是错的判据

### 5.1 IoU 会把正常行为误报成损坏

`fonts/qa.py` 的模块 docstring 记录了实测结论：

> 把中文字形并入拉丁字体后，字体整体的垂直度量（`hhea.ascent/descent`、
> `OS/2.usWinAscent`）会变成拉丁字体的值，导致同一个汉字在合并字体里比在源字体里
> **基线略高几个像素**。这会稳定地把 IoU 压到 **~0.92**，但字形轮廓其实完全无损
> （实测：UPEM 缩放与子集化的 IoU 都是 **1.0000**，ink 比 0.91~1.13）。

关键在于：**IoU 检查的是"渲染结果是否像素级相同"，而"基线偏移几个像素"是这个
流程的预期行为**（或者说，是必须单独修正的度量问题，不是字形问题）。
用一个对度量敏感、对轮廓不敏感的指标去判断轮廓完整性，就是**用错了 oracle**。

结果是把每一个正常的合并产物都判成"字形损坏（IoU 0.92）"。
这种误报比漏报更糟：它会让开发者去"修"一个没坏的东西。

`compare_glyph_shapes()` 因此被保留为一个**辅助工具**，并在 docstring 里明确警告：

> **不要**把低 IoU 直接当成错误。……判断可靠性请用 `verify_font()` 的不变量检查。

### 5.2 真正该检查的不变量

`verify_font()` 检查五件事，每件都是"是/否"的确定性判断：

| # | 不变量 | 失败类别 | 物理含义 |
|---|---|---|---|
| 1 | 码点在 cmap 里 | `missing_cmap` | 否则引擎根本找不到这个字 → 口口口 |
| 2 | 该码点渲染出来**有墨迹** | `blank` | 否则是空白 → 视觉上也是缺字 |
| 3 | 字形**不同于 `.notdef`** | `tofu` | 否则引擎画的就是豆腐块方框 |
| 4 | 没有两个码点映射到**同一个 glyph id** | `warnings` | 否则字形被互相覆盖 |
| 5 | 字体能被 FreeType/Pillow 正常加载 | `loaded=False` | 否则游戏加载就失败 |

判定 `ok` 的表达式很直白：

```python
rep.ok = not (rep.missing or rep.blank or rep.tofu)
```

### 5.3 几个实现细节

**空白类字符必须排除在"缺字"判定之外。**

ASCII 空格、NBSP、各类 Unicode 空格、ZWSP、制表/换行，渲染成空是**正确的**。
`_is_blank_by_design()` 用 `unicodedata.category(ch) in ("Zs","Zl","Zp","Cc","Cf")`
来判定。把这些误报成"缺字"会**掩盖真正的问题**。

**`tofu` 判定靠几何特征，不靠肉眼。**

先渲染 `U+FFFF`（未映射码点）来拿到 `.notdef` 的参考掩膜，
再与被检查字符的掩膜做 `np.array_equal`。如果 Pillow 渲染不出 `.notdef`，
就退化为不做这项检查（而不是误报）。

`_is_tofu_like()` 另有一组几何判据（空心矩形：宽高比接近 1、
内部空洞比例 > 0.72、边框厚度 0.8–6.0 px），作为独立工具保留。

**"渲染结果相同"不等于故障。**

很多字符在设计上就共用字形（`I`/`l`/`Ⅰ`/`Ｉ`、`°`/`。`），这是字体的正常行为。
真正的故障是**不同的码点被映射到同一个 glyph id**。
所以 `_glyph_id_collisions()` 直接查 `cmap → gid`，
而"渲染形状相同"只作为 `warnings` 里的提示输出。

**字符数上限 `char_limit=3000`。**

超过就只检查前 3000 个并记一条 warning —— 逐字渲染在大字符集上很慢，
而前 3000 个已经能覆盖绝大多数问题。

---

## 6. 三种策略与何时用哪个

`FontConfig.strategy` 是 `Literal["merge", "replace", "fallback_only"]`，默认 `"merge"`。
分发逻辑在 `FontService.patch_font()`。

### 6.1 `merge`（默认，推荐）

**做什么**：把中文字体的缺失字形**并入游戏原字体**，保留原字体的拉丁字形与整体风格。

**为什么默认**：游戏原有字体往往和 UI 设计是一套的 —— 字重、字宽、
甚至字母的圆角都跟界面配套。换成思源黑体，界面观感会明显变化。

**可以做多源**：`merge_fonts_multi()` 接受一串补充字体，每个缺字取
"第一个拥有它的字体"的字形。`plan_charset()` 负责贪心规划，
`max_sources` 默认 4。

### 6.2 `replace`（必须审计后才能用）

**做什么**：直接用某个中文字体替换原字体（`shutil.copy2`）。

**适用场景**（`merge.py` 的 docstring 列出）：原字体没有嵌入权限、
结构怪异（CFF2 可变字体）、或者本身就是图片字体。

**关键约束：必须审计每一个候选，覆盖不足就拒绝。**
实现在 `_best_single_font()`：

```python
def _best_single_font(self, candidates, required) -> tuple[Path | None, float]:
    """挑出**单独一个**覆盖最好的字体。

    ``replace`` / ``fallback_only`` 策略只能用一个字体，
    所以不能像 merge 那样"谁有就取谁的"。这里必须实测覆盖，
    否则会出现"换了字体反而缺了 7 个字"的情况 ——
    实测霞鹜新晰黑缺 ``░▒✔✕✖✘❤``，直接拿来当唯一字体就不达标。
    """
```

然后：

```python
if cov < min_cov:
    res.error = (
        f"没有任何单个字体能覆盖 {min_cov:.1%} 的字符集"
        f"（最好的 {src.name} 只有 {cov:.3%}）。"
        f"请改用 merge 策略，或补充更多中文字体候选。"
    )
    return res
```

**真实案例（为什么这条检查必须有）**：
有一次实现是"盲取规划器的第一个候选"，结果选中了 `LXGWWenKaiLite`，
覆盖率 **97.941%**，缺 `░▒✔✕✖✘❤`。

注意这个失败的样子：97.941% 听起来"几乎全对"，
但那 2% 里全是**确认框的勾、关闭按钮的叉、进度条的方块**。
游戏里每一处交互都会看到方块，而 QA 报告只会说"覆盖率 97.9%"。
所以 `replace` 要么不写，要么必须拒绝。

### 6.3 `fallback_only`

**做什么**：不动原字体，只产出一份子集化过的中文字体，由**引擎侧配置**回退。

**适用场景**：不方便改原字体文件（例如原字体在只读的打包资源里），
但引擎支持配置字体回退链。

**实现**：`subset_font_for_chars(src, dest, required)` ——
把字体裁到只含所需字符，产物体积可以小一个数量级。
覆盖判定与 `replace` 完全相同（也必须过 `min_cov`）。

**注意**：`stage_apply` 目前只把 `action == "merge"` 的补丁传给适配器
（见 `docs/ARCHITECTURE.md` 第 9 节），所以 `fallback_only` 的产物需要手工放置。

---

## 7. 小字号渲染的注意事项

### 7.1 优先 TTF，不要 OTF

FreeType **从 2.6.2 起默认关闭了 CFF 字体的 stem darkening**
（因为它在暗背景上会让字变粗、在亮背景上反之，Adobe 与 FreeType 对此长期有分歧）。
后果是：**CFF（`.otf`）字体在小字号下会显得比 TrueType 细一档。**

游戏 UI 的字号常常在 12–18 px 这个区间，正好是最敏感的范围。
所以本工具的字体候选与合并基底**优先 TTF**（`_FONT_EXTS` 里两种都认，
但目录里主要条目都是 TTF，`_as_ttf_path()` 也强制产出 `.ttf`）。

### 7.2 超采样 3–4 倍，然后用 BOX 降采样

`images/render.py` 里的做法：

```python
SUPERSAMPLE = 3
big = Image.new("RGBA", (w * ss, h * ss), ...)
...
# 缩回原尺寸。用 BOX 而不是 LANCZOS：LANCZOS 的负瓣会在文字边缘
# 产生振铃（暗边/亮边），小字号下很明显。
small = big.resize((w, h), Image.BOX)
```

**为什么不能用 LANCZOS**：LANCZOS 是一个带**负旁瓣**的重采样核
（它的数学形式是 `sinc` 的窗化近似）。负瓣会在高对比度边缘
（黑字白底、白字黑底就是最极端的情况）产生**过冲**，表现为文字外缘的一圈暗边或亮边 ——
即振铃（ringing）。字号越小、笔画越细，这一圈就越占面积。

BOX（面积平均）是**非负**的核，不会过冲。代价是它比 LANCZOS 稍"软"，
但在超采样 3 倍之后，这个差别远小于振铃的伤害。

### 7.3 字号下限

`fit_font_size()` 里：

```python
lo, hi = min_size, int(max_size or max(min_size, min(box_h * 1.6, 200)))
best = min_size
# manga-image-translator 的经验下限，避免极窄区域算出 0 号字
floor = max(min_size, int((box_h + box_w) / 200))
```

并且 `render_text_block_checked()` 会**如实报告放不下**：

```python
overflow = bool(fw > w or fh > h)
...
@property
def too_small(self) -> bool:
    return 0 < self.font_size < self.min_readable_size   # 默认 9
```

`overflow` 或 `too_small` 的块会进 `needs_review`，在 QA 报告里被标出来。
**溢出到按钮外的中文比不翻译更糟**，所以这里选择"标出来交给用户"
而不是硬画。

---

## 8. 各引擎的字体安装要点

**这一步是必需的，不是可选的。**
`EngineAdapter.wire_fonts()` 的 docstring 说明了原因：

> 把补好的字体文件复制进 `fonts/` 并不会让引擎去用它 —— 每个引擎都有自己的
> "字体指向"配置。少了这一步，用户会看到"字体文件确实被替换了，但游戏里还是口口口"，
> 而且完全查不出原因。

### 8.1 RPG Maker MV / MZ

**字体位置**：MV 在 `www/fonts/`，MZ 在 `fonts/`。
`discover_fonts()` 两个都扫，扩展名认 `.ttf/.otf/.woff/.woff2`。

**安装方式**：改 `fonts/gamefont.css` 里 `@font-face` 的 `src`。

**关键约束**：`@font-face` 里的 `fontFamily` **必须和 `js/rpg_core.js` 里
`Graphics._createFontLoader` 用的名字对得上（默认 `GameFont`）**。
所以本工具**只改 `src`、不动 `fontFamily`**：

```python
if "@font-face" in old_css:
    # 改写已有 @font-face 的 src，保留 fontFamily 名字不变 ——
    # 改名字的话 rpg_core.js 里引用的 "GameFont" 就找不到了
    fixed = re.sub(r"(src\s*:\s*)url\([^)]*\)([^;]*)(;?)", _fix, old_css, count=1)
```

**MZ 没有 `fonts/` 目录时**：新建一个并写 CSS —— 引擎会自动加载
`fonts/gamefont.css`（如果存在），这是官方支持的扩展点。

**找不到可改写的 `src` 时**：不猜，输出
`⚠️ {css.name} 里没找到可改写的 src，请手动确认字体指向`。

### 8.2 Ren'Py

**字体位置**：`game/` 下任意位置（Ren'Py 里字体就是相对 `game/` 的路径）。

**安装方式**：字体由 `gui.text_font` / `gui.name_text_font` /
`gui.interface_text_font` 等变量控制，定义在 `game/gui.rpy` 里。
**光把 ttf 放进 `game/` 不会生效。**

本工具的做法是**新增一个 `init 1` 的覆盖文件** `game/novaloc_fonts.rpy`，
而不是去改用户的 `gui.rpy`：

> 这里不改用户的 `gui.rpy`（那是人写的、还带主题注释，改了以后升级 Ren'Py
> 或用户手改会冲突），而是**新增一个 `init 1` 的覆盖文件**。Ren'Py 按 `init`
> 优先级执行，`init 1` 晚于 `gui.rpy` 的默认 `init` 但早于游戏启动，
> 所以这是官方推荐的覆盖方式，**也最容易卸载（删掉一个文件即可）**。

生成的文件头部带注释标明来源与卸载方法：

```python
"# 由 NovaLoc 新译自动生成 —— 让游戏使用补齐中文字形的字体。",
"# 想还原原始字体：删掉本文件即可。",
```

**只有 `.rpyc` 没有 `.rpy` 的发行版无法处理。** 编译产物改了游戏也不认。
`detect()` 会把这个情况写进 evidence（`⚠️ 只有 .rpyc 没有 .rpy，无法安全修改文本`）
并把置信度**乘 0.5** —— 遇到这种发行版，工具会明确报告而不是假装成功。

### 8.3 Unity + TextMeshPro

**这是唯一需要特别说明"本工具做不到"的引擎。**

Unity 有三条不同的文本路径，字体处理方式完全不同：

| 路径 | 字体怎么用 | 本工具能否处理 |
|---|---|---|
| 传统 `Text` 组件 + `Font` 资产 | 引用一个 `.ttf` 导入生成的 Font 资产 | 理论上可以，但字体资产在 `.assets` 序列化文件里，改它需要 Unity 本身的序列化格式支持 |
| TextMeshPro | `TMP_FontAsset` + **预烘焙的 SDF 图集**（`.asset` + `.png`） | **不能**。图集是位图，不是活的字体 |
| 代码内嵌字符串 | 在 `Assembly-CSharp.dll` 里 | **不能**（IL2CPP 更是编译进二进制） |

**TMP 的核心问题**：TMP 在运行时查的是 `TMP_FontAsset` 里的
`characterLookupTable` 与对应的 SDF 图集纹理。**把 `.ttf` 拷进
`StreamingAssets` 对 TMP 完全无效** —— 它根本不读那个文件。

正确做法是**重新烘焙 SDF 图集**：需要 Unity 编辑器（或自己实现
SDF 生成 + 图集重打包 + `characterLookupTable` 重建）。
这是一个独立的工作量，`docs/ROADMAP.md` 里标记为**规划中，未实现**。

Unity_Font_Replacer（GPL-3.0）正是做这件事的项目，本工具**只借鉴了
"必须重新烘焙图集"这个结论**，没有复制其代码。见
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 第 1.4 节。

**本工具对 Unity 实际做了什么**：
`UnityAdapter.discover_fonts()` 会找出游戏目录里所有的
`.ttf/.otf/.ttc`，并可以用 `merge` 策略为它们产出补丁文件；
`apply()` 会把补丁文件放到输出目录。但**没有 `wire_fonts()` 覆写** ——
也就是说工具不会去改 Unity 的字体指向，因为那需要处理 TMP 资产。
用户需要自己在 Unity 工程里重新指向，或者接受"字体文件已替换但引擎可能不读它"。

### 8.4 散装文件模式

`LooseFilesAdapter` 没有覆写 `wire_fonts()`（返回空列表）。
它会按相对路径把补丁字体与重绘贴图复制进输出目录，
字体**指向**需要用户根据具体游戏自行处理。

---

## 9. 授权规则

这一节与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 第 4 节互为补充；
这里只讲**代码里的实际约束点**。

### 9.1 发行版不分发字体

`FontService` 的模块 docstring 写明了默认策略：

> 如果本工具只产出**渲染好的贴图**、从不随包分发字体文件，那么 OFL 的署名与传染
> 条款都不触发。所以默认策略是 `bundle_fonts=False`：字体只在本机下载与合并，
> 产物留在用户自己的数据目录里。

字体的落盘位置是 `<data_root>/fonts/`，**不在仓库里**（`.gitignore` 已排除）。

### 9.2 白名单常量

```python
BUNDLE_OK_LICENSES: frozenset[str] = frozenset(
    {"OFL-1.1", "Apache-2.0", "MIT", "Public-Domain"}
)
```

`patch_font()` 里自动下载分支只遍历**可再分发**的条目：

```python
# 需要联网获取的字体：**只考虑可再分发的**（OFL/Apache/MIT/公有领域）。
# IPA、CC-BY-ND 以及微软/华为等系统字体只允许本机已装的情况下使用，绝不自动下载。
if allow_download is not False and self.cfg.font.allow_download:
    for spec in catalog.redistributable():
        ...
```

`redistributable()` 的定义同时检查两个条件：

```python
return [f for f in CATALOG if f.bundle_ok and f.license in BUNDLE_OK_LICENSES]
```

### 9.3 `lxgw-neo-xihei` 的特殊处境

霞鹜新晰黑的许可是 **IPA-1.0（IPA Font License 1.0）**，**不是 OFL**。
`catalog.py` 里它的注释：

> 许可为 IPA-1.0：可本地使用与渲染，但把字体文件打进安装包有风险，
> 默认标记为不可捆绑。

所以它的 `bundle_ok=False`。它是目录里**唯一** `bundle_ok=False` 的条目。
`local_only()` 会把它列出来。

**含义**：
- ✅ 可以在本机下载、渲染、把字形合并进产物；
- ❌ **不得**把字体文件打进安装包分发。

### 9.4 绝不捆绑的字体

`NON_REDISTRIBUTABLE_HINTS` 列出了明确"不要打包、也不要自动下载"的字体：
微软雅黑 / SimHei / SimSun / FangSong / KaiTi / DengXian / 等线 /
MiSans / HarmonyOS Sans SC / Alibaba PuHuiTi / Zpix / 全字庫（TW-Kai、TW-Sung）等。

它们**只在用户本机已安装时可被发现和使用**。
`non_redistributable_warning()` 会给一句提醒（不阻止使用，只提醒别打包）：

> 字体「X」属于商业授权字体，可以用于本地汉化，但**不要**把它打包进你分发的汉化补丁。

特别注意 **全字庫** 是 **CC BY-ND**：禁止改作。而**子集化与合并都属于改作**，
所以它连"本地合并产出补丁"这条路径都不适合分发。

### 9.5 OFL 保留字体名（RFN）

子集化后的产物是 OFL 定义的 "Modified Version"，
**必须避开**原字体的保留名。目录里登记的 RFN 是 `("Source",)`
（`noto-sans-sc` 与 `source-han-sans-sc-vf` 声明）。

`ALL_RESERVED_FONT_NAMES` 汇总了全部 RFN，`safe_subset_family_name()` 用来生成替代名：

```python
def safe_subset_family_name(original_family: str) -> str:
    """例如 ``Source Han Sans SC`` → ``NovaLoc Sans SC``。"""
```

### 9.6 结论

> **只分发渲染结果、不分发字体文件 → 没有 OFL 义务。**
> **一旦开始随包分发字体 → 必须保留版权声明、遵守 RFN、并确认该字体真的允许再分发。**

---

## 10. 快速对照表

| 你遇到的问题 | 去看这一节 |
|---|---|
| 游戏里出现口口口 | §1 原因、§2 为什么硬失败、`docs/TROUBLESHOOTING.md` |
| 不知道缺哪个字 | §3 覆盖表、`novaloc fonts audit` |
| 合并报 `unitsPerEm` 不一致 | §4.3 |
| 合并报 `Expected all items to be equal` | §4.4(c) |
| 合并后行间距变大了 | §4.5 |
| 日志里有 `LTSH NOT subset; … dropped` | §4.4(b)，正常 |
| 合并后字体文件变小了 | §4.1，子集化裁掉了用不到的字形 |
| 想判断字体有没有坏 | §5 五个不变量，不要看 IoU |
| 该选哪个策略 | §6，默认 `merge` |
| 贴图上的小字发虚/有暗边 | §7.1、§7.2 |
| 换了字体文件游戏还是不认 | §8，需要 `wire_fonts` |
| Unity TMP 没效果 | §8.3，本工具不做 TMP 图集烘焙 |
| 想打包字体一起发 | §9，先确认许可 |
