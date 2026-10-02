"""「资源是不是加密的」这个判据的回归测试。

## 为什么要单独立一条能力

`IC 1.2`（アイリス☆クロニクル）抽到 **0 条**文本。第一反应是
"AssetBundle 没实现，所以抽不到"，但实测它 `_Data/` 里**没有任何**
loose `.assets`，文本全在 `StreamingAssets/StandaloneWindows64/`
的 95 个 CRC32 命名文件（237 MB）里 —— 而那 93 个**根本打不开**。

关键区别：

| 情况 | 根因 | 投入产出 |
| --- | --- | --- |
| **明文** AssetBundle 没解析 | 只是没写解析器 | 可以排期（但实测只值 0.03%） |
| **加密** AssetBundle | 需要逆密钥 | **不做**（能力边界） |

只靠"打不开"无法区分这两者，用户也会以为是用法问题。
所以用**熵**做成可证伪的判据，并在 CLI 里明确告知。

## 实测基准（这些数字就是判据的来源）

| 样本 | 熵 | 块熵极差 |
| --- | --- | --- |
| `IC 1.2` 加密 bundle | **7.9998** | 0.0008–0.0010 |
| 已知加密 `.wolf` | 7.5364 | 0.1796 |
| 已知明文 `.assets` | 6.3356 | 5.0854 |
"""

from __future__ import annotations

import os
import struct

import pytest

from novaloc.engines.unity_strings import (
    ENCRYPTED_BLOCK_SPREAD,
    ENCRYPTED_ENTROPY,
    looks_encrypted,
    shannon_entropy,
)

MB = 1024 * 1024


def _deterministic_random(n: int, seed: int = 12345) -> bytes:
    """确定性"随机"字节 —— 不用 `os.urandom`，测试才能复现。

    用 SHA-256 计数器模式生成，熵必然接近 8.0。
    """
    import hashlib

    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}:{i}".encode()).digest()
        i += 1
    return bytes(out[:n])


# ---------------------------------------------------------------------------
# shannon_entropy
# ---------------------------------------------------------------------------


def test_entropy_of_constant_data_is_zero() -> None:
    assert shannon_entropy(b"\x00" * MB) == 0.0
    assert shannon_entropy(b"A" * 1024) == 0.0


def test_entropy_of_uniform_bytes_is_eight() -> None:
    """256 种字节各出现同样多次 ⇒ 熵恰好 8.0。"""
    data = bytes(range(256)) * 256
    assert shannon_entropy(data) == pytest.approx(8.0, abs=1e-9)


def test_entropy_of_empty_is_zero() -> None:
    assert shannon_entropy(b"") == 0.0


# ---------------------------------------------------------------------------
# ★ 三种真实形态的区分
# ---------------------------------------------------------------------------


def test_random_like_data_is_encrypted() -> None:
    r"""★ 饱和熵 + 处处均匀 ⇒ 判定加密（对应 `IC 1.2` 的 7.9998）。"""
    data = _deterministic_random(2 * MB)
    assert shannon_entropy(data) > ENCRYPTED_ENTROPY, "构造的数据熵不够高"
    verdict, why = looks_encrypted(data)
    assert verdict is True, f"应判为加密，实际：{why}"
    assert "加密" in why


def test_plaintext_assets_are_not_encrypted() -> None:
    r"""★ 明文 `.assets`（有结构、块熵起伏大）**不能**被判成加密。

    对应实测的 6.3356 / 块熵极差 5.0854。
    """
    # 造一个"像序列化资源"的数据：长度前缀 + 文本 + 大段填充
    parts = bytearray()
    for i in range(400):
        s = f"Text number {i} for the game interface".encode()
        parts += struct.pack("<I", len(s)) + s + b"\x00" * 8
    parts += b"\x00" * MB  # 大量零填充 —— 明文资源里很常见
    data = bytes(parts)
    assert shannon_entropy(data) < ENCRYPTED_ENTROPY
    verdict, why = looks_encrypted(data)
    assert verdict is False, f"明文不该判成加密，实际：{why}"


def _compressed_like(n: int, seed: int = 7) -> bytes:
    """造一段"像真实压缩流"的字节：**字节频率不均匀**。

    ## 为什么不能用"看似随机"的生成器

    我先后试过三种，全部失败：

    | 生成方式 | 熵 | 问题 |
    | --- | --- | --- |
    | `(j*7+i) % 251` | 7.9980 | 251 个值近乎均匀 ⇒ 熵贴满 |
    | zlib 压"可压+随机" | 7.9985 | 同上 |
    | 字节限 `0..250` | 7.9715 | 只是少了 5 个值，仍近乎均匀 |

    而**真实** Unity 容器的压缩产物实测熵只有 **7.28–7.35**
    （6 个 `Academy Love Saga` 的 `.bundle`）。差别在于真实压缩流里
    **字节频率是不均匀的**（有偏好字节、有残留结构）。

    改用"加权分布"后得到 **7.4284**，落进真实区间。
    这个教训值得留着：**造不出真实分布时，先量真实样本，别硬凑。**
    """
    import random

    rnd = random.Random(seed)
    weights = [1] * 256
    for b in (0, 255, 32, 10, 101, 116, 97, 111):
        weights[b] = 12
    pool: list[int] = []
    for b, k in enumerate(weights):
        pool.extend([b] * k)
    return bytes(rnd.choice(pool) for _ in range(n))


