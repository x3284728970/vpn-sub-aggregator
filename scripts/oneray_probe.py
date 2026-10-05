# OneRay 后端探测：从 GitHub runner（美国 IP）验证白标 API 可达性与协议
# 目标全部来自 libapp.so 静态还原，无猜测成分
import json
import ssl
import urllib.request
import urllib.error
import socket
import time
import random

UA = "OneRay-Android"
TIMEOUT = 20

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def http(method, url, body=None, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout, context=ctx) as r:
            raw = r.read(4000)
            return {"url": url, "status": r.status, "ms": int((time.time() - t0) * 1000),
                    "headers": dict(r.headers), "body": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read(4000)
        return {"url": url, "status": e.code, "ms": int((time.time() - t0) * 1000),
                "body": raw.decode("utf-8", "replace")}
    except Exception as e:
        return {"url": url, "error": type(e).__name__ + ": " + str(e)[:180],
                "ms": int((time.time() - t0) * 1000)}


def brief(r, keep_headers=()):
    out = {k: v for k, v in r.items() if k in ("url", "status", "error", "ms", "body")}
    for h in keep_headers:
        if "headers" in r and h in r["headers"]:
            out["h_" + h] = r["headers"][h][:120]
    if "body" in out and len(out["body"]) > 600:
        out["body"] = out["body"][:600] + "...<truncated>"
    return out


API_HOSTS = [
    "https://api.inkspindle.com",
    "https://api-hk.inkspindle.com",
    "https://api.gsldone.com",
]

results = {}

# 1. 更新源与站点
results["appcast"] = brief(http("GET", "https://www.gsldone.com/oneray/android/appcast.xml"))
results["releases_json"] = brief(http("GET", "https://dl.inkspindle.com/dengta/android/releases.json"))
results["site_home"] = brief(http("GET", "https://www.gsldone.com/"))

# 2. 各面板域名的灯塔公共配置
for i, host in enumerate(API_HOSTS):
    results[f"comm_config_{i}"] = brief(http("GET", host + "/api/v1/guest/comm/config"))

# 3. 游客登录（device_id 用明显随机值，不与真实用户冲突）
dev = "probe%08x%08x" % (random.getrandbits(32), random.getrandbits(32))
token = None
login = brief(http("POST", API_HOSTS[0] + "/api/v1/guest/gsl_guest/login",
                   {"device_id": dev, "platform": "android"}))
results["guest_login"] = login
if login.get("status") == 200:
    try:
        j = json.loads(login["body"])
        d = j.get("data") or {}
        token = d.get("auth_data") or d.get("token")
    except Exception:
        pass

# 4. 节点目录（有无 token 各打一次）
results["catalog_noauth"] = brief(http("GET", API_HOSTS[0] + "/api/v1/guest/gsl_guest/catalog"))
if token:
    auth = {"auth_data": token, "Authorization": token}
    results["catalog_auth"] = brief(http("GET", API_HOSTS[0] + "/api/v1/guest/gsl_guest/catalog", headers=auth))
    results["account_auth"] = brief(http("GET", API_HOSTS[0] + "/api/v1/user/getSubscribe", headers=auth))

print("=== PROBE RESULT JSON ===")
print(json.dumps(results, ensure_ascii=False, indent=1)[:12000])
