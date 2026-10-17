#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iPoWVPN 节点提取 —— configs 现行链路（2026-10-17）

上游架构变更实锤：订阅下发 /sub/<hash> 与 P2P /p2p-lite/* 已下线（503
nodes_unavailable / p2p_dht_capability_unavailable），App（com.ipow.vpn 4.3.x）
现行拿配置的路径是：

    POST /v1/vpn/configs
    Authorization: Bearer <jwt>              # 钱包注册即得，无需 active 订阅
    User-Agent: iPoW-Android/4.3.7
    {"platform": "android", "client_version": "4.3.7", "config_schema": 1,
     "routing": {"china_direct": false}}

响应 = xray 机器配置（regions[].nodes[].config），vless + reality + xhttp。
注意 config_schema 必须为 1（2 会 rejected，invalid_config_request）。
服务端 schema 严格（additionalProperties=false），多余字段一律 invalid_json
（响应里的 invalid_json 实为 schema 校验失败，别被名字坑了）。

输出：iPoW.txt（当前目录），每行一条 vless:// 链接。
"""

import json
import os
import sys
import urllib.parse

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import ipow as core  # 复用 Http / 钱包注册

BASE = (os.environ.get("IPOW_BASE_URL") or "").strip().rstrip("/") or "https://ipow.ai"
UA_APP = "iPoW-Android/4.3.7"
CLIENT_VERSION = "4.3.7"
CONFIG_SCHEMA = 1
BODY = {"platform": "android", "client_version": CLIENT_VERSION,
        "config_schema": CONFIG_SCHEMA, "routing": {"china_direct": False}}

# country_code → 中文（App 下发的是标准两位码；未收录的直接用原码）
CC_CN = {
    "HK": "香港", "TW": "台湾", "JP": "日本", "KR": "韩国", "SG": "新加坡",
    "US": "美国", "GB": "英国", "DE": "德国", "FR": "法国", "NL": "荷兰",
    "RU": "俄罗斯", "IN": "印度", "ID": "印度尼西亚", "MY": "马来西亚",
    "TH": "泰国", "VN": "越南", "PH": "菲律宾", "BR": "巴西", "CA": "加拿大",
    "AU": "澳大利亚", "TR": "土耳其", "AE": "阿联酋", "ZA": "南非", "AU_TO": None,
    "AUTO": "自动",
}


def region_name(cc):
    return CC_CN.get((cc or "").upper()) or (cc or "未知")


def extract_vless(node):
    """xray config.outbounds -> [(tag, region_name, vless_uri)]；non-vless 忽略。"""
    cfg = node.get("config") or {}
    out = []
    cc = node.get("region_cc") or node.get("country_code") or ""
    for ob in cfg.get("outbounds") or []:
        if ob.get("protocol") != "vless":
            continue
        vnext = (ob.get("settings") or {}).get("vnext") or []
        if not vnext:
            continue
        host = vnext[0].get("address")
        port = vnext[0].get("port")
        users = vnext[0].get("users") or []
        if not host or not port or not users:
            continue
        u = users[0]
        uuid = u.get("id") or ""
        if not uuid:
            continue
        flow = u.get("flow") or ""
        ss = ob.get("streamSettings") or {}
        network = ss.get("network") or "tcp"
        security = ss.get("security") or "none"
        rset = ss.get("realitySettings") or {}
        sni = rset.get("serverName") or ""
        pbk = rset.get("publicKey") or ""
        sid = rset.get("shortId") or ""
        fp = rset.get("fingerprint") or "chrome"
        tset = ss.get("xhttpSettings") or {}
        path = tset.get("path") or ""

        q = ["encryption=none", "security=" + security, "type=" + network]
        if sni:
            q.append("sni=" + urllib.parse.quote(sni, safe=""))
        q.append("fp=" + urllib.parse.quote(fp, safe=""))
        if pbk:
            q.append("pbk=" + pbk)
        if sid:
            q.append("sid=" + sid)
        if flow:
            q.append("flow=" + flow)
        if path:
            q.append("path=" + urllib.parse.quote(path, safe=""))
        if network == "xhttp" and tset.get("mode"):
            q.append("mode=" + tset.get("mode"))
        tag = "%s-iPoW" % region_name(cc)
        frag = urllib.parse.quote(tag, safe="")
        uri = "vless://%s@%s:%s?%s#%s" % (uuid, host, port, "&".join(q), frag)
        out.append((tag, uri))
    return out


def main():
    http = core.Http(BASE, timeout=25, min_interval=1.2)
    rec, token, sub = core.load_usable_account(http, None)
    log = getattr(core, "log", print)
    log("[configs] account %s status=%s" % (rec["address"][:12], sub.get("status")))
    log("[configs] POST /v1/vpn/configs schema=%s ..." % CONFIG_SCHEMA)
    st, body = http.request("POST", "/v1/vpn/configs", token=token, body=BODY)
    if st != 200 or not isinstance(body, dict) or "regions" not in body:
        log("[configs] 失败: %s %s" % (st, json.dumps(body, ensure_ascii=False)[:200]))
        return 2

    lines, seen = [], set()
    total = 0
    for r in body.get("regions") or []:
        cc = r.get("country_code") or ""
        for n in r.get("nodes") or []:
            # 修补 region_cc（extract_vless 用）
            n.setdefault("region_cc", cc)
            for tag, uri in extract_vless(n):
                total += 1
                base_key = uri.split("#")[0]
                if base_key in seen:
                    continue
                seen.add(base_key)
                lines.append(uri)
    log("[configs] 节点 %d 个（去重 %d）" % (total, len(lines)))
    if not lines:
        log("[configs] 未提取到 vless 节点")
        return 2
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iPoW.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("[configs] %s written: %d lines" % (out_path, len(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
