#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ButterflyDS 蝴蝶加速 - 节点提取脚本
====================================
逆向自 ButterflyDS_1.0.9.apk (Flutter + sing-box/Go 核心)

后端: Supabase
  URL : https://rmagubxxssqmcdjnxcso.supabase.co
  Key : sb_publishable_gAEgw8ovdr4ecfXu1V1OeQ_ZREoJlJE (publishable/anon)

认证: Supabase Auth (email+password) —— 服务端限定开通, 注册/匿名均被禁用
节点目录: 表 vpn_nodes (id/name/country/test_ip/test_port/is_vip/is_active)
节点配置: Edge Function get-node-config  body={"node_id": <id>}  → 需 JWT
          响应含 server_ip/port/ss_password/ss_method/protocol/uuid/sni/flow/
          public_key/short_id/transport/ws_path/ws_host/obfs_*/security/...

用法:
  # 方式1: 邮箱密码登录后全量提取
  python3 butterflyds_extract.py login --email you@xx.com --password 'xxx'

  # 方式2: 已有 Supabase access_token (JWT) 直接提取
  python3 butterflyds_extract.py token --access-token 'eyJ...'

  # 可选参数
  --out-dir nodes           输出目录 (默认 ./bfd_nodes)
  --skip-cloud-config       不逐个调云函数, 只导出目录表
  --with-free-only          只看 is_vip=false 的节点
  --timeout 30              每请求超时秒数

输出 (out-dir 下):
  session.json              登录会话 (access_token/refresh_token/user)
  nodes_raw.json            vpn_nodes 目录表全量
  node_configs.json         每个节点的云函数配置
  nodes.txt                 ss:// vless:// trojan:// 分享链接
  subscribe_base64.txt      base64 订阅
  singbox_config.json       聚合 sing-box 配置 (含全部节点 outbound)
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

SB_URL = "https://rmagubxxssqmcdjnxcso.supabase.co"
SB_KEY = "sb_publishable_gAEgw8ovdr4ecfXu1V1OeQ_ZREoJlJE"
UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"


def http(method, url, headers=None, body=None, timeout=30, raw=False):
    h = {"User-Agent": UA, "apikey": SB_KEY, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            if raw:
                return resp.status, content
            try:
                return resp.status, json.loads(content)
            except Exception:
                return resp.status, content.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content)
        except Exception:
            return e.code, content.decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


def login(email, password, timeout=30):
    url = f"{SB_URL}/auth/v1/token?grant_type=password"
    code, data = http("POST", url, body={"email": email, "password": password}, timeout=timeout)
    if code != 200:
        err = data.get("msg") or data.get("error_description") or data.get("message") or data
        raise RuntimeError(f"登录失败 [{code}]: {err}")
    return data


def auth_headers(tok):
    return {"Authorization": f"Bearer {tok}"}


def fetch_vpn_nodes(tok, timeout=30):
    nodes, offset, limit = [], 0, 1000
    while True:
        url = f"{SB_URL}/rest/v1/vpn_nodes?select=*&order=sort_order.asc&offset={offset}&limit={limit}"
        code, data = http("GET", url, headers=auth_headers(tok), timeout=timeout)
        if code != 200:
            raise RuntimeError(f"读取 vpn_nodes 失败 [{code}]: {data}")
        if not data:
            break
        nodes.extend(data)
        if len(data) < limit:
            break
        offset += limit
    return nodes


def get_node_config(tok, node_id, timeout=30):
    url = f"{SB_URL}/functions/v1/get-node-config"
    code, data = http("POST", url, headers=auth_headers(tok),
                      body={"node_id": node_id}, timeout=timeout)
    if code == 200 and isinstance(data, dict):
        return data
    return None


def make_link(c, n):
    """由云函数配置 + 目录行 构造分享链接."""
    server = c.get("server_ip")
    port = c.get("port")
    name = str(n.get("name") or c.get("node_id") or "node")
    if not server or not port:
        return None
    proto = (c.get("protocol") or "").lower()
    if proto == "ss":
        method = c.get("ss_method") or "aes-256-gcm"
        pw = c.get("ss_password") or ""
        if not pw:
            return None
        payload = base64.urlsafe_b64encode(f"{method}:{pw}".encode()).decode().rstrip("=")
        return f"ss://{payload}@{server}:{port}#{urllib.parse.quote(name)}"
    if proto == "vless" and c.get("uuid"):
        q = {k: v for k, v in {
            "encryption": "none",
            "security": c.get("security") or "reality",
            "sni": c.get("sni"),
            "fp": c.get("fingerprint") or "chrome",
            "pbk": c.get("public_key"),
            "sid": c.get("short_id"),
            "flow": c.get("flow"),
            "type": c.get("transport") or "tcp",
            "path": c.get("ws_path"),
            "host": c.get("ws_host"),
        }.items() if v}
        return f"vless://{c['uuid']}@{server}:{port}?{urllib.parse.urlencode(q)}#{urllib.parse.quote(name)}"
    if proto == "trojan" and (c.get("ss_password") or c.get("uuid")):
        pw = c.get("ss_password") or c.get("uuid")
        q = {k: v for k, v in {
            "sni": c.get("sni"), "type": c.get("transport") or "tcp",
            "path": c.get("ws_path"), "host": c.get("ws_host"),
            "security": c.get("security")}.items() if v}
        suffix = f"?{urllib.parse.urlencode(q)}" if q else ""
        return f"trojan://{pw}@{server}:{port}{suffix}#{urllib.parse.quote(name)}"
    return None


