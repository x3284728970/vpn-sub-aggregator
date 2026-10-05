#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iPoWVPN 节点一条龙提取（订阅池 + P2P 池 + 可达筛查）
====================================================
链路（2026-10-05 实测有效）:
  注册(EVM 钱包 EIP-191) -> 绑码【必须带 device_id】激活 23 小时
  订阅路径: POST /p2p-lite/v2/vpn/sessions/start(client_type) -> sub_url?session_id= -> 明文 sing-box 配置
  P2P 路径: rotate 后 token -> GET /p2p-lite/v1/nodes(目录) -> POST /p2p-lite/v2/dht/capability 逐节点
            -> AES-256-GCM 解密
  可达筛查: 服务器 TCP(+TLS) 探测（国内直连大多数 GCP IP 在 TCP 层被阻断，能连的进 reachable 清单）

v2 提速改造（2026-10-05）:
  1) 不同 client_type 的 session 并发建（实测 device_limit=2 内不互踢）；同一 client_type
     再建才会踢掉前一个，采集阶段一律不再重建 session
  2) 订阅取配置 与 P2P capability 并行；capability 用 --concurrency 并发跑
  3) 每拿到一个 server 立刻丢进探测池（边收边探），收尾只等没探完的，不再串行等两分钟
  4) HTTP 长连接复用（urllib 每请求重新握手，实测平均 1.8s -> 1.0s）
  5) 去掉逐节点 sleep(1.2) 与轮次 sleep(1)
  6) 瞬态失败（连接被重置 / 5xx）自动重试；原版一个 RemoteDisconnected 就整轮崩掉

服务端配额实测（决定上面的取值）:
  capability 成功数累计到 20~24 个开始回 429，窗口是分钟级。原地等待重试不会更快
  （试过：统一等 25s × 若干轮，5m29s 还没跑完）；所以策略改成撞墙两次立即收尾，
  已拿到的照写，剩余节点用 --p2p-wallets 换号续跑。请求起手间隔保持 1.2s
  （实测 33 个请求不触发限流），并发只用来盖单请求 ~2.6s 的延迟，不用来加大吞吐。
  同一沙箱同一账号 A/B：原版 122.7s（且没算可达筛查，用户侧 5m46s）-> v2 61.4s 全流程。

输出:
  out/all_nodes.json        全量结构化（两路合并，含 source/region 标记）
  out/all_uris.txt          全量 URI（vless:// + hysteria2://）
  out/reachable_uris.txt    【主交付】TCP(+TLS) 实测可达的 URI
  out/reachable_nodes.json  可达节点结构化数据 + 每个 server:port 的探测结果
  out/singbox_config.json   最新一份原始订阅配置
  iPoW.txt（脚本同目录）    给聚合器用的订阅文件，内容等于 reachable_uris.txt（--no-probe 时为全量）

依赖:
  pip install eth-account cryptography

用法:
  python ipow.py                     # 全流程（windows+android 订阅 + P2P 全目录 + 可达筛查）
  python ipow.py --no-probe          # 跳过可达筛查（只出全量）
  python ipow.py --skip-p2p          # 只拉订阅路径（5 个请求就出 90+ 节点，最快）
  python ipow.py --rounds 3          # 订阅路径多轮（收集移动池域名变体）
  python ipow.py --p2p-wallets 2     # 配额撞墙后换 2 个新钱包接着跑剩余节点
  python ipow.py --strict            # hysteria2 也要求 TLS 握手成功（默认只按 TCP 判定）
  环境变量 IPOW_BASE_URL 可把 API 指向中转地址
