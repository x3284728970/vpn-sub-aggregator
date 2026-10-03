"""节点重命名：把各来源脚本产出的节点统一改成「VPN名+地区」形式。

各来源脚本原来各写各的名字，风格完全不统一：

    __.py        🇭🇰香港专线01+下载专用
    lanmao.py    🇯🇵日本专线-01 专线优化
    Surfer.py    SF香港
    TF__.py      HK-香港 / HK-香港-Gold
    sulian.py    日本东京(YouTube,ChatGPT等)
    devpn.py     DeVPN-HK-0
    ipow.py      iPoW-日本-01

统一之后只保留两件信息：哪个 VPN、哪个地区。

    菜鸟香港   蓝猫日本   SF香港   银狐香港   银狐香港Gold
    速连日本   DE香港     iPoW香港

同一来源同一地区有多个节点时，第二个起追加 `-2`、`-3`（客户端与 Clash 都要求名字唯一）。
来源自己明确标注了档位的（如银狐的 Gold），保留成后缀，避免把两档线路静默合并。
"""

import base64
import json
import urllib.parse

# 产出文件名 → 节点名前缀
#
# 注意键是「节点输出文件」的名字，不是脚本名 —— 聚合器是按输出文件来的。
# 同一个来源可能有多个输出文件名（历史遗留），全部列上。
FILE_TAGS = {
    "菜鸟.txt": "菜鸟",
    "银狐.txt": "银狐", "TF__.txt": "银狐", "foxlink.txt": "银狐",
    "surfer.txt": "SF", "SF.txt": "SF",
    "sulian.txt": "速连", "速连.txt": "速连", "速连节点.txt": "速连",
    "nodes.txt": "DE", "devpn.txt": "DE",
    "蓝猫.txt": "蓝猫",
    "hongdun_nodes.txt": "红盾",
    "iPoW.txt": "iPoW",
    "蜂鸟.txt": "蜂鸟",
}

# 脚本名 → 节点名前缀（供 run_script / 文档使用，键是 scripts/ 下的文件名）
SOURCE_TAGS = {
    "__.py": "菜鸟",
    "TF__.py": "银狐",
    "Surfer.py": "SF",
    "sulian.py": "速连",
    "devpn.py": "DE",
    "lanmao.py": "蓝猫",
    "hongdun.py": "红盾",
    "ipow.py": "iPoW",
    "fengniao.py": "蜂鸟",
}

# 兼容旧调用
SOURCE_TAGS_BY_FILE = FILE_TAGS

# 源自己标注的档位后缀，原样保留
KEEP_TAGS = ("Gold",)

UNKNOWN_REGION = "未知"

# 国家/地区代码 → 中文名
REGION_CN = {
    "AE": "阿联酋", "AR": "阿根廷", "AT": "奥地利", "AU": "澳大利亚", "BE": "比利时",
    "BG": "保加利亚", "BR": "巴西", "CA": "加拿大", "CH": "瑞士", "CL": "智利",
    "CN": "中国大陆", "CO": "哥伦比亚", "CZ": "捷克", "DE": "德国", "DK": "丹麦",
    "EE": "爱沙尼亚", "EG": "埃及", "ES": "西班牙", "FI": "芬兰", "FR": "法国",
    "GB": "英国", "GR": "希腊", "HK": "香港", "HR": "克罗地亚", "HU": "匈牙利",
    "ID": "印度尼西亚", "IE": "爱尔兰", "IL": "以色列", "IN": "印度", "IS": "冰岛",
    "IT": "意大利", "JP": "日本", "KR": "韩国", "LT": "立陶宛", "LU": "卢森堡",
    "LV": "拉脱维亚", "MA": "摩洛哥", "MX": "墨西哥", "MY": "马来西亚", "NG": "尼日利亚",
    "NL": "荷兰", "NO": "挪威", "NZ": "新西兰", "PE": "秘鲁", "PH": "菲律宾",
    "PK": "巴基斯坦", "PL": "波兰", "PT": "葡萄牙", "QA": "卡塔尔", "RO": "罗马尼亚",
    "RS": "塞尔维亚", "RU": "俄罗斯", "SA": "沙特", "SE": "瑞典", "SG": "新加坡",
    "SK": "斯洛伐克", "TH": "泰国", "TR": "土耳其", "TW": "台湾", "UA": "乌克兰",
    "UK": "英国", "US": "美国", "VN": "越南", "ZA": "南非",
}

