# NovaLoc — offline model selection report (RTX 4060 Laptop 8 GB, Ollama 0.35.0 + onnxruntime-directml)

> **实现状态（2026 年补充）** —— 本报告的建议逐条落地情况：
>
> | 建议 | 状态 | 说明 |
> | --- | --- | --- |
> | §2 OCR 韩语/西里尔覆盖缺口 | ✅ **已实现** | `src/novaloc/images/rec_models.py`，按语种路由到 PP-OCRv5 分语种模型 |
> | §1 保留 `translategemma:4b`（不要换通用 instruct） | ✅ 已照做 | 未更换 |
> | §1 JSON 模式/批量能力存疑 → 自行兜底 | ✅ 已验证 | 实测 27 条真实游戏串，JSON 解析可靠；批量漏条目/空串已逐条补 |
> | §3 视觉兜底换 OCR 专精模型 | ⏸ **未做（有硬阻碍）** | 见下 |
>
> **关于 §3 未做的原因**：Ollama 官方库里**没有** `hunyuanocr` / `wevisdoc`
> 的 tag（`ollama show` 均返回未找到），而 `huggingface.co` 在本环境不可达，
> 拿不到 GGUF。要落地必须另起 `llama-server` 或 `ollama create` 导入 GGUF，
> 且 Ollama 0.35.0 是否支持 `hunyuan_vl` 架构仍未验证（原报告第 5 条未解决）。
> 在无法实测的前提下换了模型，等于用一个未验证的东西替换一个已知能用的东西。
>
> **关于下面第 7 条"未验证"**：已实测解决。v6 检测 + v5 识别**可以**混用，
> 但**不能**用 RapidOCR 的常规配置路径 —— `utils/model_resolver.py` 的
> `MODEL_ROUTES` 只注册了 PPOCRV6，传 `Rec.ocr_version=PPOCRV5` 会直接抛
> "Invalid OCR configuration."。必须用 `Rec.model_path` 直接指到 ONNX 文件。
> 另一个实测结论：**v5 分语种模型的字符集内嵌在 ONNX metadata 里**
> （韩语模型 `character` 有 23890 项），所以不需要 `ppocr_keys_v1.txt`。
> 实测效果：`게임 시작` 在 v6 下读到 `''`，路由到 v5 korean 后读到
> `'게임'`、`'시작'`。

Scope: three slots — (1) text translation → Simplified Chinese, (2) OCR **recognition** only, (3) vision fallback reader. All recommendations are offline-capable, commercially redistributable, and sized for 8 GB VRAM. Every benchmark number below is cited; vendor-claimed vs. measured is labelled explicitly.

---

## Headline conclusions

1. **Translation: your current `translategemma:4b` is already close to optimal for this GPU + license envelope.** It is *not* a generic Gemma 3 — it is Google's **TranslateGemma** (released 2026-01-15), a Gemma-3-based *translation specialist* in 4B/12B/27B. Google's own WMT24++/MetricX evaluation says the 4B matches the **12B** Gemma 3 baseline and the 12B beats the **27B** Gemma 3 baseline. Keep it; do not downgrade to a general instruct model. See §1.
2. **OCR recognition: you have a real coverage bug, and it is fixable in one line of config.** PP-OCRv6's unified recognizer covers **50 languages but excludes Korean and all Cyrillic scripts**. Your Korean and Russian support must come from **PP-OCRv5 script recognizers**, which RapidOCR 3.9.2 already ships as ONNX and can download by name. This is the single highest-value change in this report. See §2.
3. **Vision fallback: `qwen3-vl:4b` is the weakest link of the three.** On the only public benchmark that measures *cropped-region text spotting* (HunyuanOCR paper Table 13), Qwen3-VL-2B scores 29.68 overall and even Qwen3-VL-235B only 53.62, while OCR specialists score 62–71. HunyuanOCR-1.5 is the best fit but ships under Tencent's territory-restricted license; **WeVisDoc-2B (Apache-2.0)** is the fully permissive runner-up that still fits in ~1.3 GB. See §3.

---

## 1. Text translation (EN/JA/KO/RU/… → Simplified Chinese)

### Top pick — keep `translategemma:4b` (TranslateGemma 4B)

| Property | Value |
|---|---|
| Exact identifier | Ollama: `translategemma:4b` (alias `translategemma:latest`); upstream collection `google/translategemma` on HuggingFace/Kaggle |
| Params / quant | 4B, Ollama ships **Q4_K_M** |
| Download size | **3.3 GB** |
| Context | 128K declared by Ollama; multimodal (Text+Image) |
| License | **Apache-2.0** per Google's release; the Ollama page metadata says `Gemma` and the base is Gemma 3 — treat the effective terms as Gemma Terms of Use + Apache-2.0 model card. Both permit commercial use, redistribution and offline bundling. Attribution/"Gemma" naming notice required on redistribution. |
| VRAM fit | **Comfortable.** 3.3 GB weights + KV cache + ~0.5 GB overhead leaves >3 GB headroom on a 7.6 GB usable budget. Runs fully on-GPU. |

**Why it is better than the alternatives on this machine:**