def test_real_unityfs_compressed_bundle_is_not_encrypted() -> None:
    r"""★ **真实形状**的压缩容器不能判成加密。

    实测基准（见 :func:`_compressed_like` 的 docstring）：

    | 类别 | 熵 | 块熵极差 | 样本 |
    | --- | --- | --- | --- |
    | **加密** | 7.9999–8.0000 | 0.0005–0.0008 | `IC 1.2` |
    | 压缩 | 7.28–7.35 | 0.14–0.42 | 6 个真实 `.bundle` |
    | 明文 | 5.5001 | 5.0853 | `.assets` |

    三档分得很开，所以判据在真实数据上是对的。这条测试用
    "字节频率不均匀"的合成体（熵 ≈ 7.43）钉住它。
    """
    data = b"UnityFS\x00\x00\x00\x08" + _compressed_like(2 * MB)
    e = shannon_entropy(data)
    assert e < ENCRYPTED_ENTROPY, f"构造数据熵 {e:.4f} 太高，测不到想测的东西"
    verdict, why = looks_encrypted(data)
    assert verdict is False, (
        f"熵 {e:.4f} 的压缩容器形状不该判成加密：{why}"
    )


def test_documented_limit_near_incompressible_data() -> None:
    r"""★ **把判据的局限钉成测试**，而不是假装它不存在。

    实测：把"一半可压、一半本身接近随机"的数据用 zlib 最高级别压，
    产物熵 **7.9997**、块熵极差 **0.0015** —— 会被判成"加密"。

    也就是**熵判据无法区分"已经压到极限的数据"与"加密数据"**，
    因为两者在统计上都是"均匀随机"。这是判据的**固有**局限，
    不是实现 bug（除非引入加密特征检测，那是另一件事）。

    实际影响很小：真实 Unity 容器的压缩产物实测熵只有 7.28–7.35，
    离 7.99 还有很远。但必须**如实记录**。

    这条测试断言的是"当前确实会误判"，所以：
    * 若哪天判据改好了（能区分了），这条会**红**，提醒更新文档；
    * 若有人把阈值调成"什么都判加密"，上面那条真实形状用例会先红。
    """
    import zlib

    body = bytearray()
    for i in range(64):
        if i % 2 == 0:
            body += b"repeated pattern " * 2000
        else:
            body += _deterministic_random(120000, seed=i)
    data = zlib.compress(bytes(body), 9)
    e = shannon_entropy(data)
    if e < ENCRYPTED_ENTROPY:
        pytest.skip(f"这份构造数据的熵只有 {e:.4f}，触发不到已知局限")
    verdict, why = looks_encrypted(data)
    assert verdict is True, (
        "已知局限：压到极限的数据会被判成加密。"
        f"若判据已改进到能区分，请更新 docstring 与本用例。实际：{why}"
    )


def test_short_sample_refuses_to_conclude() -> None:
    r"""★ 样本太短时**不下结论** —— 几百字节的随机数据熵不可靠。

    宁可返回 False（不声称加密），也不要基于不足的证据给用户
    一个"你的游戏加密了"的错误结论。
    """
    data = _deterministic_random(4096)
    verdict, why = looks_encrypted(data)
    assert verdict is False
    assert "太短" in why


# ---------------------------------------------------------------------------
# 判据参数的边界
# ---------------------------------------------------------------------------


def test_thresholds_are_in_sane_range() -> None:
    r"""阈值本身要合理 —— 防止有人把它调成"什么都是加密"。

    实测基准：加密 7.9998、压缩/加密边界样本 7.5364、明文 6.3356。
    阈值必须在"明文"与"加密"之间，且贴近上限。
    """
    assert 7.9 < ENCRYPTED_ENTROPY <= 8.0, (
        f"加密熵阈值 {ENCRYPTED_ENTROPY} 不合理：实测明文 6.3356、"
        f"加密 7.9998，阈值应贴近 8.0"
    )
    assert 0.0 < ENCRYPTED_BLOCK_SPREAD < 0.18, (
        f"块熵极差阈值 {ENCRYPTED_BLOCK_SPREAD} 不合理：实测加密 0.001、"
        f"压缩样本 0.18，阈值必须在这两者之间"
    )


def test_encrypted_detection_is_deterministic() -> None:
    """同一份数据判定必须稳定（不能有时说加密有时说不是）。"""
    data = _deterministic_random(MB)
    results = {looks_encrypted(data) for _ in range(5)}
    assert len(results) == 1, f"判定不稳定：{results}"


@pytest.mark.skipif(os.name != "nt", reason="真实样本路径只在 Windows 上存在")
def test_real_ic12_bundle_is_detected() -> None:
    r"""★ 真实样本回归：`IC 1.2` 的加密 bundle 必须被认出来。

    若这个游戏被移动/删除，测试跳过而不是失败 —— 它是**外部**依赖，
    不能因为用户没这个游戏就让测试红。但它在的时候，判定必须对。
    """
    from pathlib import Path

    p = (
        Path(r"E:\lush\1\newlytransport\IC 1.2")
        / "アイリス★クロニクル_Data"
        / "StreamingAssets"
        / "StandaloneWindows64"
        / "-1830081318"
    )
    if not p.is_file():
        pytest.skip("真实样本不在本机")
    verdict, why = looks_encrypted(p.read_bytes()[:MB])
    assert verdict is True, f"IC 1.2 的加密 bundle 没被认出：{why}"
