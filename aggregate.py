#!/usr/bin/env python3
import os
import sys
import base64
import subprocess
import json
import time
from pathlib import Path
from datetime import datetime
import urllib.parse

try:
    import requests
except ImportError:
    requests = None

import naming

BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "scripts"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 必须排除的敏感/中间文件
EXCLUDE_FILES = {
    "last_account.json",
    "all_nodes_full_configs.json",
    "all_outbounds_merged.json",
    "subscription_base64.txt",
    "all_nodes_sharable_links.txt",
    "all_nodes_full_configs (1).json",
    # 含凭据的中间文件，绝不能进订阅
    "token.json",
    ".fengniao_token_cache.json",
    "fengniao_data.json",
    "lanmao_sub_base64.txt",
    # iPoW 的中间文件（钱包记录与原始配置，属于运行痕迹，不是订阅）
    "iPoW_wallets.json",
    "iPoW_unusable.json",
    "iPoW_nodes.json",
    "extract_accounts.jsonl",
    # ipow_get 的中间产物；订阅只取 iPoW.txt（=可达清单），避免把不可达节点混进来
    "all_nodes.json",
    "all_uris.txt",
    "reachable_uris.txt",
    "reachable_nodes.json",
    "singbox_config.json",
    "clash_proxies.yaml",
    "singbox_proxies.json",
}

# 各脚本期望的节点输出文件（用于日志报告）
EXPECTED_NODE_FILES = {
    "__.py": ["cainiao.txt"],
    "TF__.py": ["yinhu.txt", "TF__.txt", "foxlink.txt"],
    "Surfer.py": ["surfer.txt", "SF.txt"],
    "de.py": ["nodes.txt", "devpn.txt"],
    "sulian.py": ["sulian.txt", "sulianb.txt", "sulianc.txt"],
    "lanmao.py": ["lanmao.txt", "lanmao_sub_base64.txt"],
    "ipow.py": ["iPoW.txt"],
    "kite.py": ["kite.txt"],
    # fengniao.py 不加（用户自己单独用）
}

# ================================================================
# GitHub Gist 上传（私有 Gist，无需仓库/Pages 设置）
# ================================================================

GIST_DESC = "daily data"
GIST_FILENAME = "jvhe.txt"


def _github_token() -> str | None:
    token = os.environ.get("MY_GITHUB_TOKEN", "").strip()
    if not token:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
    return token or None


def _find_gist_id(token: str) -> str | None:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    page = 1
    while True:
        try:
            r = requests.get(
                "https://api.github.com/gists",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=30,
            )
        except Exception as e:
            print(f"[GIST] 列出 Gist 失败: {e}")
            return None
        if r.status_code != 200:
            return None
        gists = r.json()
        if not gists:
            return None
        for g in gists:
            if g.get("description") == GIST_DESC:
                files = g.get("files", {})
                if GIST_FILENAME in files:
                    return g["id"]
        if len(gists) < 100:
            return None
        page += 1


def upload_to_gist(content: str) -> str | None:
    if requests is None:
        print("[GIST] requests 未安装，跳过 Gist 上传")
        return None
    token = _github_token()
    if not token:
        print("[GIST] 未设置 MY_GITHUB_TOKEN / GITHUB_TOKEN，跳过 Gist 上传")
        return None

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    gist_id = _find_gist_id(token)
    payload = {
        "description": GIST_DESC,
        "public": False,
        "files": {GIST_FILENAME: {"content": content}},
    }

    try:
        if gist_id:
            r = requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers=headers,
                json=payload,
                timeout=30,
            )
            action = "更新"
        else:
            r = requests.post(
                "https://api.github.com/gists",
                headers=headers,
                json=payload,
                timeout=30,
            )
            action = "创建"

        if r.status_code in (200, 201):
            gist_id = r.json()["id"]
            raw_url = f"https://gist.githubusercontent.com/raw/{gist_id}/{GIST_FILENAME}"
            print(f"[GIST] {action}成功，raw 链接: {raw_url}")
            return raw_url
        else:
            print(f"[GIST] {action}失败: {r.status_code} {r.text[:200]}")
            return None
    except Exception as e:
        print(f"[GIST] 上传异常: {e}")
        return None