- It is **translation-specialized**, not general-purpose, and it is the *newest* (2026-01) translation model in the ≤8B permissive-license class.
- **Published benchmark evidence (Google, WMT24++ / MetricX, 55 language pairs):**
  - The **12B** TranslateGemma beats the **Gemma 3 27B** baseline — "half the compute, higher fidelity".
  - The **4B** TranslateGemma "performs comparably to the 12B baseline model".
  - Human MQM evaluation on the WMT25 test set across 10 language pairs showed large error-rate reductions in all categories **except JA→EN**, where a proper-noun mistranslation regression was found. This is EN-target only; it does not affect your JA→ZH direction, but it is a real published caveat.
  - Sources: [Google blog announcement (via gihyo.jp summary)](https://gihyo.jp/article/2026/01/translategemma), [IT之家/腾讯新闻 coverage](https://news.qq.com/rain/a/20260116A01E2P00), [TranslateGemma Technical Report, arXiv:2601.09012](https://arxiv.org/abs/2601.09012).
- Training is two-stage SFT on human + Gemini-synthesised parallel data, then RL with **MetricX-QE / AutoMQM** reward ensembles — i.e. it is optimised for the same metric class the benchmarks report, which is why the wins are large and consistent.
- It retains Gemma 3's multimodal ability, and Google states text-translation gains transferred to **image text translation (Vistra benchmark)** without multimodal-specific tuning — useful side-effect for you.
- It supports all of EN, JA, KO, RU, ZH-Hans as explicit prompt codes (the 55-language table lists `ja`, `ko`, `ru`, `zh-Hans`).
- Your existing prompt format (single user message, `{SOURCE_LANG} ({CODE}) to {TARGET_LANG} ({CODE})`, two blank lines before the payload) matches the model's expected template *exactly* — no prompt migration cost.

### Runner-up — `tencent/Hunyuan-MT-7B` (better quality, **not** permissively licensed)

| Property | Value |
|---|---|
| Exact identifier | Ollama: **does not exist** (no `hunyuan-mt` tag — verified 404 at `ollama.com/library/hunyuan-mt`). Use GGUF `mradermacher/Hunyuan-MT-7B-GGUF` (`Hunyuan-MT-7B.Q4_K_M.gguf`, **4.62 GB**) via a Modelfile, or `tencent/Hunyuan-MT-7B-fp8`. |
| Params / quant | 7B. GGUF sizes measured from the HF API: Q3_K_S 3.44 GB, Q4_K_S 4.40 GB, **Q4_K_M 4.62 GB**, Q5_K_M 5.37 GB, Q6_K 6.16 GB, Q8_0 7.98 GB |
| License | **Tencent Hunyuan Community License.** Read the text, not the summary: *"THIS LICENSE AGREEMENT DOES NOT APPLY IN THE EUROPEAN UNION, UNITED KINGDOM AND SOUTH KOREA"*, Territory = worldwide **excluding EU, UK, South Korea**; you may distribute only *in the Territory*; >100M MAU requires a separate licence; you must ship the agreement and reproduce the Section 5 use restrictions in your own EULA. |
| VRAM fit | Q4_K_M 4.62 GB + KV fits in 8 GB, but with less headroom than TranslateGemma; pick Q4_K_S (4.40 GB) or a short context if you also keep the VLM resident. |

**Evidence that it is better (published, strong):** From the [Hunyuan-MT Technical Report, arXiv:2509.05209](https://arxiv.org/abs/2509.05209) (Table 4, XCOMET-XXL):

| Model | ZH→XX | XX→ZH | EN→XX | XX→EN | XX→XX | **WMT24pp** | Mand.↔Min. |
|---|---|---|---|---|---|---|---|
| **Hunyuan-MT-7B** | 0.8758 | 0.8528 | 0.9112 | 0.9018 | 0.7829 | **0.8585** | 0.6082 |
| **Hunyuan-MT-Chimera-7B** | 0.8974 | 0.8719 | 0.9306 | 0.9132 | **0.8268** | **0.8787** | **0.6089** |
| Gemini-2.5-Pro | 0.9146 | 0.8748 | 0.9295 | 0.9432 | 0.8773 | 0.8250 | 0.5811 |
| Gemma-3-27B-IT | 0.8783 | 0.8441 | 0.9036 | 0.9331 | 0.8381 | 0.7742 | 0.4558 |
| Gemma-3-12B-IT | 0.8567 | 0.8249 | 0.8781 | 0.9189 | 0.8020 | 0.7527 | 0.4280 |
| Tower-Plus-9B † | 0.7726 | 0.7912 | 0.7884 | 0.8704 | 0.6608 | 0.6977 | 0.3912 |
| Tower-Plus-72B † | 0.7703 | 0.8235 | 0.7829 | 0.9002 | 0.7002 | 0.7276 | 0.3855 |
| Seed-X-PPO-7B † | 0.8010 | 0.7702 | 0.8181 | 0.8442 | 0.6896 | 0.7388 | 0.4206 |
| Qwen3-8B † | 0.7250 | 0.8056 | 0.7468 | 0.8825 | 0.6544 | 0.6532 | 0.3737 |
| Qwen3-14B † | 0.7826 | 0.8318 | 0.8027 | 0.9049 | 0.7228 | 0.6983 | 0.3944 |
| Qwen3-235B-A22B † | 0.8509 | 0.8569 | 0.8765 | 0.9292 | 0.8018 | 0.7665 | 0.4493 |
| Llama-3.1-8B-Instruct † | 0.6385 | 0.5148 | 0.6848 | 0.6412 | 0.4408 | 0.5130 | 0.3016 |
| DeepSeek-V3-0324 † | 0.8848 | 0.8542 | 0.9010 | 0.9319 | 0.8082 | 0.8109 | 0.4865 |
| Google-Translator | 0.7615 | 0.6243 | 0.7638 | 0.7761 | 0.6225 | 0.5796 | 0.3692 |

Two things to take from this table:

1. **Your "7B specialist beats 70B generalist" hypothesis is confirmed and quantified.** Tower-Plus-**72B** scores 0.7276 on WMT24pp; Hunyuan-MT-**7B** scores 0.8585. Also `Qwen3-235B-A22B` (0.7665) loses to Hunyuan-MT-7B (0.8585).
2. **The 7B generalist instruct models you asked me to evaluate are genuinely weak translators.** Qwen3-8B at 0.6532 on WMT24pp is *below* Gemma-3-12B (0.7527) and far below your current 4B translator class. Swapping `translategemma:4b` for `qwen3:8b` would be a regression. Also claimed (vendor): Hunyuan-MT-7B "achieved first place in 30 out of the 31 language categories it participated in" at WMT25.

**Verdict:** if you can accept the EU/UK/KR territory carve-out, Hunyuan-MT-7B is a defensible quality upgrade over TranslateGemma 4B. If NovaLoc is distributed publicly and globally, **stay on TranslateGemma 4B** and revisit `translategemma:12b` only on better hardware.

### Models evaluated and rejected for this slot

| Model | License | Why rejected |
|---|---|---|
| `Unbabel/Tower-Plus-9B`, `Tower-Plus-2B`, `Tower-Plus-72B` | **CC-BY-NC-SA-4.0** (verified via HF API `license` field; the model card README also states "License: CC-BY-NC-4.0") | **Non-commercial. Hard reject**, same class of problem as NLLB-200. Earlier claims that Tower+ is Apache-2.0 are false. |
| `Unbabel/TowerInstruct-7B-v0.2` | **CC-BY-NC-4.0** (verified) | Non-commercial. Reject. |
| `ByteDance-Seed/Seed-X-7B`, `Seed-X-PPO-7B` | `license: other` on the HF card; HF mirror refused anonymous API access for the ByteDance repos | Could not confirm commercial rights. Also **measured weakest of the 7B specialists** in the Hunyuan table (Seed-X-PPO-7B = 0.7388 WMT24pp, below Gemma-3-12B). Reject on both grounds. |
| `haoranxu/ALMA-7B-R`, `ALMA-13B-R` | **MIT** (verified) | **License is excellent**, but these are 2024 models built on Llama-2/Mistral-era bases; no WMT24pp data in the Hunyuan table. Superseded. ALMA-R's own claim is "matches or even exceeds GPT-4 or WMT winners" (author claim, not a 2025-26 benchmark). Consider only if you need MIT specifically. |
| `LGAI-EXAONE/EXAONE-3.5-7.8B`, `EXAONE-4.0-*` | `license: other` (EXAONE AI Model License) | The EXAONE licence has historically carried a non-commercial clause for research-only variants; I could not verify which version applies. Treat as unusable until verified. Also no translation-specific benchmark. |
| `meta-llama/Llama-3.3-70B-Instruct` | Llama Community License | **70B is unusable on 8 GB** (would spill to CPU massively). Llama-3.1-8B-Instruct is also the worst translator in the table (0.5130). Reject. |
| `google/gemma-3-4b-it` / `gemma3:4b`, `gemma-3-12b-it`, `gemma-3-27b-it` | Gemma Terms of Use (permissive, commercial OK) | Permissive, but strictly dominated: TranslateGemma **is** a Gemma-3 fine-tune, and the 4B TranslateGemma matches the 12B plain Gemma 3 baseline. Using plain `gemma3:4b` instead of `translategemma:4b` is a straight downgrade. Keep as emergency fallback only. |
| `Qwen2.5`/`Qwen3` instruct (4B/7B/8B/14B) | Apache-2.0 (verified for `Qwen/Qwen3-8B`, `Qwen/Qwen3-4B`) | Permissive and genuinely useful for **strict JSON output** (see caveat below), but measurably worse translation. Qwen3-8B 0.6532 vs Gemma-3-12B 0.7527 on WMT24pp (arXiv:2509.05209 Table 4). |
| SeamlessM4T / NLLB-200 | CC-BY-NC-4.0 (as you already found for NLLB) | Non-commercial. Reject. |
| Qwen-MT | API-only | No weights for offline use. Reject. |

### ⚠️ The one open risk on the translation slot

`translategemma` is a **single-turn, plain-text-output translator**. Its documented template says *"Produce only the {TARGET_LANG} translation, without any additional explanations or commentary."* There is **no documented JSON mode and no documented system prompt**. Your pipeline requires **strict JSON with placeholder preservation** across a batch of short strings.

I could not find any published evidence that TranslateGemma supports JSON-constrained decoding or multi-item batch output. Two mitigations, in order of preference:

1. Keep TranslateGemma as the *engine*, but have NovaLoc do the batching and JSON assembly itself (send N strings, parse N lines) rather than asking the model to emit JSON.
2. Add a second, small general instruct model used **only** as a structured-output re-formatter / repair step when the strict-JSON parse fails. On this budget that means `qwen3:4b` (Apache-2.0, ~2.6 GB Q4_K_M) — cheap, and it never competes with the translator for VRAM if you unload between stages.

Do **not** replace TranslateGemma with a general instruct model just to get JSON. That would trade a measured, large translation-quality win for a formatting convenience.

---

## 2. OCR text recognition (detection already solved)

### The finding that matters

**PP-OCRv6's unified recognizer cannot read Korean or Cyrillic at all.** This is an official, documented limitation, not a rumour:

> PP-OCRv6 medium/small support 50 languages: Simplified Chinese, Traditional Chinese, English, Japanese + 46 Latin-script languages. `tiny` supports 49 (no Japanese).
> — [PaddleOCR PP-OCRv6 documentation (Chinese)](https://www.paddleocr.ai/latest/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html), [English](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html)

And RapidOCR's own model list states it flatly for the recognizer:

> "该版本支持是由汉语、日语和拉丁字母组成的语言。**不包括韩语、阿拉伯语、藏语、彝族等语言**" ("This version supports languages composed of Chinese, Japanese and Latin letters. **It does not include Korean**, Arabic, Tibetan, Yi, etc.")
> — [RapidOCR model list](https://rapidai.github.io/RapidOCRDocs/main/en/model_list/)

So your "20 % of strings silently fail" symptom on Korean/Russian is expected behaviour, not a bug in your code. Russian is partially salvageable because `rs_latin` is in the 46 Latin languages, but Cyrillic is not Latin — Russian in Cyrillic script **is not covered**.

### Top pick — hybrid: PP-OCRv6 detection + script-routed PP-OCRv5 recognition

Since detection is already excellent (you measured 93.7/93.8 Hmean on rotated text, matching PaddleOCR's published 93.8 for `PP-OCRv6_medium`), **keep PP-OCRv6 medium for detection and add PP-OCRv5 script recognizers behind a language router.**

**Detection (unchanged):**
- `PP-OCRv6_det_medium.onnx` — **62,119,454 bytes** (per ModelScope listing)
- SHA256 `92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2`

**Recognition — pick by language:**

| Language / script | Model file (ONNX) | Size (bytes) | Source |
|---|---|---|---|
| zh + ja + en + 46 Latin | `PP-OCRv6_rec_medium.onnx` | 76,629,984 | [ModelScope RapidAI/RapidOCR](https://www.modelscope.cn/models/RapidAI/RapidOCR/files) v3.9.2 |
| zh (fallback / alt) | `ch_PP-OCRv5_rec_server.onnx` | 84,577,022 | same |
| zh (light) | `ch_PP-OCRv5_rec_mobile.onnx` | 16,631,306 | same |
| **Korean** | `korean_PP-OCRv5_rec_mobile.onnx` | **13,488,748** | same |
| **Russian / Belarusian / Ukrainian** | `eslav_PP-OCRv5_rec_mobile.onnx` | **7,911,802** | same |
| Russian (broader Cyrillic) | `cyrillic_PP-OCRv5_rec_mobile.onnx` | **8,074,092** | same |
| Latin scripts | `latin_PP-OCRv5_rec_mobile.onnx` | 7,904,513 | same |
| English | `en_PP-OCRv5_rec_mobile.onnx` | 7,872,351 | same |
| Thai / Greek / Arabic / Devanagari / Tamil / Telugu | `th` / `el` / `arabic` / `devanagari` / `ta` / `te` `_PP-OCRv5_rec_mobile.onnx` | ~7.8–8.0 MB each | same |

Total added download for KR + RU + Latin + EN + TH + EL: **~45 MB**. This is trivial against a 100 GB budget — the whole OCR suite is under 250 MB.

**Exact download URL pattern** (from RapidOCR's own `default_models.yaml` at tag v3.9.2, which is the canonical pinned source):

```
https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx
https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv5/rec/eslav_PP-OCRv5_rec_mobile.onnx
https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile.onnx
https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/det/PP-OCRv6_det_medium.onnx
https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/rec/PP-OCRv6_rec_medium.onnx
```

Per-model character dictionaries also live under `.../v3.9.2/paddle/PP-OCRv5/rec/<model>/<dict>.txt` (e.g. `korean_PP-OCRv5_rec_mobile/ppocrv5_korean_dict.txt`, `eslav_PP-OCRv5_rec_mobile/ppocrv5_eslav_dict.txt`) — you need these; the ONNX alone is not enough.

**How to use them with `rapidocr` / onnxruntime — no PaddlePaddle required:**

`rapidocr` on PyPI is currently **v3.9.2**, and its `default_models.yaml` contains `onnx/PP-OCRv6/{det,rec}` and `onnx/PP-OCRv5/rec/{korean,cyrillic,eslav,latin,en,...}` entries under the `onnxruntime` engine. All supported engines are `onnxruntime`, `openvino`, `torch`, `mnn`, `tensorrt` — **PaddlePaddle is an optional engine only and is not required**. `rapidocr>=3.9.0` is the version that added PP-OCRv6 ONNX support.

Config surface (from the RapidOCR docs' own equivalent-config listing):

```
Det.engine_type: onnxruntime   Det.ocr_version: PPOCRV6   Det.model_type: MEDIUM   Det.lang_type: CH
Rec.engine_type: onnxruntime   Rec.ocr_version: PPOCRV5   Rec.model_type: MOBILE   Rec.lang_type: KOREAN
Cls.engine_type: onnxruntime   Cls.ocr_version: PPOCRV4   Cls.model_type: MOBILE
```

Notes:
- For PP-OCRv6 the `lang_type` value is **always `ch`** regardless of language — "PP-OCRv6 中，不管指定哪个语种，对应的模型都是一样的。不同模型仅由 `model_type` 来区分." So do not try `lang_type=japan` with v6.
- For the v5 Korean/Cyrillic recognizers you set `Rec.ocr_version: PPOCRV5` + `Rec.lang_type: KOREAN` / `CYRILLIC` / `ESLAV` / `LATIN`.
- The RapidOCR docs explicitly annotate the v5 multilingual recognizers as `onnxruntime ✓`, `openvino ✓`, `paddle ✓`, `torch ❌`, `mnn ✓` (≥3.6.0), `tensorrt ✓` (≥3.7.0). **Your target path (onnxruntime-directml) is fully supported.**
- v5 `server` recognition variants exist **only** for `ch` and `eslav`. For Korean there is no server model — mobile only. That is fine: at 7B-class recognition heads, mobile is the only option anyway, and 13 MB models are essentially free on DirectML.
- The browser/desktop recommendation: instantiate **one** `RapidOCR` engine per routed language class and reuse it; a 13 MB ONNX rec model on a 4060 via DirectML is sub-millisecond to a few ms per text line. Peak VRAM for the whole OCR suite is well under 300 MB, so OCR never contends with the LLM.

**Measured benchmark evidence (published by PaddleOCR, not vendor marketing):**

PP-OCRv5 multilingual recognition accuracy vs the previous PP-OCRv3 generation — from the [official PP-OCRv5 multilingual page](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html) (also at the [v3.1.1 archive](http://www.paddleocr.ai/v3.1.1/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html)):

| Model | Korean dataset acc. | Latin-script dataset acc. | East-Slavic (ru/be/uk) dataset acc. |
|---|---|---|---|
| `korean_PP-OCRv5_mobile_rec` | **88.0 %** | — | — |
| `korean_PP-OCRv3_mobile_rec` | 23.0 % | — | — |
| `latin_PP-OCRv5_mobile_rec` | — | **84.7 %** | — |
| `latin_PP-OCRv3_mobile_rec` | — | 37.9 % | — |
| `eslav_PP-OCRv5_mobile_rec` | — | — | **85.8 %** |
| `cyrillic_PP-OCRv3_mobile_rec` | — | — | 50.2 % |

Datasets: 5,007 Korean text images; 3,111 Latin-script images; 7,031 Russian/Belarusian/Ukrainian images. The page also claims >30 % multilingual accuracy improvement over PP-OCRv3 overall.

PP-OCRv6 vs PP-OCRv5, in-house multi-scenario benchmark (from the [PP-OCRv6 doc](https://www.paddleocr.ai/latest/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html)) — recall% per scenario, higher is better:

| Model | W-Avg | Handwritten CN | Printed CN | Printed EN | Traditional | Ancient | **Japanese** | Screen | Card | Artistic | Industrial |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **PP-OCRv6_medium** | **83.2** | 62.1 | 91.5 | 94.1 | 78.6 | 72.4 | **90.5** | 82.5 | 88.1 | 71.2 | 77.4 |
| PP-OCRv6_small | 81.3 | 57.6 | 90.5 | 93.3 | 77.0 | 71.1 | 88.2 | 79.7 | 86.9 | 68.4 | 76.4 |
| PP-OCRv6_tiny | 73.5 | 40.1 | 86.7 | 88.4 | 65.0 | 68.4 | 89.8 | 71.2 | 80.5 | 54.7 | 62.1 |
| PP-OCRv5_server | 78.1 | 58.0 | 90.1 | 85.1 | 74.7 | 60.4 | 73.7 | 68.1 | 87.6 | 64.0 | 70.2 |
| PP-OCRv5_mobile | 73.7 | 41.7 | 86.0 | 86.0 | 72.0 | 57.8 | 75.8 | 57.6 | 81.7 | 54.0 | 59.3 |
| Qwen3-VL-235B | 74.9 | 49.7 | 82.3 | 86.2 | 76.4 | 33.6 | 66.2 | 73.8 | 78.7 | 69.6 | 74.7 |
| Gemini-3.1-Pro | 71.4 | 46.4 | 80.0 | 90.5 | 69.5 | 18.0 | 67.2 | 73.2 | 75.9 | 63.1 | 69.1 |
| GPT-5.5 | 64.2 | 19.2 | 75.7 | 82.2 | 57.5 | 63.7 | 58.6 | 67.7 | 71.1 | 53.0 | 62.4 |

Detection Hmean, same source: `PP-OCRv6_medium` **86.2** (Japanese 84.3, rotated **93.8**), `PP-OCRv6_small` 84.1, `PP-OCRv5_server` 81.6, `PP-OCRv5_mobile` 75.2. `PP-OCRv6_medium` = 34.5 M params, and is claimed to beat Qwen3-VL-235B and GPT-5.5 on accuracy.

Two conclusions from those tables:
- **Japanese recognition is a huge, measured win for v6**: 90.5 % vs 73.7 % for `PP-OCRv5_server` (+16.8 pp, the largest single-category gain reported). Keep v6 for Japanese. For comparison, `japan_PP-OCRv4_rec_mobile.onnx` exists (9,753,335 bytes) but v4 is two generations stale.
- **There is no v6 number for Korean or Cyrillic**, because the model does not support them. Your only option is v5.

ONNX-runtime latency reference (published, V100, ONNX Runtime backend, from the same PP-OCRv6 page): `PP-OCRv6_medium` 0.67 s/image end-to-end vs `PP-OCRv5_server` 0.77 s — v6 is both more accurate and ~15 % faster. `PP-OCRv6_small` is 0.53 s. On a 4060 Laptop expect roughly 3–6× those numbers (the 4060 is far below V100 in FP16), i.e. ~0.1–0.2 s per full game screen — irrelevant for a batch tool.

### Runner-up — pure PP-OCRv6 (`PP-OCRv6_rec_medium`), i.e. what you already have

Keep it as the fallback for zh/ja/en/Latin and the default when the language is unknown. It is the best single-model option in existence for those scripts and it needs no routing. It is *not* an option for Korean. Download sizes: tiny 4,489,813 B; small 21,234,383 B; medium 76,629,984 B. **License: Apache-2.0** (confirmed on `PaddlePaddle/PP-OCRv6_medium_rec` model card, `license: apache-2.0`). PaddleOCR itself is Apache-2.0.

### Do the ONNX alternatives beat PP-OCRv6 for pure recognition? Honestly: **no.**

| Candidate | Params | License | Verdict for *your* use case |
|---|---|---|---|
| **GOT-OCR2.0** (`stepfun-ai/GOT-OCR2_0`) | 580 M | **Apache-2.0** (verified via HF API) | End-to-end model — it *includes* detection, which you do not want, and its recognition is weaker than PP-OCRv6 on the published tables. Dated (2024). ONNX/GGUF community conversions exist but there is **no first-party ONNX path** and no DirectML story. Reject as a replacement; possible fallback curiosity. |
| **dots.ocr** (`rednote-hilab/dots.ocr`) | 3 B | **MIT** (verified via HF API) | Strong, but it is a *document parser*. On OmniDocBench it scores 90.77 with TextEdit 0.048 — **worse TextEdit than PP-OCRv6's own reported 83.2 W-Avg on a different benchmark, so not comparable**, and definitely not a pure-recognition win. 3 B fp16 ≈ 6–7 GB, does not leave room for the translator. A 4-bit quant exists (`helizac/dots.ocr-4bit`) but no official ONNX and no DirectML path. **Does not beat PP-OCRv6 for pure recognition.** |
| **PaddleOCR-VL-1.6** (`PaddlePaddle/PaddleOCR-VL-1.6`) | **0.96 B** (958,588,736 params) | **Apache-2.0** (verified, card says `license: apache-2.0`) | 109 languages, best-in-class document parsing. But Measured on the only comparable public table, it **loses badly to HunyuanOCR on cropped-region spotting**: 61.95 overall vs 71.40, and **58.56 vs 75.84 on the "Game" subset** (HunyuanOCR paper Table 13). It is a page parser, not a text-line reader. Also: no official ONNX; the first-party ONNX story is PaddleOCR's own export, and there is no DirectML-specific path. Great model, wrong tool for a cropped-region reader. |
| **DeepSeek-OCR-2** (`deepseek-ai/DeepSeek-OCR-2`) | 3 B | **Apache-2.0** (verified on card); DeepSeek-OCR-1.0 is **MIT** | OmniDocBench v1.6 Overall 90.25, **TextEdit 0.050**. Genuinely strong, and it **has an official GGUF** with an Ollama library entry (`deepseek-ocr`, 6.7 GB, 3B). But it is a document parser again, its Ollama entry has an **8-token context window**, and it needs CUDA/flash-attn for first-party inference. Not an onnxruntime/DirectML path. |
| `granite-docling-258M` / `SmolDocling` | 258 M | Apache-2.0 (verified) | Has real ONNX exports (`onnx-community/granite-docling-258M-ONNX`, `lamco-development/granite-docling-258M-onnx`) — the only truly ONNX-native alternative here. But it is a *layout+DocTags* model; its text-recognition accuracy is far below PP-OCRv6 and it offers no Korean/Cyrillic advantage over the v5 recognizers. Optional extra, not a primary. |

**Answer to the specific question:** for pure text-line recognition with onnxruntime-DirectML, **nothing beats the PP-OCR family**, and the correct move is not to switch families but to add the missing PP-OCRv5 script recognizers. GOT-OCR2.0, dots.ocr, DeepSeek-OCR and PaddleOCR-VL are all *end-to-end document parsers* that bundle their own detection, need CUDA-oriented stacks for first-party inference, and score worse than PP-OCRv6 on the text-recognition dimension you care about.

### Practical note on the directml/onnxruntime constraint

`onnxruntime-directml` supplies the `DmlExecutionProvider`. RapidOCR's ONNX models are plain, well-formed ONNX graphs (det = DBNet-style, rec = SVTR/CTC) with no exotic ops, so they run on DML. The only thing to watch: the PP-OCRv6 detection model has a dynamic input height/width; if DirectML complains about dynamic shapes, run detection at a few fixed buckets (e.g. 960×960, 960×1440) and keep recognition dynamic, which is where the per-line cost actually is. Nothing here requires CUDA wheels.

---

## 3. Vision fallback (read text from a cropped region; no detection)

### Top pick — `tencent/HunyuanOCR` (HunyuanOCR-1.5)

| Property | Value |
|---|---|
| Exact identifier | HuggingFace **`tencent/HunyuanOCR`** (root = 1.5; 1.0 archived in `v1.0/`). GGUF: **`ggml-org/HunyuanOCR-GGUF`**. **No Ollama library entry** (verified 404 at `ollama.com/library/hunyuanocr`) |
| Params | ~1 B class (text config: 24 layers × 1024 hidden, 16 heads / 8 KV heads; card and paper both say "1B") |
| Quant / size | `HunyuanOCR-Q8_0.gguf` ≈ **578 MB** + `mmproj-HunyuanOCR-Q8_0.gguf` ≈ **733 MB** → **~1.3 GB total**. bf16 pair: `HunyuanOCR-bf16.gguf` ~1.08 GB + `mmproj-HunyuanOCR-bf16.gguf` ~997 MB ≈ 2.1 GB |
| License | **Tencent Hunyuan Community License** (`license: other`, `license_name: tencent-hunyuan-community`). Same Territory restriction as Hunyuan-MT: **not applicable in the EU, UK, South Korea**; distribution only in the Territory; >100 M MAU needs a separate licence. |
| VRAM fit | **Trivially fits.** ~1.3 GB at Q8_0 leaves ~6 GB for the translator. This is the single most attractive property: unlike a 4B/8B VLM, HunyuanOCR can plausibly be resident *alongside* TranslateGemma on one 8 GB card. |
| Runtime path | Supports **llama.cpp with an OpenAI-compatible `llama-server`** (first-party statement on the card), and `ggml-org/HunyuanOCR-GGUF` is the official GGML org repo — so `hunyuan_vl` is implemented in llama.cpp. Ollama 0.35.0 support is *not* confirmed (no library entry); plan on either a `ollama create` Modelfile import of the two GGUFs or a separate llama-server process. |

**Benchmark evidence — and it is exactly the right benchmark.** The HunyuanOCR-1.5 paper ([arXiv:2607.04884](https://arxiv.org/abs/2607.04884)) evaluates **text spotting**, which is literally "locate and read text in an image region". Table 13, overall score and per-scenario:

| Model | Type | Overall | Art | Doc | **Game** | Hand | Ads | Receipt | Screen | Scene | Video |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **HunyuanOCR-1.5** | OCR VLM, 1 B | **71.40** | 53.21 | **79.43** | **75.84** | 78.40 | 75.03 | 65.22 | 74.51 | 65.12 | 76.09 |
| HunyuanOCR (1.0) | OCR VLM, 1 B | 70.92 | 56.76 | 73.63 | 73.54 | 77.10 | 75.34 | 63.51 | 76.58 | 64.56 | 77.31 |
| PaddleOCR-VL-1.6 | OCR VLM, 0.9 B | 61.95 | 41.36 | 72.20 | 58.56 | 70.61 | 65.24 | 61.85 | 63.63 | 54.60 | 69.52 |
| BaiduOCR | traditional | 61.90 | 38.50 | 78.95 | 59.24 | 59.06 | 66.70 | 63.66 | 68.18 | 55.53 | 67.38 |
| Qwen3.5-A17B | general VLM | 59.76 | 44.92 | 52.56 | 58.16 | 71.54 | 67.42 | 55.98 | 62.58 | 56.11 | 68.56 |
| Gemini 3.1 Pro | general VLM | 59.53 | 46.83 | 54.89 | 62.62 | 63.37 | 63.96 | 54.53 | 64.29 | 55.30 | 70.02 |
| Seed2.0 Pro | general VLM | 56.32 | 44.77 | 45.85 | 61.70 | 66.89 | 61.87 | 55.73 | 52.05 | 46.53 | 71.49 |
| Qwen3-VL-235B-A22B-Ins. | general VLM, 235 B | 53.62 | 46.15 | 43.78 | 48.00 | 68.90 | 64.01 | 47.53 | 45.91 | 54.56 | 63.79 |
| PaddleOCR | traditional | 53.38 | 32.83 | 70.23 | 51.59 | 56.39 | 57.38 | 50.59 | 63.38 | 44.68 | 53.35 |
| **Qwen3-VL-2B-Ins.** | general VLM, 2 B | **29.68** | 29.43 | 19.37 | 20.85 | 50.57 | 35.14 | 24.42 | 12.13 | 34.90 | 40.10 |
| Gemini 2.5 Pro | general VLM | 23.44 | 21.79 | 35.16 | 10.02 | 38.49 | 29.89 | 20.80 | 17.59 | 18.33 | 18.90 |

Three things fall out of this table:

1. **`Qwen3-VL-2B-Instruct` scores 29.68 overall and 20.85 on the "Game" subset** — the same family as your current `qwen3-vl:4b`, one size up. Even granting the 4B a healthy bump over the 2B, it sits in the 30–45 band. HunyuanOCR-1.5 at 75.84 on Game is **roughly 2× better on your exact content type**. This is the strongest single piece of evidence in this report.
2. **Scale does not rescue generic VLMs on this task.** Qwen3-VL-235B (53.62) and Gemini 3.1 Pro (59.53) both lose to a 1 B OCR specialist. Your instinct that "a specialist beats a generalist" holds on the vision side too.
3. **PaddleOCR-VL-1.6 is measurably worse than HunyuanOCR-1.5 on cropped-region text**, including on Game (58.56 vs 75.84), even though it wins on whole-page document parsing. This is the key distinction between "document parser" and "region reader".

Additional supporting numbers from the same paper:
- **OmniDocBench v1.6** (Table 12, Overall ↑ / TextEdit ↓): HunyuanOCR-1.5 **94.74 / 0.039** vs HunyuanOCR-1.0 92.03 / 0.048, dots.ocr 90.77 / 0.048, DeepSeek-OCR 2 90.25 / 0.050, Qwen3-VL-235B 89.78 / 0.063, olmOCR-7B 85.74 / 0.139.
- **Text-image translation** (Table 14) — closest public proxy for "read the stylised text and put it into Chinese": HunyuanOCR-1.5 **76.51** (other→en) / **76.01** (other→zh) / **83.69** (DoTA en→zh), vs Qwen3-VL-8B-Instruct 75.09 / 75.63 / 79.86, Qwen3-VL-4B-Instruct 70.38 / 70.29 / 78.45, Qwen3-VL-2B-Instruct 66.30 / 66.77 / 73.49. So even the *8B* Qwen3-VL is behind a 1B OCR specialist on other→zh.
- **Inference speed**: DFlash speculative decoding gives a claimed **6.37× Transformer / 2.14× vLLM** speedup, "fastest inference speed among all lightweight OCR VLMs" (author claim). A GGUF DFlash fork is provided for PC. On a 4060 in Q8_0, expect a cropped region to decode in well under a second even without DFlash.
- **Negative-sample handling (1.5 only)**: when a crop contains no text, it returns "no text" instead of hallucinating boxes. Directly useful for a fallback path that must not invent dialogue lines.
- **OCRBench** (Table 15): HunyuanOCR-1.5 **861** vs Qwen3-VL-2B-Instruct 858, Seed-1.6-Vision 881, Qwen3-VL-235B 920, Gemini 2.5 Pro 872. So on generic OCR-QA it is merely competitive — the win is specifically on spotting/region reading, which is your task.

### Runner-up — `Tencent/WeVisDoc-2B` (Apache-2.0, and the licence-clean choice)

| Property | Value |
|---|---|
| Exact identifier | `Tencent/WeVisDoc-2B` (also `WeVisDoc-4B`). GGUF: `prithivMLmods/WeVisDoc-2B-GGUF`, `mradermacher/WeVisDoc-2B-GGUF` |
| Params | 2,438,696,960 (2.44 B) |
| Quant / size | `WeVisDoc-2B.Q4_K_M.gguf` ≈ **1.28 GB** + `WeVisDoc-2B.mmproj-q8_0.gguf` ≈ **445 MB** → **~1.7 GB**; Q8_0 weights 2.17 GB; F16 4.07 GB |
| License | **Apache-2.0** (verified via HF API `license:apache-2.0`) — fully permissive, no territory carve-out |
| Base | Fine-tuned from **Qwen3-VL-2B-Instruct** / Qwen3-VL-4B-Instruct |
| VRAM fit | **Fits easily.** ~1.7 GB at Q4_K_M. |

**Evidence (published, from Tencent's own model card):**

| Benchmark | WeVisDoc-2B | WeVisDoc-4B |
|---|---|---|
| OmniDocBench v1.6 Overall ↑ | **95.06** | **95.38** |
| OmniDocBench v1.6 TextEdit ↓ | **0.038** | **0.036** |
| PureDocBench Avg₃ ↑ | 73.86 | 75.54 |

For reference on the same card: HunyuanOCR-1.5 94.74 / 0.039; FireRed-OCR 93.26 / 0.037; dots.ocr 90.77 / 0.048; DeepSeek-OCR 2 90.25 / 0.050; olmOCR-2-7B 85.51 / 0.106. So WeVisDoc-2B actually edges HunyuanOCR-1.5 **on whole-page parsing**, at 2× the size but with a clean licence.

**The honest caveat:** WeVisDoc is trained to turn a *page image* into structured Markdown with LaTeX/HTML. There is **no published per-scenario spotting benchmark** for it, and no "Game"/"Art" breakdown. It may well need the crop presented as a small "page". I would test it on a few hundred of your low-confidence crops before trusting it in the fallback path — but if it works, it is the only option here that is both Apache-2.0 and ≤2 GB.

### Other candidates evaluated

| Model | Params | License | Verdict |
|---|---|---|---|
| `PaddlePaddle/PaddleOCR-VL-1.6` | 0.96 B | **Apache-2.0** | APACHE-2.0 AND TINY (935 MB int8 GGUF + 882 MB mmproj; official `PaddlePaddle/PaddleOCR-VL-1.6-GGUF`). 109 languages per the [paper](https://arxiv.org/abs/2510.14528). But it *loses to HunyuanOCR on cropped-region spotting* (61.95 vs 71.40 overall; 58.56 vs 75.84 Game) and leads only on whole-page parsing (OmniDocBench 92.86 composite). Best model in this slot **if you want a permissive licence and page-level parsing**; second-best if you want a region reader. First-party inference is PaddlePaddle-based; GGUF exists but there is **no ONNX/DirectML path**. |
| `deepseek-ai/DeepSeek-OCR-2` | 3B-A0.5B | **Apache-2.0** | OmniDocBench v1.6 90.25 / TextEdit 0.050. Has an **Ollama entry (`deepseek-ocr`, 6.7 GB, context 8)** so it is the *easiest* to drop in today. But: 6.7 GB is a lot on an 8 GB card alongside anything else, the 8-token context is a serious constraint, and its spotting behaviour is unmeasured. DeepSeek-OCR 1.0 is MIT. |
| `MiniCPM-V-4_5` (`openbmb/MiniCPM-V-4_5`) | 8 B | Apache-2.0 | **OCRBench 89.0**, beating Qwen2.5-VL-7B and GLM-4.1V-9B, with a hybrid reasoning mode ([MiniCPM-V 4.5 paper, arXiv:2509.18154](https://arxiv.org/abs/2509.18154)). GGUF + Ollama exist (`minicpm-v`, 5.5 GB). Strong *general* VLM, but it is a generalist: it is on the wrong side of the specialist-vs-generalist gap that Table 13 above quantifies. Runner-up if you want one model for both vision QA and OCR. |
| `Qwen/Qwen3-VL-4B-Instruct` (`qwen3-vl:4b`, 3.3 GB) — **your current model** | 4 B | Apache-2.0 | Fine licence, good general VLM, and it is what you have. But its family scores **29.68 overall / 20.85 Game** in the 2B size, and 70.38 on MMTIT other→en vs 76.51 for a 1B OCR specialist. **Weakest link in the stack; worth replacing.** |
| `Qwen/Qwen3-VL-8B-Instruct` (`qwen3-vl:8b`, ~5–6 GB) | 8 B | Apache-2.0 | Still behind HunyuanOCR-1.5 on other→zh (75.63 vs 76.01) while being 6× larger and eating most of your VRAM. Not worth the space. |
| `llama3.2-vision:11b` (7.8 GB) | 11 B | Llama Community License | **Would spill to CPU** on 8 GB. Reject. |
| `granite3.2-vision:2b` (2.4 GB) | 2 B | Apache-2.0 | Small, permissive, but no OCR-leading evidence and a 16 K context. Not competitive with HunyuanOCR/WeVisDoc. |
| `GOT-OCR2.0` | 580 M | Apache-2.0 (verified) | Superseded by every model above; no first-party GGUF/ONNX. |
| `dots.ocr` | 3 B | **MIT** (verified) | OmniDocBench 90.77 / TextEdit 0.048; #2 open-source on OmniDocBench in a Dec-2025 third-party review ([codesota](https://www.codesota.com/ocr/dots-ocr)). Would need ~6–7 GB fp16 and has no official quant for Ollama. Reject at this VRAM. |
| `olmOCR-2-7B-1025` | 7 B | Apache-2.0 (verified) | OmniDocBench 85.51 / TextEdit 0.106 — **worse** than a 2B WeVisDoc. 7B ≈ 5 GB. Reject. |
| `rednote-hilab/dots.mocr` | 3 B | **MIT** (verified) | Newer (arXiv:2603.13032), SVG-focused. No evidence it beats HunyuanOCR on region reading. |

---

## If you had 24 GB VRAM (upgrade path)

| Slot | 8 GB pick | 24 GB pick | Gain |
|---|---|---|---|
| Translation | `translategemma:4b` (3.3 GB) | **`translategemma:12b`** (Ollama, 8.1 GB, Q4_K_M) or `tencent/Hunyuan-MT-Chimera-7B` (ensemble) | TranslateGemma 12B beat Gemma 3 27B on WMT24++/MetricX (Google claim); Hunyuan-MT-Chimera-7B posts **0.8787** WMT24pp XCOMET-XXL vs 0.8585 for the 7B and 0.8250 for Gemini-2.5-Pro (arXiv:2509.05209 Table 4). Chimera needs 6 candidate translations, so it is 6× the compute — a 24 GB card makes it tractable. |
| OCR recognition | PP-OCRv6 + PP-OCRv5 script routers (~250 MB total) | Same, plus `ch_PP-OCRv5_rec_server` / larger detection buckets at full resolution | OCR is already effectively free; more VRAM buys throughput (bigger input buckets, more parallel lines), not accuracy. Do not change the model family. |
| Vision fallback | `tencent/HunyuanOCR` Q8_0 (~1.3 GB) | `PaddleOCR-VL-1.6` or `dots.ocr` at bf16 (~2–7 GB) for whole-page parsing, **plus** keep HunyuanOCR for crops | Only worth it if you add full-page layout parsing, which is out of scope today. |
| Top-tier option | — | `translategemma:27b` (17 GB, single-H100-class per Google) | Best available quality in the permissive-licence family. |

At 24 GB you could also run the **Hunyuan-MT-Chimera-7B ensemble** (6 parallel hypotheses + a refinement pass) which is where the largest published translation gains live — but that is a throughput-heavy workflow, so treat it as a batch/offline quality mode.

---

## Summary table

| Slot | Top pick | Identifier | Size | License | 8 GB fit |
|---|---|---|---|---|---|
| Translation | TranslateGemma 4B (**keep current**) | Ollama `translategemma:4b` | 3.3 GB Q4_K_M | Apache-2.0 / Gemma Terms | ✅ ~3 GB headroom |
| Translation (quality mode) | Hunyuan-MT-7B | `mradermacher/Hunyuan-MT-7B-GGUF` Q4_K_M; no Ollama tag | 4.62 GB | Tencent Hunyuan Community (**no EU/UK/KR**) | ✅ tighter |
| OCR recognition (zh/ja/en/Latin) | PP-OCRv6 medium rec | `onnx/PP-OCRv6/rec/PP-OCRv6_rec_medium.onnx` | 76.6 MB | Apache-2.0 | ✅ |
| OCR recognition (KO) | PP-OCRv5 Korean mobile rec | `onnx/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx` | 13.5 MB | Apache-2.0 | ✅ |
| OCR recognition (RU/be/uk) | PP-OCRv5 East-Slavic mobile rec | `onnx/PP-OCRv5/rec/eslav_PP-OCRv5_rec_mobile.onnx` | 7.9 MB | Apache-2.0 | ✅ |
| OCR detection (**keep current**) | PP-OCRv6 medium det | `onnx/PP-OCRv6/det/PP-OCRv6_det_medium.onnx` | 62.1 MB | Apache-2.0 | ✅ |
| Vision fallback | HunyuanOCR-1.5 | `ggml-org/HunyuanOCR-GGUF` Q8_0 + mmproj | ~1.3 GB | Tencent Hunyuan Community (**no EU/UK/KR**) | ✅✅ |
| Vision fallback (permissive) | WeVisDoc-2B | `prithivMLmods/WeVisDoc-2B-GGUF` Q4_K_M + mmproj-q8_0 | ~1.7 GB | **Apache-2.0** | ✅ |

---

## What I could NOT verify

1. **TranslateGemma's exact per-language WMT24++ numbers.** I confirmed the claims qualitatively from Google's announcement as relayed by secondary sources (12B > Gemma 3 27B; 4B ≈ 12B baseline; 4B/12B/27B sizes) and the existence of the technical report at [arXiv:2601.09012](https://arxiv.org/abs/2601.09012), but the arXiv HTML render failed (both `ar5iv` and `arxiv.org/html` returned near-empty documents), so **I could not read the metric table directly**. No standalone, independent (non-Google) evaluation of TranslateGemma was found.
2. **TranslateGemma's licensing has an inconsistency I could not resolve.** Google's release is reported as **Apache-2.0**; the Ollama model page metadata says `license: Gemma`, and the base is Gemma 3. I do not know whether the weights are Apache-2.0 with a Gemma base, or under Gemma Terms. Resolve this before shipping — it affects your redistribution obligations, not your ability to use it offline.
3. **Whether `translategemma` supports JSON-constrained output or multi-item batching at all.** No published evidence either way. This is your key architectural risk (§1).
4. **Exact parameter count of HunyuanOCR-1.5.** The paper and card say "1B"; the `text_config` (24 layers × 1024 hidden) implies roughly 0.4–0.5 B in the language model plus an unstated vision tower. The GGUF file sizes (578 MB at Q8_0) are consistent with ~0.6–1 B total but I could not confirm the number.
5. **Whether Ollama 0.35.0 can load `hunyuan_vl` GGUFs.** No Ollama library entry exists (404). The GGUF exists in the official `ggml-org` org, so llama.cpp supports the architecture, but I could not verify Ollama's architecture allow-list for your specific version. Assume you may need a separate `llama-server`, or test an `ollama create` import.
6. **Whether WeVisDoc-2B works on a single cropped text region.** All published numbers are whole-page. No spotting/region benchmark exists for it.
7. **Whether PP-OCRv5 script recognizers can be driven directly from PP-OCRv6 detection crops.** ~~Requires a local test.~~ **已实测解决（见文首"实现状态"）**：可以混用，但必须用 `Rec.model_path` 绕过 RapidOCR 的 `MODEL_ROUTES`（它只注册了 PPOCRV6）。
8. **DirectML-specific performance or compatibility notes for PP-OCRv6 ONNX.** The published speed table uses ONNX Runtime on V100 and Xeon only ([source](https://www.paddleocr.ai/latest/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html)). No DirectML numbers exist publicly; your 84× OCR speedup measurement is the only DML data point I have seen.
9. **ByteDance Seed-X licensing.** The HF mirror refused anonymous API access for the ByteDance repos, and `github.com` is unreachable from this environment (DNS resolves to a non-public address). I could not read the licence text. Treat as unverified.
10. **EXAONE 3.5/4.0 licence scope.** Model card says `license: other`; I could not retrieve the EXAONE AI Model License text to check whether a non-commercial clause applies to the 7.8B checkpoint.
11. **Hunyuan-MT-Chimera VRAM/time cost on 8 GB.** The ensemble needs 6 candidate translations before refinement, so it is a multi-pass workflow. I did not verify whether a 7B Chimera pass is feasible within 8 GB in a single Ollama instance.
12. **The "84× faster than CPU for OCR" claim** is your measurement, not something I could corroborate or contradict from public sources.

### Environment limitations that affected this research

`huggingface.co`, `raw.githubusercontent.com`, and `github.com` are unreachable from this session (DNS resolves to non-public addresses); I worked around this via `hf-mirror.com`, `modelscope.cn`, `arxiv.org`, `paddleocr.ai`, `ollama.com`, and `pypi.org`. The `hf-mirror.com` API rate-limited partway through, so a few metadata lookups (noted above as unverified) could not be completed.
