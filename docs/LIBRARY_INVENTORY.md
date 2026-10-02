# newlytransport 全库盘点（163 个游戏）

> 数据来源：`novaloc` 引擎识别 + 抽取阶段逐游戏实测（`.scratch/_inventory2.json`）。
> 这是**能力上限**的实测基线：它回答"这个库里到底有多少文本可翻"。

## 总量

| 指标 | 实测值 |
| --- | --- |
| 游戏根 | **163** |
| 抽到文本的游戏 | **157 / 163** |
| 文本单位合计 | **4,179,819** |
| 平均每游戏 | 26,623 |
| 最大单游戏 | 389,413 |

按暖机实测吞吐 **3,839 条/小时**（见下方"实测吞吐"）推算，
全库翻译约需 **1,089 小时 ≈ 45 天**单卡连续跑。
这不是"能不能翻"的问题，而是**吞吐**问题 —— 见下面的"局限"。

### 实测吞吐从哪来

暖机后的单条调用 0.67s、冷启动 6.25s（模型加载）。按流水线真实路径
（批次 + 记忆命中 + 并发）实测约 **3,839 条/小时**。
这也是"为什么全库不能一次性跑完"的直接原因。

## 引擎分布

| 引擎 | 数量 | 说明 |
| --- | --- | --- |
| `unity` | **104** | `.assets` 散装序列化资源 |
| `rpgmaker` | **45** | MV/MZ（`www/data/*.json`）；VX Ace 的 `.rvdata2` 不在内 |
| `loose` | 9 | 兜底：只有 `.ini`/`.txt` 之类零散文件 |
| `timeout` | 5 | 引擎识别为 `unity`，但**抽取超时**（见下） |

⚠️ **真正"识别不出引擎"的游戏是 0 个。**
盘点的"unknown"曾经是**统计口径错误**：`_inventory_watchdog.py` 对超时游戏
写 `{"units": -2, "engine": None}`，被算进 `unknown`。已改为写
`engine: "timeout"` —— "没跑完"和"识别不出"必须分开，否则排查方向会跑偏。

## 5 个"抽取超时"的游戏

引擎识别都是 `unity`（`detect_engine` 0.1s 就认出来了），卡的是**抽取**：

| 游戏 | 可扫 `.assets` 总量 |
| --- | --- |
| `ricca\HolyKnightRicca_v138en\Game` | **9,410 MB** |
| `Robolife 2` | 589 MB |
| `SB_Orks Ryona BBC v2.0` | — |
| `SwarmBunker_F95v1.3.1` | — |

根因与修法见 `d51ea75`：`extract_slots` 原本对每个字节调一次 `slot_at`，
实测 2.33 MB/s。改成"numpy 跨步视图挑候选"后，在**真有文本**的
20–60 MB 文件上实测 **13–27 MB/s**，槽位逐项完全一致。

⚠️ **修正一个我曾经夸大的数字**：我一度按 16.7 MB/s 外推说
`HolyKnightRicca` 从 74 分钟降到 29 分钟。重测后发现那个 16.7 MB/s
是在**几乎不含字符串**的文件上得到的（它跳过了所有候选，属于
最好情况）。按真有内容的文件实测，这个游戏的 4,625 MB 可扫文件
**约需 5 分钟**。

## 16 个"抽到的文本 < 200 条"的游戏 —— 逐个查明

大部分**不是缺陷**，而是"这个游戏本来就没有可读文本"：

| 游戏 | 单位 | 真实原因 |
| --- | --- | --- |
| `IC 1.2` | **0** | 唯一 0 条的游戏（未深查） |
| `Foresia -The Lust Curse-` | 7 | 加密资源 |
| `Lewdcrest Lady of the Night - Branded AZEL` | 10 | **`.wolf` 加密**（修复后为 0，见下） |
| `Brave_Alchemist_Colette_v1.05` | 11 | **`.wolf` 加密**（修复后为 0，见下） |
| `Niplheim's Hunter - Branded Azel` | 108 | **`.wolf` 加密** |
| `EcchiCraftv132\…Ecchi ＆ Craft DLC…` | 103 | 本体是 170,273 条；这只是 **DLC 安装器**子目录 |
| `KaijuPrincess` / `Kaiju Princess 2` | 110 / 145 | 小体量 Unity 游戏 |
| `Undercover Agent` | 127 | 小体量 Unity 游戏 |
| `Maid knight Alicia` | 137 | 小体量 Unity 游戏 |
| `Archmage Ricka v1.3.0\episode1` | 154 | 分集目录，单集文本少 |
| `Halloween_Harem_Full_PC` | 169 | 小体量 |
| `The Winning Secret of the Newbie Strategist Princess` | 174 | 小体量 |
| `Illegal Mahjong` | 178 | 小体量 |
| `Drain_Mansion_1.8.0` | 187 | 小体量 |
| `Colony City 27 Lambda` | 199 | 小体量 |

