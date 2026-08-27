#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import base64
import uuid
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from typing import Optional, Dict, List
from collections import Counter


AES_KEY = "888f75l9u1b89a6c5l8r60g33df4f70d"
AES_IV = "5k6b7e869a4c1c88"
OSS_URL = "https://caiyunh.oss-cn-shanghai.aliyuncs.com/20166.log"
APP_VERSION = "1.0.2"
APP_TYPE = "iOS"
USER_AGENT = "NewFlyingBird/1 CFNetwork/1408.0.4 Darwin/22.5.0"
FIXED_AUTHTOKEN = "675f0c68ef8187f0fc80af73582dfd54"


def aes_decrypt(encrypted_data: bytes, key: str = AES_KEY, iv: str = AES_IV) -> str:
    key_bytes = key.encode('utf-8')
    iv_bytes = iv.encode('utf-8')
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
    decrypted = cipher.decrypt(encrypted_data)
    try:
        decrypted = unpad(decrypted, AES.block_size)
    except ValueError:
        pass
    return decrypted.decode('utf-8')


def decrypt_response(text: str) -> Dict:
    text = text.strip()
    encrypted_bytes = base64.b64decode(text)
    decrypted_text = aes_decrypt(encrypted_bytes)
    return json.loads(decrypted_text)


def get_api_server_url() -> Optional[str]:
    try:
        response = requests.get(OSS_URL, timeout=30)
        if response.status_code == 200:
            data = response.json()
            if "log" in data:
                return data["log"].strip()
        return None
    except:
        return None


def login(api_base: str) -> Optional[str]:
    url = f"{api_base}/api/appLogin"
    device_id = str(uuid.uuid4()).upper()
    
    host = api_base.replace("http://", "").replace("https://", "").split("/")[0]
    
    headers = {
        "Host": host,
        "Content-Type": "application/json; charset=utf-8",
        "Connection": "keep-alive",
        "app": APP_TYPE,
        "Accept": "*/*",
        "version": APP_VERSION,
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": USER_AGENT
    }
    
    data = {"device_id": device_id}
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        if response.status_code == 200:
            decrypted = decrypt_response(response.text)
            if "data" in decrypted and "data" in decrypted["data"]:
                auth_info = decrypted["data"]["data"]
                return auth_info.get("token", "")
        return None
    except Exception as e:
        return None


def make_request(api_base: str, endpoint: str, token: str) -> Optional[Dict]:
    url = f"{api_base}{endpoint}"
    host = api_base.replace("http://", "").replace("https://", "").split("/")[0]
    
    headers = {
        "Host": host,
        "Accept": "*/*",
        "version": APP_VERSION,
        "authtoken": FIXED_AUTHTOKEN,
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "app": APP_TYPE,
        "token": token,
        "User-Agent": USER_AGENT,
        "Connection": "keep-alive",
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": "0"
    }
    
    try:
        response = requests.post(url, headers=headers, timeout=30)
        if response.status_code != 200:
            return None
        return decrypt_response(response.text)
    except:
        return None


def extract_node_links(data: Dict) -> List[str]:
    if not data:
        return []
    all_nodes = []
    
    # code=1 表示成功，code=0 也可能表示成功
    if "code" in data and data["code"] not in [0, 1]:
        return []
    
    # 直接从 data 中提取
    if "data" in data and isinstance(data["data"], list):
        for item in data["data"]:
            if isinstance(item, dict) and "node" in item:
                node_list = item["node"]
                if isinstance(node_list, list):
                    for node in node_list:
                        if isinstance(node, str):
                            all_nodes.append(node)
    
    return all_nodes


def get_node_type(node: str) -> str:
    if node.startswith("vless://"):
        return "VLESS"
    elif node.startswith("trojan://"):
        return "TROJAN"
    elif node.startswith("ss://"):
        return "SS"
    elif node.startswith("vmess://"):
        return "VMESS"
    else:
        return "UNKNOWN"


def main():
    print("=" * 60)
    print("正在获取节点信息...")
    print("=" * 60)
    
    api_base = get_api_server_url()
    if not api_base:
        print("[!] 获取API服务器地址失败")
        return
    
    print("[*] 正在登录获取Token...")
    token = login(api_base)
    if not token:
        print("[!] 登录失败")
        return
    
    print(f"[+] 登录成功")
    print(f"    Token: {token[:30]}...")
    
    print("[*] 获取节点列表...")
    node_data = make_request(api_base, "/api/nodeList", token)
    
    if not node_data:
        print("[!] 获取节点列表失败")
        return
    
    nodes = extract_node_links(node_data)
    if not nodes:
        print("[!] 未获取到任何节点")
        return
    
    node_types = Counter()
    for node in nodes:
        node_types[get_node_type(node)] += 1
    
    print(f"\n[+] 共获取 {len(nodes)} 个节点")
    print("[*] 节点类型统计:")
    for node_type, count in sorted(node_types.items()):
        print(f"    - {node_type}: {count} 个")
    
    output_file = "菜鸟.txt"
    with open(output_file, "w", encoding="utf-8") as f:
        for node in nodes:
            f.write(node + "\n")
    
    print(f"\n[+] 节点链接已保存到: {output_file}")
    print("=" * 60)


if __name__ == "__main__":
    main()