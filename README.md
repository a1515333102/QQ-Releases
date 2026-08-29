# QQ Releases (Windows + Linux)

从腾讯官网自动同步 QQ 安装包，发布到本仓库 [Releases](../../releases)。

> 非官方镜像，版权归腾讯所有。仅方便下载与版本归档。

## 包含内容

| 平台 | 包格式 |
|------|--------|
| Windows | QQNT x64 / x86 / arm64 `.exe` |
| Linux | x64 / arm64 的 `.deb` / `.rpm` / `.AppImage`，以及 loongarch64、mips64el `.deb` |

数据来源：

- `https://cdn-go.cn/qq-web/im.qq.com_new/latest/rainbow/pcConfig.json`
- `https://cdn-go.cn/qq-web/im.qq.com_new/latest/rainbow/linuxConfig.js`

## 使用

1. 打开本仓库 **Releases**，下载对应安装包。
2. 或用命令行（把 `OWNER/REPO` 和 tag 换成你的）：

```bash
# 查看最新 release
gh release view --repo OWNER/REPO

# 下载某个文件
gh release download --repo OWNER/REPO -p "QQ_*_x64_*.exe"
```

## 自动同步

GitHub Actions 每 12 小时检查一次官网配置；有新版本则下载并创建 Release。

也可在 Actions 页手动运行 **Sync QQ Releases**。

本地试跑（只下载、不发布）：

```bash
python scripts/sync.py --download-only
```

发布（需已登录 `gh`，且在仓库目录）：

```bash
python scripts/sync.py --publish
```

## 说明

- 安装包不进入 git 历史，只挂在 GitHub Releases。
- 若官网 CDN 或配置字段变更，需更新 `scripts/sync.py`。
