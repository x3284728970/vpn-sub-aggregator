#!/usr/bin/env python3
"""iPoW.ai 节点提取（并发优化版 / 聚合订阅接入版）

输出节点名字统一为 `<源标识>-<地区>-<序号>`，例如 `iPoW-日本-03`，
方便在客户端里一眼看出这条节点来自哪个 VPN、落地在哪个地区。

【实测限流语义（重要）】
- 限流是**出口 IP 级滑动窗口**，不是单纯的账号配额：窗口充足时单个账号能取 24 个节点，
  随着本机累计用量上升，新账号的可取数量会下降（实测掉到 16 个），继续猛注册会让服务端
  直接停发试用订阅 —— 新账号返回 `status: inactive / plan_id: phase1 / provider: none`，
  capability 接口回 402 `subscription_inactive`。
- 超限返回 429 且带 `Retry-After`（实测 262~293 秒）。该值是**递减的剩余窗口**，
  实测 300 秒后恢复。所以撞限流时**等窗口**才是有效解法，换钱包解决不了 IP 级限流，
  只会加速风控。脚本默认 `--on-429 wait` 就是基于这一点。
- 窗口充足时按 24 次/钱包预切批，正常情况下一次跑完不会撞限流。

【相对原版的优化】
1. 并发拉取：`--concurrency` 默认 6，24 个节点实测 1.5 秒（原版串行约 24 秒）。
2. 限流处理改为「原地等窗口」（默认），不再像原版那样无脑换号 —— 原版在 IP 级限流下会把
   `--wallets` 额度全部烧成死账号，还拿不到几个节点。
3. 注册后立即校验订阅状态，遇到风控停发试用就立刻收手，不继续注册加重封禁。
4. 不可用节点缓存（`iPoW_unusable.json`，默认 6 小时）：原版每次重跑都重新探测，
   实测 47 个节点里有 7 个不可用，白烧 15% 的配额。
5. 采纳服务端的 `route_failure_exclude_node_ids`，明说要排除的节点直接跳过，连配额都不花。
6. 瞬态错误（网络异常/5xx）退避重试，不再像原版那样一次性永久拉黑。
7. 修正 `--wallets` 的差一错误（原版 `--wallets 1` 实际会用掉 2 个钱包）。
8. 钱包记录改为合并写入，不再每次覆盖。
9. 地区中文名 + 源前缀重命名，并追加 `iPoW.txt`（纯链接）与 base64 订阅两种聚合器友好的产物。

用法示例：
    python ipow.py                          # 拉取全部节点到脚本目录
    python ipow.py --limit 6                # 只取 6 个有效节点
    python ipow.py --concurrency 8          # 8 线程并发
    python ipow.py --source-tag HX           # 自定义源前缀
    python ipow.py -o /sdcard/Download      # 安卓 Termux：输出到系统下载目录
    python ipow.py -v                       # 详细日志
"""

import argparse
import base64
import concurrent.futures
import json
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 可选硬件加速检测（若装了库自动启用加速，若没装则完全走内置纯 Python 引擎）
# ---------------------------------------------------------------------------
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    _HAS_ETH_ACCOUNT = True
except ImportError:
    _HAS_ETH_ACCOUNT = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_BASE = 'https://ipow.ai'
USER_AGENT = 'Dart/3.10 (dart:io)'
CLIENT_TYPE = 'android'
CLIENT_VERSION = '4.3.4'
CAPABILITY = 'vless-reality:no-flow'
DEFAULT_SNI = 'www.cloudflare.com'
DEFAULT_FINGERPRINT = 'chrome'

# 产物文件名一律带 iPoW_ 前缀，避免与 scripts/ 下其它来源撞名
MERGED_NODES_FILE = 'iPoW_nodes.json'
LINKS_FILE = 'iPoW.txt'
CLASH_FILE = 'iPoW_clash.yaml'
SINGBOX_FILE = 'iPoW_singbox.json'
SUB_B64_FILE = 'iPoW_sub_base64.txt'
WALLETS_FILE = 'iPoW_wallets.json'
UNUSABLE_FILE = 'iPoW_unusable.json'

DEFAULT_SOURCE_TAG = 'iPoW'
# 运行时由 --source-tag 覆盖，所有节点名都带上它，标明来源是哪个 VPN
_SOURCE_TAG = DEFAULT_SOURCE_TAG
# 实测：单账号恰好 24 次 capability 请求的配额
QUOTA_PER_WALLET = 24
# 不可用节点缓存有效期（秒），默认 6 小时
UNUSABLE_TTL = 6 * 3600

# 地区码 → 中文名（目录里出现过的 24 个，外加常见备用）
REGION_CN = {
    'AU': '澳大利亚', 'BE': '比利时', 'BR': '巴西', 'CA': '加拿大', 'CL': '智利',
    'DE': '德国', 'FI': '芬兰', 'FR': '法国', 'GB': '英国', 'HK': '香港',
    'ID': '印度尼西亚', 'IL': '以色列', 'IN': '印度', 'JP': '日本', 'KR': '韩国',
    'MX': '墨西哥', 'NL': '荷兰', 'PL': '波兰', 'QA': '卡塔尔', 'SE': '瑞典',
    'SG': '新加坡', 'TW': '台湾', 'US': '美国', 'ZA': '南非',
    'AE': '阿联酋', 'AR': '阿根廷', 'AT': '奥地利', 'CH': '瑞士', 'CZ': '捷克',
    'DK': '丹麦', 'EE': '爱沙尼亚', 'EG': '埃及', 'ES': '西班牙', 'GR': '希腊',
    'HU': '匈牙利', 'IE': '爱尔兰', 'IT': '意大利', 'LT': '立陶宛', 'LV': '拉脱维亚',
    'MY': '马来西亚', 'NG': '尼日利亚', 'NO': '挪威', 'NZ': '新西兰', 'PH': '菲律宾',
    'PT': '葡萄牙', 'RO': '罗马尼亚', 'RS': '塞尔维亚', 'RU': '俄罗斯', 'SA': '沙特',
    'TH': '泰国', 'TR': '土耳其', 'UA': '乌克兰', 'VN': '越南',
}

