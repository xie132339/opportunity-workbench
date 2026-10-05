# 安装与使用手册

本手册面向从 GitHub Desktop 克隆项目的新电脑。工作台本身是 Flask/Jinja2/SQLite 源码项目；不要求 Docker，也不会自动下载、运行或登录第三方采集程序。

## 1. 环境要求

- macOS、Linux 或 Windows 10/11。
- Python 3.12；可从 <https://www.python.org/downloads/> 安装。Windows 推荐安装 Python Launcher (`py`)。
- GitHub Desktop 只用于克隆、提交和同步代码，不是应用运行时依赖。
- 网络需能访问 PyPI 安装 `requirements.txt` 中的依赖。中国大陆网络环境需要时自行配置可信的 PyPI 镜像。

## 2. 克隆后安装

在 GitHub Desktop 选择 **File → Clone Repository…**，克隆自己的 `opportunity-workbench` 仓库并打开本地目录。也可用：

```bash
git clone <你的仓库地址>
cd opportunity-workbench
```

macOS/Linux：

```bash
bash setup.sh
.venv/bin/python app.py serve
```

Windows PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.venv\Scripts\python.exe app.py serve
```

安装脚本只在 `.env` 不存在时从 `.env.example` 生成本机配置，并生成本机随机 `WORKBENCH_SECRET`；不会覆盖既有设置。脚本不会复制开发电脑上的 `data/` 数据库或任何登录凭据。

访问 <http://127.0.0.1:5002/>。仅本机回环地址可访问。按 `Control+C` 停止服务。

## 3. 后台采集

页面服务与采集 worker 是两个独立进程。只有来源配置已启用且其数据入口实际可访问时，worker 才会采集；新克隆后数据库是空的，先在“渠道管理”查看来源状态。

macOS/Linux：

```bash
.venv/bin/python app.py worker
```

Windows PowerShell：

```powershell
.venv\Scripts\python.exe app.py worker
```

手动立即扫描一次：

```bash
.venv/bin/python app.py scan
```

`worker` 和 `scan` 可能向外部公开站点发出请求；仅针对允许访问的公开入口低频使用。关闭终端、关机或休眠会停止本机采集。

## 4. 外部服务和功能边界

工作台可独立启动，但不是所有渠道都内置在本仓库。它们通过本机 HTTP API/RSS 连接；没有运行对应服务时，不应把相应渠道算作已接通。

| 外部服务 | 作用 | 是否启动工作台的前提 |
|---|---|---|
| changedetection.io | 监控网页变化与历史快照 | 否；需要网页监控时单独安装/启动，并在 `.env` 配置 API 地址和令牌 |
| RSSHub | 提供各站点 RSS 适配路由 | 否；需要 RSSHub 路由来源时单独启动，按路由配置 `RSSHUB_BASE` |
| ai-goofish-monitor | 闲鱼搜索任务后台 | 否；需要闲鱼功能时单独运行并在工作台“闲鱼任务”配置 `GOOFISH_BASE`；账号登录态只保存在该服务本机 |
| OneBot v11 网关 | QQ 消息发送适配 | 否；只在本人配置并启用 QQ 通道时需要。项目本身不含 QQ 登录态 |
| 企业微信/Server酱 | 外部消息发送服务 | 否；需要本人配置 webhook/SendKey 并启用通知后才使用 |

这些依赖的安装方式、来源许可与当前已验证状态见[多来源与消息二开接入](integrations.md)、[渠道清单](sources.md)。没有商城授权凭据或真实公开数据的功能仍会显示缺口；装上服务不等于已获得低价或利润数据。

## 5. 配置

安装脚本生成的 `.env` 默认关闭外发消息 (`NOTIFY_ENABLED=0`)。常用配置：

- `CHANGED_API_BASE` / `CHANGED_API_TOKEN`：changedetection.io API 地址和令牌。
- `RSSHUB_BASE`：本机 RSSHub 服务地址。
- `GOOFISH_BASE`、`GOOFISH_API_USER`、`GOOFISH_API_PASSWORD`：闲鱼本机服务配置。
- `WORKBENCH_DB`：SQLite 文件路径；不设置时使用项目下 `data/workbench.sqlite3`。
- `NOTIFY_ENABLED`、`NOTIFY_CHANNELS` 及对应 webhook/SendKey/令牌：消息发送配置。默认关闭；不要把密钥粘贴到页面、README 或 Git 提交。

`.env` 是纯文本密钥文件。桌面系统请限制本机账户访问；macOS/Linux 安装脚本会设置文件权限 `600`。`.env.example` 只有配置键与占位内容，不包含本人凭据。

## 6. 数据与备份

- 新克隆后的业务库在 `data/workbench.sqlite3`，首次访问时由 `db.initialize()` 创建。
- 备份前停止 `worker` 和网页服务，再复制 SQLite 文件；恢复时放回原路径或设置 `WORKBENCH_DB` 指向备份文件。
- Git 忽略数据库与运行目录。换电脑时需自行安全迁移数据库；不要把登录态、API token 或个人交易账本发布到公开仓库。
- `.env`、`data/`、`.venv/` 和服务自身的数据目录均不随 GitHub 仓库克隆。

## 7. GitHub Desktop 更新流程

1. 在 **Current Branch** 确认正在使用非保护功能分支。
2. 在 **Changes** 检查文件清单与 diff，确认没有 `.env`、SQLite 文件、登录态或第三方服务数据。
3. 填写 Summary，点击 **Commit to <当前分支>**；再点击 **Push origin** 上传提交。
4. 另一台电脑在 GitHub Desktop 选择 **Fetch origin** 后 **Pull origin**，然后重启工作台进程。新提交的代码不会覆盖远端已有数据库，因为数据库不在仓库内。

完整核心状态和未完成的数据源/利润边界见 [核心目标与缺陷台账](core-acceptance.md)。

## 8. 常见问题

- `python3.12` / `py -3.12` 找不到：安装 Python 3.12 后重新打开终端。
- `pip install` 失败：检查网络或 PyPI 镜像；不要从未知网站下载预编译依赖。
- 5002 端口被占用：关闭已运行的工作台进程，再启动；不要同时对同一 SQLite 库启动多个写入 worker。
- 页面能开但渠道没数据：检查渠道页健康状态、worker 是否运行、源站是否有可解析公开数据以及服务配置。页面成功启动不能证明源站价格、优惠资格或利润正确。
- Windows PowerShell 阻止脚本：只对当前 PowerShell 进程运行 `Set-ExecutionPolicy -Scope Process Bypass` 后重试，不需要更改系统级策略。
