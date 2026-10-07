#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iPoWVPN 节点一条龙提取（订阅池 + P2P 池 + 可达筛查）
====================================================
链路（2026-10-05 实测有效）:
  注册(EVM 钱包 EIP-191) -> 绑码【必须带 device_id】激活 23 小时
  订阅路径: POST /p2p-lite/v2/vpn/sessions/start(client_type) -> sub_url?session_id= -> 明文 sing-box 配置
  P2P 路径: rotate 后 token -> GET /p2p-lite/v1/nodes(目录) -> POST /p2p-lite/v2/dht/capability 逐节点
            -> AES-256-GCM 解密
  可达筛查: 服务器 TCP(+TLS) 探测（国内直连大多数 GCP IP 在 TCP 层被阻断，能连的进 reachable 清单）

v2 提速改造（2026-10-05）:
  1) 订阅按 client_type 串行“建 session -> 立刻拉配置”；服务端 device_limit=2，
     同设备只保留最近两个 session，批量建完再拉会吃 409 vpn_session_required
  2) 订阅取配置 与 P2P capability 并行；capability 用 --concurrency 并发跑
  3) 每拿到一个 server 立刻丢进探测池（边收边探），收尾只等没探完的，不再串行等两分钟
  4) HTTP 长连接复用（urllib 每请求重新握手，实测平均 1.8s -> 1.0s）
  5) 去掉逐节点 sleep(1.2) 与轮次 sleep(1)
  6) 瞬态失败（连接被重置 / 5xx）自动重试；原版一个 RemoteDisconnected 就整轮崩掉

服务端配额实测（决定上面的取值）:
  capability 成功数累计到 20~24 个开始回 429，窗口是分钟级。原地等待重试不会更快
  （试过：统一等 25s × 若干轮，5m29s 还没跑完）；所以策略改成撞墙两次立即收尾，
  已拿到的照写，剩余节点用 --p2p-wallets 换号续跑。请求起手间隔保持 1.2s
  （实测 33 个请求不触发限流），并发只用来盖单请求 ~2.6s 的延迟，不用来加大吞吐。
  同一沙箱同一账号 A/B：原版 122.7s（且没算可达筛查，用户侧 5m46s）-> v2 61.4s 全流程。

输出:
  out/all_nodes.json        全量结构化（两路合并，含 source/region 标记）
  out/all_uris.txt          全量 URI（vless:// + hysteria2://）
  out/reachable_uris.txt    【主交付】TCP(+TLS) 实测可达的 URI
  out/reachable_nodes.json  可达节点结构化数据 + 每个 server:port 的探测结果
  out/singbox_config.json   最新一份原始订阅配置
  out/clash_proxies.yaml    --emit-configs：Clash Meta 代理组（可直接导入手机客户端）
  out/singbox_proxies.json  --emit-configs：sing-box outbounds 配置
  iPoW.txt（脚本同目录）    给聚合器用的订阅文件，内容等于 reachable_uris.txt（--no-probe 时为全量）

v3 合并（2026-10-07：并入用户收到的 Termux 交流版）:
  1) 内置 Keccak-256 / secp256k1 / AES-256-GCM 纯 Python 实现，零依赖也能跑（--pure-crypto 强制）
  2) hysteria2 分享链接纠偏：server_name 是 IP 时不输出 sni 参数，统一带 insecure=1
  3) 默认 client_types 补上 ios（官方 ios 池里另有 n-*.node.ipow.ai 域名节点）
  4) 新增 --selftest / --emit-configs / --out-dir；订阅拉取全部合并为一次请求（不再逐国查）
  5) 登录慢响应误判修复：重试 + 超时放宽 + 拒登原因可见（不再静默烧新钱包）

依赖:
  pip install eth-account cryptography（推荐装库加速；缺失时自动退回内置纯 Python 实现，Termux 可不装）

用法:
  python ipow.py                     # 全流程（windows+android+ios 订阅 + P2P 全目录 + 可达筛查）
  python ipow.py --no-probe          # 跳过可达筛查（只出全量）
  python ipow.py --skip-p2p          # 只拉订阅路径（5 个请求就出 90+ 节点，最快）
  python ipow.py --rounds 3          # 订阅路径多轮（收集移动池域名变体）
  python ipow.py --p2p-wallets 2     # 配额撞墙后换 2 个新钱包接着跑剩余节点
  python ipow.py --strict            # hysteria2 也要求 TLS 握手成功（默认只按 TCP 判定）
  python ipow.py --pure-crypto       # 强制内置纯 Python 密码学（先 --selftest 校验再放心用）
  python ipow.py --selftest          # 只跑密码学自检后退出（内置向量 + 与库交叉校验）
  python ipow.py --emit-configs      # 额外生成 Clash 代理组 / sing-box outbounds（手机导入用）
  python ipow.py --out-dir /sdcard/Download   # 输出目录；Termux 上直接扔进下载区
  环境变量 IPOW_BASE_URL 可把 API 指向中转地址