# ================================================================
# 脚本运行
# ================================================================


def run_script(name, extra_env=None, args=None):
    script_path = SCRIPTS_DIR / name
    if not script_path.exists():
        print(f"[SKIP] {name} 不存在")
        return None

    print(f"\n{'='*60}")
    print(f"[*] 运行 {name}")
    print(f"{'='*60}")

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env.setdefault("PYTHONUNBUFFERED", "1")

    cmd = [sys.executable, str(script_path)] + list(args or [])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(SCRIPTS_DIR),
            timeout=1200,  # 单脚本上限 20 分钟
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if stdout:
            print(stdout[-4000:])

        if result.returncode != 0:
            print(f"[WARN] {name} 退出码 {result.returncode}")
            if stderr:
                print(f"[STDERR] {stderr[:1000]}")
            return False
        else:
            print(f"[OK] {name} 完成")
            return True

    except subprocess.TimeoutExpired:
        print(f"[TIMEOUT] {name} 超过 1200 秒，已终止")
        return False
    except Exception as e:
        print(f"[ERROR] {name}: {e}")
        return False


# ================================================================
# 收集与合并
# ================================================================


def collect_outputs():
    candidates = []
    for root, dirs, files in os.walk(str(SCRIPTS_DIR)):
        for f in files:
            fp = Path(root) / f
            if f in EXCLUDE_FILES:
                continue
            if f.endswith((".txt", ".json", ".log")):
                candidates.append(fp)
    for f in OUTPUT_DIR.iterdir():
        if f.is_file() and f.name not in EXCLUDE_FILES:
            candidates.append(f)
    unique = sorted(set(candidates))
    print(f"[*] 收集到 {len(unique)} 个输出文件:")
    for u in unique:
        try:
            size = u.stat().st_size
        except Exception:
            size = -1
        try:
            shown = u.relative_to(BASE_DIR)
        except ValueError:
            shown = u
        print(f"    - {shown} ({size} bytes)")
    return unique


def is_node_line(line: str) -> bool:
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return False
    return line.startswith(("vless://", "vmess://", "trojan://", "ss://", "ssr://", "tuic://",
                            "hysteria2://", "hy2://"))


def extract_links(path: Path):
    links = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            if is_node_line(line):
                links.append(line)
    except Exception as e:
        print(f"[WARN] 读取 {path} 失败: {e}")
    return links


def merge_all(file_list):
    seen = set()
    merged = []

    for fpath in file_list:
        for link in extract_links(fpath):
            try:
                base = link.split("#", 1)[0] if "#" in link else link
                if "?" in base:
                    proto_and_rest, query = base.split("?", 1)
                    params = urllib.parse.parse_qsl(query, keep_blank_values=True)
                    params.sort(key=lambda x: x[0])
                    key = f"{proto_and_rest}?{urllib.parse.urlencode(params)}"
                else:
                    key = base
                key = key.strip()
                if not key:
                    continue
                if key not in seen:
                    seen.add(key)
                    merged.append((link, fpath.name))
            except Exception:
                if link and link not in seen:
                    seen.add(link)
                    merged.append((link, fpath.name))

    return merged


def count_by_type(links):
    stats = {}
    for l in links:
        if l.startswith("vless://"):
            t = "VLESS"
        elif l.startswith("vmess://"):
            t = "VMESS"
        elif l.startswith("trojan://"):
            t = "TROJAN"
        elif l.startswith("ss://"):
            t = "SS"
        elif l.startswith(("hysteria2://", "hy2://")):
            t = "HY2"
        else:
            t = "OTHER"
        stats[t] = stats.get(t, 0) + 1
    return stats