# 名字里的中文地区关键词。按长度倒序匹配，避免「印度」抢占「印度尼西亚」
_CN_KEYWORDS = [
    ("印度尼西亚", "印度尼西亚"), ("印度尼西亞", "印度尼西亚"),
    ("澳大利亚", "澳大利亚"), ("澳洲", "澳大利亚"), ("紐西蘭", "新西兰"),
    ("新西兰", "新西兰"), ("阿根廷", "阿根廷"), ("罗马尼亚", "罗马尼亚"),
    ("爱沙尼亚", "爱沙尼亚"), ("拉脱维亚", "拉脱维亚"), ("立陶宛", "立陶宛"),
    ("马来西亚", "马来西亚"), ("尼日利亚", "尼日利亚"), ("保加利亚", "保加利亚"),
    ("克罗地亚", "克罗地亚"), ("斯洛伐克", "斯洛伐克"), ("塞尔维亚", "塞尔维亚"),
    ("哥伦比亚", "哥伦比亚"), ("巴基斯坦", "巴基斯坦"), ("斯里兰卡", "斯里兰卡"),
    ("新加坡", "新加坡"), ("加拿大", "加拿大"), ("墨西哥", "墨西哥"), ("葡萄牙", "葡萄牙"),
    ("西班牙", "西班牙"), ("乌克兰", "乌克兰"), ("俄罗斯", "俄罗斯"), ("土耳其", "土耳其"),
    ("以色列", "以色列"), ("卡塔尔", "卡塔尔"), ("菲律宾", "菲律宾"), ("越南", "越南"),
    ("泰国", "泰国"), ("波兰", "波兰"), ("瑞典", "瑞典"), ("瑞士", "瑞士"),
    ("芬兰", "芬兰"), ("挪威", "挪威"), ("丹麦", "丹麦"), ("冰岛", "冰岛"),
    ("希腊", "希腊"), ("捷克", "捷克"), ("奥地利", "奥地利"), ("比利时", "比利时"),
    ("荷兰", "荷兰"), ("法国", "法国"), ("德国", "德国"), ("英国", "英国"),
    ("美国", "美国"), ("韩国", "韩国"), ("日本", "日本"), ("印度", "印度"),
    ("台湾", "台湾"), ("台灣", "台湾"), ("香港", "香港"), ("澳门", "澳门"),
    ("南非", "南非"), ("埃及", "埃及"), ("巴西", "巴西"), ("智利", "智利"),
    ("秘鲁", "秘鲁"), ("意大利", "意大利"), ("爱尔兰", "爱尔兰"), ("匈牙利", "匈牙利"),
    ("卢森堡", "卢森堡"), ("摩洛哥", "摩洛哥"), ("沙特", "沙特"), ("阿联酋", "阿联酋"),
    ("中国大陆", "中国大陆"), ("中国", "中国大陆"),
]

# 城市名 → 地区（只在没有更直接的地区信息时兜底）
_CITY_TO_REGION = {
    "东京": "日本", "大阪": "日本", "埼玉": "日本", "名古屋": "日本",
    "首尔": "韩国", "釜山": "韩国",
    "新加坡": "新加坡", "狮城": "新加坡",
    "台北": "台湾", "新北": "台湾", "彰化": "台湾", "高雄": "台湾",
    "孟买": "印度", "班加罗尔": "印度", "新德里": "印度",
    "圣保罗": "巴西", "里约": "巴西",
    "法兰克福": "德国", "柏林": "德国",
    "巴黎": "法国", "马赛": "法国",
    "伦敦": "英国", "曼彻斯特": "英国",
    "阿姆斯特丹": "荷兰", "苏黎世": "瑞士", "马德里": "西班牙",
    "米兰": "意大利", "华沙": "波兰", "斯德哥尔摩": "瑞典",
    "赫尔辛基": "芬兰", "雅典": "希腊", "布拉格": "捷克",
    "布达佩斯": "匈牙利", "布鲁塞尔": "比利时", "里斯本": "葡萄牙",
    "维也纳": "奥地利", "哥本哈根": "丹麦", "奥斯陆": "挪威",
    "都柏林": "爱尔兰", "布加勒斯特": "罗马尼亚", "基辅": "乌克兰",
    "莫斯科": "俄罗斯", "伊斯坦布尔": "土耳其", "特拉维夫": "以色列",
    "迪拜": "阿联酋", "开罗": "埃及", "约翰内斯堡": "南非",
    "多伦多": "加拿大", "温哥华": "加拿大", "蒙特利尔": "加拿大",
    "洛杉矶": "美国", "圣何塞": "美国", "西雅图": "美国", "达拉斯": "美国",
    "纽约": "美国", "芝加哥": "美国", "凤凰城": "美国", "亚特兰大": "美国",
    "拉斯维加斯": "美国", "迈阿密": "美国", "硅谷": "美国",
    "悉尼": "澳大利亚", "墨尔本": "澳大利亚", "布里斯班": "澳大利亚",
    "奥克兰": "新西兰", "雅加达": "印度尼西亚", "曼谷": "泰国",
    "吉隆坡": "马来西亚", "马尼拉": "菲律宾", "河内": "越南", "胡志明": "越南",
    "墨西哥城": "墨西哥", "圣地亚哥": "智利", "布宜诺斯艾利斯": "阿根廷",
}

# 名字里出现这些词说明节点是自动/负载均衡入口，没有固定地区
_AUTO_KEYWORDS = ("自动选择", "自动", "auto", "loadbalance", "负载均衡", "随机")

