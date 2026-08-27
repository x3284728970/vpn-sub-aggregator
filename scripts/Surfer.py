import json
import uuid
import urllib.request
import urllib.error
import urllib.parse
import base64
import re

from Crypto.Cipher import AES

AES_KEY = b'999f75dcb1b89a6c528dfn33df4f70d1'
AES_IV = b'5k2f5cjt9a4c1c99'

OSS_CONFIG_URL = 'https://qiyuqiyu.oss-ap-northeast-1.aliyuncs.com/200255.log'
API_BASE_FALLBACK = 'http://8.210.52.158/apiV2'

API_BASE = API_BASE_FALLBACK


def unpad(data):
    return data[:-data[-1]]


def decrypt_response(b64):
    try:
        raw = base64.b64decode(b64)
        cipher = AES.new(AES_KEY[:32], AES.MODE_CBC, AES_IV)
        return unpad(cipher.decrypt(raw)).decode()
    except:
        return b64


def pad_b64(s):
    return s + '=' * (-len(s) % 4)


def decode_b64(s):
    try:
        return base64.b64decode(pad_b64(s)).decode('utf-8', errors='ignore')
    except:
        return ""


def encode_b64(s):
    return base64.b64encode(s.encode('utf-8')).decode('utf-8')


def get_node_identity(link):
    try:
        if link.startswith(('vmess://', 'ssr://')):
            b64_part = link.split('://')[1]
            decoded = decode_b64(b64_part.replace('-', '+').replace('_', '/'))
            if link.startswith('vmess://'):
                data = json.loads(decoded)
                return f"{data.get('add')}:{data.get('port')}"
            else:
                host_port = decoded.split(':')[0:2]
                return ":".join(host_port)

        elif link.startswith(('vless://', 'trojan://', 'ss://')):
            url_part = link.split('://')[1]
            if '@' in url_part:
                url_part = url_part.split('@')[1]
            host_port = url_part.split('/')[0].split('?')[0].split('#')[0]
            return host_port
    except:
        pass
    return str(uuid.uuid4())


def fetch_api_base():
    try:
        res = urllib.request.urlopen(OSS_CONFIG_URL, timeout=10)
        data = json.loads(res.read().decode())
        return data.get('log', API_BASE_FALLBACK)
    except:
        return API_BASE_FALLBACK


def post(api_path, headers):
    url = API_BASE + api_path
    req = urllib.request.Request(url, method='POST')
    for k, v in headers.items():
        req.add_header(k, str(v))
    req.add_header('User-Agent', 'okhttp/4.9.0')
    req.add_header('Content-Length', '0')
    try:
        res = urllib.request.urlopen(req, timeout=15)
        return res.read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()
    except Exception as e:
        return str(e)


def parse_response(raw):
    try:
        return json.loads(decrypt_response(raw))
    except:
        try:
            return json.loads(raw)
        except:
            return {}


def traveler_login(device_id):
    return parse_response(post('/traveler_login', {
        "deviceId": device_id,
        "devicetype": "2",
        "devicename": "iPhone",
        "language": "1",
        "authtoken": "",
        "token": ""
    }))


def node_list(authtoken, jwt, device_id):
    return parse_response(post('/node_list', {
        "deviceId": device_id,
        "devicetype": "2",
        "devicename": "iPhone",
        "language": "1",
        "authtoken": authtoken,
        "token": jwt
    }))


def extract_nodes(res):
    raw_nodes = []
    if isinstance(res.get('data'), list):
        for g in res['data']:
            raw_nodes += g.get('node', [])

    seen_identities = set()
    unique_nodes = []
    for link in raw_nodes:
        if link.startswith('sub://'):
            continue
        identity = get_node_identity(link)
        if identity not in seen_identities:
            seen_identities.add(identity)
            unique_nodes.append(link)
    return unique_nodes


def get_region(name):
    match = re.search(r'(香港|台湾|新加坡|日本|美国|韩国|英国|俄罗斯|印度|法国|德国|荷兰|澳洲|加拿大|阿根廷|土耳其|巴西|意大利|HK|TW|SG|JP|US|KR|UK|IT)', name, re.I)
    if match:
        region = match.group(1).upper()
        mapping = {"HK": "香港", "TW": "台湾", "SG": "新加坡", "JP": "日本", "US": "美国", "KR": "韩国", "UK": "英国", "IT": "意大利"}
        return mapping.get(region, region)
    return "未知"


def rename_link(link):
    try:
        if link.startswith('vmess://'):
            data = json.loads(decode_b64(link[8:]))
            data['ps'] = f"SF{get_region(data.get('ps', ''))}"
            return 'vmess://' + encode_b64(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
        elif link.startswith(('vless://', 'trojan://', 'ss://')):
            base, fragment = link.split('#', 1) if '#' in link else (link, "")
            new_name = f"SF{get_region(urllib.parse.unquote(fragment))}"
            return f"{base}#{urllib.parse.quote(new_name)}"
    except:
        pass
    return link


def count_nodes_by_region(nodes):
    region_count = {}
    for link in nodes:
        try:
            if link.startswith('vmess://'):
                data = json.loads(decode_b64(link[8:]))
                region = get_region(data.get('ps', ''))
            elif '#' in link:
                fragment = link.split('#')[1]
                region = get_region(urllib.parse.unquote(fragment))
            else:
                region = "未知"
            region_count[region] = region_count.get(region, 0) + 1
        except:
            region_count["未知"] = region_count.get("未知", 0) + 1
    return region_count


def main():
    global API_BASE
    API_BASE = fetch_api_base()
    device_id = str(uuid.uuid4())

    login = traveler_login(device_id)
    if not login.get("data"):
        print("登录失败")
        return

    # 打印用户信息
    user_data = login["data"]
    print("=" * 50)
    print("用户信息:")
    print(f"  设备ID: {device_id}")
    print(f"  认证令牌: {user_data.get('token', 'N/A')[:20]}...")
    if 'user' in user_data:
        print(f"  用户: {user_data['user']}")
    print("=" * 50)

    nodes = extract_nodes(node_list(login["data"]["token"], login["data"]["auth_data"], device_id))
    if not nodes:
        print("未获取到节点")
        return

    # 统计各区域节点数量
    region_stats = count_nodes_by_region(nodes)
    print("\n节点统计信息:")
    total = 0
    for region, count in sorted(region_stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {region}: {count} 个")
        total += count
    print(f"  总计: {total} 个节点")
    print("=" * 50)

    # 重命名并保存到文件
    renamed = [rename_link(n) for n in nodes]
    renamed.sort(key=lambda x: 0 if "SF新加坡" in x else 1)

    # 只保存节点链接到 surfer.txt，每行一个
    with open('surfer.txt', 'w', encoding='utf-8') as f:
        for link in renamed:
            f.write(link + '\n')

    print(f"\n节点链接已保存到: surfer.txt (共 {len(renamed)} 个节点)")


if __name__ == '__main__':
    main()