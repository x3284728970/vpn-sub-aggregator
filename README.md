# VPN 节点聚合订阅

每 48 小时自动运行一次，聚合 5 个来源的节点并生成统一订阅。

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
│   ├── __.py        # 菜鸟
│   ├── TF__.py      # 银狐 / foxlink
│   ├── Surfer.py    # 冲浪者
│   ├── fengniao.py  # 蜂鸟加速器
│   ├── sulian.py    # 速连 VPN
│   ├── zytvpn.py    # 纵云梯
│   ├── butterflyds.py # ButterflyDS
│   └── devpn.py     # DeVPN
├── aggregate.py     # 聚合主脚本
├── requirements.txt
├── .github/workflows/aggregate.yml
└── README.md
```

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
| `BEN_TOKEN` | `ben_1.py` 所需 Token（可选，未设置则跳过该来源） |

### 3. 手动触发
**Actions → VPN Sub Aggregator → Run workflow**

## 隐私与安全
- 订阅放在 **私有 Gist**，知道 raw 链接的人才能访问
- `aggregate.py` 会自动排除敏感中间文件（`last_account.json`、全量配置等）
- `jvhe.txt` 包含所有有效节点，泄露后可直接使用，**不要发公开聊天**
- 建议 Gist 链接仅自己使用，不要分享

## 自定义
- 修改定时频率：编辑 `.github/workflows/aggregate.yml` 中的 `cron`
- 剔除某个来源：注释掉 `aggregate.py` 里对应的 `run_script("xxx.py")`
- 节点去重规则：修改 `aggregate.py` 的 `merge_all` 中的 `key` 生成逻辑