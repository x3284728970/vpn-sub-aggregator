#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import uuid
import base64
from typing import Dict, List, Optional
from urllib.parse import quote
import requests


class FoxLinkNodeExtractor:
    
    BASE_URL = "https://foxlink.top"
    
    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self.user_id = None
        self.device_id = None
        
        self.session.headers.update({
            "User-Agent": "foxlink/2 CFNetwork/1408.0.4 Darwin/22.5.0",
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive"
        })
    
    def register_device(self) -> bool:
        print("[1/2] 注册新设备...")
        
        app_instance_id = str(uuid.uuid4()).upper()
        device_identifier = f"ios-{uuid.uuid4()}"
        
        register_data = {
            "app_channel": "ios",
            "os_name": "iOS",
            "app_instance_id": app_instance_id,
            "device_brand": "Apple",
            "app_version": "9.0.0",
            "device_identifier": device_identifier,
            "device_type": "app",
            "platform": "ios",
            "device_name": "iPhone",
            "os_version": "16.5",
            "device_model": "iPhone15,2",
            "app_build": "2"
        }
        
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/api/v1/registrations",
                params={"locale": "zh-CN"},
                json=register_data,
                timeout=30
            )
            
            if resp.status_code == 201:
                data = resp.json()
                if data.get("code") == "success":
                    self.token = data["data"]["tokens"]["access_token"]
                    self.user_id = data["data"]["user"]["user_id"]
                    
                    # 从Token中解析device_id
                    payload = self.token.split('.')[1]
                    payload += '=' * (4 - len(payload) % 4)
                    decoded = base64.urlsafe_b64decode(payload)
                    payload_data = json.loads(decoded)
                    self.device_id = payload_data.get("device_id")
                    
                    print(f"   ✅ 注册成功!")
                    print(f"   👤 User ID: {self.user_id}")
                    print(f"   📱 Device ID: {self.device_id}")
                    return True
            else:
                print(f"   ❌ 注册失败: {resp.status_code}")
                return False
                
        except Exception as e:
            print(f"   ❌ 注册异常: {e}")
            return False
    
    def fetch_node_config(self) -> Optional[Dict]:
        print("[2/2] 获取节点配置...")
        
        if not self.token:
            print("   ❌ 无Token")
            return None
        
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Host": "foxlink.top"
        }
        
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/api/v2/proxy_config",
                headers=headers,
                params={"whitelist_version": "1", "locale": "zh-CN"},
                timeout=30
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == "success":
                    config_data = data.get("data", {})
                    countries = config_data.get("countries", [])
                    total_nodes = sum(len(c.get("nodes", [])) for c in countries)
                    print(f"   ✅ 获取成功! {len(countries)} 个国家/地区, {total_nodes} 个节点")
                    return config_data
            return None
        except Exception:
            return None
    
    def build_vless_link(self, node: Dict, country_code: str) -> Optional[str]:
        try:
            display_name = node.get("display_name", "Unknown")
            node_type = node.get("node_type", "ordinary")
            
            credentials = node.get("credentials", {})
            uuid_val = credentials.get("uuid", "")
            
            if not uuid_val and node_type == "gold":
                uuid_val = "00000000-0000-0000-0000-000000000000"
            
            if not uuid_val:
                return None
            
            endpoint = node.get("endpoint", {})
            host = endpoint.get("host", "")
            port = endpoint.get("port", 443)
            server_name = endpoint.get("server_name", "www.cloudflare.com")
            alpn = endpoint.get("alpn", ["h2", "http/1.1"])
            
            reality = node.get("reality_options", {})
            public_key = reality.get("public_key", "")
            short_id = reality.get("short_id", "")
            
            if not public_key or not short_id:
                return None
            
            flow = credentials.get("flow", "")
            
            link = f"vless://{uuid_val}@{host}:{port}"
            
            params = [
                "encryption=none",
                "security=reality",
                f"sni={server_name}",
                "fp=chrome",
                f"pbk={public_key}",
                f"sid={short_id}",
                "type=tcp",
                "headerType=none",
                f"alpn={','.join(alpn)}"
            ]
            
            if flow:
                params.append(f"flow={flow}")
            
            link += "?" + "&".join(params)
            
            name = display_name
            if country_code:
                name = f"{country_code}-{name}"
            if node_type == "gold":
                name = f"{name}-Gold"
            
            link += f"#{quote(name)}"
            
            return link
            
        except Exception:
            return None
    
    def extract_links(self, config: Dict) -> List[str]:
        links = []
        countries = config.get("countries", [])
        
        for country in countries:
            country_code = country.get("code", "")
            for node in country.get("nodes", []):
                if node.get("protocol") != "vless":
                    continue
                
                link = self.build_vless_link(node, country_code)
                if link:
                    links.append(link)
        
        return links
    
    def run(self) -> List[str]:
        if not self.register_device():
            return []
        
        config = self.fetch_node_config()
        if not config:
            return []
        
        links = self.extract_links(config)
        return links


def main():
    extractor = FoxLinkNodeExtractor()
    links = extractor.run()
    
    if links:
        with open("yinhu.txt", "w", encoding="utf-8") as f:
            for link in links:
                f.write(link + "\n")
        
        countries = set()
        gold_count = 0
        normal_count = 0
        
        for link in links:
            if "#" in link:
                name = link.split("#")[-1]
                try:
                    from urllib.parse import unquote
                    name = unquote(name)
                except:
                    pass
                if "-Gold" in name:
                    gold_count += 1
                else:
                    normal_count += 1
                country = name.split("-")[0] if "-" in name else "Unknown"
                countries.add(country)
        
        print(f"\n✅ 已保存 {len(links)} 个节点到 yinhu.txt")
        print(f"📍 国家/地区: {len(countries)} 个")
        print(f"📡 普通节点: {normal_count} 个")
        print(f"🥇 黄金节点: {gold_count} 个")
    else:
        print("❌ 注册或获取节点失败")


if __name__ == "__main__":
    main()