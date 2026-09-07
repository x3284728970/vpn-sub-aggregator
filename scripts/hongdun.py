#!/usr/bin/env python3
"""
红盾（Red Shield）自动注册 & 获取节点 - Python 优化版
"""

import hashlib
import json
import random
import string
import time
import requests
import base64
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ==================== 配置区 ====================
CONFIG = {
    "salt": "NjNXNzA4SXcyOXBsazRCQ0g1MW4=",  # base64 编码的盐值
    "host": "http://207.148.33.174",
    "ua": "pt=IOS,version=1.0.3,verId=10,system=16.2,bundleId=com.red.shield,deviceId={{DEVICE}},lang=zh-Hans-US,brand=Apple,model=iPhone13-4,net=4G;",
    "bundle_id": "com.red.shield",
    "channel": "10000",
    "platform": "2",
    "ver": "3",
    "lang": "zh-CN",
    "request_timeout": 15,
    "retry_times": 3,
    "retry_delay": 1.0,
    "max_workers": 5,
}
# ================================================

# 全局锁
_lock = Lock()


def md5(text: str) -> str:
    """MD5 哈希"""
    return hashlib.md5(str(text).encode('utf-8')).hexdigest()


def generate_device_id() -> str:
    """生成 UUID 格式的设备 ID"""
    template = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx"
    result = []
    for ch in template:
        if ch == 'x':
            result.append(hex(random.randint(0, 15))[2:])
        elif ch == 'y':
            result.append(hex(random.randint(0, 15) & 0x3 | 0x8)[2:])
        else:
            result.append(ch)
    return ''.join(result).upper()


def sign(params: dict, ts: str) -> str:
    """
    签名算法：
    1. 按 key 字典序排序
    2. 拼接成 {key}{value} 格式
    3. 末尾追加 SALT + 时间戳
    4. MD5 哈希
    """
    s = ""
    for k in sorted(params.keys()):
        s += f"{{{k}}}{{{params[k]}}}"
    s += CONFIG["salt"] + ts
    return md5(s)


def build_request(path: str, params: dict, token: str = None, uid: str = None) -> dict:
    """构建请求参数"""
    ts = str(int(time.time()))
    all_params = dict(params)
    if token:
        all_params["token"] = token
    if uid:
        all_params["uid"] = uid

    sign_val = sign(all_params, ts)

    headers = {
        "Accept-Encoding": "gzip, deflate",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "207.148.33.174",
        "User-Agent": CONFIG["ua"].replace("{{DEVICE}}", CONFIG.get("device_id", "")),
        "Accept-Language": "zh-Hans-US;q=1, en-GB;q=0.9, en-US;q=0.8, zh-Hant-US;q=0.7",
        "SIGN": sign_val,
        "TIMESTAMP": ts,
    }

    body_parts = []
    for k, v in all_params.items():
        if v is not None:
            body_parts.append(f"{k}={urllib.parse.quote(str(v))}")
    body = "&".join(body_parts)

    return {
        "url": CONFIG["host"] + path,
        "method": "POST",
        "headers": headers,
        "body": body,
    }


def http_request(req: dict, timeout: int = None) -> dict:
    """发送 HTTP 请求并解析 JSON"""
    timeout = timeout or CONFIG["request_timeout"]
    last_error = None
    for attempt in range(CONFIG["retry_times"]):
        try:
            resp = requests.post(
                req["url"],
                data=req["body"],
                headers=req["headers"],
                timeout=timeout,
                verify=False,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = e
            if attempt < CONFIG["retry_times"] - 1:
                time.sleep(CONFIG["retry_delay"] * (attempt + 1))
    print(f"[红盾] 请求失败: {req['url']}, 错误: {last_error}")
    return {}


def login(device_id: str) -> tuple:
    """设备 ID 登录/注册，获取 token + uid"""
    params = {
        "bundleId": CONFIG["bundle_id"],
        "channel": CONFIG["channel"],
        "deviceId": device_id,
        "lang": CONFIG["lang"],
        "platform": CONFIG["platform"],
        "ver": CONFIG["ver"],
        "tt": "U",
        "code": "##X-4m6Goo4zzPi1hF##",
        "chCode": "tt09",
    }
    req = build_request("/api/user.loginByDeviceId", params)
    resp = http_request(req)

    if resp.get("response_status", {}).get("code") != 0:
        raise RuntimeError(f"登录失败: {resp}")

    data = resp.get("response_data", {})
    token = data.get("token", "")
    uid = data.get("userinfo", {}).get("uid", "")
    if not token or not uid:
        raise RuntimeError(f"未获取到 token 或 uid: {resp}")

    return token, uid


def get_nodes(token: str, uid: str) -> list:
    """获取节点列表"""
    params = {
        "bundleId": CONFIG["bundle_id"],
        "channel": CONFIG["channel"],
        "deviceId": CONFIG.get("device_id", ""),
        "lang": CONFIG["lang"],
        "platform": CONFIG["platform"],
        "ver": CONFIG["ver"],
    }
    req = build_request("/api/node.getNodeList", params, token, uid)
    resp = http_request(req)

    if resp.get("response_status", {}).get("code") != 0:
        raise RuntimeError(f"获取节点失败: {resp}")

    nodes = resp.get("response_data", [])
    if not isinstance(nodes, list) or len(nodes) == 0:
        raise RuntimeError("节点列表为空或格式错误")

    return nodes


def build_ss_link(node: dict) -> str:
    """构建 ss:// 链接"""
    name = node.get("name", f"{node.get('country', '')}-{node.get('city', '')}")
    method = node.get("method", "aes-256-cfb")
    pwd = node.get("password", "")
    ip = node.get("ip", "")
    port = node.get("port", 0)

    if not all([method, pwd, ip, port]):
        return ""

    userinfo = f"{method}:{pwd}"
    base64_userinfo = base64.b64encode(userinfo.encode("utf-8")).decode("utf-8")
    return f"ss://{base64_userinfo}@{ip}:{port}#{urllib.parse.quote(name)}"


def fetch_nodes(device_id: str) -> list:
    """完整流程：登录 + 获取节点"""
    token, uid = login(device_id)
    nodes = get_nodes(token, uid)
    return nodes


def main():
    print("=" * 60)
    print("红盾（Red Shield）节点获取工具 - Python 优化版")
    print("=" * 60)

    # 生成设备 ID
    device_id = generate_device_id()
    CONFIG["device_id"] = device_id
    print(f"设备 ID: {device_id}")

    try:
        # 获取节点
        nodes = fetch_nodes(device_id)
        print(f"✅ 获取到 {len(nodes)} 个节点")

        # 生成 ss:// 链接
        ss_links = []
        for node in nodes:
            link = build_ss_link(node)
            if link:
                ss_links.append(link)

        print(f"✅ 生成 {len(ss_links)} 条 ss:// 链接")

        # 输出链接
        print("\n" + "=" * 60)
        print("所有节点链接：")
        print("=" * 60)
        for link in ss_links:
            print(link)
        print("=" * 60)

        # 保存到文件
        output_file = "hongdun_nodes.txt"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(ss_links) + "\n")
        print(f"\n✅ 已保存到: {output_file}")

    except Exception as e:
        print(f"\n❌ 错误: {e}")


if __name__ == "__main__":
    main()
