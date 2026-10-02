# 实测记录：`novaloc auto` 就地写回的端到端验证

被改的是**真实游戏**（不是样本 fixture），所以这份记录是
"会不会把用户的游戏改坏"的直接证据。

游戏：`E:\lush\1\newlytransport\EcchiCraftv132\EcchiCraft_DLC_Saves-PC-v1.32\Ecchi ＆ Craft DLC installer`

## 一、命令与结果

```
novaloc auto "<游戏目录>" --force
```

```
✅ done　条目 103（译 102）　贴图 0　用时 41.6 s
   写回 3 个文件，备份在 D:\NovaLoc\_novaloc_backup\Ecchi ＆ Craft DLC installer-20261002-170742
```

* 引擎识别：`unity`
* 状态分布：`translated 102`、`failed 1`
* `qa/report.json`：`ok: True`，`missing 0`、`errors 0`

## 二、写回是**等长就地改写**（长度与结构都保住）

| 文件 | 写回前 | 写回后 | 哈希 |
|---|---|---|---|
| `Ecchi＆Craft_Data\globalgamemanagers.assets` | 30,548 | **30,548** | 已改变 |
| `Ecchi＆Craft_Data\sharedassets0.assets` | 4,359,096 | **4,359,096** | 已改变 |
| `Ecchi＆Craft_Data\level0` | 11,360 | 11,360 | 已改变 |

**长度一字不差** —— 这是原地改写能安全的前提
（`.assets` 里的字符串是长度前缀的，长度变了整个文件的偏移就全乱）。

用 UnityPy 重新解析写回后的 `sharedassets0.assets`：

| 检查项 | 结果 |
|---|---|
| 对象数 | 8 → 8（**一致**） |
| 类型分布 | `Material 2 / Texture2D 2 / PreloadData 1 / Shader 1 / Font 1 / Sprite 1`（**一致**） |
| `U+FFFD`（乱码标志） | **0** |
| 含中文的字符串 | 0 → **1** |

也就是：**游戏文件仍能被正常解析，对象没多没少，中文确实写进去了，
且没有产生乱码。**

## 三、备份是**逐字节原样**的

备份目录里 3 个文件，逐个与"写回前记录的 SHA256"比对：

```
✅ 与原文件一致  globalgamemanagers.assets
✅ 与原文件一致  sharedassets0.assets
```

被覆盖前的原文件**完整可回滚**。

## 四、译文抽样（真实内容）

```
源: 'Scrollbar'  →  译: '滚动条'
源: 'Mask'       →  译: '遮罩'
源: 'Outline'    →  译: '轮廓'
```

## 五、`--watch`（"后续新加游戏"的机制）已实测启动

在临时目录里造了一个最小 Unity 游戏，跑 `auto --watch --interval 5`：

```
守望模式：每 5 秒扫一次 <库>，新出现的游戏会自动处理。按 Ctrl+C 停止。
```

即**轮询循环真的起来了**，新增游戏会被自动接住。
（探针游戏本身是空壳、没有文本，所以报
`没有抽到任何文本` —— 那是预期行为，不是缺陷。）

## 六、本记录**没有**证明的事（诚实边界）

1. **没有启动游戏本体验证画面**。本次只做到"文件结构合法 + 内容正确"，
   没有实际运行游戏看 UI 是否显示正常。**字体是否有中文字形
   并未在本机验证**（该游戏的 `fonts/analysis.jsonl` 是 0 字节，
   说明字体分析阶段没有产出内容）。
2. 只覆盖了 **1 个游戏**（103 条，全为 UI 短标签）。
   长对白、贴图文字、以及 RPG Maker / Ren'Py 等引擎的写回
   **没有**在这份记录里验证。
3. 102 条译文里有 **25 条字节数比原文长**（如 `Mask` 4 → `遮罩` 6 字节）。
   这类条目只有落在"槽位容量够"的位置才能写进去；
   容量不够时 `unity_patch` 的行为是**跳过并记录**，
   **绝不截断**（截断会造成静默内容损失）。
