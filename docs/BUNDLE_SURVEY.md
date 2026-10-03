# 勘查报告：AssetBundle（UnityFS）里的文本到底值不值得做

> 本报告是 **objective (1)** 的前置决策依据。所有数字均为本机实测，
> 复现脚本在 `.scratch/`（`_survey_bundles.py`、`_survey_bundle_text.py`、
> `_bundle_rt2.py`、`_jerez_dbg.py`、`_jerez_gain.py`）。

## 0. 先说结论

**值得做，但收益高度集中，且必须实现"跳过已有中文"。**

| 问题 | 实测答案 |
| --- | --- |
| 库里有 AssetBundle 吗？ | 72 个 Unity 游戏里 **46 个含 UnityFS 包**（最多者 5857 个包） |
| 包里普遍有文本吗？ | **没有**。25 个"无文本"、11 个"仅少量"、**只有约 10 个有真文本** |
| 回写技术可行吗？ | **可行**（解包→等长改字→重打→重开验证，资产数不变、改动保留） |
| 代价 | **6.83 s / 188 MB 包**；但含文本的包通常只有 1~4 个 ⇒ 可接受 |
| 最大单个收益 | **Jerez's Arena：3,653 处"有源文但缺简中"** |

⇒ **不要**为"包"这个形态本身做通用解包器（25/46 是白干），
而应：**只在包里找到真文本时才解包**，且**跳过已有简中的槽位**。

---

## 1. 包有多少（`.scratch/_survey_bundles.py`）

先按 Unity 特征（`*_Data/globalgamemanagers` 或 `level0`）筛，再查 UnityFS 头。

* 有 Unity 特征的：**72** 个
* 其中含 ≥1 个 UnityFS 包的：**46** 个

包数量分布（抽样统计，未全量）::

    385  Isekai Sex Boutique v1.2.5
    382  Smash_Girls_v1.0.9_DLC
    381  DemonFoxLoveCourse
    378  JerezArena Ⅲ 1.0.30
    375  Jerez's Arena II
    229  Academy Love Saga Tennis Angels EX
    223  Guardians of Eden
    ...

## 2. ★ 但包里**普遍没有文本**（`.scratch/_survey_bundle_text.py`）

对每个游戏打开**最大的一个**包（成本可控），统计资产类型 +
从 `MonoBehaviour` typetree 捞"像句子的"字符串：

| 判定 | 游戏数 |
| --- | --- |
| **无文本** | **25** |
| 仅少量（1~4 种） | 11 |
| 有文本 | 10 |

**关键反例**（"包很大但没文本"）：

* **Isekai Sex Boutique**（5857 个包）：包里只有
  `'Enter Your Name'`、`'New stage unlocked'`、`'Shop Level Lv 0'`
  —— **UI 标签**，以及 `'Noto Sans JP'` 之类的**字体名**、版权声明。
* **Smash_Girls**（4163 个包）：`'Choice textChoice text…'`（占位符）、
  `'HCG04_Live2d_脖子.psd(未找到对应图层)'`（**开发期警告**）。
* **変態カノジョ**：4065 个 TextAsset，内容是 `'チンコしまいAG_1074.fade'`
  —— **动画名**。

⇒ 若不做这一步而直接造通用解包器，**54% 的游戏（25/46）纯属浪费**。

## 3. 有文本的少数游戏

| 游戏 | 包内真文本 | 判断 |
| --- | --- | --- |
| **Jerez's Arena** | **11,215 句英文对白** | ★★★ 真收益 |
| **Ride_Me_Taxi_Driver** | 2,872 句 | 但**已是中文**（繁中）⇒ 跳过 |
| **SummerClover** | 253 句 | 部分（含日/繁中） |
| I_will_purify_you | 34 句 | 小 |
| Ideal_Hikikomori | 99 处 | UI 标签为主 |
| Ghost Marriage | 7 | **Lorem ipsum 占位符** |
| Academy Love Saga | 10 | 场景名（`Sex Scene Bridget 1`） |

## 4. 回写可行性（`.scratch/_bundle_rt2.py`）

在 **Jerez's Arena / `scriptableobjects_assets_all.bundle`（187.9 MB）** 上：

1. `UnityPy.load()` 打开包，找到 `MonoBehaviour` 的 typetree；
2. 按字段路径**等长**改写（`'Noto Sans CJK TC'` → `'NovalocRT CJK TC'`）；
3. `obj.save_typetree(tree)`；
4. `env.save(pack="lz4", out_path=...)` → **6.83 s**；
5. 产物 197,168,227 B（原 197,069,993 B，**100.0%**）；
6. **重新打开验证**：资产类型与数量**完全一致** ✅；改后的值**能读到** ✅。