# ---------------------------------------------------------------------------
# 异常定义
# ---------------------------------------------------------------------------
class IpowError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code

class QuotaExhausted(IpowError):
    def __init__(self, detail=''):
        super().__init__(
            '配额已耗尽 (402)' + ('：' + detail if detail else ''),
            code='quota_exhausted')

class RateLimited(IpowError):
    def __init__(self, retry_after, detail=''):
        self.retry_after = max(int(retry_after or 0), 1)
        super().__init__('服务端限流 (429)，需等待 {} 秒{}'.format(
            self.retry_after, '：' + detail if detail else ''))


class TransientError(IpowError):
    """网络异常与 5xx —— 可重试，不能据此把节点判死。"""

# ---------------------------------------------------------------------------
# 内置纯 Python 密码学模块 (100% 标准库)
# ---------------------------------------------------------------------------

# 1. Keccak-256
def _keccak_256(data: bytes) -> bytes:
    state = [[0] * 5 for _ in range(5)]
    RC = [
        0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
        0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
        0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
        0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
        0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
        0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
    ]
    r_rot = [
        [0, 36, 3, 41, 18],
        [1, 44, 10, 45, 2],
        [62, 6, 43, 15, 61],
        [28, 55, 25, 21, 56],
        [27, 20, 39, 8, 14],
    ]
    rate = 136
    pad_len = rate - (len(data) % rate)
    padded = data + (b'\x81' if pad_len == 1 else b'\x01' + b'\x00' * (pad_len - 2) + b'\x80')

    def rotl64(x, n):
        return ((x << (n % 64)) | (x >> (64 - (n % 64)))) & 0xFFFFFFFFFFFFFFFF

    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(17):
            val = int.from_bytes(block[i * 8:(i + 1) * 8], 'little')
            state[i % 5][i // 5] ^= val

        for round_idx in range(24):
            C = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
            D = [C[(x + 4) % 5] ^ rotl64(C[(x + 1) % 5], 1) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    state[x][y] ^= D[x]

            B = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    B[y][(2 * x + 3 * y) % 5] = rotl64(state[x][y], r_rot[x][y])

            for x in range(5):
                for y in range(5):
                    state[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y])

            state[0][0] ^= RC[round_idx]

    out = bytearray()
    for y in range(5):
        for x in range(5):
            out.extend(state[x][y].to_bytes(8, 'little'))
            if len(out) >= 32:
                return bytes(out[:32])

# 2. Secp256k1 椭圆曲线与以太坊地址/签名
_SECP_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP_Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_SECP_Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_SECP_G = (_SECP_Gx, _SECP_Gy)
_SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

def _secp_inv(k, p):
    return pow(k, p - 2, p)

def _secp_point_add(p1, p2):
    if p1 is None: return p2
    if p2 is None: return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2: return None
    if x1 == x2:
        m = (3 * x1 * x1) * _secp_inv(2 * y1, _SECP_P) % _SECP_P
    else:
        m = (y2 - y1) * _secp_inv(x2 - x1, _SECP_P) % _SECP_P
    x3 = (m * m - x1 - x2) % _SECP_P
    y3 = (m * (x1 - x3) - y1) % _SECP_P
    return (x3, y3)

def _secp_point_mul(k, p):
    res = None
    cur = p
    while k > 0:
        if k & 1: res = _secp_point_add(res, cur)
        cur = _secp_point_add(cur, cur)
        k >>= 1
    return res

def _pure_private_key_to_address(privkey_bytes: bytes) -> str:
    d = int.from_bytes(privkey_bytes, 'big')
    pub = _secp_point_mul(d, _SECP_G)
    pub_bytes = pub[0].to_bytes(32, 'big') + pub[1].to_bytes(32, 'big')
    addr_hex = _keccak_256(pub_bytes)[12:].hex().lower()
    h = _keccak_256(addr_hex.encode('ascii')).hex()
    res = [c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(addr_hex)]
    return '0x' + ''.join(res)

def _pure_sign_personal_message(privkey_hex: str, message_str: str) -> str:
    raw_hex = privkey_hex[2:] if privkey_hex.startswith('0x') else privkey_hex
    privkey_bytes = bytes.fromhex(raw_hex)
    msg_bytes = message_str.encode('utf-8')
    prefix = f'\x19Ethereum Signed Message:\n{len(msg_bytes)}'.encode('ascii')
    z = int.from_bytes(_keccak_256(prefix + msg_bytes), 'big')
    d = int.from_bytes(privkey_bytes, 'big')

    while True:
        k = random.SystemRandom().randint(1, _SECP_N - 1)
        r_point = _secp_point_mul(k, _SECP_G)
        r = r_point[0] % _SECP_N
        if r == 0: continue
        s = (_secp_inv(k, _SECP_N) * (z + r * d)) % _SECP_N
        if s == 0: continue
        v = 27 + (r_point[1] % 2)
        if s > _SECP_N // 2:
            s = _SECP_N - s
            v = 27 + (1 - (r_point[1] % 2))
        break

    return '0x' + r.to_bytes(32, 'big').hex() + s.to_bytes(32, 'big').hex() + bytes([v]).hex()

# 3. 纯 Python AES-256-GCM 解密
_AES_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16
]
_AES_RCON = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]

def _aes_key_expansion(key: bytes):
    nk = len(key) // 4
    nr = nk + 6
    w = [[key[4*i], key[4*i+1], key[4*i+2], key[4*i+3]] for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_AES_SBOX[b] for b in temp]
            temp[0] ^= _AES_RCON[i // nk]
        elif nk > 6 and (i % nk == 4):
            temp = [_AES_SBOX[b] for b in temp]
        w.append([a ^ b for a, b in zip(w[i - nk], temp)])
    return w, nr

def _aes_xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)