# 两字母代码候选表：只在名字里以独立词元出现时才认，避免 DeVPN 里的 DE 被误判
_CC_TOKENS = set(REGION_CN)

_SEPARATORS = " -_/|,.()（）[]【】+*:：\t"


def _b64pad(s):
    s = s.replace("-", "+").replace("_", "/")
    return s + "=" * (-len(s) % 4)


def flag_to_cc(text):
    """把旗帜 emoji 还原成国家代码。🇭🇰 = U+1F1ED U+1F1F0 → HK。"""
    out = []
    i = 0
    while i < len(text) - 1:
        a, b = ord(text[i]), ord(text[i + 1])
        if 0x1F1E6 <= a <= 0x1F1FF and 0x1F1E6 <= b <= 0x1F1FF:
            out.append(chr(a - 0x1F1E6 + 65) + chr(b - 0x1F1E6 + 65))
            i += 2
        else:
            i += 1
    return out


def _tokens(text):
    for sep in _SEPARATORS:
        text = text.replace(sep, " ")
    return [t for t in text.split(" ") if t]


def detect_region(*texts):
    """从候选文本里识别地区，返回中文地区名。识别不出来返回 UNKNOWN_REGION。

    中文地区关键词优先于旗帜 emoji：实测部分厂商把所有节点的名字都挂上 🇨🇳 前缀
    （如「🇨🇳台湾专线01」），此时文案里的「台湾」才是真实落地，旗帜只是装饰。
    """
    raw = "  ".join(t for t in texts if t)
    if not raw:
        return UNKNOWN_REGION

    # 1) 中文地区关键词，长的优先
    for keyword, region in _CN_KEYWORDS:
        if keyword in raw:
            return region

    # 2) 旗帜 emoji（名字里没有中文地区时的主要线索）
    for cc in flag_to_cc(raw):
        if cc in REGION_CN:
            return REGION_CN[cc]

    # 3) 独立出现的国家代码词元，避免 DeVPN 里的 DE 被误判
    upper = {t.upper() for t in _tokens(raw)}
    for cc in sorted(upper & _CC_TOKENS):
        return REGION_CN[cc]

    # 4) 城市名兜底
    for city, region in _CITY_TO_REGION.items():
        if city in raw:
            return region

    # 5) 自动/负载均衡入口
    lowered = raw.lower()
    for kw in _AUTO_KEYWORDS:
        if kw in lowered:
            return "自动"

    return UNKNOWN_REGION


def _vmess_payload(link):
    try:
        data = json.loads(base64.b64decode(_b64pad(link[8:])).decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def current_name(link):
    """取链接当前的节点名。解析失败返回 None。"""
    if link.startswith("vmess://"):
        data = _vmess_payload(link)
        return None if data is None else str(data.get("ps") or "")
    if "#" in link:
        return urllib.parse.unquote(link.split("#", 1)[1])
    return ""


def with_name(link, name):
    """把链接的节点名换成 name，协议本体不动。"""
    if link.startswith("vmess://"):
        data = _vmess_payload(link)
        if data is None:
            return link
        data["ps"] = name
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return "vmess://" + base64.b64encode(body).decode("ascii")
    base = link.split("#", 1)[0] if "#" in link else link
    return base + "#" + urllib.parse.quote(name, safe="")


def build_name(source_tag, region, originals):
    base = "{}{}".format(source_tag, region)
    for tag in KEEP_TAGS:
        if tag and any(tag in (o or "") for o in originals):
            base += tag
            break
    return base


def apply(pairs):
    """pairs 是 (链接, 来源文件名[, 同名端点的所有原始名字]) 序列。

    返回重命名并排好序的链接列表。名字全局唯一：同一 (来源, 地区, 档位)
    的第二个节点起追加 -2、-3。

    同一个端点常被多个来源同时收录，去重后只留先收集到的那条。此时把各来源
    给这个端点起过的名字都拿来做地区识别，避免先到的那个名字信息不全
    （例如先到的名字是「SF未知」，但另一来源叫它「香港中转」）。
    """
    used = {}
    named = []

    for item in pairs:
        link, source_file = item[0], item[1]
        alt_names = item[2] if len(item) > 2 else None

        tag = FILE_TAGS.get(source_file)
        if not tag:
            named.append(link)
            continue

        primary = current_name(link)
        if primary is None:
            named.append(link)
            continue

        candidates = [primary] + [n for n in (alt_names or []) if n and n != primary]
        region = UNKNOWN_REGION
        for cand in candidates:
            region = detect_region(cand)
            if region != UNKNOWN_REGION:
                break

        base = build_name(tag, region, candidates)
        seq = used.get(base, 0) + 1
        used[base] = seq
        name = base if seq == 1 else "{}-{}".format(base, seq)
        named.append(with_name(link, name))

    # 按名字排序：同一来源的节点挨在一起，组内按地区
    return sorted(named, key=lambda l: current_name(l) or l)