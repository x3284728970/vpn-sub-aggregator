# VPN 节点聚合订阅

每 48 小时自动运行一次，聚合 8 个来源的节点并生成统一订阅。

## 节点命名

各来源脚本原来各写各的名字，风格完全不统一：

```
__.py        🇭🇰香港专线01+下载专用
lanmao.py    🇯🇵日本专线-01 专线优化
Surfer.py    SF香港
TF__.py      HK-香港 / HK-香港-Gold
sulian.py    日本东京(YouTube,ChatGPT等)
devpn.py     DeVPN-HK-0
ipow.py      iPoW-日本-01
```

`naming.py` 在聚合时统一重写成 **`VPN名+地区`**：

```
菜鸟香港   蓝猫日本   SF香港   银狐香港   银狐香港Gold
速连日本   DE香港     iPoW香港   iPoW香港-2
```

| 前缀 | 来源脚本 | 产出文件 |
|------|----------|----------|
| `菜鸟` | `__.py` | `菜鸟.txt` |
| `银狐` | `TF__.py` | `银狐.txt` |
| `SF` | `Surfer.py` | `surfer.txt` |
| `速连` | `sulian.py` | `速连节点.txt` |
| `DE` | `devpn.py` | `nodes.txt` |
| `蓝猫` | `lanmao.py` | `蓝猫.txt` |
| `红盾` | `hongdun.py` | `hongdun_nodes.txt` |
| `iPoW` | `ipow.py` | `iPoW.txt` |

规则细节：

- **地区识别**按 中文关键词 → 旗帜 emoji → 国家代码 → 城市名 的顺序。中文关键词优先，
  因为实测部分厂商把所有节点都挂上 `🇨🇳` 前缀（如「🇨🇳台湾专线01」），此时文案里的
  「台湾」才是真实落地，旗帜只是装饰。
- **重名**：同一 VPN 同一地区的第二个节点起追加 `-2`、`-3`。客户端与 Clash 都要求
  代理名唯一，不这么做 Clash 会直接报错。
- **档位**：来源自己标注了档位的保留后缀，如银狐的 `银狐香港Gold`，避免把两档线路静默合并。
- **自动/负载均衡入口**没有固定地区，命名为 `速连自动`。
- 同一个端点常被多个来源同时收录，去重后只留先收集到的那条。此时会把各来源给这个
  端点起过的名字**都**拿来做地区识别，避免先到的名字信息不全（实测有条节点先到的名字是
  `SF未知`，靠另一来源的「马来西亚直连」补回了 `SF马来西亚`）。
- 改前缀或增删来源：编辑 `naming.py` 的 `FILE_TAGS`。

## 订阅方式（二选一）

### 方式 A：GitHub Gist（推荐）
不需要仓库公开，不需要 Pages，私有 Gist 直链直接当订阅链接。

**优点：**
- 不需要把仓库设为 Public
- 不需要配置 GitHub Pages
- 直链短，直接导入客户端

**步骤：**

1. 创建 GitHub Personal Access Token（Classic）：
   - __Settings → Developer settings → Personal access tokens → Tokens (classic)__
   - 勾选 `gist` 权限即可
   - 复制 token

2. 仓库 **Settings → Secrets and variables → Actions** → 添加：
   - `MY_GITHUB_TOKEN` = 第 1 步的 PAT

3. **Actions → VPN Sub Aggregator → Run workflow**，跑完后查看日志，会输出类似：
   ```
   [GIST] 创建成功，raw 链接: https://gist.githubusercontent.com/raw/xxx/jvhe.txt
   ```

4. 把那个 raw 链接粘贴到 Shadowrocket / Clash / V2RayN 等客户端即可

### 方式 B：GitHub Pages（已移除）
如果后续需要，可以把 Pages 部署加回来，但默认只走 Gist。

## 目录
```
.
├── scripts/
│   ├── __.py          # 菜鸟
│   ├── TF__.py        # 银狐 / foxlink
│   ├── Surfer.py      # 冲浪者
│   ├── sulian.py      # 速连 VPN
│   ├── devpn.py       # DeVPN
│   ├── lanmao.py      # 蓝猫 VPN
│   ├── hongdun.py     # 红盾 VPN（接口已不下发节点，实际产出 0 个）
│   ├── ipow.py        # iPoW.ai（并发优化版）
│   └── fengniao.py    # 蜂鸟加速器（未接入聚合，单独运行）
├── naming.py          # 统一节点命名：VPN名+地区
├── aggregate.py       # 聚合主脚本
├── purge_old_sub.py   # 旧订阅作废脚本
├── requirements.txt
├── .github/workflows/aggregate.yml
└── README.md
```

## iPoW 来源（`scripts/ipow.py`）

自动注册 Web3 钱包 → 建立会话 → 逐个拉取并解密 VLESS-Reality 节点，输出按地区重命名的节点。

### 输出文件（都在 `scripts/` 下）

| 文件 | 用途 |
|------|------|
| `iPoW.txt` | 纯 `vless://` 链接，聚合器直接收集 |
| `iPoW_sub_base64.txt` | Base64 订阅，可当独立订阅导入 |
| `iPoW_clash.yaml` | Clash Meta / Mihomo 配置 |
| `iPoW_singbox.json` | Sing-box outbounds |
| `iPoW_nodes.json` | 节点全量元数据 |
| `iPoW_wallets.json` | 用过的钱包记录（按地址合并，不覆盖历史） |
| `iPoW_unusable.json` | 不可用节点缓存，默认 6 小时 |