"""
import argparse
import base64
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone

from eth_account import Account
from eth_account.messages import encode_defunct

# GitHub Actions 里 Secret 没配时环境变量是空串，不能当成地址用
BASE = (os.environ.get("IPOW_BASE_URL") or "").strip().rstrip("/") or "https://ipow.ai"
UA = "iPoWVPN/4.3.5 (Android)"
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state")
OUT = os.path.join(ROOT, "out")
LINKS_FILE = os.path.join(ROOT, "iPoW.txt")

# capability 的 client_type 必须与 session 一致，否则 409
P2P_CLIENT_TYPE = "android"

# GCP region slug -> 中文地区名（订阅 tag 与 P2P 目录 id 都是这个格式）
GCP_REGION = {
    "africa-south1": "南非", "asia-east1": "台湾", "asia-east2": "香港", "asia-northeast1": "日本",
    "asia-northeast2": "韩国", "asia-northeast3": "韩国", "asia-south1": "印度", "asia-south2": "印度",
    "asia-southeast1": "新加坡", "asia-southeast2": "印度尼西亚", "australia-southeast1": "澳大利亚",
    "australia-southeast2": "澳大利亚", "europe-central2": "波兰", "europe-north1": "芬兰",
    "europe-southwest1": "西班牙", "europe-west1": "比利时", "europe-west2": "英国",
    "europe-west3": "德国", "europe-west4": "荷兰", "europe-west6": "瑞士", "europe-west8": "意大利",
    "europe-west9": "法国", "europe-west10": "德国", "europe-west12": "意大利",
    "me-west1": "以色列", "northamerica-northeast1": "加拿大", "southamerica-east1": "巴西",
    "southamerica-west1": "智利", "us-central1": "美国", "us-east1": "美国", "us-east4": "美国",
    "us-east5": "美国", "us-south1": "美国", "us-west1": "美国", "us-west2": "美国",
    "us-west3": "美国", "us-west4": "美国",
}

# transient: 连接被重置/超时/5xx —— 换连接重试即可，不是业务错误
_TRANSIENT = (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError,
              ConnectionAbortedError, BrokenPipeError, TimeoutError, socket.timeout, OSError)


def log(msg):
    print(msg, flush=True)


def b64d(s):
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- HTTP 层

class Http:
    """长连接 HTTPS 客户端：每 (线程, host) 一条连接复用，带限速、瞬态重试、429 退避。

    urllib 每个请求重新握手，实测单次请求平均 1.8s，复用连接后 1.0s。
    """

    def __init__(self, base=BASE, timeout=25, min_interval=0.9, retries=3,
                 max_wait=120, quiet=False):
        self.timeout = timeout
        self.retries = max(int(retries), 0)
        self.max_wait = max_wait
        self.quiet = quiet
        self.limiter = _RateLimiter(min_interval)
        self._local = threading.local()
        self._ctx = ssl.create_default_context()
        self.stats = {}
        self._slock = threading.Lock()
        self.base = self._split(base)
        host = self.base["host"]
        if not host or any(c.isspace() for c in host):
            raise ValueError("API 地址无法解析: %r（检查 --base-url 或环境变量 IPOW_BASE_URL）" % base)

    @staticmethod
    def _split(url):
        if "://" not in url:
            url = "https://" + url
        u = urllib.parse.urlsplit(url)
        return {"host": u.hostname, "port": u.port or (443 if u.scheme == "https" else 80),
                "secure": u.scheme == "https", "prefix": u.path.rstrip("/"),
                "netloc": u.netloc, "key": u.scheme + "://" + u.netloc}

    def _resolve(self, url):
        """把 path / 绝对 URL 归一成 (目标主机, 请求行路径)。"""
        if "://" in url:
            tgt = self._split(url)
            u = urllib.parse.urlsplit(url)
            path = (u.path or "/") + (("?" + u.query) if u.query else "")
            return tgt, path
        return self.base, self.base["prefix"] + url

    def _conn(self, tgt):
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns
        conn = conns.get(tgt["key"])
        if conn is None:
            cls = http.client.HTTPSConnection if tgt["secure"] else http.client.HTTPConnection
            kwargs = {"timeout": self.timeout}
            if tgt["secure"]:
                kwargs["context"] = self._ctx
            conn = cls(tgt["host"], tgt["port"], **kwargs)
            conns[tgt["key"]] = conn
        return conn

    def _drop(self, tgt):
        conns = getattr(self._local, "conns", {})
        conn = conns.pop(tgt["key"], None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def request(self, method, url, token=None, body=None, retries=None):
        """返回 (status, parsed)。429 原样返回，由调用方决定退避。

        retries 可以按调用点覆盖：订阅配置那一发带着全量节点，值得多试几次。
        """
        tgt, path = self._resolve(url)
        headers = {"User-Agent": UA, "Accept": "application/json", "Host": tgt["netloc"]}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        limit = self.retries if retries is None else max(int(retries), 0)
        last = None
        for attempt in range(limit + 1):
            self.limiter.wait()
            t = time.time()
            try:
                conn = self._conn(tgt)
                conn.request(method, path, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status, hdrs = resp.status, resp.headers
            except _TRANSIENT as exc:
                self._drop(tgt)
                last = "%s: %s" % (type(exc).__name__, exc)
                if attempt >= limit:
                    raise TransientError("%s %s 连接失败 %s" % (method, path, last))
                time.sleep(min(0.6 * (2 ** attempt), 6.0))
                continue
            except Exception as exc:                       # 未知异常同样按瞬态处理
                self._drop(tgt)
                last = "%s: %s" % (type(exc).__name__, exc)
                if attempt >= limit:
                    raise TransientError("%s %s 失败 %s" % (method, path, last))
                time.sleep(min(0.6 * (2 ** attempt), 6.0))
                continue
            with self._slock:
                key = "%s %s" % (method, (path.split("?")[0] or path)[:48])
                self.stats.setdefault(key, []).append(time.time() - t)
            txt = raw.decode("utf-8", "replace")
            try:
                parsed = json.loads(txt)
            except Exception:
                parsed = txt
            if status >= 500:
                if attempt >= limit:
                    raise TransientError("%s %s 服务端 %s %s" % (method, path, status, txt[:120]))
                time.sleep(min(0.5 * (2 ** attempt), 4.0))
                continue
            if status == 429:
                retry = hdrs.get("Retry-After") or hdrs.get("retry-after")
                try:
                    retry = int(float(retry))
                except (TypeError, ValueError):
                    retry = 20
                return status, {"error": {"code": "rate_limited",
                                          "retry_after": min(retry, self.max_wait)}}
            return status, parsed
        raise TransientError("%s %s 失败 %s" % (method, path, last))





class _RateLimiter:
    """全局最小间隔限速，避免并发把服务端打成 429。"""

    def __init__(self, min_interval):
        self.min_interval = max(float(min_interval), 0.0)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if delay > 0:
            time.sleep(delay)


class TransientError(RuntimeError):
    pass


# ---------------------------------------------------------------- 账号

def login(http, rec):
    st, ch = http.request("POST", "/v1/auth/wallet/challenge", body={"address": rec["address"]})
    acct = Account.from_key(rec["private_key"])
    sig = acct.sign_message(encode_defunct(text=ch["message"]))
    st, vr = http.request("POST", "/v1/auth/wallet/verify",
                          body={"address": rec["address"], "signature": "0x" + sig.signature.hex(),
                                "nonce": ch["nonce"]})
    return vr["token"], vr.get("subscription") or {}


def register_new(http, bind_code):
    acct = Account.create()
    addr = acct.address
    device_id = os.urandom(8).hex()
    st, ch = http.request("POST", "/v1/auth/wallet/challenge", body={"address": addr})
    sig = acct.sign_message(encode_defunct(text=ch["message"]))
    st, vr = http.request("POST", "/v1/auth/wallet/verify",
                          body={"address": addr, "signature": "0x" + sig.signature.hex(),
                                "nonce": ch["nonce"]})
    token = vr["token"]
    rec = {"address": addr, "private_key": acct.key.hex(), "device_id": device_id,
           "user_id": vr["user"]["id"],
           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if bind_code:
        st, bd = http.request("POST", "/v1/me/referral/bind", token=token,
                              body={"referral_code": bind_code, "device_id": device_id})
        rec["bind"] = {"code": bind_code, "status": st,
                       "usage_bonus_granted": bd.get("usage_bonus_granted") if isinstance(bd, dict) else None}
    path = os.path.join(STATE, "extract_accounts.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return rec, token


def account_expired(sub):
    exp = sub.get("expires_at", "")
    try:
        return datetime.fromisoformat(exp.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except Exception:
        return False


def load_usable_account(http, bind_code, try_count=6, workers=4):
    """最近的账号并发试登录，命中第一个 active 就用；都不行就注册新号。"""
    path = os.path.join(STATE, "extract_accounts.jsonl")
    recs = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    recs = list(reversed(recs))[:try_count]
    hit = {}
    if recs:
        with ThreadPoolExecutor(max_workers=min(workers, len(recs))) as ex:
            futs = {ex.submit(login, http, r): r for r in recs}
            done, pending = wait(futs, timeout=http.timeout * 2)
            for f in pending:
                f.cancel()
            for f in done:
                rec = futs[f]
                try:
                    token, sub = f.result()
                except Exception as exc:
                    log("[account] %s 登录失败: %s" % (rec["address"], str(exc)[:100]))
                    continue
                if sub.get("status") == "active" and not account_expired(sub):
                    prev = hit.get("rank")
                    rank = recs.index(rec)
                    if prev is None or rank < prev:
                        hit = {"rank": rank, "rec": rec, "token": token, "sub": sub}
                        log("[account] reuse %s (active until %s)" % (rec["address"], sub.get("expires_at", "")))
    if hit:
        return hit["rec"], hit["token"], hit["sub"]
    log("[account] registering new account...")
    rec, token = register_new(http, bind_code)
    token, sub = login(http, rec)
    log("[account] new: %s" % rec["address"])
    return rec, token, sub


# ---------------------------------------------------------------- 可达筛查

class ProbePool:
    """按需探测 (host, port)，结果缓存，节点边收集边探。

    TCP 是底线；vless-reality 再补一次 TLS 握手（复用同一条连接，不重复握 TCP）。
    hysteria2 走 QUIC，TCP 层无法证明可用（实测发 QUIC Version Negotiation
    触发包这些节点全部沉默），所以只按 TCP 判定，strict 模式下才要求 TLS。
    """

    def __init__(self, workers=24, tcp_timeout=4.0, tls_timeout=6.0,
                 sni="www.cloudflare.com", strict=False):
        self.workers = workers
        self.tcp_timeout = tcp_timeout
        self.tls_timeout = tls_timeout
        self.sni = sni
        self.strict = strict
        self._ex = ThreadPoolExecutor(max_workers=workers)
        self._lock = threading.Lock()
        self._results = {}
        self._futs = []

    def submit(self, host, port, need_tls):
        key = (host, port)
        with self._lock:
            if key in self._results or any(f[0] == key for f in self._futs):
                return
            fut = self._ex.submit(self._probe, host, port, need_tls)
            self._futs.append((key, fut))

    def _probe(self, host, port, need_tls):
        res = {"tcp": False, "tls": None, "ms": None}
        t = time.time()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            raw = socket.create_connection((host, port), timeout=self.tcp_timeout)
        except Exception:
            res["ms"] = round(time.time() - t, 2)
            return res
        res["tcp"] = True
        if need_tls or self.strict:
            try:
                raw.settimeout(self.tls_timeout)
                with ctx.wrap_socket(raw, server_hostname=self.sni):
                    res["tls"] = True
            except Exception:
                res["tls"] = False
            finally:
                try:
                    raw.close()
                except Exception:
                    pass
        else:
            try:
                raw.close()
            except Exception:
                pass
        res["ms"] = round(time.time() - t, 2)
        return res

    def wait(self, timeout=None):
        with self._lock:
            pending = [(k, f) for k, f in self._futs if not f.done()]
            done_keys = [k for k, f in self._futs if f.done()]
        for k in done_keys:
            self._harvest(k)
        if pending:
            log("[probe] waiting %d pending checks..." % len(pending))
            wait([f for _k, f in pending], timeout=timeout)
            for k, f in pending:
                self._harvest(k)
        self._ex.shutdown(wait=False)

    def _harvest(self, key):
        with self._lock:
            if key in self._results:
                return True
            pair = next(((k, f) for k, f in self._futs if k == key), None)
            if pair is None or not pair[1].done():
                return False
            try:
                self._results[key] = pair[1].result()
            except Exception as exc:
                self._results[key] = {"tcp": False, "tls": None, "ms": None, "err": str(exc)[:80]}
            return True

    def reachable(self, host, port, need_tls):
        with self._lock:
            res = self._results.get((host, port))
        if res is None:
            return False
        if not res["tcp"]:
            return False
        if self.strict or need_tls:
            return bool(res["tls"])
        return True

    def snapshot(self):
        with self._lock:
            return dict(self._results)


# ---------------------------------------------------------------- 采集

class Collector:
    """按 (type, server, uuid/password) 去重收集节点，新节点立刻送进探测池。"""

    def __init__(self, probe=None):
        self._lock = threading.Lock()
        self.nodes = {}
        self.probe = probe

    def add(self, node):
        port = node.get("port")
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        key = (node["type"], node["server"], node.get("uuid") or node.get("password"))
        with self._lock:
            fresh = key not in self.nodes
            if fresh:
                self.nodes[key] = node
        if fresh and self.probe is not None and node["server"] and port:
            self.probe.submit(node["server"], port, node["type"] != "hysteria2")
        return fresh

    def values(self):
        with self._lock:
            return list(self.nodes.values())


def node_uri(n, name=None):
    tag = name or n.get("tag") or ""
    if n["type"] == "vless":
        q = ("encryption=none&security=reality&sni=%s&fp=%s&pbk=%s&sid=%s&type=tcp"
             % (n["sni"], n.get("fp") or "chrome", n["pbk"], n["sid"]))
        if n.get("flow"):
            q += "&flow=" + n["flow"]
        return "vless://%s@%s:%s?%s#%s" % (n["uuid"], n["server"], n["port"], q,
                                           urllib.parse.quote(str(tag), safe=""))
    if n["type"] == "hysteria2":
        return "hysteria2://%s@%s:%s/?sni=%s#%s" % (
            n["password"], n["server"], n["port"],
            urllib.parse.quote(n.get("sni") or n["server"], safe=""),
            urllib.parse.quote(str(tag), safe=""))
    return None


# ---------------------------------------------------------------- 订阅路径

def start_session(http, token, device_id, country, client_type):
    st, ss = http.request("POST", "/p2p-lite/v2/vpn/sessions/start", token=token,
                          body={"client_type": client_type, "device_id": device_id,
                                "device_name": "Device", "client_version": "4.3.5",
                                "preferred_country": country})
    if st != 200 or not isinstance(ss, dict):
        return None, ss
    return (ss.get("session") or {}).get("id"), ss


def get_sub_url(http, token):
    st, rot = http.request("POST", "/v1/me/subscription/token/rotate", token=token, body={})
    if st != 200 or not isinstance(rot, dict):
        return None, None
    url = (rot.get("subscription") or {}).get("url") or rot.get("url")
    if not url:
        return None, None
    return url, urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]


def parse_sub_nodes(conf):
    nodes = []
    for o in conf.get("outbounds", []):
        t = o.get("type")
        if t == "vless":
            tls = o.get("tls") or {}
            reality = tls.get("reality") or {}
            nodes.append({
                "type": "vless", "source": "sub", "tag": o.get("tag"), "server": o.get("server"),
                "port": o.get("server_port"), "uuid": o.get("uuid"), "flow": o.get("flow") or "",
                "sni": tls.get("server_name"), "fp": (tls.get("utls") or {}).get("fingerprint"),
                "pbk": reality.get("public_key"), "sid": reality.get("short_id")})
        elif t == "hysteria2":
            tls = o.get("tls") or {}
            nodes.append({
                "type": "hysteria2", "source": "sub", "tag": o.get("tag"), "server": o.get("server"),
                "port": o.get("server_port"), "password": o.get("password"),
                "sni": tls.get("server_name")})
    return nodes


def sub_pull(http, token, rec, sub_url, country, client_type, rounds, collector, store,
             sid0=None, quiet=False):
    """订阅路径：session_id 决定拉到哪一份配置；首轮复用已建好的 session，避免互踢。"""
    for i in range(rounds):
        if i == 0 and sid0:
            sid = sid0
        else:
            sid, ss = start_session(http, token, rec["device_id"], country, client_type)
            if not sid:
                log("[sub:%s r%d] session failed: %s" % (client_type, i + 1, str(ss)[:140]))
                continue
        st, conf = http.request("GET", sub_url + "?session_id=" + sid, retries=6)
        if st != 200 or not isinstance(conf, dict) or "outbounds" not in conf:
            log("[sub:%s r%d] config fetch failed(%s): %s"
                % (client_type, i + 1, st, str(conf)[:140]))
            continue
        store["last_conf"] = conf
        nodes = parse_sub_nodes(conf)
        new = sum(1 for n in nodes if collector.add(n))
        log("[sub:%s r%d] %d nodes (%d new) | total %d"
            % (client_type, i + 1, len(nodes), new, len(collector.nodes)))
    return len(collector.nodes)


# ---------------------------------------------------------------- P2P 路径

class Throttle:
    """请求节奏与配额闸门。

    实测（2026-10-05）：capability 成功数累计到 20~24 个就开始回 429，窗口是分钟级，
    一次运行里反复重试不会提前拿到配额（试过：4 次统一等 25s 换来 5 分半）。
    所以这里的策略是：撞墙两次就判定本账号本轮配额用完，立即收尾，
    剩下的节点要么等窗口，要么用 --p2p-wallets 换号续跑。
    """

    def __init__(self, limiter, base_interval=1.2, max_interval=6.0,
                 max_429=2, gate_wait=12.0):
        self.limiter = limiter
        self.base_interval = base_interval
        self.max_interval = max_interval
        self.max_429 = max_429
        self.gate_wait = gate_wait
        self._lock = threading.Lock()
        self._until = 0.0
        self.hits = 0
        self.abort = threading.Event()

    def on_429(self, retry_after):
        """记录一次 429。返回 True 表示还可以继续，False 表示该收手。"""
        with self._lock:
            self.hits += 1
            self.limiter.min_interval = min(max(self.limiter.min_interval * 3.0, 2.0), self.max_interval)
            if self.hits >= self.max_429:
                self.abort.set()
                log("[throttle] 连续 %d 次 429，判定配额已用完，停止本轮 capability" % self.hits)
                return False
            wait_s = min(float(retry_after or 10), self.gate_wait)
            self._until = max(self._until, time.monotonic() + wait_s)
            log("[throttle] 429 -> 请求间隔 %.2fs，统一等 %.0fs" % (self.limiter.min_interval, wait_s))
            return True

    def gate(self):
        while True:
            with self._lock:
                left = self._until - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 2.0))


def decrypt_profile(resp):
    ep = resp.get("encrypted_profile")
    if not ep:
        raise ValueError("no encrypted_profile")
    keys = {k.get("key_id"): k.get("key") for k in (resp.get("profile_decryption_keys") or [])}
    key_str = keys.get(ep.get("key_id")) or resp.get("profile_decryption_key")
    if not key_str:
        raise ValueError("no decryption key")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pt = AESGCM(b64d(key_str)).decrypt(b64d(ep["nonce"]), b64d(ep["ciphertext"]), None)
    return json.loads(pt.decode("utf-8", "replace"))


def node_from_capability(prof):
    tls = prof.get("tls") or {}
    return {"type": "vless", "source": "p2p", "tag": prof.get("tag"), "server": prof.get("server"),
            "port": prof.get("server_port"), "uuid": prof.get("uuid"), "flow": prof.get("flow") or "",
            "sni": tls.get("server_name"), "fp": (tls.get("utls") or {}).get("fingerprint"),
            "pbk": (tls.get("reality") or {}).get("public_key"),
            "sid": (tls.get("reality") or {}).get("short_id")}


def capability_one(http, token, sid, sub_token, device_id, nid, cc, throttle):
    body = {"session_id": sid, "client_type": P2P_CLIENT_TYPE, "device_id": device_id,
            "country": cc or "AUTO", "capabilities": ["vless-reality:no-flow"],
            "subscription_token": sub_token, "allow_country_switch": True, "node_id": nid}
    throttle.gate()
    st, cap = http.request("POST", "/p2p-lite/v2/dht/capability", token=token, body=body)
    if st == 200 and isinstance(cap, dict):
        return cap, None
    code = (cap.get("error") or {}).get("code", "") if isinstance(cap, dict) else str(cap)[:60]
    last = "%s %s" % (st, code)
    if st == 429:
        retry = (cap.get("error") or {}).get("retry_after", 10) if isinstance(cap, dict) else 10
        if throttle.on_429(retry):
            throttle.gate()                        # 第一次撞墙：等一小会再试这一个
            st, cap = http.request("POST", "/p2p-lite/v2/dht/capability", token=token, body=body)
            if isinstance(cap, dict) and st == 200:
                return cap, None
        return None, "429 quota"
    if code == "p2p_dht_capability_unavailable":
        return None, last                          # 节点下线，重试只是白烧配额
    if st in (503, 500):                           # 服务端临时故障，补一次
        throttle.gate()
        st, cap = http.request("POST", "/p2p-lite/v2/dht/capability", token=token, body=body)
        if isinstance(cap, dict) and st == 200:
            return cap, None
        return None, "%s %s" % (st, (cap.get("error") or {}).get("code", "")
                                if isinstance(cap, dict) else "")
    return None, last


def p2p_catalog(http, cat_info):
    """拉 P2P 目录，顺带把地区信息存进 cat_info 供命名用。"""
    st, cat = http.request("GET", "/p2p-lite/v1/nodes?redacted=1")
    if st != 200 or not isinstance(cat, dict):
        log("[p2p] catalog failed: %s" % str(cat)[:140])
        return []
    entries = cat.get("nodes") or cat.get("entries") or []
    items = []
    for e in entries:
        nid = e.get("id") or e.get("node_id")
        if not nid:
            continue
        items.append((nid, e.get("country_code") or ""))
        cat_info[nid] = {"country_code": (e.get("country_code") or "").upper(),
                         "country": e.get("country") or "", "city": e.get("city") or "",
                         "protocols": e.get("protocols") or []}
    return items


def scrape_nodes(http, token, rec, sid, sub_token, items, collector, throttle,
                 workers=4, quiet=False, deadline=None, label="p2p"):
    """对给定节点列表逐个 capability。返回 (成功, 跳过, 未处理)。撞墙或超时立刻收手。"""
    ok = skip = 0
    leftover = []
    lock = threading.Lock()

    def one(idx_nid):
        nonlocal ok, skip
        idx, (nid, cc) = idx_nid
        if throttle.abort.is_set() or (deadline is not None and time.monotonic() > deadline):
            with lock:
                leftover.append((nid, cc))
            return
        cap, err = capability_one(http, token, sid, sub_token, rec["device_id"], nid, cc, throttle)
        with lock:
            if cap is None:
                skip += 1
                log("[%s %2d/%d] %-30s -> %s" % (label, idx + 1, len(items), nid, err))
                return
            try:
                node = node_from_capability(decrypt_profile(cap))
            except Exception as exc:
                skip += 1
                log("[%s %2d/%d] %-30s decrypt fail: %s" % (label, idx + 1, len(items), nid, exc))
                return
            fresh = collector.add(node)
            ok += 1
            if not quiet or fresh:
                log("[%s %2d/%d] %-30s -> %s" % (label, idx + 1, len(items), nid, node["server"]))

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
        list(ex.map(one, enumerate(items)))
    return ok, skip, leftover


def run_p2p_phase(http, args, token, rec, sid, sub_token, collector, cat_info, throttle,
                  workers=4, todo=None, label="p2p"):
    """一轮 P2P：目录（或指定清单）-> capability。返回 (成功, 跳过, 未处理)。"""
    if todo is None:
        items = p2p_catalog(http, cat_info)
        if not items:
            return 0, 0, []
        log("[%s] catalog: %d nodes, concurrency %d" % (label, len(items), workers))
    else:
        items = list(todo)
        log("[%s] 续跑剩余 %d 个节点" % (label, len(items)))
    deadline = time.monotonic() + args.p2p_deadline
    ok, skip, leftover = scrape_nodes(http, token, rec, sid, sub_token, items, collector,
                                      throttle, workers=workers, quiet=args.quiet,
                                      deadline=deadline, label=label)
    log("[%s] ok=%d skip=%d 未处理=%d (429 %d 次)" % (label, ok, skip, len(leftover), throttle.hits))
    return ok, skip, leftover


# ---------------------------------------------------------------- 地区名

def _cc_table():
    """优先复用仓库里的 naming.py（同一份国家代码表），单独拷走脚本时退回代码。"""
    try:
        import naming
        return dict(getattr(naming, "REGION_CN", {}))
    except Exception:
        return {}


CC_CN = _cc_table()
_TAG_SUFFIXES = ("-hy2", "-h2", "-reality", "-noflow")


def _slug_of(tag):
    base = tag
    for suf in _TAG_SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    if base.startswith("gcp-"):
        base = base[4:]
    parts = base.split("-")
    for i in range(len(parts), 1, -1):
        cand = "-".join(parts[:i])
        if cand in GCP_REGION:
            return cand
    return None


def region_of(node, cat_info):
    tag = str(node.get("tag") or "")
    base = tag
    for suf in _TAG_SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    info = cat_info.get(base) or cat_info.get(tag) or {}
    cc = (info.get("country_code") or "").upper()
    if cc and cc in CC_CN:
        return CC_CN[cc]
    slug = _slug_of(tag)
    if slug:
        return GCP_REGION[slug]
    return info.get("country") or ""


def apply_names(nodes, cat_info):
    """tag 是 gcp-asia-east1-1 这种内部 id，换成中文地区名，订阅里才可读。
    同名节点自动加序号（地区、地区2、地区3…），hy2 后缀保留在序号之后。"""
    counts = {}
    for n in nodes:
        region = region_of(n, cat_info)
        if not region:
            n["region"] = ""
            n["name"] = n.get("tag") or ""
            continue
        n["region"] = region
        suffix = "·H2" if n["type"] == "hysteria2" else ""
        c = counts.get(region, 0) + 1
        counts[region] = c
        n["name"] = (region if c == 1 else "%s%d" % (region, c)) + suffix
    # 保持输出稳定：按地区、序号排序
    nodes.sort(key=lambda n: (n.get("region") or "", n.get("name") or ""))


# ---------------------------------------------------------------- 主流程

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="iPoWVPN one-shot node extractor")
    ap.add_argument("--rounds", type=int, default=1, help="订阅路径每个 client_type 的轮数")
    ap.add_argument("--country", default="AUTO")
    ap.add_argument("--bind-code", default="849a64")
    ap.add_argument("--base-url", default=BASE,
                    help="API 地址，默认 https://ipow.ai；出口被风控时指向 cf_relay.mjs 中转")
    ap.add_argument("--client-types", default="windows,android")
    ap.add_argument("--no-probe", action="store_true", help="跳过可达筛查")
    ap.add_argument("--skip-p2p", action="store_true", help="跳过 P2P capability 路径")
    ap.add_argument("--concurrency", type=int, default=4, help="capability 并发度")
    ap.add_argument("--p2p-deadline", type=float, default=90.0,
                    help="单轮 capability 的时间上限，超了先把已拿到的写出去")
    ap.add_argument("--p2p-wallets", type=int, default=0,
                    help="配额撞墙后最多再注册几个新钱包续跑剩余节点（默认 0 = 不换号）")
    ap.add_argument("--probe-workers", type=int, default=24, help="可达探测并发度")
    ap.add_argument("--tcp-timeout", type=float, default=4.0)
    ap.add_argument("--tls-timeout", type=float, default=6.0)
    ap.add_argument("--probe-deadline", type=float, default=240.0, help="收尾等待探测结果的上限秒数")
    ap.add_argument("--timeout", type=float, default=25.0, help="单次 HTTP 请求超时")
    ap.add_argument("--min-interval", type=float, default=1.2,
                    help="全局请求起手最小间隔（实测 1.2s 跑满 33 个请求不触发限流）")
    ap.add_argument("--retries", type=int, default=3, help="瞬态失败重试次数")
    ap.add_argument("--max-wait", type=float, default=25.0, help="撞 429 时单次统一等待上限")
    ap.add_argument("--strict", action="store_true", help="hysteria2 也要求 TLS 握手成功")
    ap.add_argument("--no-stats", action="store_true", help="不打印接口耗时统计")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--links-scope", choices=["reachable", "all"], default="reachable",
                    help="iPoW.txt 的内容范围：reachable=只写探得通的（默认），all=全量节点")
    return ap.parse_args(argv)


def print_stats(http, t0, no_stats=False):
    if no_stats:
        return
    total = 0.0
    rows = []
    for key, vals in http.stats.items():
        total += sum(vals)
        rows.append((sum(vals), key, len(vals), max(vals)))
    rows.sort(reverse=True)
    print("\n==== 接口耗时 ====")
    for s, key, n, mx in rows[:10]:
        print("%-52s n=%-3d sum=%6.1fs avg=%5.2fs max=%5.2fs" % (key[:52], n, s, s / n, mx))
    print("接口累计 %.1fs | 墙钟 %.1fs | 平均并发度 %.1f"
          % (total, time.monotonic() - t0, total / max(time.monotonic() - t0, 0.001)))


def main(argv=None):
    args = parse_args(argv)
    t0 = time.monotonic()
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)

    base = (args.base_url or "").strip() or BASE
    if base != "https://ipow.ai":
        log("[base] %s" % base)
    http = Http(base, timeout=args.timeout, min_interval=args.min_interval,
                retries=args.retries, max_wait=args.max_wait, quiet=args.quiet)
    probe = None if args.no_probe else ProbePool(
        workers=args.probe_workers, tcp_timeout=args.tcp_timeout,
        tls_timeout=args.tls_timeout, strict=args.strict)
    collector = Collector(probe)
    store = {}
    cat_info = {}

    rec, token, sub = load_usable_account(http, args.bind_code)
    log("[account] status=%s plan=%s until=%s" % (
        sub.get("status"), sub.get("plan_id"), sub.get("expires_at")))
    if sub.get("status") != "active":
        log("[account] 订阅不是 active：出口 IP 被风控时新注册的号会直接是 inactive/phase1，"
            "capability 会回 402 subscription_inactive。换出口 IP 再跑（IPOW_BASE_URL 可指向中转）。")
    client_types = [c.strip() for c in args.client_types.split(",") if c.strip()]
    needed = list(dict.fromkeys(client_types + ([] if args.skip_p2p else [P2P_CLIENT_TYPE])))

    # 不同 client_type 的 session 实测不互踢（device_limit=2），并发建；
    # 同一个 client_type 再建才会踢掉前一个，所以采集阶段不再重建 session
    sessions = {}
    with ThreadPoolExecutor(max_workers=min(len(needed), 2) or 1) as ex:
        futs = {ex.submit(start_session, http, token, rec["device_id"], args.country, ct): ct
                for ct in needed}
        for f in as_completed(futs):
            ct = futs[f]
            try:
                sid, ss = f.result()
            except Exception as exc:
                log("[session:%s] 异常: %s" % (ct, str(exc)[:140]))
                continue
            if sid:
                sessions[ct] = sid
            else:
                log("[session:%s] failed: %s" % (ct, str(ss)[:140]))
    if not sessions:
        log("no session available")
        sys.exit(2)

    sub_url, sub_token = get_sub_url(http, token)
    if not sub_url:
        log("[sub] rotate 失败，订阅与 P2P 都缺 subscription_token，直接收工")
        client_types = []

    throttle = Throttle(http.limiter, base_interval=args.min_interval)

    def run_sub(ct):
        try:
            sub_pull(http, token, rec, sub_url, args.country, ct, args.rounds, collector, store,
                     sid0=sessions.get(ct), quiet=args.quiet)
        except Exception as exc:
            log("[sub:%s] 阶段异常: %s: %s" % (ct, type(exc).__name__, exc))

    def run_p2p():
        sid = sessions.get(P2P_CLIENT_TYPE)
        if not sid:
            log("[p2p] 没有 %s session，跳过" % P2P_CLIENT_TYPE)
            return
        try:
            _ok, _skip, todo = run_p2p_phase(http, args, token, rec, sid, sub_token,
                                             collector, cat_info, throttle,
                                             workers=max(args.concurrency, 1))
            # 配额是账号级、分钟级的：本号撞墙后换号续跑，比原地等窗口快得多
            extra = 0
            while todo and extra < args.p2p_wallets:
                extra += 1
                log("[p2p] 换第 %d 个新钱包续跑剩余 %d 个节点" % (extra, len(todo)))
                try:
                    rec2, token2 = register_new(http, args.bind_code)
                    token2, sub2 = login(http, rec2)
                    if sub2.get("status") != "active":
                        log("[p2p] 新号 %s 状态 %s（出口被风控时新号直接 inactive），放弃换号"
                            % (rec2["address"], sub2.get("status")))
                        break
                    sid2, _ss = start_session(http, token2, rec2["device_id"], args.country,
                                              P2P_CLIENT_TYPE)
                    if not sid2:
                        log("[p2p] 新号建 session 失败，放弃换号")
                        break
                    _url2, token2_sub = get_sub_url(http, token2)
                    if not token2_sub:
                        log("[p2p] 新号 rotate 失败，放弃换号")
                        break
                    throttle2 = Throttle(http.limiter, base_interval=args.min_interval,
                                         max_429=1)
                    run_p2p_phase(http, args, token2, rec2, sid2, token2_sub, collector,
                                  cat_info, throttle2, workers=max(args.concurrency, 1),
                                  todo=todo, label="p2p+w%d" % extra)
                    todo = []
                except Exception as exc:
                    log("[p2p] 换号失败: %s: %s" % (type(exc).__name__, exc))
                    break
        except Exception as exc:
            log("[p2p] 阶段异常: %s: %s" % (type(exc).__name__, exc))

    jobs = [(ct, (lambda c=ct: run_sub(c))) for ct in client_types if ct in sessions]
    p2p_job = None if (args.skip_p2p or not sub_token) else run_p2p
    if args.rounds > 1 and p2p_job is not None:
        # 多轮会重建 session，P2P 用的 session 可能被踢，只能先跑订阅
        with ThreadPoolExecutor(max_workers=max(len(jobs), 1)) as ex:
            list(ex.map(lambda item: item[1](), jobs))
        p2p_job()
    else:
        with ThreadPoolExecutor(max_workers=max(len(jobs) + (1 if p2p_job else 0), 1)) as ex:
            futs = [ex.submit(fn) for _name, fn in jobs]
            if p2p_job is not None:
                futs.append(ex.submit(p2p_job))
            for f in as_completed(futs):
                f.result()

    nodes = collector.values()
    if not nodes:
        log("no nodes collected")
        print_stats(http, t0, args.no_stats)
        sys.exit(2)
    nodes.sort(key=lambda n: (str(n["server"]), n["type"]))
    apply_names(nodes, cat_info)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if probe is not None:
        servers = sorted({(n["server"], int(n["port"])) for n in nodes if n["server"] and n["port"]})
        log("[probe] %d 个 server:port 已提交，等待剩余结果（上限 %.0fs）..."
            % (len(servers), args.probe_deadline))
        probe.wait(timeout=args.probe_deadline)
        snap = probe.snapshot()
        log("[probe] 完成 %d/%d | TCP 通 %d | TLS 通 %d"
            % (len(snap), len(servers), sum(1 for r in snap.values() if r["tcp"]),
               sum(1 for r in snap.values() if r["tls"])))

    with open(os.path.join(OUT, "all_nodes.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": ts, "account": rec["address"], "node_count": len(nodes),
                   "nodes": nodes}, f, ensure_ascii=False, indent=1)
    uris = [u for u in (node_uri(n, n.get("name")) for n in nodes) if u]
    with open(os.path.join(OUT, "all_uris.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(uris) + "\n")
    if store.get("last_conf"):
        with open(os.path.join(OUT, "singbox_config.json"), "w", encoding="utf-8") as f:
            json.dump(store["last_conf"], f, ensure_ascii=False, indent=1)
    print("\nDONE: %d nodes | all_nodes.json, all_uris.txt (%.1fs)" % (len(nodes), time.monotonic() - t0))

    deliver = uris
    if probe is not None:
        r_nodes = [n for n in nodes if probe.reachable(n["server"], int(n["port"]),
                                                     n["type"] != "hysteria2")]
        r_uris = [u for u in (node_uri(n, n.get("name")) for n in r_nodes) if u]
        deliver = r_uris
        header = ["# iPoWVPN reachable nodes (TCP+TLS verified) @ %s" % ts,
                  "# servers: %d/%d reachable" % (
                      len({n["server"] for n in r_nodes}), len({n["server"] for n in nodes})),
                  "# nodes: %d/%d" % (len(r_nodes), len(nodes))]
        with open(os.path.join(OUT, "reachable_uris.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(header + r_uris) + "\n")
        with open(os.path.join(OUT, "reachable_nodes.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_at": ts,
                       "probe": {"%s:%d" % k: v for k, v in sorted(probe.snapshot().items())},
                       "node_count": len(r_nodes), "nodes": r_nodes}, f, ensure_ascii=False, indent=1)
        print("[probe] reachable %d nodes -> reachable_uris.txt" % len(r_uris))

    # --links-scope 在下面统一决定 deliver；命名在 apply_names 里已经换成中文地区名
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(deliver) + "\n")
    print("[out] %s <- %d 条 (scope=%s)" % (os.path.relpath(LINKS_FILE, ROOT), len(deliver), args.links_scope))
    print_stats(http, t0, args.no_stats)
    print("\nfiles in %s:" % os.path.relpath(OUT, ROOT))
    for fn in sorted(os.listdir(OUT)):
        print("  ", fn)


if __name__ == "__main__":
    main()