"""
import argparse
import base64
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

# eth_account / cryptography 是可选加速项；缺失时自动使用下方内置纯 Python 实现

# GitHub Actions 里 Secret 没配时环境变量是空串，不能当成地址用
BASE = (os.environ.get("IPOW_BASE_URL") or "").strip().rstrip("/") or "https://ipow.ai"
UA = "iPoWVPN/4.3.5 (Android)"
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state")
OUT = os.path.join(ROOT, "out")
LINKS_FILE = os.path.join(ROOT, "iPoW.txt")

# capability 的 client_type 必须与 session 一致，否则 409
P2P_CLIENT_TYPE = "android"

# GCP region slug -> 中文地区名（订阅 tag 与 P2P 目录 id 都是这个格式）
GCP_REGION = {
    "africa-south1": "南非", "asia-east1": "台湾", "asia-east2": "香港", "asia-northeast1": "日本",
    "asia-northeast2": "韩国", "asia-northeast3": "韩国", "asia-south1": "印度", "asia-south2": "印度",
    "asia-southeast1": "新加坡", "asia-southeast2": "印度尼西亚", "australia-southeast1": "澳大利亚",
    "australia-southeast2": "澳大利亚", "europe-central2": "波兰", "europe-north1": "芬兰",
    "europe-southwest1": "西班牙", "europe-west1": "比利时", "europe-west2": "英国",
    "europe-west3": "德国", "europe-west4": "荷兰", "europe-west6": "瑞士", "europe-west8": "意大利",
    "europe-west9": "法国", "europe-west10": "德国", "europe-west12": "意大利",
    "me-west1": "以色列", "northamerica-northeast1": "加拿大", "southamerica-east1": "巴西",
    "southamerica-west1": "智利", "us-central1": "美国", "us-east1": "美国", "us-east4": "美国",
    "us-east5": "美国", "us-south1": "美国", "us-west1": "美国", "us-west2": "美国",
    "us-west3": "美国", "us-west4": "美国",
}

# transient: 连接被重置/超时/5xx —— 换连接重试即可，不是业务错误
_TRANSIENT = (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError,
              ConnectionAbortedError, BrokenPipeError, TimeoutError, socket.timeout, OSError)


def log(msg):
    print(msg, flush=True)


def b64d(s):
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- 纯标准库密码学

# ================================================================
# 内置纯标准库密码学（移植自用户提供的 Termux 版 ipow 脚本）
# Termux/无 pip 环境零依赖可用；装了 eth_account / cryptography 时自动用库加速。
# ================================================================
try:
    from eth_account import Account as _LibAccount
    from eth_account.messages import encode_defunct as _lib_encode_defunct
    _HAVE_ETH = True
except Exception:
    _LibAccount = None
    _lib_encode_defunct = None
    _HAVE_ETH = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _LibAESGCM
    _HAVE_AES = True
except Exception:
    _LibAESGCM = None
    _HAVE_AES = False

_PURE_CRYPTO = False            # --pure-crypto 时置 True，强制走内置实现


def _use_lib_eth():
    return _HAVE_ETH and not _PURE_CRYPTO


def _use_lib_aes():
    return _HAVE_AES and not _PURE_CRYPTO


def crypto_engine_name():
    return "eth:%s aes:%s%s" % ("lib" if _use_lib_eth() else "pure",
                                "lib" if _use_lib_aes() else "pure",
                                " (--pure-crypto)" if _PURE_CRYPTO else "")


def new_wallet():
    """返回 (private_key_hex, address)。"""
    if _use_lib_eth():
        acct = _LibAccount.create()
        return acct.key.hex(), acct.address
    priv = os.urandom(32)
    return priv.hex(), _pure_private_key_to_address(priv)


def sign_message(private_key_hex, message):
    """EIP-191 personal_sign，返回 0x 开头的 65 字节签名（r||s||v）。"""
    if _use_lib_eth():
        acct = _LibAccount.from_key(private_key_hex)
        sig = acct.sign_message(_lib_encode_defunct(text=message))
        return "0x" + sig.signature.hex()
    return _pure_sign_personal_message(private_key_hex, message)


def _keccak_256(data):
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
    for i in range(4):
        val = state[i % 5][i // 5]
        out.extend(val.to_bytes(8, 'little'))
    return bytes(out)


# ---------------------------------------------------------------- secp256k1（纯 Python）
_SECP_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
)


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2:
        return None
    if x1 == x2:
        m = (3 * x1 * x1 * pow(2 * y1, _SECP_P - 2, _SECP_P)) % _SECP_P
    else:
        m = ((y2 - y1) * pow(x2 - x1, _SECP_P - 2, _SECP_P)) % _SECP_P
    x3 = (m * m - x1 - x2) % _SECP_P
    y3 = (m * (x1 - x3) - y1) % _SECP_P
    return (x3, y3)


def _point_mul(p, k):
    res = None
    curr = p
    while k:
        if k & 1:
            res = _point_add(res, curr)
        curr = _point_add(curr, curr)
        k >>= 1
    return res


def _pure_private_key_to_address(priv_bytes):
    if isinstance(priv_bytes, str):
        raw_hex = priv_bytes[2:] if priv_bytes.startswith('0x') else priv_bytes
        priv_bytes = bytes.fromhex(raw_hex)
    k = int.from_bytes(priv_bytes, 'big')
    if not (1 <= k < _SECP_N):
        raise ValueError("Invalid private key")
    pt = _point_mul(_SECP_G, k)
    pub_uncompressed = pt[0].to_bytes(32, 'big') + pt[1].to_bytes(32, 'big')
    addr_hash = _keccak_256(pub_uncompressed)
    return '0x' + addr_hash[12:].hex()


def _pure_sign_personal_message(priv_bytes, message):
    if isinstance(priv_bytes, str):
        raw_hex = priv_bytes[2:] if priv_bytes.startswith('0x') else priv_bytes
        priv_bytes = bytes.fromhex(raw_hex)
    msg_bytes = message.encode('utf-8')
    prefix = ("\x19Ethereum Signed Message:\n%d" % len(msg_bytes)).encode('utf-8')
    h = _keccak_256(prefix + msg_bytes)
    e = int.from_bytes(h, 'big')
    d = int.from_bytes(priv_bytes, 'big')

    k_seed = _keccak_256(priv_bytes + h)
    k = (int.from_bytes(k_seed, 'big') % (_SECP_N - 1)) + 1

    pt = _point_mul(_SECP_G, k)
    r = pt[0] % _SECP_N
    s = (pow(k, _SECP_N - 2, _SECP_N) * (e + r * d)) % _SECP_N
    recid = pt[1] & 1
    if s > _SECP_N // 2:
        s = _SECP_N - s
        recid ^= 1
    v = 27 + recid

    return '0x' + r.to_bytes(32, 'big').hex() + s.to_bytes(32, 'big').hex() + bytes([v]).hex()# ---------------------------------------------------------------- AES-256-GCM（纯 Python，解密用）
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
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
_RCON = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _aes_key_expansion(key):
    nk = len(key) // 4
    nr = nk + 6
    w = list(key)
    i = nk
    while i < 4 * (nr + 1):
        temp = w[(i - 1) * 4: i * 4]
        if i % nk == 0:
            temp = [_AES_SBOX[temp[1]], _AES_SBOX[temp[2]], _AES_SBOX[temp[3]], _AES_SBOX[temp[0]]]
            temp[0] ^= _RCON[i // nk]
        elif nk > 6 and i % nk == 4:
            temp = [_AES_SBOX[b] for b in temp]
        for j in range(4):
            w.append(w[(i - nk) * 4 + j] ^ temp[j])
        i += 1
    return bytes(w), nr


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)


def _aes_encrypt_block(block, w, nr):
    state = list(block)
    for i in range(16):
        state[i] ^= w[i]
    for round_idx in range(1, nr):
        state = [_AES_SBOX[b] for b in state]
        s0, s4, s8, s12 = state[0], state[4], state[8], state[12]
        s1, s5, s9, s13 = state[5], state[9], state[13], state[1]
        s2, s6, s10, s14 = state[10], state[14], state[2], state[6]
        s3, s7, s11, s15 = state[15], state[3], state[7], state[11]
        for c, (r0, r1, r2, r3) in enumerate(
                [(s0, s1, s2, s3), (s4, s5, s6, s7), (s8, s9, s10, s11), (s12, s13, s14, s15)]):
            t = r0 ^ r1 ^ r2 ^ r3
            state[c * 4] = r0 ^ t ^ _xtime(r0 ^ r1)
            state[c * 4 + 1] = r1 ^ t ^ _xtime(r1 ^ r2)
            state[c * 4 + 2] = r2 ^ t ^ _xtime(r2 ^ r3)
            state[c * 4 + 3] = r3 ^ t ^ _xtime(r3 ^ r0)
        round_key = w[round_idx * 16:(round_idx + 1) * 16]
        for i in range(16):
            state[i] ^= round_key[i]
    state = [_AES_SBOX[b] for b in state]
    state = [
        state[0], state[5], state[10], state[15],
        state[4], state[9], state[14], state[3],
        state[8], state[13], state[2], state[7],
        state[12], state[1], state[6], state[11]
    ]
    round_key = w[nr * 16:(nr + 1) * 16]
    for i in range(16):
        state[i] ^= round_key[i]
    return bytes(state)


def _ghash(h_bytes, data):
    h = int.from_bytes(h_bytes, 'big')
    r = 0xE1000000000000000000000000000000
    y = 0
    for i in range(0, len(data), 16):
        x = int.from_bytes(data[i:i + 16], 'big')
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


def _pure_aes_gcm_decrypt(key, nonce, ct_and_tag, aad=b''):
    if len(nonce) != 12:
        raise ValueError('Nonce 长度必须为 12 字节')
    if len(ct_and_tag) < 16:
        raise ValueError('密文长度不足(缺少 Tag)')
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
        block = ct[i:i + 16]
        pt.extend(bytes(a ^ b for a, b in zip(block, ks[:len(block)])))
        counter += 1
    return bytes(pt)


# ---------------------------------------------------------------- 自检 / 小工具

def is_ipv4(addr):
    if not addr or not isinstance(addr, str):
        return False
    parts = addr.strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255):
            return False
    return True


_SELFTEST_VEC = {
    "keccak_empty": "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
    "keccak_abc": "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
    "address_of_one": "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf",
    "sign_message": "iPoW selftest message",
    "sign_of_one": ("0xf4520b3331528fd9277030c8655632ac35eec4b4d6f1d058eed1a421b1baf6c3"
                    "37a756a1a1f54d7004741cb3880437c647f66fd8b42763cbe1fb834e94018a1b1c"),
    "aes_key": "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
    "aes_nonce": "000102030405060708090a0b",
    "aes_ct": ("2e52b94ce584a768a026f4e6919a1d01e5a2e247845b29195b138af77160e43d"
               "5c0e62806a81cda8c234420a"),
    "aes_pt": "iPoW aes-gcm selftest vector",
}


def run_selftest():
    """内置固定向量（零依赖可跑）+ 有库时随机样例交叉验证。返回进程退出码。"""
    fails = []

    def check(name, cond, detail=""):
        print("[%s] %s%s" % ("OK" if cond else "FAIL", name,
                             (" " + str(detail)) if (detail and not cond) else ""))
        if not cond:
            fails.append(name)

    v = _SELFTEST_VEC
    check("keccak('') 固定向量", _keccak_256(b"").hex() == v["keccak_empty"])
    check("keccak('abc') 固定向量", _keccak_256(b"abc").hex() == v["keccak_abc"])
    pk1 = bytes.fromhex("00" * 31 + "01")
    check("priv=1 地址派生", _pure_private_key_to_address(pk1).lower() == v["address_of_one"])
    sig1 = _pure_sign_personal_message(pk1, v["sign_message"])
    check("EIP-191 签名固定向量", sig1 == v["sign_of_one"], sig1)
    pt = _pure_aes_gcm_decrypt(bytes.fromhex(v["aes_key"]), bytes.fromhex(v["aes_nonce"]),
                               bytes.fromhex(v["aes_ct"]), b"")
    check("AES-256-GCM 固定向量", pt.decode() == v["aes_pt"])
    try:
        bad = bytearray(bytes.fromhex(v["aes_ct"]))
        bad[-1] ^= 1
        _pure_aes_gcm_decrypt(bytes.fromhex(v["aes_key"]), bytes.fromhex(v["aes_nonce"]),
                              bytes(bad), b"")
        check("GCM 篡改检测", False, "改了 tag 竟然没报错")
    except ValueError:
        check("GCM 篡改检测", True)

    if _HAVE_ETH:
        ok = True
        for _ in range(3):
            pk = os.urandom(32)
            try:
                ok = ok and _pure_private_key_to_address(pk).lower() == _LibAccount.from_key(pk).address.lower()
            except Exception:
                ok = False
        check("随机 3 键：纯地址派生 vs eth_account", ok)
        try:
            rec = _LibAccount.recover_message(_lib_encode_defunct(text=v["sign_message"]),
                                              signature=sig1)
            check("纯签名经 eth_account 回收一致", rec.lower() == v["address_of_one"])
        except Exception as exc:
            check("纯签名经 eth_account 回收一致", False, str(exc)[:80])
    try:
        import eth_utils as _eu
        data = os.urandom(64)
        check("随机 keccak vs eth_utils", _keccak_256(data).hex() == _eu.keccak(data).hex())
    except Exception:
        pass
    if _HAVE_AES:
        ok = True
        for _ in range(3):
            k, n, p = os.urandom(32), os.urandom(12), os.urandom(123)
            try:
                ok = ok and _pure_aes_gcm_decrypt(k, n, _LibAESGCM(k).encrypt(n, p, None), b"") == p
            except Exception:
                ok = False
        check("随机 3 组：纯 GCM 解密 vs cryptography", ok)

    print("engine: %s" % crypto_engine_name())
    print("结果: %s" % ("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), ", ".join(fails))))
    return 1 if fails else 0


# ---------------------------------------------------------------- HTTP 层

class Http:
    """长连接 HTTPS 客户端：每 (线程, host) 一条连接复用，带限速、瞬态重试、429 退避。

    urllib 每个请求重新握手，实测单次请求平均 1.8s，复用连接后 1.0s。
    """

    def __init__(self, base=BASE, timeout=25, min_interval=0.9, retries=3,
                 max_wait=120, quiet=False):
        self.timeout = timeout
        self.retries = max(int(retries), 0)
        self.max_wait = max_wait
        self.quiet = quiet
        self.limiter = _RateLimiter(min_interval)
        self._local = threading.local()
        self._ctx = ssl.create_default_context()
        self.stats = {}
        self._slock = threading.Lock()
        self.base = self._split(base)
        host = self.base["host"]
        if not host or any(c.isspace() for c in host):
            raise ValueError("API 地址无法解析: %r（检查 --base-url 或环境变量 IPOW_BASE_URL）" % base)

    @staticmethod
    def _split(url):
        if "://" not in url:
            url = "https://" + url
        u = urllib.parse.urlsplit(url)
        return {"host": u.hostname, "port": u.port or (443 if u.scheme == "https" else 80),
                "secure": u.scheme == "https", "prefix": u.path.rstrip("/"),
                "netloc": u.netloc, "key": u.scheme + "://" + u.netloc}

    def _resolve(self, url):
        """把 path / 绝对 URL 归一成 (目标主机, 请求行路径)。"""
        if "://" in url:
            tgt = self._split(url)
            u = urllib.parse.urlsplit(url)
            path = (u.path or "/") + (("?" + u.query) if u.query else "")
            return tgt, path
        return self.base, self.base["prefix"] + url

    def _conn(self, tgt):
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns
        conn = conns.get(tgt["key"])
        if conn is None:
            cls = http.client.HTTPSConnection if tgt["secure"] else http.client.HTTPConnection
            kwargs = {"timeout": self.timeout}
            if tgt["secure"]:
                kwargs["context"] = self._ctx
            conn = cls(tgt["host"], tgt["port"], **kwargs)
            conns[tgt["key"]] = conn
        return conn

    def _drop(self, tgt):
        conns = getattr(self._local, "conns", {})
        conn = conns.pop(tgt["key"], None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def request(self, method, url, token=None, body=None, retries=None):
        """返回 (status, parsed)。429 原样返回，由调用方决定退避。

        retries 可以按调用点覆盖：订阅配置那一发带着全量节点，值得多试几次。
        """
        tgt, path = self._resolve(url)
        headers = {"User-Agent": UA, "Accept": "application/json", "Host": tgt["netloc"]}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        limit = self.retries if retries is None else max(int(retries), 0)
        last = None
        for attempt in range(limit + 1):
            self.limiter.wait()
            t = time.time()
            try:
                conn = self._conn(tgt)
                conn.request(method, path, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status, hdrs = resp.status, resp.headers
            except _TRANSIENT as exc:
                self._drop(tgt)
                last = "%s: %s" % (type(exc).__name__, exc)
                if attempt >= limit:
                    raise TransientError("%s %s 连接失败 %s" % (method, path, last))
                time.sleep(min(0.6 * (2 ** attempt), 6.0))
                continue
            except Exception as exc:                       # 未知异常同样按瞬态处理
                self._drop(tgt)
                last = "%s: %s" % (type(exc).__name__, exc)
                if attempt >= limit:
                    raise TransientError("%s %s 失败 %s" % (method, path, last))
                time.sleep(min(0.6 * (2 ** attempt), 6.0))
                continue
            with self._slock:
                key = "%s %s" % (method, (path.split("?")[0] or path)[:48])
                self.stats.setdefault(key, []).append(time.time() - t)
            txt = raw.decode("utf-8", "replace")
            try:
                parsed = json.loads(txt)
            except Exception:
                parsed = txt
            if status >= 500:
                if attempt >= limit:
                    raise TransientError("%s %s 服务端 %s %s" % (method, path, status, txt[:120]))
                time.sleep(min(0.5 * (2 ** attempt), 4.0))
                continue
            if status == 429:
                retry = hdrs.get("Retry-After") or hdrs.get("retry-after")
                try:
                    retry = int(float(retry))
                except (TypeError, ValueError):
                    retry = 20
                return status, {"error": {"code": "rate_limited",
                                          "retry_after": min(retry, self.max_wait)}}
            return status, parsed
        raise TransientError("%s %s 失败 %s" % (method, path, last))





class _RateLimiter:
    """全局最小间隔限速，避免并发把服务端打成 429。"""

    def __init__(self, min_interval):
        self.min_interval = max(float(min_interval), 0.0)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if delay > 0:
            time.sleep(delay)


class TransientError(RuntimeError):
    pass


# ---------------------------------------------------------------- 账号

def login(http, rec, tries=3):
    """challenge -> EIP-191 签名 -> verify。

    服务端偶发把单个请求拖到几十秒，个别时候 verify 会吃 challenge_expired 或瞬时 5xx；
    整个流程最多重试 tries 次（每次重新拿 challenge），最终仍失败时带原因抛错。
    """
    last = None
    for attempt in range(tries):
        if attempt:
            time.sleep(1.0)
        st, ch = http.request("POST", "/v1/auth/wallet/challenge", body={"address": rec["address"]})
        if st != 200 or not isinstance(ch, dict) or not ch.get("message"):
            last = "challenge %s %s" % (st, str(ch)[:100])
            continue
        sig = sign_message(rec["private_key"], ch["message"])
        st, vr = http.request("POST", "/v1/auth/wallet/verify",
                              body={"address": rec["address"], "signature": sig,
                                    "nonce": ch.get("nonce")})
        if st == 200 and isinstance(vr, dict) and vr.get("token"):
            return vr["token"], vr.get("subscription") or {}
        last = "verify %s %s" % (st, str(vr)[:120])
    raise RuntimeError("登录失败: %s" % last)


def register_new(http, bind_code):
    priv, addr = new_wallet()
    device_id = os.urandom(8).hex()
    st, ch = http.request("POST", "/v1/auth/wallet/challenge", body={"address": addr})
    sig = sign_message(priv, ch["message"])
    st, vr = http.request("POST", "/v1/auth/wallet/verify",
                          body={"address": addr, "signature": sig, "nonce": ch.get("nonce")})
    if st != 200 or not isinstance(vr, dict) or not vr.get("token"):
        raise RuntimeError("注册失败: http %s %s" % (st, str(vr)[:140]))
    token = vr["token"]
    rec = {"address": addr, "private_key": priv, "device_id": device_id,
           "user_id": (vr.get("user") or {}).get("id"),
           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if bind_code:
        st, bd = http.request("POST", "/v1/me/referral/bind", token=token,
                              body={"referral_code": bind_code, "device_id": device_id})
        rec["bind"] = {"code": bind_code, "status": st,
                       "usage_bonus_granted": bd.get("usage_bonus_granted") if isinstance(bd, dict) else None}
    path = os.path.join(STATE, "extract_accounts.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return rec, token, vr.get("subscription") or {}


def account_expired(sub):
    exp = sub.get("expires_at", "")
    try:
        return datetime.fromisoformat(exp.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except Exception:
        return False


def load_usable_account(http, bind_code, try_count=6, workers=4):
    """最近的账号并发试登录，命中第一个 active 就用；都不行就注册新号。"""
    path = os.path.join(STATE, "extract_accounts.jsonl")
    recs = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    recs = list(reversed(recs))[:try_count]
    hit = {}
    if recs:
        with ThreadPoolExecutor(max_workers=min(workers, len(recs))) as ex:
            futs = {ex.submit(login, http, r): r for r in recs}
            # 服务端偶发把单个请求拖到 50s+；给足余量，别把还在跑的登录当失败
            done, pending = wait(futs, timeout=max(150.0, http.timeout * 6))
            for f in pending:
                log("[account] 登录超时未返回，跳过该账号")
                f.cancel()
            for f in done:
                rec = futs[f]
                try:
                    token, sub = f.result()
                except Exception as exc:
                    log("[account] %s 登录失败: %s" % (rec["address"], str(exc)[:100]))
                    continue
                if sub.get("status") == "active" and not account_expired(sub):
                    prev = hit.get("rank")
                    rank = recs.index(rec)
                    if prev is None or rank < prev:
                        hit = {"rank": rank, "rec": rec, "token": token, "sub": sub}
                        log("[account] reuse %s (active until %s)" % (rec["address"], sub.get("expires_at", "")))
                else:
                    log("[account] %s status=%s expires=%s，跳过"
                        % (rec["address"], sub.get("status"), sub.get("expires_at", "")))
    if hit:
        return hit["rec"], hit["token"], hit["sub"]
    log("[account] registering new account...")
    rec, token, sub = register_new(http, bind_code)
    if sub.get("status") != "active":
        # 注册响应偶尔（出口被批或激活延迟）直接给 inactive，紧接着复查登录一次通常就 active
        try:
            token, sub = login(http, rec)
        except Exception as exc:
            log("[account] 注册后复查登录失败: %s" % str(exc)[:120])
    log("[account] new: %s" % rec["address"])
    return rec, token, sub


# ---------------------------------------------------------------- 可达筛查

class ProbePool:
    """按需探测 (host, port)，结果缓存，节点边收集边探。

    TCP 是底线；vless-reality 再补一次 TLS 握手（复用同一条连接，不重复握 TCP）。
    hysteria2 走 QUIC，TCP 层无法证明可用（实测发 QUIC Version Negotiation
    触发包这些节点全部沉默），所以只按 TCP 判定，strict 模式下才要求 TLS。
    """

    def __init__(self, workers=24, tcp_timeout=4.0, tls_timeout=6.0,
                 sni="www.cloudflare.com", strict=False):
        self.workers = workers
        self.tcp_timeout = tcp_timeout
        self.tls_timeout = tls_timeout
        self.sni = sni
        self.strict = strict
        self._ex = ThreadPoolExecutor(max_workers=workers)
        self._lock = threading.Lock()
        self._results = {}
        self._futs = []

    def submit(self, host, port, need_tls):
        key = (host, port)
        with self._lock:
            if key in self._results or any(f[0] == key for f in self._futs):
                return
            fut = self._ex.submit(self._probe, host, port, need_tls)
            self._futs.append((key, fut))

    def _probe(self, host, port, need_tls):
        res = {"tcp": False, "tls": None, "ms": None}
        t = time.time()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            raw = socket.create_connection((host, port), timeout=self.tcp_timeout)
        except Exception:
            res["ms"] = round(time.time() - t, 2)
            return res
        res["tcp"] = True
        if need_tls or self.strict:
            try:
                raw.settimeout(self.tls_timeout)
                with ctx.wrap_socket(raw, server_hostname=self.sni):
                    res["tls"] = True
            except Exception:
                res["tls"] = False
            finally:
                try:
                    raw.close()
                except Exception:
                    pass
        else:
            try:
                raw.close()
            except Exception:
                pass
        res["ms"] = round(time.time() - t, 2)
        return res

    def wait(self, timeout=None):
        with self._lock:
            pending = [(k, f) for k, f in self._futs if not f.done()]
            done_keys = [k for k, f in self._futs if f.done()]
        for k in done_keys:
            self._harvest(k)
        if pending:
            log("[probe] waiting %d pending checks..." % len(pending))
            wait([f for _k, f in pending], timeout=timeout)
            for k, f in pending:
                self._harvest(k)
        self._ex.shutdown(wait=False)

    def _harvest(self, key):
        with self._lock:
            if key in self._results:
                return True
            pair = next(((k, f) for k, f in self._futs if k == key), None)
            if pair is None or not pair[1].done():
                return False
            try:
                self._results[key] = pair[1].result()
            except Exception as exc:
                self._results[key] = {"tcp": False, "tls": None, "ms": None, "err": str(exc)[:80]}
            return True

    def reachable(self, host, port, need_tls):
        with self._lock:
            res = self._results.get((host, port))
        if res is None:
            return False
        if not res["tcp"]:
            return False
        if self.strict or need_tls:
            return bool(res["tls"])
        return True

    def snapshot(self):
        with self._lock:
            return dict(self._results)


# ---------------------------------------------------------------- 采集

class Collector:
    """按 (type, server, uuid/password) 去重收集节点，新节点立刻送进探测池。"""

    def __init__(self, probe=None):
        self._lock = threading.Lock()
        self.nodes = {}
        self.probe = probe

    def add(self, node):
        port = node.get("port")
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        key = (node["type"], node["server"], node.get("uuid") or node.get("password"))
        with self._lock:
            fresh = key not in self.nodes
            if fresh:
                self.nodes[key] = node
        if fresh and self.probe is not None and node["server"] and port:
            self.probe.submit(node["server"], port, node["type"] != "hysteria2")
        return fresh

    def values(self):
        with self._lock:
            return list(self.nodes.values())


def node_uri(n, name=None):
    tag = name or n.get("tag") or ""
    if n["type"] == "vless":
        q = ("encryption=none&security=reality&sni=%s&fp=%s&pbk=%s&sid=%s&type=tcp"
             % (n["sni"], n.get("fp") or "chrome", n["pbk"], n["sid"]))
        if n.get("flow"):
            q += "&flow=" + n["flow"]
        return "vless://%s@%s:%s?%s#%s" % (n["uuid"], n["server"], n["port"], q,
                                           urllib.parse.quote(str(tag), safe=""))
    if n["type"] == "hysteria2":
        # SNI 必须是域名：官方订阅里 hy2 的 server_name 直接是物理 IP，
        # 客户端拿 IP 当 SNI 会握手异常 —— 是 IP 就不带 sni 参数、统一 insecure=1
        q = "insecure=1"
        sni = str(n.get("sni") or "")
        if sni and not is_ipv4(sni):
            q += "&sni=" + urllib.parse.quote(sni, safe="")
        return "hysteria2://%s@%s:%s/?%s#%s" % (
            n["password"], n["server"], n["port"], q,
            urllib.parse.quote(str(tag), safe=""))
    return None


# ---------------------------------------------------------------- 订阅路径

def start_session(http, token, device_id, country, client_type):
    st, ss = http.request("POST", "/p2p-lite/v2/vpn/sessions/start", token=token,
                          body={"client_type": client_type, "device_id": device_id,
                                "device_name": "Device", "client_version": "4.3.5",
                                "preferred_country": country})
    if st != 200 or not isinstance(ss, dict):
        return None, ss
    return (ss.get("session") or {}).get("id"), ss


def get_sub_url(http, token):
    st, rot = http.request("POST", "/v1/me/subscription/token/rotate", token=token, body={})
    if st != 200 or not isinstance(rot, dict):
        return None, None
    url = (rot.get("subscription") or {}).get("url") or rot.get("url")
    if not url:
        return None, None
    return url, urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]


def parse_sub_nodes(conf):
    nodes = []
    for o in conf.get("outbounds", []):
        t = o.get("type")
        if t == "vless":
            tls = o.get("tls") or {}
            reality = tls.get("reality") or {}
            nodes.append({
                "type": "vless", "source": "sub", "tag": o.get("tag"), "server": o.get("server"),
                "port": o.get("server_port"), "uuid": o.get("uuid"), "flow": o.get("flow") or "",
                "sni": tls.get("server_name"), "fp": (tls.get("utls") or {}).get("fingerprint"),
                "pbk": reality.get("public_key"), "sid": reality.get("short_id")})
        elif t == "hysteria2":
            tls = o.get("tls") or {}
            nodes.append({
                "type": "hysteria2", "source": "sub", "tag": o.get("tag"), "server": o.get("server"),
                "port": o.get("server_port"), "password": o.get("password"),
                "sni": tls.get("server_name")})
    return nodes


def sub_pull(http, token, rec, sub_url, country, client_type, rounds, collector, store,
             quiet=False):
    """订阅路径：必须“建完 session 立刻拉配置”。

    服务端 device_limit=2：同一设备只有最近两个 session 有效，新建会顶掉最老的。
    所以每个 client_type（每一轮）都现场建 session 并马上拉取，不能先批量建再拉。
    """
    for i in range(rounds):
        sid, ss = start_session(http, token, rec["device_id"], country, client_type)
        if not sid:
            log("[sub:%s r%d] session failed: %s" % (client_type, i + 1, str(ss)[:140]))
            continue
        st, conf = http.request("GET", sub_url + "?session_id=" + sid, retries=6)
        if st != 200 or not isinstance(conf, dict) or "outbounds" not in conf:
            log("[sub:%s r%d] config fetch failed(%s): %s"
                % (client_type, i + 1, st, str(conf)[:140]))
            continue
        store["last_conf"] = conf
        nodes = parse_sub_nodes(conf)
        new = sum(1 for n in nodes if collector.add(n))
        log("[sub:%s r%d] %d nodes (%d new) | total %d"
            % (client_type, i + 1, len(nodes), new, len(collector.nodes)))
    return len(collector.nodes)


# ---------------------------------------------------------------- P2P 路径

class Throttle:
    """请求节奏与配额闸门。

    实测（2026-10-05）：capability 成功数累计到 20~24 个就开始回 429，窗口是分钟级，
    一次运行里反复重试不会提前拿到配额（试过：4 次统一等 25s 换来 5 分半）。
    所以这里的策略是：撞墙两次就判定本账号本轮配额用完，立即收尾，
    剩下的节点要么等窗口，要么用 --p2p-wallets 换号续跑。
    """

    def __init__(self, limiter, base_interval=1.2, max_interval=6.0,
                 max_429=2, gate_wait=12.0):
        self.limiter = limiter
        self.base_interval = base_interval
        self.max_interval = max_interval
        self.max_429 = max_429
        self.gate_wait = gate_wait
        self._lock = threading.Lock()
        self._until = 0.0
        self.hits = 0
        self.abort = threading.Event()

    def on_429(self, retry_after):
        """记录一次 429。返回 True 表示还可以继续，False 表示该收手。"""
        with self._lock:
            self.hits += 1
            self.limiter.min_interval = min(max(self.limiter.min_interval * 3.0, 2.0), self.max_interval)
            if self.hits >= self.max_429:
                self.abort.set()
                log("[throttle] 连续 %d 次 429，判定配额已用完，停止本轮 capability" % self.hits)
                return False
            wait_s = min(float(retry_after or 10), self.gate_wait)
            self._until = max(self._until, time.monotonic() + wait_s)
            log("[throttle] 429 -> 请求间隔 %.2fs，统一等 %.0fs" % (self.limiter.min_interval, wait_s))
            return True

    def gate(self):
        while True:
            with self._lock:
                left = self._until - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 2.0))


def decrypt_profile(resp):
    ep = resp.get("encrypted_profile")
    if not ep:
        raise ValueError("no encrypted_profile")
    keys = {k.get("key_id"): k.get("key") for k in (resp.get("profile_decryption_keys") or [])}
    key_str = keys.get(ep.get("key_id")) or resp.get("profile_decryption_key")
    if not key_str:
        raise ValueError("no decryption key")
    key_b, nonce_b, ct_b = b64d(key_str), b64d(ep["nonce"]), b64d(ep["ciphertext"])
    if _use_lib_aes():
        pt = _LibAESGCM(key_b).decrypt(nonce_b, ct_b, None)
    else:
        pt = _pure_aes_gcm_decrypt(key_b, nonce_b, ct_b, b"")
    return json.loads(pt.decode("utf-8", "replace"))


def node_from_capability(resp):
    """把 capability 响应转成节点；顺带从 signed_record 里取延迟/城市等元信息。"""
    prof = decrypt_profile(resp)
    tls = prof.get("tls") or {}
    node = {"type": "vless", "source": "p2p", "tag": prof.get("tag"), "server": prof.get("server"),
            "port": prof.get("server_port"), "uuid": prof.get("uuid"), "flow": prof.get("flow") or "",
            "sni": tls.get("server_name"), "fp": (tls.get("utls") or {}).get("fingerprint"),
            "pbk": (tls.get("reality") or {}).get("public_key"),
            "sid": (tls.get("reality") or {}).get("short_id")}
    payload = (resp.get("signed_record") or {}).get("payload") or {}
    if isinstance(payload, dict):
        if payload.get("latency_ms") is not None:
            node["latency_ms"] = payload.get("latency_ms")
        if payload.get("region"):
            node["region_slug"] = payload.get("region")
        if payload.get("city"):
            node["city"] = payload.get("city")
    return node


def capability_one(http, token, sid, sub_token, device_id, nid, cc, throttle):
    body = {"session_id": sid, "client_type": P2P_CLIENT_TYPE, "device_id": device_id,
            "country": cc or "AUTO", "capabilities": ["vless-reality:no-flow"],
            "subscription_token": sub_token, "allow_country_switch": True, "node_id": nid}
    throttle.gate()
    st, cap = http.request("POST", "/p2p-lite/v2/dht/capability", token=token, body=body)
    if st == 200 and isinstance(cap, dict):
        return cap, None
    code = (cap.get("error") or {}).get("code", "") if isinstance(cap, dict) else str(cap)[:60]
    last = "%s %s" % (st, code)
    if st == 429:
        retry = (cap.get("error") or {}).get("retry_after", 10) if isinstance(cap, dict) else 10
        if throttle.on_429(retry):
            throttle.gate()                        # 第一次撞墙：等一小会再试这一个
            st, cap = http.request("POST", "/p2p-lite/v2/dht/capability", token=token, body=body)
            if isinstance(cap, dict) and st == 200:
                return cap, None
        return None, "429 quota"
    if code == "p2p_dht_capability_unavailable":
        return None, last                          # 节点下线，重试只是白烧配额
    if st in (503, 500):                           # 服务端临时故障，补一次
        throttle.gate()
        st, cap = http.request("POST", "/p2p-lite/v2/dht/capability", token=token, body=body)
        if isinstance(cap, dict) and st == 200:
            return cap, None
        return None, "%s %s" % (st, (cap.get("error") or {}).get("code", "")
                                if isinstance(cap, dict) else "")
    return None, last


def p2p_catalog(http, cat_info):
    """拉 P2P 目录，顺带把地区信息存进 cat_info 供命名用。"""
    st, cat = http.request("GET", "/p2p-lite/v1/nodes?redacted=1")
    if st != 200 or not isinstance(cat, dict):
        log("[p2p] catalog failed: %s" % str(cat)[:140])
        return []
    entries = cat.get("nodes") or cat.get("entries") or []
    items = []
    for e in entries:
        nid = e.get("id") or e.get("node_id")
        if not nid:
            continue
        items.append((nid, e.get("country_code") or ""))
        cat_info[nid] = {"country_code": (e.get("country_code") or "").upper(),
                         "country": e.get("country") or "", "city": e.get("city") or "",
                         "protocols": e.get("protocols") or []}
    return items


def scrape_nodes(http, token, rec, sid, sub_token, items, collector, throttle,
                 workers=4, quiet=False, deadline=None, label="p2p"):
    """对给定节点列表逐个 capability。返回 (成功, 跳过, 未处理)。撞墙或超时立刻收手。"""
    ok = skip = 0
    leftover = []
    lock = threading.Lock()

    def one(idx_nid):
        nonlocal ok, skip
        idx, (nid, cc) = idx_nid
        if throttle.abort.is_set() or (deadline is not None and time.monotonic() > deadline):
            with lock:
                leftover.append((nid, cc))
            return
        cap, err = capability_one(http, token, sid, sub_token, rec["device_id"], nid, cc, throttle)
        with lock:
            if cap is None:
                skip += 1
                log("[%s %2d/%d] %-30s -> %s" % (label, idx + 1, len(items), nid, err))
                return
            try:
                node = node_from_capability(cap)
            except Exception as exc:
                skip += 1
                log("[%s %2d/%d] %-30s decrypt fail: %s" % (label, idx + 1, len(items), nid, exc))
                return
            fresh = collector.add(node)
            ok += 1
            if not quiet or fresh:
                log("[%s %2d/%d] %-30s -> %s" % (label, idx + 1, len(items), nid, node["server"]))

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
        list(ex.map(one, enumerate(items)))
    return ok, skip, leftover


def run_p2p_phase(http, args, token, rec, sid, sub_token, collector, cat_info, throttle,
                  workers=4, todo=None, label="p2p"):
    """一轮 P2P：目录（或指定清单）-> capability。返回 (成功, 跳过, 未处理)。"""
    if todo is None:
        items = p2p_catalog(http, cat_info)
        if not items:
            return 0, 0, []
        log("[%s] catalog: %d nodes, concurrency %d" % (label, len(items), workers))
    else:
        items = list(todo)
        log("[%s] 续跑剩余 %d 个节点" % (label, len(items)))
    deadline = time.monotonic() + args.p2p_deadline
    ok, skip, leftover = scrape_nodes(http, token, rec, sid, sub_token, items, collector,
                                      throttle, workers=workers, quiet=args.quiet,
                                      deadline=deadline, label=label)
    log("[%s] ok=%d skip=%d 未处理=%d (429 %d 次)" % (label, ok, skip, len(leftover), throttle.hits))
    return ok, skip, leftover


# ---------------------------------------------------------------- 地区名

def _cc_table():
    """优先复用仓库里的 naming.py（同一份国家代码表），单独拷走脚本时退回代码。"""
    try:
        import naming
        return dict(getattr(naming, "REGION_CN", {}))
    except Exception:
        return {}


CC_CN = _cc_table()
_TAG_SUFFIXES = ("-hy2", "-h2", "-reality", "-noflow")


def _slug_of(tag):
    base = tag
    for suf in _TAG_SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    if base.startswith("gcp-"):
        base = base[4:]
    parts = base.split("-")
    for i in range(len(parts), 1, -1):
        cand = "-".join(parts[:i])
        if cand in GCP_REGION:
            return cand
    return None


def region_of(node, cat_info):
    tag = str(node.get("tag") or "")
    base = tag
    for suf in _TAG_SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    info = cat_info.get(base) or cat_info.get(tag) or {}
    cc = (info.get("country_code") or "").upper()
    if cc and cc in CC_CN:
        return CC_CN[cc]
    slug = _slug_of(tag)
    if slug:
        return GCP_REGION[slug]
    return info.get("country") or ""


def apply_names(nodes, cat_info):
    """tag 是 gcp-asia-east1-1 这种内部 id，换成中文地区名，订阅里才可读。
    同名节点自动加序号（地区、地区2、地区3…），hy2 后缀保留在序号之后。"""
    counts = {}
    for n in nodes:
        region = region_of(n, cat_info)
        if not region:
            n["region"] = ""
            n["name"] = n.get("tag") or ""
            continue
        n["region"] = region
        suffix = "·H2" if n["type"] == "hysteria2" else ""
        c = counts.get(region, 0) + 1
        counts[region] = c
        n["name"] = (region if c == 1 else "%s%d" % (region, c)) + suffix
    # 保持输出稳定：按地区、序号排序
    nodes.sort(key=lambda n: (n.get("region") or "", n.get("name") or ""))


# ---------------------------------------------------------------- 配置生成（--emit-configs）

def _nport(n):
    try:
        return int(n.get("port") or 0)
    except (TypeError, ValueError):
        return 0


def _stag(n):
    return str(n.get("name") or n.get("tag") or n.get("server") or "node").replace("'", "")


def build_clash_yaml(nodes):
    lines = ["# iPoWVPN nodes (Clash Meta / Mihomo)", "proxies:"]
    for n in nodes:
        name = _stag(n)
        lines.append("  - name: '%s'" % name)
        if n["type"] == "hysteria2":
            lines.append("    type: hysteria2")
            lines.append("    server: %s" % n.get("server"))
            lines.append("    port: %s" % _nport(n))
            lines.append("    password: %s" % (n.get("password") or ""))
            sni = str(n.get("sni") or "")
            if sni and not is_ipv4(sni):
                lines.append("    sni: %s" % sni)
            lines.append("    skip-cert-verify: true")
            lines.append("    udp: true")
            continue
        lines.append("    type: vless")
        lines.append("    server: %s" % n.get("server"))
        lines.append("    port: %s" % _nport(n))
        lines.append("    uuid: %s" % (n.get("uuid") or ""))
        lines.append("    network: tcp")
        if n.get("flow"):
            lines.append("    flow: %s" % n["flow"])
        lines.append("    tls: true")
        lines.append("    udp: true")
        lines.append("    servername: %s" % (n.get("sni") or "www.cloudflare.com"))
        lines.append("    client-fingerprint: %s" % (n.get("fp") or "chrome"))
        lines.append("    reality-opts:")
        lines.append("      public-key: '%s'" % (n.get("pbk") or ""))
        lines.append("      short-id: '%s'" % (n.get("sid") or ""))
    return "\n".join(lines) + "\n"


def build_singbox_json(nodes):
    outbounds = []
    for n in nodes:
        tag = _stag(n)
        if n["type"] == "hysteria2":
            tls = {"enabled": True, "insecure": True}
            sni = str(n.get("sni") or "")
            if sni and not is_ipv4(sni):
                tls["server_name"] = sni
            outbounds.append({"type": "hysteria2", "tag": tag, "server": n.get("server"),
                              "server_port": _nport(n), "password": n.get("password") or "",
                              "tls": tls})
            continue
        ob = {"type": "vless", "tag": tag, "server": n.get("server"), "server_port": _nport(n),
              "uuid": n.get("uuid") or "", "packet_encoding": "xudp",
              "tls": {"enabled": True, "server_name": n.get("sni") or "www.cloudflare.com",
                      "utls": {"enabled": True, "fingerprint": n.get("fp") or "chrome"},
                      "reality": {"enabled": True, "public_key": n.get("pbk") or "",
                                  "short_id": n.get("sid") or ""}}}
        if n.get("flow"):
            ob["flow"] = n["flow"]
        outbounds.append(ob)
    return {"outbounds": outbounds}


def emit_configs(nodes, probe, out_dir):
    """写 clash_proxies.yaml / singbox_proxies.json；可达节点排前面。"""
    ordered = list(nodes)
    if probe is not None:
        reach_ids = {id(n) for n in ordered
                     if probe.reachable(n["server"], _nport(n), n["type"] != "hysteria2")}
        ordered = ([n for n in ordered if id(n) in reach_ids]
                   + [n for n in ordered if id(n) not in reach_ids])
    path_yaml = os.path.join(out_dir, "clash_proxies.yaml")
    path_sb = os.path.join(out_dir, "singbox_proxies.json")
    with open(path_yaml, "w", encoding="utf-8") as f:
        f.write(build_clash_yaml(ordered))
    with open(path_sb, "w", encoding="utf-8") as f:
        json.dump(build_singbox_json(ordered), f, ensure_ascii=False, indent=1)
    log("[out] clash_proxies.yaml / singbox_proxies.json <- %d nodes（可达优先）" % len(ordered))


# ---------------------------------------------------------------- 主流程

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="iPoWVPN one-shot node extractor")
    ap.add_argument("--rounds", type=int, default=1, help="订阅路径每个 client_type 的轮数")
    ap.add_argument("--country", default="AUTO")
    ap.add_argument("--bind-code", default="849a64")
    ap.add_argument("--base-url", default=BASE,
                    help="API 地址，默认 https://ipow.ai；出口被风控时指向 cf_relay.mjs 中转")
    ap.add_argument("--client-types", default="windows,android,ios")
    ap.add_argument("--no-probe", action="store_true", help="跳过可达筛查")
    ap.add_argument("--skip-p2p", action="store_true", help="跳过 P2P capability 路径")
    ap.add_argument("--concurrency", type=int, default=4, help="capability 并发度")
    ap.add_argument("--p2p-deadline", type=float, default=90.0,
                    help="单轮 capability 的时间上限，超了先把已拿到的写出去")
    ap.add_argument("--p2p-wallets", type=int, default=0,
                    help="配额撞墙后最多再注册几个新钱包续跑剩余节点（默认 0 = 不换号）")
    ap.add_argument("--probe-workers", type=int, default=24, help="可达探测并发度")
    ap.add_argument("--tcp-timeout", type=float, default=4.0)
    ap.add_argument("--tls-timeout", type=float, default=6.0)
    ap.add_argument("--probe-deadline", type=float, default=240.0, help="收尾等待探测结果的上限秒数")
    ap.add_argument("--timeout", type=float, default=25.0, help="单次 HTTP 请求超时")
    ap.add_argument("--min-interval", type=float, default=1.2,
                    help="全局请求起手最小间隔（实测 1.2s 跑满 33 个请求不触发限流）")
    ap.add_argument("--retries", type=int, default=3, help="瞬态失败重试次数")
    ap.add_argument("--max-wait", type=float, default=25.0, help="撞 429 时单次统一等待上限")
    ap.add_argument("--strict", action="store_true", help="hysteria2 也要求 TLS 握手成功")
    ap.add_argument("--no-stats", action="store_true", help="不打印接口耗时统计")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--links-scope", choices=["reachable", "all"], default="reachable",
                    help="iPoW.txt 的内容范围：reachable=只写探得通的（默认），all=全量节点")
    ap.add_argument("--pure-crypto", action="store_true",
                    help="强制使用内置纯 Python 密码学实现（零依赖路径；默认有库就用库）")
    ap.add_argument("--selftest", action="store_true", help="只做密码学自检后退出")
    ap.add_argument("--emit-configs", action="store_true",
                    help="额外生成 clash_proxies.yaml / singbox_proxies.json（手机导入用）")
    ap.add_argument("--out-dir", default=None,
                    help="输出目录（默认 脚本目录/out；Termux 上可指向 /sdcard/Download）")
    return ap.parse_args(argv)


def print_stats(http, t0, no_stats=False):
    if no_stats:
        return
    total = 0.0
    rows = []
    for key, vals in http.stats.items():
        total += sum(vals)
        rows.append((sum(vals), key, len(vals), max(vals)))
    rows.sort(reverse=True)
    print("\n==== 接口耗时 ====")
    for s, key, n, mx in rows[:10]:
        print("%-52s n=%-3d sum=%6.1fs avg=%5.2fs max=%5.2fs" % (key[:52], n, s, s / n, mx))
    print("接口累计 %.1fs | 墙钟 %.1fs | 平均并发度 %.1f"
          % (total, time.monotonic() - t0, total / max(time.monotonic() - t0, 0.001)))


def main(argv=None):
    args = parse_args(argv)
    global _PURE_CRYPTO
    _PURE_CRYPTO = bool(args.pure_crypto)
    if args.selftest:
        sys.exit(run_selftest())
    t0 = time.monotonic()
    out_dir = os.path.abspath(args.out_dir) if args.out_dir else OUT
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    log("[crypto] engine=%s" % crypto_engine_name())

    base = (args.base_url or "").strip() or BASE
    if base != "https://ipow.ai":
        log("[base] %s" % base)
    http = Http(base, timeout=args.timeout, min_interval=args.min_interval,
                retries=args.retries, max_wait=args.max_wait, quiet=args.quiet)
    probe = None if args.no_probe else ProbePool(
        workers=args.probe_workers, tcp_timeout=args.tcp_timeout,
        tls_timeout=args.tls_timeout, strict=args.strict)
    collector = Collector(probe)
    store = {}
    cat_info = {}

    rec, token, sub = load_usable_account(http, args.bind_code)
    log("[account] status=%s plan=%s until=%s" % (
        sub.get("status"), sub.get("plan_id"), sub.get("expires_at")))
    if sub.get("status") != "active":
        log("[account] 订阅不是 active：出口 IP 被风控时新注册的号会直接是 inactive/phase1，"
            "capability 会回 402 subscription_inactive。换出口 IP 再跑（IPOW_BASE_URL 可指向中转）。")
    client_types = [c.strip() for c in args.client_types.split(",") if c.strip()]

    sub_url, sub_token = get_sub_url(http, token)
    if not sub_url:
        log("[sub] rotate 失败，订阅与 P2P 都缺 subscription_token，直接收工")
        client_types = []

    throttle = Throttle(http.limiter, base_interval=args.min_interval)

    def run_sub(ct):
        try:
            sub_pull(http, token, rec, sub_url, args.country, ct, args.rounds, collector, store,
                     quiet=args.quiet)
        except Exception as exc:
            log("[sub:%s] 阶段异常: %s: %s" % (ct, type(exc).__name__, exc))

    def run_p2p():
        # P2P 单独现场建 session：前面的订阅 session 大概率已被顶掉（device_limit=2）
        sid, ss = start_session(http, token, rec["device_id"], args.country, P2P_CLIENT_TYPE)
        if not sid:
            log("[p2p] 建 %s session 失败: %s" % (P2P_CLIENT_TYPE, str(ss)[:140]))
            return
        try:
            _ok, _skip, todo = run_p2p_phase(http, args, token, rec, sid, sub_token,
                                             collector, cat_info, throttle,
                                             workers=max(args.concurrency, 1))
            # 配额是账号级、分钟级的：本号撞墙后换号续跑，比原地等窗口快得多
            extra = 0
            while todo and extra < args.p2p_wallets:
                extra += 1
                log("[p2p] 换第 %d 个新钱包续跑剩余 %d 个节点" % (extra, len(todo)))
                try:
                    rec2, token2, sub2 = register_new(http, args.bind_code)
                    if sub2.get("status") != "active":
                        try:
                            token2, sub2 = login(http, rec2)   # 注册响应可能慢一拍，复查一次
                        except Exception as exc:
                            log("[p2p] 新号复查登录失败: %s" % str(exc)[:120])
                    if sub2.get("status") != "active":
                        log("[p2p] 新号 %s 状态 %s（出口被风控时新号直接 inactive），放弃换号"
                            % (rec2["address"], sub2.get("status")))
                        break
                    sid2, _ss = start_session(http, token2, rec2["device_id"], args.country,
                                              P2P_CLIENT_TYPE)
                    if not sid2:
                        log("[p2p] 新号建 session 失败，放弃换号")
                        break
                    _url2, token2_sub = get_sub_url(http, token2)
                    if not token2_sub:
                        log("[p2p] 新号 rotate 失败，放弃换号")
                        break
                    throttle2 = Throttle(http.limiter, base_interval=args.min_interval,
                                         max_429=1)
                    run_p2p_phase(http, args, token2, rec2, sid2, token2_sub, collector,
                                  cat_info, throttle2, workers=max(args.concurrency, 1),
                                  todo=todo, label="p2p+w%d" % extra)
                    todo = []
                except Exception as exc:
                    log("[p2p] 换号失败: %s: %s" % (type(exc).__name__, exc))
                    break
        except Exception as exc:
            log("[p2p] 阶段异常: %s: %s" % (type(exc).__name__, exc))

    # 订阅阶段严格串行：device_limit=2，各 client_type 的 session 互相顶，
    # 每个类型“建完立刻拉”才拿得到配置；P2P 在订阅全部完成后另建 session 再跑
    for ct in client_types:
        run_sub(ct)
    if not args.skip_p2p and sub_token:
        run_p2p()

    nodes = collector.values()
    if not nodes:
        log("no nodes collected")
        print_stats(http, t0, args.no_stats)
        sys.exit(2)
    nodes.sort(key=lambda n: (str(n["server"]), n["type"]))
    apply_names(nodes, cat_info)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if probe is not None:
        servers = sorted({(n["server"], int(n["port"])) for n in nodes if n["server"] and n["port"]})
        log("[probe] %d 个 server:port 已提交，等待剩余结果（上限 %.0fs）..."
            % (len(servers), args.probe_deadline))
        probe.wait(timeout=args.probe_deadline)
        snap = probe.snapshot()
        log("[probe] 完成 %d/%d | TCP 通 %d | TLS 通 %d"
            % (len(snap), len(servers), sum(1 for r in snap.values() if r["tcp"]),
               sum(1 for r in snap.values() if r["tls"])))

    with open(os.path.join(out_dir, "all_nodes.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": ts, "account": rec["address"], "node_count": len(nodes),
                   "nodes": nodes}, f, ensure_ascii=False, indent=1)
    uris = [u for u in (node_uri(n, n.get("name")) for n in nodes) if u]
    with open(os.path.join(out_dir, "all_uris.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(uris) + "\n")
    if store.get("last_conf"):
        with open(os.path.join(out_dir, "singbox_config.json"), "w", encoding="utf-8") as f:
            json.dump(store["last_conf"], f, ensure_ascii=False, indent=1)
    print("\nDONE: %d nodes | all_nodes.json, all_uris.txt (%.1fs)" % (len(nodes), time.monotonic() - t0))

    deliver = uris
    if probe is not None:
        r_nodes = [n for n in nodes if probe.reachable(n["server"], int(n["port"]),
                                                     n["type"] != "hysteria2")]
        r_uris = [u for u in (node_uri(n, n.get("name")) for n in r_nodes) if u]
        deliver = r_uris
        header = ["# iPoWVPN reachable nodes (TCP+TLS verified) @ %s" % ts,
                  "# servers: %d/%d reachable" % (
                      len({n["server"] for n in r_nodes}), len({n["server"] for n in nodes})),
                  "# nodes: %d/%d" % (len(r_nodes), len(nodes))]
        with open(os.path.join(out_dir, "reachable_uris.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(header + r_uris) + "\n")
        with open(os.path.join(out_dir, "reachable_nodes.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_at": ts,
                       "probe": {"%s:%d" % k: v for k, v in sorted(probe.snapshot().items())},
                       "node_count": len(r_nodes), "nodes": r_nodes}, f, ensure_ascii=False, indent=1)

    if args.links_scope == "all":
        # 全量：可达的排前面，不可达的跟在后面（不插注释）
        reachable_uris = []
        unreachable_uris = []
        if probe is not None:
            for n in nodes:
                u = node_uri(n, n.get("name"))
                if not u:
                    continue
                if probe.reachable(n["server"], int(n["port"]), n["type"] != "hysteria2"):
                    reachable_uris.append(u)
                else:
                    unreachable_uris.append(u)
            deliver = reachable_uris + unreachable_uris
            if reachable_uris:
                print("[probe] reachable %d nodes -> reachable_uris.txt" % len(reachable_uris))
        else:
            deliver = uris
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(deliver) + "\n")
    print("[out] %s <- %d 条 (scope=%s)" % (os.path.relpath(LINKS_FILE, ROOT), len(deliver), args.links_scope))
    if args.emit_configs:
        emit_configs(nodes, probe, out_dir)
    print_stats(http, t0, args.no_stats)
    print("\nfiles in %s:" % os.path.relpath(out_dir, ROOT))
    for fn in sorted(os.listdir(out_dir)):
        print("  ", fn)


if __name__ == "__main__":
    main()
