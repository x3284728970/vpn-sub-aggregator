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

# 城市名。命中就直接拿城市名当地区名（银狐的 `CA-多伦多` → 多伦多）
_CITY_NAMES = [
    # 日本
    "东京", "大阪", "名古屋", "埼玉", "横滨", "札幌", "福冈",
    # 韩国
    "首尔", "釜山", "仁川",
    # 台湾
    "台北", "新北", "彰化", "高雄", "台中", "台南",
    # 东南亚
    "雅加达", "曼谷", "吉隆坡", "马尼拉", "河内", "胡志明", "泗水", "仰光", "金边",
    # 南亚 / 中东
    "孟买", "班加罗尔", "新德里", "金奈", "海得拉巴", "卡拉奇", "达卡",
    "迪拜", "阿布扎比", "利雅得", "特拉维夫", "伊斯坦布尔", "安卡拉", "多哈",
    # 欧洲
    "法兰克福", "柏林", "慕尼黑", "巴黎", "马赛", "伦敦", "曼彻斯特",
    "阿姆斯特丹", "鹿特丹", "苏黎世", "日内瓦", "马德里", "巴塞罗那",
    "米兰", "罗马", "华沙", "斯德哥尔摩", "赫尔辛基", "雅典", "布拉格",
    "布达佩斯", "布鲁塞尔", "里斯本", "维也纳", "哥本哈根", "奥斯陆",
    "都柏林", "布加勒斯特", "索菲亚", "基辅", "莫斯科", "圣彼得堡",
    "卢森堡", "塔林", "里加", "维尔纽斯", "萨格勒布", "贝尔格莱德",
    # 北美
    "多伦多", "温哥华", "蒙特利尔", "卡尔加里",
    "洛杉矶", "圣何塞", "圣克拉拉", "西雅图", "达拉斯", "纽约",
    "芝加哥", "马纳萨斯", "凤凰城", "亚特兰大", "拉斯维加斯", "迈阿密",
    "硅谷", "波特兰", "丹佛", "休斯顿", "费城", "波士顿", "华盛顿",
    "阿什本", "盐湖城", "堪萨斯城", "哥伦布", "底特律", "明尼阿波利斯",
    "墨西哥城", "瓜达拉哈拉",
    # 南美
    "圣保罗", "里约", "布宜诺斯艾利斯", "圣地亚哥", "利马", "波哥大", "基多",
    # 大洋洲 / 非洲
    "悉尼", "雪梨", "墨尔本", "布里斯班", "珀斯", "奥克兰", "惠灵顿",
    "约翰内斯堡", "开普敦", "开罗", "拉各斯", "内罗毕", "卡萨布兰卡",
]

# 按长度倒序，避免「东京」抢占「日本东京」这种（两者不冲突，但长名优先更稳）
_CITY_ORDER = sorted(_CITY_NAMES, key=len, reverse=True)

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

    优先级：**城市名 → 中文地区关键词 → 旗帜 emoji → 国家代码**。

    城市排在地区前面是刻意的：源把城市写进名字时就按城市落地命名，
    银狐的 `CA-多伦多` → `多伦多`，比笼统的 `加拿大` 有用；速连的 `日本东京` → `东京`。
    中文关键词又优先于旗帜，因为实测部分厂商把所有节点都挂 🇨🇳 前缀
    （如「🇨🇳台湾专线01」），此时文案里的「台湾」才是真实落地，旗帜只是装饰。
    """
    raw = "  ".join(t for t in texts if t)
    if not raw:
        return UNKNOWN_REGION

    # 1) 城市名，长的优先
    for city in _CITY_ORDER:
        if city in raw:
            return city

    # 2) 中文地区关键词，长的优先
    for keyword, region in _CN_KEYWORDS:
        if keyword in raw:
            return region

    # 3) 旗帜 emoji（名字里没有中文线索时的主要依据）
    for cc in flag_to_cc(raw):
        if cc in REGION_CN:
            return REGION_CN[cc]

    # 4) 独立出现的国家代码词元，避免 DeVPN 里的 DE 被误判
    upper = {t.upper() for t in _tokens(raw)}
    for cc in sorted(upper & _CC_TOKENS):
        return REGION_CN[cc]

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


def build_name(source_tag, region, original):
    base = "{}{}".format(source_tag, region)
    for tag in KEEP_TAGS:
        if tag and tag in (original or ""):
            base += tag
            break
    return base


def apply(pairs):
    """pairs 是 (链接, 来源文件名) 序列，返回重命名并排好序的链接列表。

    名字全局唯一：同一 (来源, 地区, 档位) 的第二个节点起追加序号，如 `DE香港2`。
    识别不出地区的就保持「未知」，不猜、也不拿别的来源起的名字来顶。
    """
    used = {}
    named = []

    for link, source_file in pairs:
        tag = FILE_TAGS.get(source_file)
        if not tag:
            named.append(((source_file, 0, link), link))
            continue

        original = current_name(link)
        if original is None:
            named.append(((source_file, 0, link), link))
            continue

        region = detect_region(original)
        base = build_name(tag, region, original)
        seq = used.get(base, 0) + 1
        used[base] = seq
        name = base if seq == 1 else "{}{}".format(base, seq)
        # 排序键带上序号，否则字典序会把「DE日本10」排到「DE日本2」前面
        named.append(((base, seq, ""), with_name(link, name)))

    # 按名字排序：同一来源的节点挨在一起，组内按地区、地区内按序号
    named.sort(key=lambda item: item[0])
    return [link for _key, link in named]