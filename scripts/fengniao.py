#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蜂鸟加速器 - 自动注册并拉取节点（优化版，自定义输出路径与文件名）
用法：
    python fengniao_sign_fixed.py
    python fengniao_sign_fixed.py --serial X --delay 1.0 --protocol trojan,vmess
"""
import base64
import hashlib
import json
import os
import sys
import time
import uuid
from urllib.request import Request, urlopen
from urllib.parse import quote

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ---------- 固定配置（按用户要求） ----------
API_BASE = "https://api.go01.top/proxy"
AES_IV = b"A-16-Byte-String"
DEFAULT_DELAY = 1.5

# 输出目录（脚本所在目录，兼容 CI 与本地）
try:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _SCRIPT_DIR = os.getcwd()
OUTPUT_DIR = _SCRIPT_DIR + "/"
# 固定文件名
DATA_JSON = "蜂鸟数据.json"
NODES_TXT = "蜂鸟节点.txt"
TOKEN_CACHE_FILE = ".fengniao_token_cache.json"   # 仍放在输出目录下

# ---------- 工具函数 ----------
def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()

def gen_serial() -> str:
    return base64.b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes[:16]).decode()

def build_decrypt_key(ts_str: str, rid: str, token: str) -> str:
    if int(ts_str) % 2 == 0:
        seq = [("rid", 16), ("rid", 8), ("ts", -3), ("rid", 12), ("rid", 26), ("ts", -2), ("rid", 2), ("rid", 18),
               ("tok", 16), ("tok", 8), ("ts", -3), ("tok", 12), ("tok", 26), ("ts", -2), ("tok", 2), ("tok", 18)]
    else:
        seq = [("rid", 5), ("rid", 11), ("ts", -2), ("rid", 8), ("rid", 27), ("ts", -1), ("rid", 9), ("rid", 21),
               ("tok", 5), ("tok", 11), ("ts", -2), ("tok", 8), ("tok", 27), ("ts", -1), ("tok", 9), ("tok", 21)]
    out = []
    for src, idx in seq:
        s = rid if src == "rid" else (ts_str if src == "ts" else token)
        out.append(s[idx])
    return "".join(out)

def decrypt_node(content_b64: str, ts_str: str, rid: str, token: str):
    key = build_decrypt_key(ts_str, rid, token)
    aes = AES.new(key.encode(), AES.MODE_CBC, AES_IV)
    try:
        pt = unpad(aes.decrypt(base64.b64decode(content_b64)), 16)
        return pt.decode("utf-8", errors="replace"), key
    except Exception:
        return None, key

def build_key(ts_str: str, rid: str, token: str) -> str:
    L = len(ts_str)
    ts2 = ts_str[L - 2]
    if int(ts_str) % 2 == 0:
        k8 = rid[16] + rid[8] + ts_str[L - 3] + rid[12] + rid[26] + ts2 + rid[2] + rid[18]
    else:
        k8 = rid[5] + rid[11] + ts2 + rid[8] + rid[27] + ts_str[L - 1] + rid[9] + rid[21]
    idx = int(ts2)
    k8 += (token[idx] if token else rid[idx])
    return k8

# ---------- 请求签名 ----------
def make_sign(params: dict, token: str, serial: str) -> tuple:
    now = int(time.time() * 1000)
    rid = uuid.uuid4().hex
    p = dict(params)
    p.update({
        "version": "v3.1.1",
        "rankVersion": "10",
        "serialNumber": serial,
        "requestTimestamp": str(now),
        "requestId": rid,
        "clientType": "Android",
        "promoteChannel": "S100",
        "clientModel": "XT2201-2",
    })
    if token:
        p["token"] = token
    ps = "&".join(f"{k}={p[k]}" for k in sorted(p))
    key = build_key(str(now), rid, token)
    sign = md5(ps + key)
    return p, sign

def api_request(path: str, params: dict, token: str = "", serial: str = "") -> dict:
    p, sign = make_sign(params, token, serial)
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in {**p, "sign": sign}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    data = ("".join(parts) + f"--{boundary}--\r\n").encode()
    req = Request(f"{API_BASE}{path}", data=data, method="POST")
    req.add_header("User-Agent", "okhttp-okgo/jeasonlzy")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.8")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urlopen(req, timeout=15) as r:
            return {
                "status": r.status,
                "body": r.read().decode("utf-8", "replace"),
                "requestTimestamp": p["requestTimestamp"],
                "requestId": p["requestId"]
            }
    except Exception as e:
        return {"status": -1, "body": str(e), "requestTimestamp": "", "requestId": ""}

# ---------- 协议转换 ----------
def to_trojan_link(plain: str, name: str = "") -> str:
    parts = plain.split(',')
    if len(parts) < 5:
        return ""
    proto, server, port, password, method = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip()
    if proto.lower() != "trojan":
        return ""
    link = f"trojan://{password}@{server}:{port}?encryption={method}"
    if name:
        link += f"#{quote(name)}"
    return link

def to_vmess_link(plain: str, name: str = "") -> str:
    parts = plain.split(',')
    if len(parts) < 6 or parts[0].lower() != "vmess":
        return ""
    config = {
        "v": "2",
        "ps": name or "蜂鸟",
        "add": parts[1].strip(),
        "port": parts[2].strip(),
        "id": parts[3].strip(),
        "aid": parts[4].strip() if len(parts) > 4 else "0",
        "net": parts[6].strip() if len(parts) > 6 else "tcp",
        "type": parts[7].strip() if len(parts) > 7 else "none",
        "host": parts[8].strip() if len(parts) > 8 else "",
        "path": parts[9].strip() if len(parts) > 9 else "",
        "tls": "tls" if len(parts) > 10 and parts[10].strip().lower() == "tls" else "",
    }
    return "vmess://" + base64.b64encode(json.dumps(config).encode()).decode()

def to_ss_link(plain: str, name: str = "") -> str:
    parts = plain.split(',')
    if len(parts) < 4 or parts[0].lower() != "ss":
        return ""
    method, password, server, port = parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip() if len(parts)>4 else "443"
    userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
    link = f"ss://{userinfo}@{server}:{port}"
    if name:
        link += f"#{quote(name)}"
    return link

def convert_plain_to_links(plain: str, name: str, protocols: list) -> list:
    if not plain:
        return []
    links = []
    proto = plain.split(',')[0].strip().lower()
    if "trojan" in protocols and proto == "trojan":
        l = to_trojan_link(plain, name)
        if l: links.append(l)
    if "vmess" in protocols and proto == "vmess":
        l = to_vmess_link(plain, name)
        if l: links.append(l)
    if "ss" in protocols and proto == "ss":
        l = to_ss_link(plain, name)
        if l: links.append(l)
    return links

# ---------- 主流程 ----------
def fetch_nodes(serial: str, delay: float, protocols: list, cache_token: bool):
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cache_path = os.path.join(OUTPUT_DIR, TOKEN_CACHE_FILE)

    token = ""
    if cache_token and os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                data = json.load(f)
                if data.get("serial") == serial:
                    token = data.get("token", "")
                    print(f"使用缓存的 token (serial={serial[:8]}...)")
        except:
            pass

    if not token:
        print("== server/info ==")
        api_request("/server/info", {}, serial=serial)

        print("== user/auto/login ==")
        r = api_request("/user/auto/login", {}, serial=serial)
        if r["status"] != 200:
            print("登录请求失败:", r["body"])
            return
        try:
            body = json.loads(r["body"])
        except:
            print("响应解析失败:", r["body"][:200])
            return
        if body.get("code") != 0:
            print("登录失败:", body.get("msg", r["body"][:200]))
            return
        if "data" not in body or "token" not in body.get("data", {}):
            print("风控提示: 服务端返回 code:0 但未发放 token，请稍后重试或用 --serial 指定已有账号")
            return
        token = body["data"]["token"]
        print(f"获取新 token: {token[:8]}... (registerGift: {body['data'].get('registerGift')})")

        if cache_token:
            with open(cache_path, "w") as f:
                json.dump({"serial": serial, "token": token}, f)
            print(f"token 已缓存至 {cache_path}")

    print("== user/my/info ==")
    r = api_request("/user/my/info", {}, token=token, serial=serial)
    try:
        info = json.loads(r["body"])["data"]
        print(f"  账号 {info.get('account')}  vipEndTime={info.get('vipEndTime')}  recommenderId={info.get('recommenderId')}")
    except:
        pass

    print("== user/fetch/node/list ==")
    r = api_request("/user/fetch/node/list", {"vipType": "vip"}, token=token, serial=serial)
    if r["status"] != 200:
        print("节点列表请求失败:", r["body"][:200])
        return
    try:
        nodes = json.loads(r["body"])["data"]
    except:
        print("节点列表解析失败:", r["body"][:200])
        return
    print(f"共 {len(nodes)} 个节点，仅处理协议: {protocols}，详情间隔 {delay}s")

    all_links = []
    results = []

    iterator = tqdm(nodes, desc="拉取节点") if HAS_TQDM else nodes
    for idx, node in enumerate(iterator):
        node_name = node.get("name", f"节点{idx+1}")
        plain = None
        for attempt in range(4):
            r = api_request("/user/fetch/node/detail", {"nodeId": node["id"]}, token=token, serial=serial)
            if r["status"] != 200:
                if attempt < 3:
                    time.sleep(delay * (attempt + 1))
                    continue
                else:
                    print(f"  {node_name}: 请求失败 {r['body'][:80]}")
                    break
            try:
                content = json.loads(r["body"])["data"]["content"]
                plain, key = decrypt_node(content, r["requestTimestamp"], r["requestId"], token)
                if plain:
                    break
                else:
                    if attempt < 3:
                        time.sleep(delay * (attempt + 1))
                    else:
                        print(f"  {node_name}: 解密失败 (key={key})")
            except Exception as e:
                if attempt < 3:
                    time.sleep(delay * (attempt + 1))
                else:
                    print(f"  {node_name}: 异常 {e}")
        if plain:
            links = convert_plain_to_links(plain, node_name, protocols)
            all_links.extend(links)
            results.append({"name": node_name, "plain": plain, "links": links})
            if HAS_TQDM:
                iterator.set_postfix({"成功": node_name})
        else:
            results.append({"name": node_name, "plain": None, "links": []})
        if idx < len(nodes) - 1:
            time.sleep(delay)

    # 保存 JSON（蜂鸟数据.json）
    json_path = os.path.join(OUTPUT_DIR, DATA_JSON)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"数据已保存至 {json_path}")

    # 保存节点链接（蜂鸟节点.txt）
    links_path = os.path.join(OUTPUT_DIR, NODES_TXT)
    with open(links_path, "w", encoding="utf-8") as f:
        f.write("\n".join(all_links))
    print(f"共生成 {len(all_links)} 条链接，保存至 {links_path}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="蜂鸟加速器节点拉取（固定输出目录与文件名）")
    parser.add_argument("--serial", help="指定序列号（默认随机生成）")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="节点请求间隔（秒）")
    parser.add_argument("--protocol", default="trojan,vmess,ss", help="需要转换的协议，逗号分隔 (trojan,vmess,ss)")
    parser.add_argument("--no-cache-token", action="store_true", help="禁用 token 缓存")
    args = parser.parse_args()

    serial = args.serial or gen_serial()
    if not args.serial:
        print(f"生成新序列号: {serial}")

    protocols = [p.strip().lower() for p in args.protocol.split(",") if p.strip()]
    fetch_nodes(serial, args.delay, protocols, not args.no_cache_token)

if __name__ == "__main__":
    main()