def make_outbound(c, n):
    """构造 sing-box outbound."""
    server = c.get("server_ip")
    port = c.get("port")
    name = str(n.get("name") or c.get("node_id") or "node")
    if not server or not port:
        return None
    proto = (c.get("protocol") or "").lower()
    if proto == "ss":
        ob = {"type": "shadowsocks", "tag": name, "server": server,
              "server_port": port, "method": c.get("ss_method") or "aes-256-gcm",
              "password": c.get("ss_password") or "", "udp_over_tcp": False}
        if c.get("obfs_type") and c["obfs_type"] not in (None, "none"):
            ob["plugin"] = "obfs-local"
            ob["plugin_opts"] = (f"obfs={c['obfs_type']};"
                                 f"obfs-host={c.get('obfs_host') or ''};"
                                 f"obfs-uri={c.get('obfs_path') or '/'}")
        return ob
    if proto == "vless" and c.get("uuid"):
        ob = {"type": "vless", "tag": name, "server": server,
              "server_port": port, "uuid": c["uuid"]}
        if c.get("flow"):
            ob["flow"] = c["flow"]
        if c.get("security"):
            ob["tls"] = {"enabled": True, "server_name": c.get("sni"),
                         "utls": {"enabled": True, "fingerprint": c.get("fingerprint") or "chrome"}}
            if c.get("public_key"):
                ob["tls"]["reality"] = {"enabled": True, "public_key": c["public_key"],
                                        "short_id": c.get("short_id")}
        if c.get("ws_path"):
            ob["transport"] = {"type": "ws", "path": c["ws_path"],
                               "headers": {"Host": c.get("ws_host") or ""}}
        return ob
    return None