def _aes_encrypt_block(block: bytes, w, nr: int) -> bytes:
    state = [[block[r + 4*c] for c in range(4)] for r in range(4)]
    for c in range(4):
        for r in range(4):
            state[r][c] ^= w[c][r]

    for round_num in range(1, nr):
        for r in range(4):
            for c in range(4):
                state[r][c] = _AES_SBOX[state[r][c]]
        state[1] = state[1][1:] + state[1][:1]
        state[2] = state[2][2:] + state[2][:2]
        state[3] = state[3][3:] + state[3][:3]
        for c in range(4):
            s0, s1, s2, s3 = state[0][c], state[1][c], state[2][c], state[3][c]
            state[0][c] = _aes_xtime(s0 ^ s1) ^ s1 ^ s2 ^ s3
            state[1][c] = _aes_xtime(s1 ^ s2) ^ s2 ^ s3 ^ s0
            state[2][c] = _aes_xtime(s2 ^ s3) ^ s3 ^ s0 ^ s1
            state[3][c] = _aes_xtime(s3 ^ s0) ^ s0 ^ s1 ^ s2
        for c in range(4):
            for r in range(4):
                state[r][c] ^= w[round_num * 4 + c][r]

    for r in range(4):
        for c in range(4):
            state[r][c] = _AES_SBOX[state[r][c]]
    state[1] = state[1][1:] + state[1][:1]
    state[2] = state[2][2:] + state[2][:2]
    state[3] = state[3][3:] + state[3][:3]
    for c in range(4):
        for r in range(4):
            state[r][c] ^= w[nr * 4 + c][r]

    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[r + 4*c] = state[r][c]
    return bytes(out)

def _ghash(h_bytes, data):
    h = int.from_bytes(h_bytes, 'big')
    r = 0xE1000000000000000000000000000000
    y = 0
    for i in range(0, len(data), 16):
        x = int.from_bytes(data[i:i+16], 'big')
        v = y ^ x
        z = 0
        for bit in range(128):
            if (h >> (127 - bit)) & 1:
                z ^= v
            if v & 1:
                v = (v >> 1) ^ r
            else:
                v >>= 1
        y = z
    return y.to_bytes(16, 'big')

def _pure_aes_gcm_decrypt(key: bytes, nonce: bytes, ct_and_tag: bytes, aad: bytes = b'') -> bytes:
    if len(nonce) != 12:
        raise ValueError('Nonce 长度必须为 12 字节')
    if len(ct_and_tag) < 16:
        raise ValueError('密文长度不足（缺少 Tag）')
    ct = ct_and_tag[:-16]
    expected_tag = ct_and_tag[-16:]

    w, nr = _aes_key_expansion(key)
    h_bytes = _aes_encrypt_block(b'\x00' * 16, w, nr)

    j0 = nonce + b'\x00\x00\x00\x01'
    j0_enc = _aes_encrypt_block(j0, w, nr)

    pad_aad = aad + b'\x00' * (-len(aad) % 16)
    pad_ct = ct + b'\x00' * (-len(ct) % 16)
    len_blk = (len(aad) * 8).to_bytes(8, 'big') + (len(ct) * 8).to_bytes(8, 'big')
    ghash_data = pad_aad + pad_ct + len_blk

    ghash_out = _ghash(h_bytes, ghash_data)
    computed_tag = bytes(a ^ b for a, b in zip(ghash_out, j0_enc))
    if computed_tag != expected_tag:
        raise ValueError('GCM 校验标签不匹配')

    counter = 2
    pt = bytearray()
    for i in range(0, len(ct), 16):
        cb = nonce + counter.to_bytes(4, 'big')
        ks = _aes_encrypt_block(cb, w, nr)
        block = ct[i:i+16]
        pt.extend(bytes(a ^ b for a, b in zip(block, ks[:len(block)])))
        counter += 1
    return bytes(pt)

# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------

def b64d(s):
    s = s.replace('-', '+').replace('_', '/')
    return base64.b64decode(s + '=' * (-len(s) % 4))


def country_code_from_node_id(node_id):
    m = re.search(r'-([a-z]{2})-\d+$', node_id or '')
    return m.group(1).upper() if m else None


def get_default_download_dir():
    """获取系统默认下载目录（安卓上自动使用 /sdcard/Download，电脑上使用用户下载文件夹）"""
    # 1. Windows 平台
    if os.name == 'nt':
        win_dl = os.path.join(os.path.expanduser('~'), 'Downloads')
        if os.path.isdir(win_dl):
            return win_dl
        return BASE_DIR

    # 2. Android (Termux) 平台优先检测
    android_paths = [
        '/sdcard/Download',
        '/storage/emulated/0/Download',
        os.path.expanduser('~/storage/downloads'),
    ]
    for p in android_paths:
        if os.path.isdir(p) and os.access(p, os.W_OK):
            return p

    # 尝试创建 /sdcard/Download
    for parent in ['/sdcard', '/storage/emulated/0']:
        if os.path.isdir(parent):
            target = os.path.join(parent, 'Download')
            try:
                os.makedirs(target, exist_ok=True)
                if os.access(target, os.W_OK):
                    return target
            except Exception:
                pass

    # 3. Linux / macOS 桌面平台
    user_dl = os.path.join(os.path.expanduser('~'), 'Downloads')
    if os.path.isdir(user_dl) and os.access(user_dl, os.W_OK):
        return user_dl

    # 4. 回退到脚本自身所在目录
    return BASE_DIR


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# 网络客户端
# ---------------------------------------------------------------------------

class _RateLimiter:
    """全局最小请求间隔，多线程共享，保证并发时也不会把服务端打爆。"""

    def __init__(self, min_interval):
        self.min_interval = max(float(min_interval or 0), 0.0)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            gap = self._next_at - now
            if gap > 0:
                time.sleep(gap)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self.min_interval


