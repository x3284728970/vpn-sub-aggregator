#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迅狐（FoxLink VPN，原「银狐」）节点提取 —— 合并优化版

合并了两个版本的长处：
- 旧版（TF__.py）：iOS 注册参数、vless 链接拼装、gold 节点全零 uuid 兜底
- 新版（探测脚本）：android 注册（带 app_signing_cert_hash）、v3 接口、多域名轮换、429 退避

实测（2026-10-08）：android 注册 201；v3/v2 proxy_config 均 200 且节点 schema 相同
（都带 region_code 与 tls_mode）。42 节点 = 21 普通 + 21 黄金；黄金节点凭据为空，
沿用全零 uuid 兜底、名字带 -Gold 后缀（naming.py 的 KEEP_TAGS 会保留该后缀）。

产出：xunhu.txt（当前目录），每行一条节点链接。
"""

import json
import time
import uuid
import warnings
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass
warnings.filterwarnings("ignore")

HOSTS = [
    "https://foxlink.top",
    "https://foxlink.online",
    "https://foxlink.work",
    "https://foxlink.autos",
    "https://fl1.me",
    "https://foxlink.vip",
]

# Android 客户端签名证书哈希（新 App 注册必填，实测 201）
SHA256_SIG = "f3a0e39e22368ad5832c9cddc428af47bcd14c7eb3d231561e4351411ee0c46f"

ANDROID_UA = "okhttp/4.12.0"
IOS_UA = "foxlink/2 CFNetwork/1408.0.4 Darwin/22.5.0"

GOLD_FALLBACK_UUID = "00000000-0000-0000-0000-000000000000"


def android_register_body() -> Dict:
    return {
        "nickname": None,
        "platform": "android",
        "device_identifier": "android-" + uuid.uuid4().hex[:16],
        "app_instance_id": str(uuid.uuid4()),
        "previous_device_identifier": None,
        "app_signing_cert_hash": SHA256_SIG,
        "device_type": "phone",
        "device_name": "Pixel 8",
        "device_model": "Pixel 8",
        "device_brand": "Google",
        "os_name": "Android",
        "os_version": "14",
        "app_version": "2.1.7",
        "app_build": "29",
        "app_channel": "openinstall",
        "invite_code": None,
        "promotion_channel_id": None,
        "install_referrer_token": None,
    }


def ios_register_body() -> Dict:
    return {
        "app_channel": "ios",
        "os_name": "iOS",
        "app_instance_id": str(uuid.uuid4()).upper(),
        "device_brand": "Apple",
        "app_version": "9.0.0",
        "device_identifier": "ios-" + str(uuid.uuid4()),
        "device_type": "app",
        "platform": "ios",
        "device_name": "iPhone",
        "os_version": "16.5",
        "device_model": "iPhone15,2",
        "app_build": "2",
    }


class XunHuExtractor:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False  # 备用域名证书链未必完整，禁校验更稳
        self.host_i = 0
        self.ua = ANDROID_UA
        self.token: Optional[str] = None
        self.user_id: Optional[str] = None

    # ---------- HTTP ----------

    def _next_host(self) -> str:
        h = HOSTS[self.host_i % len(HOSTS)]
        self.host_i += 1
        return h

    def api(self, method: str, path: str, body: Optional[Dict] = None,
            token: Optional[str] = None, timeout: int = 30, retries: int = 3):
        """带域名轮换与 429 退避的请求；返回 (status, text)，全失败返回 (None, err)。"""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last = None
        for i in range(retries):
            base = self._next_host()
            headers = {
                "User-Agent": self.ua,
                "Accept": "application/json",
                "locale": "zh-CN",
            }
            if token:
                headers["Authorization"] = "Bearer " + token
            if body is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
            try:
                r = self.session.request(
                    method, base + path, data=data, headers=headers,
                    params={"locale": "zh-CN"}, timeout=timeout,
                )
                if r.status_code == 429:
                    wait = 8 + 8 * i
                    print(f"   [限流 429] 等待 {wait}s ...", flush=True)
                    time.sleep(wait)
                    continue
                return r.status_code, r.text
            except Exception as e:
                last = e
                time.sleep(3)
        return None, repr(last)

    # ---------- 注册 ----------

    def register(self) -> bool:
        print("[1/2] 注册新设备 ...")
        attempts = [
            ("android", android_register_body(), ANDROID_UA),
            ("ios", ios_register_body(), IOS_UA),
        ]
        for label, body, ua in attempts:
            self.ua = ua
            st, txt = self.api("POST", "/api/v1/registrations", body=body)
            if st == 201:
                try:
                    d = json.loads(txt)["data"]
                    self.token = (d.get("tokens") or {}).get("access_token")
                    if not self.token:
                        d2 = d.get("token") or {}
                        self.token = d2.get("access_token") if isinstance(d2, dict) else None
                    self.user_id = (d.get("user") or {}).get("user_id")
                    if self.token:
                        print(f"   ✅ {label} 注册成功: user_id={self.user_id}")
                        return True
                    print(f"   [{label}] 响应缺少 access_token，换下一方式")
                except Exception as e:
                    print(f"   [{label}] 响应解析失败: {e!r}")
            else:
                print(f"   [{label}] 注册失败: {st} {str(txt)[:120]}")
        return False

    # ---------- 拉取配置 ----------

    def fetch_config(self) -> Optional[Dict]:
        print("[2/2] 获取节点配置 ...")
        for ver, path in (("v3", "/api/v3/proxy_config?whitelist_version=1"),
                          ("v2", "/api/v2/proxy_config?whitelist_version=1")):
            st, txt = self.api("GET", path, token=self.token)
            if st == 200:
                try:
                    d = json.loads(txt)
                    data = d.get("data") or {}
                    countries = data.get("countries") or []
                    total = sum(len(c.get("nodes") or []) for c in countries)
                    if d.get("code") == "success" and countries:
                        print(f"   ✅ {ver} 成功: {len(countries)} 个国家/地区, {total} 个节点")
                        return data
                except Exception as e:
                    print(f"   [{ver}] 解析失败: {e!r}")
            else:
                print(f"   [{ver}] 失败: {st} {str(txt)[:120]}")
        return None

    # ---------- 链接拼装 ----------

    @staticmethod
    def build_link(node: Dict, country_code: str) -> Optional[str]:
        try:
            proto = (node.get("protocol") or "").lower()
            display = node.get("display_name") or node.get("localized_name") or "Unknown"
            node_type = node.get("node_type", "ordinary")
            cred = node.get("credentials") or {}

            ep = node.get("endpoint") or {}
            host = ep.get("host") or node.get("ip_address") or ""
            port = ep.get("port", 443)
            if not host or not port:
                return None
            sni = ep.get("server_name") or ""
            alpn = ",".join(ep.get("alpn") or [])
            transport = (node.get("transport") or "tcp").lower()
            tls_mode = (node.get("tls_mode") or
                        ("reality" if node.get("reality_options") else "tls")).lower()

            name = f"{country_code}-{display}" if country_code else display
            if node_type == "gold":
                name += "-Gold"
            frag = quote(name)

            if proto == "vless":
                uuid_val = cred.get("uuid") or ""
                if not uuid_val and node_type == "gold":
                    uuid_val = GOLD_FALLBACK_UUID
                if not uuid_val:
                    return None

                params = ["encryption=none"]
                if tls_mode == "reality":
                    reality = node.get("reality_options") or {}
                    pbk = reality.get("public_key") or ""
                    sid = reality.get("short_id") or ""
                    if not pbk or not sid:
                        return None
                    params += ["security=reality", f"sni={sni}", "fp=chrome",
                               f"pbk={pbk}", f"sid={sid}"]
                elif tls_mode == "tls":
                    params += ["security=tls", f"sni={sni}"]
                else:
                    params += ["security=none"]

                params += [f"type={transport}", "headerType=none"]
                if alpn:
                    params.append(f"alpn={alpn}")
                flow = cred.get("flow") or ""
                if flow:
                    params.append(f"flow={flow}")
                return f"vless://{uuid_val}@{host}:{port}?" + "&".join(params) + f"#{frag}"

            if proto == "hysteria2":
                opts = node.get("hysteria2_options") or {}
                pw = cred.get("password") or cred.get("uuid") or opts.get("password") or ""
                if not pw:
                    return None
                q = [f"sni={sni}"] if sni else []
                if opts.get("obfs"):
                    q.append(f"obfs={opts['obfs']}")
                    if opts.get("obfs_password"):
                        q.append(f"obfs-password={opts['obfs_password']}")
                q.append("insecure=0")
                return f"hysteria2://{pw}@{host}:{port}?" + "&".join(q) + f"#{frag}"

            if proto == "tuic":
                opts = node.get("tuic_options") or {}
                uuid_val = cred.get("uuid") or opts.get("uuid") or ""
                pw = cred.get("password") or opts.get("password") or ""
                if not uuid_val or not pw:
                    return None
                q = []
                if sni:
                    q.append(f"sni={sni}")
                if alpn:
                    q.append(f"alpn={alpn}")
                if opts.get("congestion_control"):
                    q.append(f"congestion_control={opts['congestion_control']}")
                q.append("insecure=0")
                return f"tuic://{uuid_val}:{pw}@{host}:{port}?" + "&".join(q) + f"#{frag}"

            return None
        except Exception:
            return None

    def extract_links(self, config: Dict) -> List[str]:
        links: List[str] = []
        for country in config.get("countries") or []:
            cc = country.get("code") or ""
            for node in country.get("nodes") or []:
                link = self.build_link(node, node.get("region_code") or cc)
                if link:
                    links.append(link)
        return links

    def run(self) -> Optional[Dict]:
        if not self.register():
            return None
        return self.fetch_config()


def main() -> int:
    ex = XunHuExtractor()
    config = ex.run()
    if not config:
        print("❌ 注册或获取节点失败")
        return 1

    links = ex.extract_links(config)
    if not links:
        print("❌ 未提取到任何节点链接")
        return 1

    with open("xunhu.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(links) + "\n")

    from urllib.parse import unquote
    countries = set()
    gold = normal = 0
    for link in links:
        nm = unquote(link.split("#", 1)[1]) if "#" in link else ""
        if "-Gold" in nm:
            gold += 1
        else:
            normal += 1
        cc = nm.split("-", 1)[0] if "-" in nm else ""
        if cc:
            countries.add(cc)

    print(f"\n✅ 已保存 {len(links)} 个节点到 xunhu.txt")
    print(f"📍 国家/地区: {len(countries)} 个")
    print(f"📡 普通节点: {normal} 个 | 🥇 黄金节点: {gold} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())