### `.wolf` 加密游戏是**诚实的不可解**

Wolf RPG 的 `Data/*.wolf`（`BasicData.wolf`、`BGM.wolf`…）是加密容器
（熵 7.54–7.67，接近随机）。共 **4 个游戏**属于这类，**不打算支持** ——
解密需要逆向密钥，且各游戏/版本不一致，投入产出比极低。

⚠️ 这些游戏修前会报 **10–11 条单位**，看似"能翻一部分"，实际全是
`Game.ini` 的运行参数（`Start=0`、`WindowModeFlag=1`、`ProxyPort=`）。
修复 `4c55855`（`loose.py` 的 `_is_engine_config_line`）后诚实报 **0 条**。
**诚实报 0 比虚报 11 条有用** —— 后者会让人误判覆盖率。

## 明确不可解或未覆盖

| 格式 | 游戏数 | 说明 |
| --- | --- | --- |
| `.wolf`（Wolf RPG 加密） | 4 | 熵接近随机，需逆向密钥 |
| `.rgss3a`（RPG Maker VX Ace 打包） | 2 | `Holy Knight Liviria`、`Heavenly Beauty x Silver Bubble Ninetales` |
| `root.pfs`（Siglus/Artemis） | 1 | 未实现解析 |
| AssetBundle（`.unity3d`） | — | **有意不做**，见下 |

### 为什么不做 AssetBundle —— 但必须区分**两种**情况

#### 情况 A：普通（**明文**）AssetBundle —— 数据支撑"不做"

实测三个游戏，包内可翻文本相对于 `.assets` **可以忽略**：

| 游戏 | bundle 内可翻文本 | `.assets` 内 | 占比 |
| --- | --- | --- | --- |
| `Academy Love Saga` | 1,030 个 bundle → **4 条** | 11,817 | 0.03% |
| `AliceInCradle` | → **4 条** | 127,693 | 0.003% |
| `Arena Story` | → **1 条** | 83,578 | 0.001% |

技术上可行（`UnityPy.save()` 往返 24→24 对象、0 丢失；
`catalog.json` 里**没有** `m_BundleHash`/`m_Hash`/`m_Crc`，不会被校验拒绝），
但**产能应花在 99.97% 上**。结论：**不投入**。

#### 情况 B：**加密** AssetBundle —— 与 `.wolf` 同类，能力上不可解

`IC 1.2`（アイリス☆クロニクル）是**全库唯一**一个如此的游戏，
而且它正好是那 6 个"抽不到文本"之外的**第 7 个陷阱**：

* `_Data/` 里**没有任何** loose `.assets` / `level*`（所以 novaloc 抽到 0 条）；
* 255 MB 的 `data.unity3d` 里扫到的 `TextAsset` 只有 **8 个**
  （全是 `*.physics3` 物理参数，**不是对白**）；
* 文本在 `StreamingAssets/StandaloneWindows64/` 的 **95 个文件（237 MB）**里，
  文件名是 CRC32（`-1830081318` 等，即 Unity `Caching` 的命名）；
* 其中 **93 个是加密的**，只有 `magic aura set` 与 `magic aura set.manifest`
  这 2 个是明文（且 `UnityPy` 只能读出 0 个对象）。

**加密判据（可证伪，用熵）**：

| 样本 | 熵 | 块熵 min–max | 判定 |
| --- | --- | --- | --- |
| `IC 1.2/-1830081318` | **7.9998** | 7.9967–7.9976 | ★ **加密（熵饱和）** |
| 已知加密 `.wolf` | 7.5364 | 7.4913–7.6709 | 高熵（压缩或加密） |
| 已知明文 `.assets` | 6.3356 | 2.2090–7.2944 | 明文（有结构） |

加密数据的熵**饱和在 8.0**，且**整文件处处均匀**（块熵几乎不动），
而明文 `.assets` 的块熵从 2.21 到 7.29 起伏很大 —— 这个对比就是证据。

要解开得从 4.6 MB 的 `global-metadata.dat` / `GameAssembly.dll` 里
逆出 AES 密钥，**不打算做**。

> ⚠️ 这两条结论**不能混着说**。此前我把"bundle 里只有 1–4 条文本"
> 和"`IC 1.2` 抽不到"当成同一件事，其实前者是**明文 bundle 性价比低**，
> 后者是**加密根本打不开** —— 根因不同，投入产出也不同。

## 结论：这个库的可翻性

* **157/163 游戏抽得到文本**，共 **4.18M 单位** —— 覆盖率不是瓶颈。
* 6 个抽不到的是：4 个 `.wolf` 加密 + 2 个 `.rgss3a`（另有 1 个 `root.pfs`
  在 157 之内但内容不全）。
* **真正的瓶颈是两个**：
  1. **吞吐** —— 3,839 条/小时 ⇒ 全库 45 天单卡；
  2. **长条目质量** —— 见 `docs/ACCEPTANCE.md` 的"已知限制"一节。
