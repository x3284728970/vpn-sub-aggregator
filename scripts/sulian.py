#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
sulian.py - 速连 VPN 节点获取脚本 (优化版)

用法：
    python3 sulian.py
    python3 sulian.py --count 20 --clip
    python3 sulian.py --region 香港 日本 --output my_nodes.txt
    python3 sulian.py --notify --quiet
"""

import argparse
import base64
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
import uuid
import logging
from urllib.parse import quote
import requests

# ---------- 配置 ----------
class Config:
    BASE_URL = "https://8ycloud.top"
    USER_AGENT = "SpeedLink/1.0.0 (com.speedlinktoo.app; build:1; iOS 16.5.0) Alamofire/5.11.2"
    TIMEOUT = 20
    MAX_RETRIES = 3
    RETRY_BACKOFF = 1.5
    DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "速连节点.txt")
    REQUEST_DELAY = 0.1  # 每个节点生成后的延迟，避免风控

# ---------- 日志与颜色 ----------
class ColoredFormatter(logging.Formatter):
    grey = "\x1b[38;20m"
    green = "\x1b[32;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    fmt = "%(message)s"

    FORMATS = {
        logging.DEBUG: grey + fmt + reset,
        logging.INFO: green + fmt + reset,
        logging.WARNING: yellow + fmt + reset,
        logging.ERROR: red + fmt + reset,
        logging.CRITICAL: bold_red + fmt + reset,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno, self.fmt)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)

logger = logging.getLogger("sulian")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setFormatter(ColoredFormatter())
logger.addHandler(ch)

# ---------- 工具函数 ----------
def retry_call(func, *args, max_retries=Config.MAX_RETRIES, backoff=Config.RETRY_BACKOFF, **kwargs):
    """带指数退避的重试"""
    last_exc = None
    for i in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if i < max_retries:
                wait = backoff * (i + 1) + random.uniform(0, 0.5)
                logger.debug(f"重试 {i+1}/{max_retries} 后等待 {wait:.1f}s: {e}")
                time.sleep(wait)
            else:
                raise last_exc

def copy_to_clipboard(text: str):
    system = platform.system().lower()
    candidates = []
    if system == "darwin":
        candidates.append(["pbcopy"])
    elif system == "linux":
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"],
                    ["wl-copy"], ["termux-clipboard-set"]):
            if shutil.which(cmd[0]):
                candidates.append(cmd)
                break
    elif system == "windows":
        candidates.append(["clip"])
    if shutil.which("apple-clipboard"):
        candidates.append(["apple-clipboard", "set"])
    for cmd in candidates:
        try:
            p = subprocess.run(cmd, input=text.encode("utf-8"), timeout=10)
            if p.returncode == 0:
                return cmd[0]
        except Exception:
            continue
    return None

def send_notification(title: str, body: str):
    candidates = []
    if shutil.which("apple-notification"):
        candidates.append(["apple-notification", "schedule",
                           "--title", title, "--body", body, "--after", "0"])
    if shutil.which("termux-notification"):
        candidates.append(["termux-notification", "--title", title, "--content", body])
    for cmd in candidates:
        try:
            p = subprocess.run(cmd, timeout=10, capture_output=True)
            if p.returncode == 0:
                return cmd[0]
        except Exception:
            continue
    return None

def extract_short_id(raw_sid: str) -> str:
    """清洗 short_id，确保为16位十六进制字符串，不足补0"""
    if not raw_sid:
        return "0" * 16
    hex_chars = ''.join(c for c in raw_sid if c.isdigit() or c.lower() in 'abcdef')
    if not hex_chars:
        return "0" * 16
    # 取前16位，不足右补0
    sid = hex_chars[:16].lower()
    if len(sid) < 16:
        sid = sid.ljust(16, '0')
    return sid

# ---------- 核心客户端 ----------
class SpeedLinkClient:
    def __init__(self, base_url=Config.BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": Config.USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-Hans-CN;q=1.0",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive"
        })
        self.user_id = None

    def generate_client_id(self):
        random_uuid = str(uuid.uuid4()).upper()
        return base64.b64encode(random_uuid.encode()).decode()

    def register_device(self):
        """注册设备，获取 user_id"""
        client_id = self.generate_client_id()
        url = f"{self.base_url}/sl/connect/init"
        headers = {"Content-Type": "application/json"}
        data = {"client_id": client_id}
        try:
            resp = self.session.post(url, headers=headers, json=data, timeout=Config.TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 2000:
                self.user_id = result.get("d", {}).get("user_id")
                return True
            else:
                logger.error(f"注册失败: {result}")
                return False
        except Exception as e:
            logger.error(f"注册请求异常: {e}")
            return False

    def get_node_list(self):
        """获取节点列表"""
        if not self.user_id:
            raise RuntimeError("未注册设备")
        url = f"{self.base_url}/sl/server/list"
        params = {"ak": self.user_id}
        try:
            resp = self.session.get(url, headers={"Content-Type": "application/json"},
                                    params=params, timeout=Config.TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 2000:
                return result.get("d", [])
            else:
                logger.error(f"获取节点失败: {result}")
                return None
        except Exception as e:
            logger.error(f"获取节点异常: {e}")
            return None

    def generate_vless_link(self, node):
        uuid_val = node.get("uuid", "")
        host = node.get("host_addr", "")
        port = node.get("port", "443")
        sni = node.get("sni", "www.apple.com")
        pbk = node.get("public_key", "")
        raw_sid = node.get("short_id", "")
        flow = node.get("flow", "xtls-rprx-vision")
        label = node.get("label_zh", "未命名")

        if not uuid_val or not host or not pbk:
            logger.debug(f"节点缺少关键字段: {node}")
            return None

        sid = extract_short_id(raw_sid)
        if not sid:
            return None

        base = f"vless://{uuid_val}@{host}:{port}"
        params = (
            f"encryption=none"
            f"&security=reality"
            f"&sni={sni}"
            f"&fp=safari"
            f"&pbk={pbk}"
            f"&sid={sid}"
            f"&type=tcp"
            f"&flow={flow}"
        )
        encoded_label = quote(label, safe='')
        return f"{base}?{params}#{encoded_label}"

    def run(self, region_filter=None, count_limit=0):
        """执行主流程，返回生成的链接列表"""
        if not retry_call(self.register_device):
            logger.error("设备注册失败，退出")
            return []

        nodes = retry_call(self.get_node_list)
        if not nodes:
            logger.error("未获取到节点列表")
            return []

        logger.info(f"获取到 {len(nodes)} 个节点")

        # 按地区过滤（label_zh 模糊匹配）
        if region_filter:
            keep = set(region_filter)
            before = len(nodes)
            nodes = [n for n in nodes if any(k in n.get("label_zh", "") for k in keep)]
            logger.info(f"地区过滤 '{region_filter}': {len(nodes)}/{before}")

        if count_limit > 0:
            nodes = nodes[:count_limit]
            logger.info(f"限制前 {count_limit} 个节点")

        links = []
        for idx, node in enumerate(nodes, 1):
            link = self.generate_vless_link(node)
            if link:
                links.append(link)
                logger.debug(f"[{idx}/{len(nodes)}] 生成成功: {node.get('label_zh')}")
            else:
                logger.warning(f"[{idx}/{len(nodes)}] 节点 {node.get('label_zh')} 生成失败")
            if Config.REQUEST_DELAY > 0:
                time.sleep(random.uniform(0, Config.REQUEST_DELAY))

        logger.info(f"成功生成 {len(links)}/{len(nodes)} 条链接")
        return links

# ---------- 主程序 ----------
def main():
    ap = argparse.ArgumentParser(description="速连 VPN 节点获取脚本 (优化版)")
    ap.add_argument("--output", default=Config.DEFAULT_OUTPUT,
                    help=f"输出文件路径 (默认 {Config.DEFAULT_OUTPUT})")
    ap.add_argument("--clip", action="store_true", help="将节点链接复制到剪贴板")
    ap.add_argument("--notify", action="store_true", help="发送系统通知")
    ap.add_argument("--count", type=int, default=0, help="只取前 N 个节点")
    ap.add_argument("--region", nargs="+", default=None, help="按标签模糊过滤地区 (如 香港 日本)")
    ap.add_argument("--quiet", action="store_true", help="只显示错误信息")
    ap.add_argument("--debug", action="store_true", help="显示调试信息")
    ap.add_argument("--base", default=Config.BASE_URL, help="自定义 API 地址")
    args = ap.parse_args()

    # 设置日志级别
    if args.quiet:
        logger.setLevel(logging.ERROR)
    elif args.debug:
        logger.setLevel(logging.DEBUG)

    logger.info(f"后端: {args.base}")
    logger.info(f"输出: {args.output}")

    client = SpeedLinkClient(args.base)
    try:
        t0 = time.time()
        links = client.run(region_filter=args.region, count_limit=args.count)

        if not links:
            logger.warning("没有生成任何有效节点，退出")
            return 2

        # 保存文件
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write("\n".join(links))
                f.write("\n")
            logger.info(f"✅ 已保存 {len(links)} 条链接到 {args.output}")
        except Exception as e:
            logger.error(f"保存文件失败: {e}")
            return 1

        # 保存凭证
        cred_path = args.output + ".account"
        with open(cred_path, "w", encoding="utf-8") as f:
            f.write(f"user_id={client.user_id}\n")
        logger.debug(f"凭证已存 {cred_path}")

        # 剪贴板
        if args.clip:
            blob = "\n".join(links)
            used = copy_to_clipboard(blob)
            tag = f"已复制 ({used})" if used else "无可用命令，请手动复制"
            logger.info(f"📋 剪贴板: {tag}")

        # 通知
        if args.notify:
            used = send_notification("速连 VPN", f"✅ {len(links)} 个节点已生成")
            tag = f"已推送 ({used})" if used else "无可用命令"
            logger.info(f"🔔 通知: {tag}")

        elapsed = time.time() - t0
        logger.info(f"✅ 完成！共 {len(links)} 个节点 (耗时 {elapsed:.1f}s)")
        return 0

    except KeyboardInterrupt:
        logger.warning("\n用户中断")
        return 130
    except Exception as e:
        logger.error(f"❌ 执行失败: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())