class IpowClient:
    def __init__(self, base=API_BASE, timeout=20, verbose=False,
                 min_interval=0.15, retries=3):
        self.base = base.rstrip('/')
        self.timeout = timeout
        self.verbose = verbose
        self.retries = max(int(retries), 0)
        self.limiter = _RateLimiter(min_interval)
        self._local = threading.local()
        if not _HAS_REQUESTS:
            try:
                self.ssl_ctx = ssl.create_default_context()
            except Exception:
                self.ssl_ctx = ssl._create_unverified_context()

    @property
    def session(self):
        """每个线程各自持有一个 requests.Session，避免连接池跨线程竞争。"""
        if not _HAS_REQUESTS:
            return None
        sess = getattr(self._local, 'session', None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({
                'User-Agent': USER_AGENT,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            })
            self._local.session = sess
        return sess

    def _request(self, method, path, body=None, token=None):
        """带瞬态重试的请求入口。只有网络异常与 5xx 会重试，业务错误直接抛。"""
        attempt = 0
        while True:
            try:
                return self._request_once(method, path, body, token)
            except TransientError as exc:
                if attempt >= self.retries:
                    raise
                attempt += 1
                delay = min(0.5 * (2 ** attempt), 8.0)
                if self.verbose:
                    print('  !! 瞬态失败，{:.1f}s 后重试 {}/{}: {}'.format(
                        delay, attempt, self.retries, exc))
                time.sleep(delay)

    def _request_once(self, method, path, body=None, token=None):
        url = self.base + path
        if self.verbose:
            shown = json.dumps(body, ensure_ascii=False)[:160] if body else ''
            print('  -> {} {} {}'.format(method, path, shown))

        self.limiter.wait()
        session = self.session
        if session is not None:
            headers = {'Authorization': 'Bearer ' + token} if token else {}
            try:
                r = session.request(method, url, json=body, headers=headers,
                                    timeout=self.timeout)
            except Exception as err:
                raise TransientError('网络请求失败 ({} {}): {}'.format(method, path, err))
            status_code = r.status_code
            resp_headers = r.headers
            resp_text = r.text
        else:
            headers = {
                'User-Agent': USER_AGENT,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            }
            if token:
                headers['Authorization'] = 'Bearer ' + token
            data = json.dumps(body).encode('utf-8') if body is not None else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                    status_code = resp.status
                    resp_headers = resp.headers
                    resp_text = resp.read().decode('utf-8', errors='replace')
            except urllib.error.HTTPError as err:
                status_code = err.code
                resp_headers = err.headers
                resp_text = err.read().decode('utf-8', errors='replace')
            except Exception as err:
                raise TransientError(f'网络请求失败 ({method} {path}): {err}')

        if self.verbose:
            print('  <- {} {}'.format(status_code, resp_text[:160]))

        if status_code == 429:
            raw = resp_headers.get('Retry-After') or resp_headers.get('retry-after')
            try:
                retry_after = int(float(raw))
            except (TypeError, ValueError):
                retry_after = 30
            raise RateLimited(retry_after, resp_text[:120])
        if status_code == 401:
            raise IpowError('JWT 已失效 (401)')
        if status_code == 402:
            raise QuotaExhausted(resp_text[:160])
        if status_code >= 500:
            raise TransientError('服务端 {} {}: {}'.format(
                status_code, path, resp_text[:160]))
        if status_code >= 400:
            code = None
            try:
                code = (json.loads(resp_text).get('error') or {}).get('code')
            except Exception:
                pass
            raise IpowError('HTTP {} {}: {}'.format(
                status_code, path, resp_text[:300]), code=code)

        try:
            return json.loads(resp_text)
        except Exception:
            raise IpowError('响应不是 JSON: ' + resp_text[:200])

    def challenge(self, address):
        return self._request('POST', '/v1/auth/wallet/challenge',
                             {'address': address})

    def verify(self, address, signature, nonce, device_id, device_name):
        return self._request('POST', '/v1/auth/wallet/verify', {
            'address': address,
            'signature': signature,
            'nonce': nonce,
            'device_id': device_id,
            'device_name': device_name,
            'client_type': CLIENT_TYPE,
        })

    def session_start(self, token, device_id, device_name):
        return self._request('POST', '/p2p-lite/v2/vpn/sessions/start', {
            'client_type': CLIENT_TYPE,
            'device_id': device_id,
            'device_name': device_name,
            'client_version': CLIENT_VERSION,
            'preferred_country': 'AUTO',
        }, token=token)

    def capability(self, token, session_id, country, subscription_token,
                   device_id, node_id=None):
        body = {
            'session_id': session_id,
            'client_type': CLIENT_TYPE,
            'device_id': device_id,
            'country': country,
            'capabilities': [CAPABILITY],
            'subscription_token': subscription_token,
            'allow_country_switch': True,
        }
        if node_id:
            body['node_id'] = node_id
        return self._request('POST', '/p2p-lite/v2/dht/capability',
                             body, token=token)

# ---------------------------------------------------------------------------
# 解密与配置生成
# ---------------------------------------------------------------------------

def region_label(node):
    """地区中文名：优先用地区码查表，查不到退回服务端给的国家英文名。"""
    code = (node.get('country_code') or '').upper()
    if code and code in REGION_CN:
        return REGION_CN[code]
    name = node.get('country')
    if name:
        return str(name)
    return code or '未知'


def node_name(node):
    """节点显示名。已在 assign_display_names 里定名的直接用，否则退化为「源-地区」。"""
    if node.get('display_name'):
        return node['display_name']
    return '{}-{}'.format(_SOURCE_TAG, region_label(node))


def assign_display_names(nodes, source_tag):
    """按地区排序后统一编号，名字形如 `iPoW-日本-03`，并重建 vless 链接。"""
    ordered = sorted(nodes, key=lambda n: (region_label(n),
                                           n.get('node_id') or ''))
    counter = {}
    for n in ordered:
        region = region_label(n)
        counter[region] = counter.get(region, 0) + 1
        n['region_cn'] = region
        n['display_name'] = '{}-{}-{:02d}'.format(source_tag, region, counter[region])
        n['vless_url'] = build_vless_url(n)
    return ordered


def build_vless_url(node):
    name = urllib.parse.quote(node_name(node), safe='')
    fp = node.get('fingerprint') or DEFAULT_FINGERPRINT
    parts = [
        'encryption=none',
        'flow=',
        'security=reality',
        'sni={}'.format(node.get('sni') or DEFAULT_SNI),
        'fp={}'.format(fp),
        'pbk={}'.format(node['public_key']),
        'sid={}'.format(node['short_id']),
        'type=tcp',
    ]
    return 'vless://{}@{}:{}?{}#{}'.format(
        node['uuid'], node['server'], node['port'], '&'.join(parts), name)


def build_clash_yaml(nodes):
    lines = [
        '# Clash Meta / Mihomo Proxies Configuration',
        'proxies:',
    ]
    for n in nodes:
        lines.append("  - name: '{}'".format(node_name(n)))
        lines.append('    type: vless')
        lines.append('    server: {}'.format(n['server']))
        lines.append('    port: {}'.format(n['port']))
        lines.append('    uuid: {}'.format(n['uuid']))
        lines.append('    network: tcp')
        lines.append('    tls: true')
        lines.append('    udp: true')
        lines.append('    servername: {}'.format(n.get('sni') or DEFAULT_SNI))
        lines.append('    client-fingerprint: {}'.format(
            n.get('fingerprint') or DEFAULT_FINGERPRINT))
        lines.append('    reality-opts:')
        lines.append("      public-key: '{}'".format(n['public_key']))
        lines.append("      short-id: '{}'".format(n['short_id']))
    return '\n'.join(lines)


def build_singbox_json(nodes):
    outbounds = []
    for n in nodes:
        outbounds.append({
            'type': 'vless',
            'tag': node_name(n),
            'server': n['server'],
            'server_port': n['port'],
            'uuid': n['uuid'],
            'packet_encoding': 'xudp',
            'tls': {
                'enabled': True,
                'server_name': n.get('sni') or DEFAULT_SNI,
                'utls': {
                    'enabled': True,
                    'fingerprint': n.get('fingerprint') or DEFAULT_FINGERPRINT,
                },
                'reality': {
                    'enabled': True,
                    'public_key': n['public_key'],
                    'short_id': n['short_id'],
                },
            },
        })
    return {'outbounds': outbounds}


def write_configs(nodes, work_dir, quiet=False):
    os.makedirs(work_dir, exist_ok=True)
    nodes = assign_display_names(nodes, _SOURCE_TAG)
    links = [n['vless_url'] for n in nodes]
    paths = {
        'links': os.path.join(work_dir, LINKS_FILE),
        'json': os.path.join(work_dir, MERGED_NODES_FILE),
        'clash': os.path.join(work_dir, CLASH_FILE),
        'singbox': os.path.join(work_dir, SINGBOX_FILE),
        'b64': os.path.join(work_dir, SUB_B64_FILE),
    }
    with open(paths['links'], 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(links) + '\n')
    with open(paths['json'], 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(nodes, indent=2, ensure_ascii=False) + '\n')
    with open(paths['clash'], 'w', encoding='utf-8', newline='\n') as f:
        f.write(build_clash_yaml(nodes) + '\n')
    with open(paths['singbox'], 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(build_singbox_json(nodes), indent=2,
                            ensure_ascii=False) + '\n')
    b64 = base64.b64encode('\n'.join(links).encode('utf-8')).decode('ascii')
    with open(paths['b64'], 'w', encoding='utf-8', newline='\n') as f:
        f.write(b64 + '\n')
    if not quiet:
        print('\n' + '=' * 60)
        print('🎉 节点配置已成功生成到:')
        for k, p in paths.items():
            print('  {:8s} -> {}'.format(k.upper(), p))
        print('=' * 60)
    return paths


def decrypt_profile(resp):
    ep = resp.get('encrypted_profile')
    if not ep:
        raise IpowError('响应里没有 encrypted_profile')
    keys = {k.get('key_id'): k.get('key')
            for k in (resp.get('profile_decryption_keys') or [])}
    key_str = keys.get(ep.get('key_id')) or resp.get('profile_decryption_key')
    if not key_str:
        raise IpowError('响应里没有可用的 profile 解密密钥')

    key_bytes = b64d(key_str)
    nonce_bytes = b64d(ep['nonce'])
    ct_bytes = b64d(ep['ciphertext'])

    if _HAS_CRYPTOGRAPHY:
        plaintext = AESGCM(key_bytes).decrypt(nonce_bytes, ct_bytes, None)
    else:
        plaintext = _pure_aes_gcm_decrypt(key_bytes, nonce_bytes, ct_bytes, b'')

    return json.loads(plaintext.decode('utf-8', errors='replace'))


def node_from_capability(resp):
    profile = decrypt_profile(resp)
    payload = (resp.get('signed_record') or {}).get('payload') or {}
    tls = profile.get('tls') or {}
    reality = tls.get('reality') or {}
    node_id = (resp.get('selected_node_id') or resp.get('node_id')
               or payload.get('node_id'))
    node = {
        'node_id': node_id,
        'country': resp.get('selected_country_name') or payload.get('country'),
        'country_code': (resp.get('selected_country_code')
                         or country_code_from_node_id(node_id)),
        'city': payload.get('city'),
        'server': profile.get('server'),
        'port': profile.get('server_port'),
        'uuid': profile.get('uuid'),
        'sni': tls.get('server_name'),
        'public_key': reality.get('public_key'),
        'short_id': reality.get('short_id'),
        'vless_url': None,
        'region': payload.get('region'),
        'latency_ms': payload.get('latency_ms'),
    }
    for field in ('node_id', 'server', 'port', 'uuid', 'public_key', 'short_id'):
        if not node.get(field):
            raise IpowError('解出的节点缺少字段 {}: {}'.format(field, node))
    node['vless_url'] = build_vless_url(node)
    return node

# ---------------------------------------------------------------------------
# 钱包生成与注册
# ---------------------------------------------------------------------------

def generate_wallet():
    if _HAS_ETH_ACCOUNT:
        account = Account.create()
        return '0x' + account.key.hex(), account.address
    priv = os.urandom(32)
    address = _pure_private_key_to_address(priv)
    return '0x' + priv.hex(), address


def sign_login_message(pk, message):
    if _HAS_ETH_ACCOUNT:
        account = Account.from_key(pk)
        signed = account.sign_message(encode_defunct(text=message))
        return '0x' + bytes(signed.signature).hex()
    return _pure_sign_personal_message(pk, message)


def random_device_id():
    return ''.join(random.choices('0123456789abcdef', k=16))


def random_device_name():
    brands = ['Xiaomi', 'Redmi', 'Huawei', 'Honor', 'Samsung', 'Vivo', 'iQOO', 'Oppo', 'OnePlus']
    return '{} {}'.format(random.choice(brands), random.randint(1000, 9999))


def register_new_wallet(client, verbose=False):
    pk, address = generate_wallet()
    device_id = random_device_id()
    device_name = random_device_name()

    if verbose:
        print(f'  新钱包: {address}')
        print(f'  设备ID: {device_id} ({device_name})')

    ch = client.challenge(address)
    message = ch.get('message') or ch.get('nonce')
    if not message:
        raise IpowError(f'challenge 缺少 message: {ch}')

    signature = sign_login_message(pk, message)

    res = client.verify(address, signature, ch.get('nonce'),
                        device_id, device_name)

    sub = res.get('subscription') or {}
    state = {
        'private_key': pk,
        'address': address.lower(),
        'jwt': res.get('token'),
        'user_id': sub.get('user_id'),
        'subscription_token': sub.get('token'),
        'subscription_url': sub.get('url'),
        'plan_id': sub.get('plan_id'),
        'status': sub.get('status'),
        'provider': sub.get('provider'),
        'expires_at': sub.get('expires_at'),
        'device_id': device_id,
        'device_name': device_name,
    }

    if res.get('is_new_user'):
        if verbose:
            print('  ✅ 新账号已创建')
    else:
        if verbose:
            print('  ⚡ 已有钱包，复用登录')

    return state

# ---------------------------------------------------------------------------
# 从单个钱包拉取节点（并发）
# ---------------------------------------------------------------------------

def _fetch_one(client, token, session_id, sub_token, device_id, item, stop):
    """拉取单个节点，返回 (node_id, status, payload)。

    status: ok / unusable / limited / quota / mismatch / skipped
    """
    node_id, country = item
    # 同批里已经有人撞到限流，剩下的就别再发了，省得白挨 429
    if stop.is_set():
        return node_id, 'skipped', None
    try:
        resp = client.capability(token, session_id, country, sub_token,
                                 device_id, node_id=node_id)
    except RateLimited as exc:
        stop.set()
        return node_id, 'limited', exc.retry_after
    except QuotaExhausted:
        stop.set()
        return node_id, 'quota', None
    except IpowError as exc:
        code = getattr(exc, 'code', None)
        if code in ('p2p_dht_capability_unavailable',
                    'p2p_lite_country_mismatch'):
            return node_id, 'unusable', code
        return node_id, 'unusable', str(exc)[:120]

    got = resp.get('selected_node_id') or resp.get('node_id')
    if got != node_id:
        return node_id, 'mismatch', got
    try:
        return node_id, 'ok', node_from_capability(resp)
    except IpowError as exc:
        return node_id, 'unusable', '解密失败: {}'.format(exc)


def pull_with_wallet(client, state, session_id, device_id, args, batch):
    """并法拉取一批节点。batch 已按剩余配额切好，正常情况下不会触发 429。"""
    nodes = []
    unusable = set()
    ok_ids = []
    limited = None
    stop = threading.Event()
    token = state['jwt']
    sub_token = state.get('subscription_token')

    workers = max(1, min(args.concurrency, len(batch)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_fetch_one, client, token, session_id, sub_token,
                        device_id, item, stop)
            for item in batch
        ]
        for fut in concurrent.futures.as_completed(futures):
            node_id, status, payload = fut.result()
            if status == 'ok':
                nodes.append(payload)
                ok_ids.append(node_id)
                if not args.quiet:
                    print('    ✔ {:<32} {}'.format(node_id, payload['server']))
            elif status == 'limited':
                limited = payload
                print('    ⚠ 触发 {} 限流（Retry-After={}s）'.format(
                    '402' if payload is None else '429', payload))
            elif status == 'quota':
                limited = 0
                print('    ⚠ 触发 402 配额耗尽')
            elif status == 'unusable':
                unusable.add(node_id)
                if args.verbose:
                    print('    ✘ {:<32} {}'.format(node_id, payload))
            elif status == 'mismatch':
                unusable.add(node_id)
                if args.verbose:
                    print('    ✘ {:<32} 未命中 {}'.format(node_id, payload))

    if limited is not None:
        skipped = len(batch) - len(ok_ids) - len(unusable)
        if skipped > 0:
            print('    已中止本批剩余 {} 个请求，剩余节点留给下一个钱包'.format(skipped))
    return nodes, limited, ok_ids, unusable

# ---------------------------------------------------------------------------
# 合并去重
# ---------------------------------------------------------------------------

def merge_nodes(existing, new_nodes):
    by_id = {n['node_id']: n for n in existing}
    added = 0
    for n in new_nodes:
        nid = n.get('node_id')
        if nid and nid not in by_id:
            added += 1
        if nid:
            by_id[nid] = n
    return list(by_id.values()), added


def load_unusable(path, ttl):
    """读不可用节点缓存，过期的丢弃。原版每次重跑都重新探测，白烧配额。"""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    now = time.time()
    raw = data.get('nodes') if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    alive = {k: v for k, v in raw.items()
             if isinstance(v, (int, float)) and now - v < ttl}
    return alive


def save_unusable(path, mapping):
    write_json(path, {'updated_at': time.time(), 'nodes': mapping})


def merge_wallets(path, new_wallets):
    """钱包记录按地址合并，不覆盖历史（原版每次运行都整份盖掉）。"""
    old = []
    try:
        with open(path, encoding='utf-8') as f:
            old = json.load(f) or []
    except Exception:
        old = []
    if isinstance(old, dict):
        old = old.get('wallets') or []
    by_addr = {w.get('address'): w for w in old if isinstance(w, dict)}
    for w in new_wallets:
        by_addr.setdefault(w.get('address'), w)
    return [w for w in by_addr.values() if w.get('address')]

# ---------------------------------------------------------------------------
# 主流程入口
# ---------------------------------------------------------------------------

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

    ap = argparse.ArgumentParser(
        prog='ipow.py',
        description='iPoW.ai 节点提取（并发优化版）：自动注册钱包 → 全网节点 → 按地区重命名')
    ap.add_argument('--concurrency', type=int, default=6,
                    help='并发线程数，默认 %(default)s（实测 6 线程 24 节点约 1.5 秒）')
    ap.add_argument('--interval', type=float, default=0.15,
                    help='全局限最小请求间隔（秒），默认 %(default)s')
    ap.add_argument('--quota-per-wallet', type=int, default=QUOTA_PER_WALLET,
                    help='单钱包请求配额，默认 %(default)s（实测值）')
    ap.add_argument('--wallets', type=int, default=3,
                    help='最多注册多少个钱包，默认 %(default)s（换号会加重风控，别调大）')
    ap.add_argument('--limit', type=int, default=None,
                    help='限制最终有效节点数（默认全部）')
    ap.add_argument('--source-tag', default=DEFAULT_SOURCE_TAG,
                    help='节点名前缀，标明来自哪个 VPN，默认 %(default)s')
    ap.add_argument('--on-429', choices=('wait', 'switch'), default='wait',
                    help='撞到限流时原地等窗口还是换钱包，默认 %(default)s'
                         '（实测限流是出口 IP 级滑动窗口，换号解决不了，还会加重风控）')
    ap.add_argument('--max-wait', type=int, default=600,
                    help='--on-429 wait 时的累计等待上限（秒），默认 %(default)s')
    ap.add_argument('--unusable-ttl', type=int, default=UNUSABLE_TTL,
                    help='不可用节点缓存有效期（秒），默认 %(default)s')
    ap.add_argument('--retries', type=int, default=3,
                    help='网络异常/5xx 重试次数，默认 %(default)s')
    ap.add_argument('--timeout', type=float, default=20.0,
                    help='单请求超时（秒），默认 %(default)s')
    ap.add_argument('--base-url', default=API_BASE,
                    help='接口地址，默认 %(default)s')
    ap.add_argument('-o', '--out-dir', default=BASE_DIR,
                    help='输出目录，默认脚本所在目录 %(default)s')
    ap.add_argument('--android-out', action='store_true',
                    help='安卓 Termux：自动改用 /sdcard/Download 作为输出目录')
    ap.add_argument('--no-cache', action='store_true',
                    help='忽略并清空不可用节点缓存，强制重新探测')
    ap.add_argument('-v', '--verbose', action='store_true', help='详细输出')
    ap.add_argument('--quiet', action='store_true', help='只输出最终结果')
    args = ap.parse_args(argv)

    global _SOURCE_TAG
    _SOURCE_TAG = args.source_tag

    out_dir = os.path.abspath(args.android_out and get_default_download_dir()
                              or args.out_dir)
    try:
        os.makedirs(out_dir, exist_ok=True)
        probe = os.path.join(out_dir, '.perm_test')
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
    except Exception:
        print('⚠️ 输出目录不可写: {}，改用当前工作目录'.format(out_dir))
        if args.android_out:
            print('💡 Termux 请先执行 termux-setup-storage 并在弹窗点「允许」')
        out_dir = os.getcwd()

    if not args.quiet:
        engine = ('内置纯 Python 引擎 (Termux 零依赖)'
                  if not (_HAS_ETH_ACCOUNT and _HAS_CRYPTOGRAPHY)
                  else '硬件加速引擎')
        print('=' * 60)
        print('  iPoW.ai 节点提取（并发优化版）')
        print('=' * 60)
        print('运行环境: {}'.format(engine))
        print('源标识前缀: {} | 并发: {} | 保存目录: {}'.format(
            _SOURCE_TAG, args.concurrency, out_dir))
        print('=' * 60)

    client = IpowClient(base=args.base_url, timeout=args.timeout,
                        verbose=args.verbose, min_interval=args.interval,
                        retries=args.retries)

    unusable_path = os.path.join(out_dir, UNUSABLE_FILE)
    wallets_path = os.path.join(out_dir, WALLETS_FILE)
    cache = {} if args.no_cache else load_unusable(unusable_path, args.unusable_ttl)

    # 节点目录
    try:
        cat_body = client._request('GET', '/p2p-lite/v1/nodes?redacted=1')
    except IpowError:
        cat_body = client._request('GET', '/p2p-lite/v1/nodes')
    entries = cat_body.get('nodes') or cat_body.get('entries') or []
    if not entries:
        print('错误: 服务端未返回任何节点')
        return 1

    excluded = set(cat_body.get('route_failure_exclude_node_ids') or [])
    if excluded and not args.quiet:
        print('服务端要求排除: {}'.format(', '.join(sorted(excluded))))

    candidates = []
    for e in entries:
        nid = e.get('id') or e.get('node_id')
        if not nid or nid in excluded:
            continue
        candidates.append((nid, e.get('country_code') or 'AUTO'))

    cached_skip = [nid for nid, _ in candidates if nid in cache]
    if cached_skip and not args.quiet:
        print('缓存跳过 {} 个已知不可用节点: {}'.format(
            len(cached_skip), ', '.join(cached_skip[:6]) + (' ...' if len(cached_skip) > 6 else '')))
    remaining = [(nid, cc) for nid, cc in candidates if nid not in cache]

    need = args.limit or len(candidates)
    print('目录共 {} 个节点，本次目标 {} 个'.format(len(entries), need))

    all_nodes = []
    seen_ids = set()
    unusable_ids = set()
    wallets_used = []
    ok_requests = 0

    reused = None      # 等窗口后继续复用的钱包 (state, session_id)
    waited = 0

    while remaining and len(seen_ids) < need and len(wallets_used) < args.wallets:
        if reused is None:
            print('\n{}'.format('─' * 50))
            print('钱包 #{} | 待取 {} 个'.format(len(wallets_used) + 1, len(remaining)))

            try:
                state = register_new_wallet(client, verbose=args.verbose)
            except IpowError as exc:
                print('  ❌ 注册失败: {}'.format(exc))
                break

            # 服务端风控：短时间注册账号过多时，新账号直接不发试用订阅
            if state.get('status') != 'active':
                print('  ❌ 服务端没有给这个账号发放可用订阅:')
                print('     status={} plan={} provider={}'.format(
                    state.get('status'), state.get('plan_id'), state.get('provider')))
                print('     这是服务端风控，继续换号只会加重，已停止。')
                print('     处理办法：等一段时间再跑，或者换一个网络出口。')
                break

            wallets_used.append({
                'address': state['address'],
                'user_id': state['user_id'],
                'plan_id': state['plan_id'],
                'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            print('  钱包 {} （用户 {} | 计划 {}）'.format(
                state['address'], state['user_id'], state['plan_id']))

            try:
                session = client.session_start(state['jwt'], state['device_id'],
                                               state['device_name'])
            except IpowError as exc:
                print('  ❌ 开启会话失败: {}，跳过该钱包'.format(exc))
                continue
            session_id = (session.get('session') or {}).get('id')
            if not session_id:
                print('  ❌ sessions/start 未返回 session_id，跳过该钱包')
                continue
            reused = (state, session_id)

        state, session_id = reused
        want = need - len(seen_ids)
        # 预留 3 个余量，抵消不可用节点吃掉的名额
        take = min(args.quota_per_wallet, len(remaining), want + 3)
        batch = remaining[:take]
        print('  本批 {} 个（单钱包配额 {}）'.format(len(batch), args.quota_per_wallet))

        nodes, limited, ok_ids, batch_unusable = pull_with_wallet(
            client, state, session_id, state['device_id'], args, batch)

        for nid in ok_ids:
            seen_ids.add(nid)
        for nid in batch_unusable:
            unusable_ids.add(nid)
            cache[nid] = time.time()
        ok_requests += len(ok_ids) + len(batch_unusable)

        if nodes:
            all_nodes, _ = merge_nodes(all_nodes, nodes)
            print('  ✅ 本批 {} 个（累计 {}/{}）'.format(
                len(nodes), len(all_nodes), need))
            write_json(os.path.join(out_dir, MERGED_NODES_FILE),
                       assign_display_names(list(all_nodes), _SOURCE_TAG))
        else:
            print('  ⚠️ 本批未取到节点')

        if batch_unusable:
            save_unusable(unusable_path, cache)

        remaining = [(nid, cc) for nid, cc in candidates
                     if nid not in seen_ids and nid not in unusable_ids]

        if limited is not None:
            if args.on_429 == 'wait' and limited and waited + limited <= args.max_wait:
                print('  ⏳ 原地等待服务端窗口 {}s（累计 {}s / 上限 {}s），'
                      '用同一个钱包继续'.format(limited, waited, args.max_wait))
                time.sleep(limited + 2)
                waited += limited
            else:
                reused = None
                if remaining:
                    print('  🔄 换钱包继续（剩余 {} 个）'.format(len(remaining)))
                    time.sleep(1)
        else:
            # 本批没撞限流。只有把单钱包配额用满，才需要换钱包
            if len(batch) >= args.quota_per_wallet:
                reused = None
            if remaining and len(seen_ids) < need:
                time.sleep(1)

    if all_nodes:
        all_nodes = assign_display_names(list(all_nodes), _SOURCE_TAG)[:need]

    write_json(wallets_path, merge_wallets(wallets_path, wallets_used))
    if unusable_ids:
        save_unusable(unusable_path, cache)

    if all_nodes:
        write_configs(all_nodes, out_dir, quiet=args.quiet)

    print('\n{}'.format('=' * 60))
    print('【iPoW 节点提取汇总】')
    print('  使用钱包: {} 个 | 消耗配额: {} 次'.format(len(wallets_used), ok_requests))
    print('  有效节点: {} 个 | 已知不可用: {} 个'.format(len(all_nodes), len(unusable_ids)))
    regions = {}
    for n in all_nodes:
        r = n.get('region_cn') or region_label(n)
        regions[r] = regions.get(r, 0) + 1
    if regions:
        print('  覆盖地区: {} 个'.format(len(regions)))
        print('    ' + '，'.join('{}×{}'.format(k, regions[k])
                                 for k in sorted(regions)))
    if all_nodes:
        print('  节点名示例: {}'.format('，'.join(n['display_name'] for n in all_nodes[:4])))
        print('  纯链接文件: {}'.format(os.path.join(out_dir, LINKS_FILE)))
        print('  base64 订阅: {}'.format(os.path.join(out_dir, SUB_B64_FILE)))
    print('=' * 60)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