### 限流语义（实测，决定脚本策略）

这是这套接口最需要注意的地方：

- 限流是**出口 IP 级滑动窗口**，不是单纯的账号配额。窗口充足时单个账号能取 **24 个**节点；
  随着本机累计用量上升，新账号可取数量会下降（实测掉到 **16 个**）。
- 超限返回 `429` 并带 `Retry-After`，该值是**递减的剩余窗口**（实测 293 → 233 → 172 → 112 → 52），
  约 **300 秒后恢复**。
- 继续猛注册账号会触发风控：新账号直接不发试用订阅，返回
  `status: inactive / plan_id: phase1 / provider: none`，capability 接口回 `402 subscription_inactive`。

所以脚本的处理是：**撞限流就原地等窗口**（默认 `--on-429 wait`），而不是换钱包。
换号解决不了 IP 级限流，只会把钱包额度烧成死账号并加速风控。
注册后还会立刻校验订阅状态，一旦发现风控停发试用就当场收手。

### 相对原版的优化

| 项 | 原版 | 现在 |
|----|------|------|
| 拉取方式 | 串行，每节点固定 sleep | 6 线程并发，全局限速器兜底 |
| 24 个节点耗时 | 约 24 秒 | **约 1.5 秒** |
| 撞 429 | 无脑换钱包 | 原地等窗口（可 `--on-429 switch` 切回） |
| 订阅被风控 | 继续注册，直到钱包上限耗尽 | 注册后校验状态，当场停止 |
| 不可用节点 | 每次重跑都重新探测，实测 47 个里 7 个不可用，白烧 15% 配额 | 带 TTL 的缓存文件，跨次跳过 |
| 服务端排除列表 | 忽略 | 采纳 `route_failure_exclude_node_ids` |
| 网络异常/5xx | 一次性永久拉黑节点 | 退避重试，只有业务拒绝才拉黑 |
| `--wallets N` | 实际会用 N+1 个 | 修正 |
| 钱包记录 | 每次运行整份覆盖 | 按地址合并 |
| 节点名 | `iPoW-ZA-gcp-africa-south1-za-1` | `iPoW南非` |

> 红盾（`hongdun.py`）目前拿不到节点：`node.getNodeList` 只返回 `[{"recommend": 1}]`，
> 不再下发真实节点数据，脚本拿到 1 个空节点、生成 0 条链接。留着不影响聚合，
> 想干净些可以把 `aggregate.py` 里的 `run_script("hongdun.py")` 注释掉。

### 单独运行

```bash
cd scripts
python3 ipow.py                      # 全部节点，输出到本目录
python3 ipow.py --limit 6            # 只取 6 个有效节点
python3 ipow.py --source-tag HX      # 换前缀：HX-日本-01
python3 ipow.py --concurrency 8      # 8 线程
python3 ipow.py --android-out        # Termux：输出到 /sdcard/Download
```

只用标准库也能跑（缺 `cryptography` / `eth_account` 时自动切内置纯 Python 引擎，
已对着 Keccak-256 / secp256k1 / AES-256-GCM 的公开测试向量校验过）。

## 部署到 GitHub

### 1. 推送代码
```bash
git init
git add .
git commit -m "feat: VPN 节点聚合订阅（Gist 版）"
git remote add origin https://github.com/<你的用户名>/vpn-sub-aggregator.git
git branch -M main
git push -u origin main
```

### 2. 配置 Secrets
进入仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Name | 说明 |
|------|------|
| `MY_GITHUB_TOKEN` | GitHub PAT（Classic，勾选 `gist` scope） |

其余来源都不需要额外的 Token。`ipow.py` 会在运行时自建 Web3 钱包，也无需任何配置。

### 3. 手动触发
**Actions → VPN Sub Aggregator → Run workflow**

## 隐私与安全
- 订阅放在 **私有 Gist**，知道 raw 链接的人才能访问
- `aggregate.py` 会自动排除敏感中间文件（`last_account.json`、全量配置等）
- `ipow.py` 的 `iPoW_wallets.json` 里存着自动注册的钱包私钥，属于凭据，已在
  `EXCLUDE_FILES` 与 `.gitignore` 里排除，**不要提交**
- `scripts/` 下的节点产物（`*.txt` / `*.json` / `*.yaml`）全部被 `.gitignore` 挡住，
  只在 CI 运行时生成，不会进仓库
- `jvhe.txt` 包含所有有效节点，泄露后可直接使用，**不要发公开聊天**
- 建议 Gist 链接仅自己使用，不要分享

## 自定义
- 修改定时频率：编辑 `.github/workflows/aggregate.yml` 中的 `cron`
- 剔除某个来源：注释掉 `aggregate.py` 里对应的 `run_script("xxx.py")`
- 节点去重规则：修改 `aggregate.py` 的 `merge_all` 中的 `key` 生成逻辑
- iPoW 节点前缀：改 `ipow.py` 的 `--source-tag`（默认 `iPoW`）