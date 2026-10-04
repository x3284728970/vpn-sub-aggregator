#!/usr/bin/env python3
import argparse
import base64
import os
import sys

import requests

# 默认只作废这个（用户发出去的那个）
DEFAULT_TARGETS = ["sub_b64.txt"]

# 替换用的「死节点」——地址不可达，客户端解析得出但连不上
DEAD_NODE = (
    "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1"
    "?encryption=none&security=none&type=tcp#%E6%8D%A2%E9%93%BE%E6%8E%A5%E4%BA%86"
)
DEAD_CONTENT_B64 = base64.b64encode(DEAD_NODE.encode()).decode()

API = "https://api.github.com"


def headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def list_all_gists(token):
    out = []
    page = 1
    while True:
        r = requests.get(
            f"{API}/gists",
            headers=headers(token),
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[!] 列出 Gist 失败: HTTP {r.status_code} {r.text[:200]}")
            if r.status_code == 404:
                print("    → 通常是 token 缺少 gist scope")
            return out
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不修改")
    ap.add_argument("--targets", default=None,
                    help="要作废的文件名，逗号分隔（默认 sub_b64.txt）")
    ap.add_argument("--keep", action="append", default=[],
                    help="保留的文件名，可多次指定（默认保留 jvhe.txt）")
    ap.add_argument("--desc", default=None, help="只处理该 description 的 Gist")
    args = ap.parse_args()

    if args.targets:
        purge_targets = [x.strip() for x in args.targets.split(",") if x.strip()]
    else:
        purge_targets = ["sub_b64.txt"]   # 默认只作废这一个
    keep = set(args.keep) or {"jvhe.txt"}

    token = os.environ.get("MY_GITHUB_TOKEN", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("[!] 未设置 MY_GITHUB_TOKEN")
        sys.exit(1)

    gists = list_all_gists(token)
    print(f"[*] 账号下共 {len(gists)} 个 Gist")

    hit = 0
    for g in gists:
        gid = g["id"]
        desc = g.get("description") or ""
        files = g.get("files", {})
        if args.desc and desc != args.desc:
            continue

        targets = [fn for fn in files if fn in purge_targets and fn not in keep]
        if not targets:
            continue

        hit += 1
        print(f"\n[*] Gist {gid} | {desc} | public={g.get('public')}")
        print(f"    文件: {list(files.keys())}")
        print(f"    待作废: {targets}")

        if args.dry_run:
            continue

        # 覆盖成死节点（保留文件名，raw 链接不变）
        payload = {
            "description": desc or "VPN 节点订阅",
            "files": {fn: {"content": DEAD_CONTENT_B64} for fn in targets},
        }
        r = requests.patch(f"{API}/gists/{gid}", headers=headers(token), json=payload, timeout=30)
        if r.status_code == 200:
            for fn in targets:
                print(f"    [OK] 已作废 {fn} -> https://gist.githubusercontent.com/raw/{gid}/{fn}")
        else:
            print(f"    [!] 失败: HTTP {r.status_code} {r.text[:200]}")

    if hit == 0:
        print("\n[!] 没找到需要作废的 Gist 文件")
        print("    如果确定存在，检查 token 是否有 gist scope")
    else:
        print(f"\n[*] 处理完成，共 {hit} 个 Gist")
        print("    raw 链接有 CDN 缓存，几分钟后生效；对方客户端下次更新即失效")


if __name__ == "__main__":
    main()