### 踩到的坑（已修，写下来避免重复）

* `Environment.save(pack, out_path)` 内部直接
  `open(join(out_path, basename), "wb")` ⇒ **`out_path` 必须已存在**，
  否则 `FileNotFoundError`。第一版就栽在这里。
* `env.save()` **只写被标记改动的文件**（`is_changed`）⇒
  对"包里没文本"的游戏没有额外代价，天然符合第 2 节的结论。

## 5. ★★ 最大发现：**跳过已有中文比翻译更重要**（`.scratch/_jerez_dbg.py`）

Jerez's Arena 用的是**标准本地化表**结构，每个槽位**五语言并列**：

```
.csvLines[].localizeText[]                    25995 对
.LocalizeNarratives[].pages[].cmds[].localizeTexts[]  20590 对
.ActorNames[].names[]                           375 对
--------------------------------------------------------
合计 46960 对（非空 29131）

tag 取值：'English' / 'Japanese' / 'Chinese (Simplified)'
          / 'Chinese (Taiwan)' / 'Korean'  （另有 275 个 'English ' 尾随空格！）
```

同一槽位的四个样本值::

    '王都赫雷斯的一角，車水馬龍的大道旁矗立著一棟豪華的宅邸。'   ← 繁中
    'On a corner of the capital of Honece, beside a bustling avenue…'  ← 英
    '王都赫雷斯的一角，车水马龙的大道旁矗立着一栋豪华的宅邸。'   ← **简中（已存在）**
    '王都ヘレスの一角、賑やかな大通り沿いに、一棟の豪邸がそびえ立っている。' ← 日

⚠️ **`'English '` 带尾随空格** —— tag 匹配必须 `strip()`，否则会漏 275 条。

### 收益判定（`.scratch/_jerez_gain.py`，9,392 个槽位组）

| 判定 | 数量 | 占比 |
| --- | --- | --- |
| **已有简中** | 4,580 | **48.8%** |
| **★缺简中（有源文）** | **3,653** | **38.9%** |
| 全空 | 1,159 | 12.3% |

按路径拆开看，**两个路径的性质完全不同**：

| 路径 | 组数 | 已有简中 | 缺简中 |
| --- | --- | --- | --- |
| `.LocalizeNarratives[]…localizeTexts` | 4,118 | **3,999** | 116 |
| `.csvLines[].localizeText` | 5,199 | 506 | **3,537** |
| `.ActorNames[].names` | 75 | 75 | 0 |

⇒ **主线剧情已官方汉化（3999/4118）**，而
**`csvLines`（CSV 导入的对话/文本，3537/5199 缺简中）才是真缺口**。

### 这直接否掉了"无脑全翻"

若不做"该槽位已有简中 ⇒ 跳过"的判断，就会：
* 把 **4,580 条已存在的中文**再送进模型（浪费预算）；
* 更糟的是**覆盖掉官方译文**（质量倒退）。

## 6. 由此得出的实现设计

1. **只读抽取**：从 UnityFS 包里读 `MonoBehaviour` typetree，
   按「**同一容器内的多语言槽位组**」聚合，而不是按"逐条字符串"。
2. **槽位组语义**：一个组的身份 = `(资产路径, 字段路径, 组序号)`；
   组内的语言键 = `tag.strip()`。
3. **跳过的判据（事先定好）**：
   * 该组 `Chinese (Simplified)` 非空 ⇒ **不翻**（已有官方译文）；
   * 该组无任何源文（英/日）⇒ **不翻**（空槽位）；
   * 否则 ⇒ 取源文（优先 `Japanese`，其次 `English`）翻译，
     译文**只写回 `Chinese (Simplified)` 槽**，不动其他语言。
4. **回写前必备份**（已有 `_novaloc_backup` 机制）。
5. **`tag` 比较必须 `strip()`**（`'English '`）。
6. **不做通用解包器**：只有"扫描后发现该包有真文本"才解包，
   避免 25/46 的白干。

## 7. 本报告**没有**验证的部分（不许含糊）

* 回写后的包**能否被真实游戏加载** —— 只验证了 UnityPy 能重开、
  资产数与改动一致；**没有启动游戏看画面**。
  这是本机条件下无法完成的验证，必须如实说明。
* 6.83 s/包 只测了一个包（187.9 MB）。小包应更快，但**未测**。
* 其他 9 个"有文本"游戏的槽位结构**未逐个勘查**
  （可能不是"五语言并列"形态，实现必须能退化为**通用文本抽取**）。
