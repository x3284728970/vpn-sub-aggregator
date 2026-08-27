#!/usr/bin/env python3
"""
zytvpn - 纵云梯 VPN 一键注册脚本 (优化美化版)

跨平台适配：iSH/Juno (iOS)、Termux (Android)、macOS、Linux、Windows
依赖：Python 3.8+，pycryptodome 或 cryptography

用法：
    python3 zytvpn.py
    python3 zytvpn.py --region 香港 日本 新加坡 美国
    python3 zytvpn.py --clip --notify
    python3 zytvpn.py --count 50
    python3 zytvpn.py --online --quiet
    python3 zytvpn.py --delay 0.5 --concurrency 20
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
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# ---------- 配置 ----------
class Config:
    API_BASE = "http://47.129.170.28/service"
    APP_SECRET = "Fireworks.show.vpn"
    DEVICE_ID_BASE = "0DF-7930-4EAB-9419-AFF2EDA"
    AES_KEY = b"odjwiejwu2ieo929d923ifi9qK9wiOJK"
    AES_IV = b"Oe9935kjso393024"
    USER_AGENT = "Postpop/1 CFNetwork/1402.0.8 Darwin/22.2.0"
    TIMEOUT = 20
    SSLEYE_ENCRYPT = "https://www.ssleye.com/ssltool/aes_encrypt_hander"
    SSLEYE_DECRYPT = "https://www.ssleye.com/ssltool/aes_decrypt_hander"
    DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "纵云梯节点.txt")
    MAX_RETRIES = 3
    RETRY_BACKOFF = 1.5

# ---------- 日志与颜色 ----------
class ColoredFormatter(logging.Formatter):
    """带颜色的日志格式化器"""
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

logger = logging.getLogger("zytvpn")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setFormatter(ColoredFormatter())
logger.addHandler(ch)

# ---------- 加密引擎 ----------
def _load_crypto():
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad, unpad
        return ("pycryptodome", AES, pad, unpad, None, None, None, None)
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives import padding as _pad
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        return ("cryptography", None, None, None, _pad, Cipher, algorithms, modes)
    except ImportError:
        pass
    return None

CRYPTO = _load_crypto()

class AESCipher:
    def __init__(self, key=Config.AES_KEY, iv=Config.AES_IV, online=False):
        self.key = key
        self.iv = iv
        self.online = online
        if online:
            logger.warning("使用在线加密 (ssleye.com)，数据将经第三方服务器，请谨慎！")
        elif CRYPTO is None:
            raise RuntimeError("未安装加密库，请执行: pip install pycryptodome")

    def encrypt(self, plaintext: str) -> str:
        if self.online:
            return self._encrypt_online(plaintext)
        return self._encrypt_local(plaintext)

    def decrypt(self, ciphertext_b64: str) -> str:
        if self.online:
            return self._decrypt_online(ciphertext_b64)
        return self._decrypt_local(ciphertext_b64)

    def _encrypt_local(self, plaintext: str) -> str:
        if CRYPTO[0] == "pycryptodome":
            AES, pad, _ = CRYPTO[1], CRYPTO[2], CRYPTO[3]
            cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
            ct = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
            return base64.b64encode(ct).decode()
        else:
            _pad, Cipher, algorithms, modes = CRYPTO[4], CRYPTO[5], CRYPTO[6], CRYPTO[7]
            padder = _pad.PKCS7(128).padder()
            padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
            enc = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
            ct = enc.update(padded) + enc.finalize()
            return base64.b64encode(ct).decode()

    def _decrypt_local(self, ciphertext_b64: str) -> str:
        if CRYPTO[0] == "pycryptodome":
            AES, unpad = CRYPTO[1], CRYPTO[3]
            cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
            pt = unpad(cipher.decrypt(base64.b64decode(ciphertext_b64)), AES.block_size)
            return pt.decode("utf-8")
        else:
            _pad, Cipher, algorithms, modes = CRYPTO[4], CRYPTO[5], CRYPTO[6], CRYPTO[7]
            ct = base64.b64decode(ciphertext_b64)
            dec = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
            padded = dec.update(ct) + dec.finalize()
            unpadder = _pad.PKCS7(128).unpadder()
            pt = unpadder.update(padded) + unpadder.finalize()
            return pt.decode("utf-8")

    def _ssleye_form(self, text: str) -> dict:
        return {
            "text": text, "encode_flag": "utf8",
            "key": self.key.decode(), "iv": self.iv.decode(),
            "mode": "CBC", "padding": "pkcs5",
            "out_mode": "base64", "mactag": "",
        }

    def _http_post(self, url, body=None, headers=None):
        headers = dict(headers or {})
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = body
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=Config.TIMEOUT) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except URLError as e:
            raise RuntimeError(f"网络错误: {e}")

    def _encrypt_online(self, plaintext: str) -> str:
        status, text = self._http_post(Config.SSLEYE_ENCRYPT, body=self._ssleye_form(plaintext))
        j = json.loads(text)
        if j.get("msg") is None:
            raise RuntimeError(f"ssleye encrypt 失败: {j}")
        return j["msg"]

    def _decrypt_online(self, ciphertext_b64: str) -> str:
        status, text = self._http_post(Config.SSLEYE_DECRYPT, body=self._ssleye_form(ciphertext_b64))
        j = json.loads(text)
        if j.get("msg") is None:
            raise RuntimeError(f"ssleye decrypt 失败: {j}")
        return j["msg"]

# ---------- API 客户端 ----------
class VPNClient:
    def __init__(self, base=Config.API_BASE, cipher=None):
        self.base = base.rstrip("/")
        self.cipher = cipher or AESCipher()

    def call(self, endpoint, payload):
        if not endpoint.endswith(".php"):
            endpoint += ".php"
        plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        ciphertext = self.cipher.encrypt(plaintext)
        url = f"{self.base}/{endpoint}"
        status, text = self._http_post(
            url,
            body={"key": ciphertext},
            headers={"Content-Type": "application/json", "User-Agent": Config.USER_AGENT},
        )
        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {text[:200]}")
        inner_json = self.cipher.decrypt(text.strip())
        return json.loads(inner_json)

    def _http_post(self, url, body=None, headers=None):
        headers = dict(headers or {})
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = body
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=Config.TIMEOUT) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except URLError as e:
            raise RuntimeError(f"网络错误: {e}")

# ---------- 工具函数 ----------
def random_suffix():
    return f"{random.randint(10000, 99999)}{random.randint(10000, 99999)}"

def retry_call(func, *args, max_retries=Config.MAX_RETRIES, backoff=Config.RETRY_BACKOFF, **kwargs):
    """带指数退避的重试装饰器"""
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

def step_register(client):
    suffix = random_suffix()
    device_id = f"{Config.DEVICE_ID_BASE}{suffix}"
    j = retry_call(client.call, "Accountinfo", {
        "task": "UserRegistration",
        "app_secret": Config.APP_SECRET,
        "device_id": device_id,
    })
    if str(j.get("status", "")).lower() not in ("success", "200", "0", ""):
        raise RuntimeError(f"注册失败: {j}")
    return j["data"]["account_ID"], device_id

def step_list_lines(client, account_id, device_id):
    j = retry_call(client.call, "line", {
        "task": "vps_info",
        "account_ID": account_id,
        "device_id": device_id,
    })
    return j.get("data") or []

def step_register_host(client, account_id, server):
    return retry_call(client.call, "line", {
        "account_ID": account_id,
        "task": "register_host",
        "way": 2,
        "server": server,
    })

def build_trojan_url(password, host, port, name_cn, describe_cn, idx):
    note = f"{name_cn}-{describe_cn}({idx})" if describe_cn else f"{name_cn}({idx})"
    return f"trojan://{quote(password, safe='')}@{host}:{port}?mux=1#{quote(note)}"

# ---------- 剪贴板与通知 ----------
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

# ---------- 进度条（带tqdm回退） ----------
def show_progress(iterable, total=None, desc="进度", unit="个"):
    try:
        from tqdm import tqdm
        return tqdm(iterable, total=total, desc=desc, unit=unit, dynamic_ncols=True)
    except ImportError:
        # 自定义简易进度条
        class DummyProgress:
            def __init__(self, iterable, total, desc, unit):
                self.iterable = iterable
                self.total = total
                self.desc = desc
                self.unit = unit
                self.count = 0
                self.start = time.time()
                self.last_update = 0
            def __iter__(self):
                for item in self.iterable:
                    yield item
                    self.count += 1
                    now = time.time()
                    if now - self.last_update > 0.2 or self.count == self.total:
                        self.last_update = now
                        pct = self.count / self.total * 100
                        elapsed = now - self.start
                        eta = elapsed / self.count * (self.total - self.count) if self.count else 0
                        bar_len = 30
                        filled = int(bar_len * self.count / self.total)
                        bar = '█' * filled + '░' * (bar_len - filled)
                        sys.stdout.write(f"\r{self.desc} [{bar}] {self.count}/{self.total} ({pct:5.1f}%) {elapsed:.1f}s ETA {eta:.1f}s")
                        sys.stdout.flush()
                print()
        return DummyProgress(iterable, total=total, desc=desc, unit=unit)

# ---------- 主逻辑 ----------
def main():
    ap = argparse.ArgumentParser(description="纵云梯 VPN 一键注册脚本 (优化美化版)")
    ap.add_argument("--online", action="store_true", help="使用 ssleye 在线加解密 (不推荐)")
    ap.add_argument("--count", type=int, default=0, help="只取前 N 个节点 (0=全部)")
    ap.add_argument("--output", default=Config.DEFAULT_OUTPUT,
                    help=f"输出文件 (默认 {Config.DEFAULT_OUTPUT})")
    ap.add_argument("--clip", action="store_true", help="将节点链接复制到剪贴板")
    ap.add_argument("--notify", action="store_true", help="发送系统通知")
    ap.add_argument("--base", default=Config.API_BASE, help="自定义后端 API 地址")
    ap.add_argument("--concurrency", type=int, default=50, help="并发线程数 (默认 50)")
    ap.add_argument("--delay", type=float, default=0.1, help="每个请求之间的最小延迟 (秒)")
    ap.add_argument("--region", nargs="+", default=None, help="仅保留指定地区 (如 香港 日本)")
    ap.add_argument("--quiet", action="store_true", help="减少输出 (只显示错误)")
    ap.add_argument("--debug", action="store_true", help="显示调试信息")
    args = ap.parse_args()

    # 设置日志级别
    if args.quiet:
        logger.setLevel(logging.ERROR)
    elif args.debug:
        logger.setLevel(logging.DEBUG)

    logger.info(f"后端: {args.base}")
    logger.info(f"加密: {'在线 ssleye' if args.online else (CRYPTO[0] if CRYPTO else '未安装')}")
    logger.info(f"输出: {args.output}")
    if args.online:
        logger.warning("在线加密会将数据发送至 ssleye.com，请注意隐私安全！")

    # 初始化
    cipher = AESCipher(online=args.online)
    client = VPNClient(args.base, cipher)

    try:
        t0 = time.time()
        logger.info("[1/3] 注册账号 ...")
        account_id, device_id = retry_call(step_register, client)
        logger.info(f"   ✓ account_ID = {account_id}  (40 分钟有效)")
        logger.info(f"   ✓ device_id  = {device_id}")

        logger.info("[2/3] 拉取线路列表 ...")
        lines = retry_call(step_list_lines, client, account_id, device_id)
        if not isinstance(lines, list):
            raise RuntimeError(f"线路列表格式异常: {lines!r}")
        logger.info(f"   ✓ 共 {len(lines)} 条线路")

        if args.region:
            keep = set(args.region)
            before = len(lines)
            lines = [ln for ln in lines if ln.get("name_cn") in keep]
            logger.info(f"   → 过滤 {args.region}: 剩 {len(lines)}/{before}")

        if args.count:
            lines = lines[:args.count]
            logger.info(f"   → 只取前 {len(lines)} 条")

        lines.sort(key=lambda x: -int(x.get("recommend", 0)))

        logger.info(f"[3/3] 注册节点 (并发 {args.concurrency}, 延迟 {args.delay}s) ...")
        ok = []
        fail = []
        start = time.time()

        def reg(idx, ln):
            try:
                retry_call(step_register_host, client, account_id, ln["server"])
                return idx, ln, None
            except Exception as e:
                return idx, ln, str(e)

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futures = [ex.submit(reg, i, ln) for i, ln in enumerate(lines, 1)]
            progress = show_progress(futures, total=len(lines), desc="注册节点", unit="个")
            for fut in progress:
                idx, ln, err = fut.result()
                if err:
                    fail.append((idx, ln["server"], err))
                else:
                    url = build_trojan_url(
                        password=account_id,
                        host=ln["server"],
                        port=ln["port"],
                        name_cn=ln.get("name_cn") or ln["server"],
                        describe_cn=ln.get("describe_cn") or "",
                        idx=idx,
                    )
                    ok.append((idx, url))
                # 请求间隔
                if args.delay > 0:
                    time.sleep(random.uniform(0, args.delay))

        logger.info(f"   ✓ 成功 {len(ok)}，失败 {len(fail)}")

        if fail:
            logger.warning(f"⚠️ 失败 {len(fail)} 个:")
            for idx, server, err in fail[:10]:
                logger.warning(f"   [{idx}] {server}: {err[:80]}")
            if len(fail) > 10:
                logger.warning(f"   ... 还有 {len(fail)-10} 个")

        # 保存结果
        ok.sort(key=lambda x: x[0])
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("\n".join(u for _, u in ok))
            f.write("\n")

        cred_path = args.output + ".account"
        with open(cred_path, "w", encoding="utf-8") as f:
            f.write(f"account_id={account_id}\ndevice_id={device_id}\n")
        logger.info(f"   📝 凭证已存 {cred_path}")

        if ok and args.clip:
            blob = "\n".join(u for _, u in ok)
            used = copy_to_clipboard(blob)
            tag = f"已复制 ({used})" if used else "无可用命令，请手动复制"
            logger.info(f"   📋 剪贴板: {tag}")

        if ok and args.notify:
            used = send_notification("纵云梯 VPN", f"✅ {len(ok)} 个节点已生成")
            tag = f"已推送 ({used})" if used else "无可用命令"
            logger.info(f"   🔔 通知: {tag}")

        elapsed = time.time() - t0
        logger.info(f"✅ 完成！{len(ok)}/{len(lines)} 个节点已写入 {args.output} (总耗时 {elapsed:.1f}s)")
        return 0 if ok else 2

    except KeyboardInterrupt:
        logger.warning("\n用户中断")
        return 130
    except Exception as e:
        logger.error(f"❌ 失败: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())