# VPN 节点聚合订阅

每 48 小时自动运行一次，聚合 5 个来源的节点并生成统一订阅。

## 目录
```
.
├── scripts/
│   ├── __.py        # 菜鸟
│   ├── TF__.py      # 银狐 / foxlink
│   ├── Surfer.py    # 冲浪者
│   ├── ben_1.py     # BEN VPN
│   └── devpn.py     # DeVPN
├── aggregate.py     # 聚合主脚本
├── requirements.txt
├── .github/workflows/aggregate.yml
└── README.md
```

## 部署到 GitHub (Private Repo)

### 1. 创建私有仓库
在 GitHub 上新建仓库，**务必选择 Private**（避免节点链接公开扫描）。

### 2. 推送代码
```bash
git init
git add .
git commit -m "feat: 初始化聚合订阅"
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

### 3. 设置 Secrets
进入仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Name | 说明 |
|------|------|
| `BEN_TOKEN` | `ben_1.py` 所需 Token（从服务商获取） |

### 4. 启用 GitHub Pages
**Settings → Pages → Source** 选择 `gh-pages` 分支，根目录 `/ (root)`。

### 5. 订阅链接
Pages 部署完成后，你的订阅链接为：

```
https://<用户名>.github.io/<仓库名>/sub_b64.txt
```

> 注意：因为仓库是 Private，GitHub Pages 仅对仓库协作者开放，需要登录 GitHub 才能访问，天然防止了公网扫库。

### 6. 安装到设备
- **Shadowrocket / Clash / V2RayN** 等支持 Base64 订阅的客户端，直接粘贴上面的 URL 即可。
- 如果客户端因 GitHub Pages 需要登录而无法直接导入，可以手动下载 `sub_b64.txt` 后导入，或改用本地方案。

## 隐私与安全
- 所有脚本的 API 凭证尽量通过 **GitHub Secrets** 注入，不硬编码到文件。
- 仓库设为 Private，Pages 默认不会暴露给未授权用户。
- `sub_b64.txt` 包含所有有效节点，泄露后可直接使用，建议仅通过安全的 GitHub Pages 链接访问。
- 各脚本已做基础节流（如 `ben_1.py` 的 1s 间隔），降低被服务商识别封禁的风险。

## 手动触发
进入 **Actions → VPN Sub Aggregator → Run workflow**，可手动强制执行一次立即更新。

## 自定义
- 修改定时频率：编辑 `.github/workflows/aggregate.yml` 中的 `cron`（GitHub cron 格式为 `分 时 日 月 周`）
- 剔除某个来源：修改 `aggregate.py` 中的 `run_script` 调用
- 节点去重规则：修改 `aggregate.py` 的 `merge_links` 中的 `key` 生成逻辑