def main():
    ap = argparse.ArgumentParser(description="ButterflyDS 节点提取")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out-dir", default="bfd_nodes")
    common.add_argument("--skip-cloud-config", action="store_true")
    common.add_argument("--with-free-only", action="store_true")
    common.add_argument("--timeout", type=int, default=30)
    sub = ap.add_subparsers(dest="mode")
    p_login = sub.add_parser("login", parents=[common], help="邮箱密码登录")
    p_login.add_argument("--email", required=True)
    p_login.add_argument("--password", required=True)
    p_token = sub.add_parser("token", parents=[common], help="直接用 access_token")
    p_token.add_argument("--access-token", required=True)
    args = ap.parse_args()

    # 交互模式: 直接运行不带参数时, 逐步询问
    if args.mode is None:
        try:
            print("ButterflyDS 节点提取器")
            print("  1) 邮箱密码登录")
            print("  2) 使用 access_token (JWT)")
            while True:
                choice = input("请选择 [1/2]: ").strip()
                if choice in ("1", "2"):
                    break
            if choice == "1":
                args.mode = "login"
                args.email = input("邮箱: ").strip()
                import getpass
                args.password = getpass.getpass("密码: ")
            else:
                args.mode = "token"
                args.access_token = input("access_token (eyJ...): ").strip()
        except EOFError:
            ap.error("需要子命令 login/token; 直接运行不带参数可进入交互模式")
        # 交互模式未经过子命令解析, 补齐默认值
        args.out_dir = "bfd_nodes"
        args.skip_cloud_config = False
        args.with_free_only = False
        args.timeout = 30

    os.makedirs(args.out_dir, exist_ok=True)
    tok = None
    if args.mode == "login":
        print(f"[*] 登录 {args.email} ...")
        session = login(args.email, args.password, args.timeout)
        tok = session["access_token"]
        print(f"[+] 登录成功 user_id={session.get('user', {}).get('id')}")
        with open(os.path.join(args.out_dir, "session.json"), "w") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
    else:
        tok = args.access_token
        print("[*] 使用提供的 access_token")

    print("[*] 拉取 vpn_nodes 目录 ...")
    nodes = fetch_vpn_nodes(tok, args.timeout)
    if args.with_free_only:
        nodes = [n for n in nodes if not n.get("is_vip")]
    print(f"[+] 目录 {len(nodes)} 个节点")
    with open(os.path.join(args.out_dir, "nodes_raw.json"), "w") as f:
        json.dump(nodes, f, ensure_ascii=False, indent=2)

    configs = {}
    for n in nodes:
        if not args.skip_cloud_config:
            c = get_node_config(tok, n["id"], args.timeout)
            configs[n["id"]] = c if c else {"error": "fetch failed"}
            time.sleep(0.15)
    if not args.skip_cloud_config:
        with open(os.path.join(args.out_dir, "node_configs.json"), "w") as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)
        print(f"[+] 云函数配置 {len(configs)} 条 → node_configs.json")

    links, outbounds, failed = [], [], []
    for n in nodes:
        c = configs.get(n["id"]) if not args.skip_cloud_config else {}
        if args.skip_cloud_config or not c or "error" in c:
            # 目录表里只有测速地址, 无法构成真实节点; 标记
            failed.append((n["id"], n.get("name"), "无云函数配置"))
            continue
        l = make_link(c, n)
        if l:
            links.append(l)
        ob = make_outbound(c, n)
        if ob:
            outbounds.append(ob)
        else:
            failed.append((n["id"], n.get("name"), "配置无法解析"))

    links = list(dict.fromkeys(links))
    print(f"[+] 生成分享链接 {len(links)} 条")
    with open(os.path.join(args.out_dir, "nodes.txt"), "w") as f:
        f.write("\n".join(links) + ("\n" if links else ""))
    sub_b64 = base64.b64encode("\n".join(links).encode()).decode()
    with open(os.path.join(args.out_dir, "subscribe_base64.txt"), "w") as f:
        f.write(sub_b64)
    print(f"[+] nodes.txt / subscribe_base64.txt 已写出 (订阅 {len(sub_b64)} 字符)")

    if outbounds:
        sb = {
            "log": {"level": "info", "timestamp": True},
            "dns": {"servers": [{"tag": "dns-remote", "address": "https://1.1.1.1/dns-query"}]},
            "inbounds": [{"type": "tun", "tag": "tun-in", "mtu": 1500,
                          "auto_route": True, "strict_route": False, "stack": "system"}],
            "outbounds": outbounds + [{"type": "direct", "tag": "direct"},
                                      {"type": "block", "tag": "block"}],
            "route": {"final": "direct", "rules": [{"action": "sniff"}]},
        }
        with open(os.path.join(args.out_dir, "singbox_config.json"), "w") as f:
            json.dump(sb, f, ensure_ascii=False, indent=2)
        print(f"[+] singbox_config.json ({len(outbounds)} outbounds)")

    # 可读汇总
    try:
        alln = json.load(open(os.path.join(args.out_dir, "nodes_raw.json")))
        allc = json.load(open(os.path.join(args.out_dir, "node_configs.json")))
        names = {int(n["id"]): n for n in alln}
        rows = []
        for nid_str, c in allc.items():
            n = names.get(int(nid_str), {})
            if "error" in c or not c.get("server_ip"):
                continue
            rows.append((n.get("sort_order", 0), n.get("name", nid_str), n.get("country_code", ""),
                         c["server_ip"], c.get("port"), c.get("protocol", "?"),
                         c.get("ss_method", ""), "是" if n.get("is_vip") else "否"))
        rows.sort()
        with open(os.path.join(args.out_dir, "节点汇总.txt"), "w", encoding="utf-8") as f:
            f.write("ButterflyDS 蝴蝶加速 节点汇总\n" + "=" * 60 + "\n")
            for r in rows:
                f.write(f"{str(r[1]):<10} [{r[2]}] {r[3]}:{r[4]}  {r[5]}/{r[6]}  VIP:{r[7]}\n")
            f.write("=" * 60 + f"\n共 {len(rows)} 个节点\n")
        print(f"[+] 节点汇总.txt ({len(rows)} 行)")
    except Exception as e:
        print(f"[!] 汇总生成跳过: {e}")

    for n in nodes:
        c = configs.get(n["id"]) if not args.skip_cloud_config else {}
        if args.skip_cloud_config or not c or "error" in c:
            continue
        l = make_link(c, n)
        if l:
            print(f"  {n.get('name')}: {l[:110]}")
    if failed:
        print(f"[!] {len(failed)} 个跳过: {[(f[1] or f[0]) for f in failed][:20]}")


if __name__ == "__main__":
    main()