# ================================================================
# main
# ================================================================


def main():
    print("=" * 60)
    print("VPN 节点聚合器")
    print(f"开始时间: {datetime.now().isoformat()}")
    print(f"工作目录: {BASE_DIR}")
    print("=" * 60)

    # 依次运行提取脚本

    # 依次运行提取脚本
    results = {}
    results["TF__.py"] = run_script("TF__.py")
    results["Surfer.py"] = run_script("Surfer.py")
    results["__.py"] = run_script("__.py")
    # fengniao.py 不加（用户自己单独用）
    # results["fengniao.py"] = run_script("fengniao.py")
    results["sulian.py"] = run_script("sulian.py")
    # zytvpn.py 已删除（注册接口返回 error，无法修复）
    # butterflyds.py 已删除（需要交互式输入，无法自动化）
    # de.py / lanmao.py moved to the panel, not aggregated here
    # results["devpn.py"] = run_script("devpn.py")
    # results["lanmao.py"] = run_script("lanmao.py")
    # ipow.py：订阅 + P2P 两路并发采集，接口有请求频率闸门（撞 429 自动降速重试）
    results["ipow.py"] = run_script("ipow.py", args=["--links-scope", "all"])
    results["kite.py"] = run_script("kite.py")

    # 收集输出文件
    files = collect_outputs()
    if not files:
        print("[!] 没有收集到任何输出文件")
        sys.exit(1)

    # 合并去重
    pairs = merge_all(files)
    print(f"\n[*] 合并去重后共 {len(pairs)} 个节点")

    # 按「VPN名+地区」统一重命名
    merged = naming.apply(pairs)

    per_source = {}
    for _link, src in pairs:
        tag = naming.FILE_TAGS.get(src, src)
        per_source[tag] = per_source.get(tag, 0) + 1

    print("[*] 来源统计（重命名后的节点名前缀）:")
    for tag in sorted(per_source, key=lambda t: (-per_source[t], t)):
        print(f"    {tag:<10} {per_source[tag]} 个")
    examples = [naming.current_name(l) for l in merged[:6]]
    print("[*] 节点名示例: " + "，".join(e for e in examples if e))

    # 统计
    stats = count_by_type(merged)
    print(f"[*] 类型统计: {stats}")

    # 写出纯文本订阅
    sub_txt = OUTPUT_DIR / "sub.txt"
    sub_txt.write_text("\n".join(merged) + "\n", encoding="utf-8")
    print(f"[+] 纯文本订阅: {sub_txt}")

    # 写出 Base64 订阅
    b64_content = base64.b64encode(
        "\n".join(merged).encode("utf-8")
    ).decode("ascii")

    sub_b64 = OUTPUT_DIR / "jvhe.txt"
    sub_b64.write_text(b64_content, encoding="utf-8")
    print(f"[+] Base64 订阅: {sub_b64} (长度 {len(b64_content)})")

    # 上传到 GitHub Gist（私有 Gist，直链当订阅地址）
    gist_raw_url = upload_to_gist(b64_content)
    if gist_raw_url:
        print(f"[+] 订阅链接: {gist_raw_url}")
    else:
        print(f"[!] Gist 上传失败或跳过，订阅仅在本地: file://{sub_b64}")

    # 写出统计报告
    report = {
        "timestamp": datetime.now().isoformat(),
        "total_nodes": len(merged),
        "by_type": stats,
        "source_files": [str(p.relative_to(BASE_DIR)) for p in files],
        "results": {k: ("skip" if v is None else ("ok" if v else "fail")) for k, v in results.items()},
        "gist_raw_url": gist_raw_url,
    }
    report_path = OUTPUT_DIR / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] 运行报告: {report_path}")

    print("=" * 60)
    print("聚合完成")
    print(f"结束时间: {datetime.now().isoformat()}")
    print("=" * 60)


if __name__ == "__main__":
    main()