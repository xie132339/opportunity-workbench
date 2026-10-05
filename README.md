# 多平台机会工作台（源码开发版）

> 2026-10-04 阶段纠正：先补齐来源、商品身份、公开报价、同款行情、转售参考和预计费用，再筛选与预估利润；本人结算、库存及实际交易属于后续执行，不得作为当前采集验收的前提。当前口径以核心台账为准。


> 当前目标、9项核心验收、缺陷状态与下一处理项统一记录在 [核心目标与缺陷台账](docs/core-acceptance.md)。整体未完成；核心闭环通过前，暂停无直接关联的功能扩张。

面向个人的中国市场线索工作台：公开入口与人工线索汇集、来源状态、行情证据、成本核算、库存和实际收益。**页面条目不等于有利润**。先从源码运行和二开；场景验收后再制作 Docker 部署。

## 当前组成

- 监控底座：changedetection.io 独立源码项目；在另一台电脑上需单独克隆、安装和运行，版本与当前开发环境记录见 `docs/environment.md`。
- 业务层：本仓库 Python 3.12、Flask/Jinja2/SQLite。两者通过 changedetection 公开 API 连接，不直接读取监控数据库。
- 已实际提取的入口和边界见 [docs/sources.md](docs/sources.md)。产品完整范围见 [docs/requirements.md](docs/requirements.md)。
- 在页面新增渠道的具体步骤与各方式边界也见 [docs/sources.md](docs/sources.md)；公开网页解析只接受已适配站点。
- 判断低价是否可能盈利的实际步骤与证据门槛见 [docs/profit-playbook.md](docs/profit-playbook.md)。
- RSSHub、闲鱼结果、授权优惠数据和 QQ/微信消息的二开接入见 [docs/integrations.md](docs/integrations.md)。闲鱼账号、搜索任务和结果统一在 [工作台闲鱼任务页](http://127.0.0.1:5002/xianyu) 管理；外部消息在 [本机消息接入页](http://127.0.0.1:5002/messages) 配置。

## 从源码安装与启动

本项目支持 macOS/Linux 和 Windows，要求 Python 3.12。初次安装使用 `bash setup.sh`（macOS/Linux）或 PowerShell 执行 `./setup.ps1`（Windows）；脚本创建本地 `.venv`、安装锁定版本依赖，并在不存在时从模板生成 `.env`。随后运行：

```bash
# macOS / Linux
.venv/bin/python app.py serve
```

```powershell
# Windows PowerShell
.venv\Scripts\python.exe app.py serve
```

打开 <http://127.0.0.1:5002/>。源码启动、后台 worker、可选服务、配置与备份步骤见[安装与使用手册](docs/installation.md)。不需要 Docker。克隆后的数据库为空；不会把当前电脑的机会、账号或历史数据库一并上传。

监控底座、RSSHub、闲鱼服务及消息网关是分开的可选/外部服务，工作台可单独启动，但相应功能只有配置并运行这些服务后才可用。准确的依赖和限制见[集成说明](docs/integrations.md)。自动下单、签到、抢购脚本不会作为数据源执行；软件不替用户下单。

渠道管理页可用 GitHub CLI 刷新公开适配目录，候选先保存源码路径、许可证和更新时间，再由本机 RSS 解析器实测；只有解析出当前公开条目的候选可以批量接入。命令行等价操作为 `.venv/bin/python app.py channels-refresh` 和 `.venv/bin/python app.py channels-validate`。

业务数据默认位于忽略的 `data/workbench.sqlite3`；`.env`、虚拟环境和数据库均不提交 Git。源码按业务职责拆分：采集与渠道发现、商品链接解析、商家页核验、优惠、比价与低价历史、通知及闲鱼接入分别位于独立模块；Flask 页面路由目前集中在 `app.py`。

## GitHub 仓库与换电脑克隆

本项目仓库：<https://github.com/xie132339/opportunity-workbench>（公开仓库）。完整源码发布在 `feature/bootstrap` 分支；`main` 保留仓库初始化内容，不承载本次代码发布。使用 GitHub Desktop 克隆后，在分支菜单切换到 `feature/bootstrap`，再按[安装与使用手册](docs/installation.md)配置 Python 环境与可选外部服务。克隆只包含源码和文档，不包含本机数据库、`.env`、账号或采集历史。

GitHub Desktop 官方操作说明：[克隆仓库](https://docs.github.com/en/desktop/adding-and-cloning-repositories/cloning-and-forking-repositories-from-github-desktop)。

## 二开位置

| 改动 | 文件 |
|---|---|
| 新公开入口解析规则与 RSS | `scanner.py` |
| GitHub 候选发现、实测与接入 | `channel_discovery.py` |
| 数据对象、初始化、迁移 | `db.py` |
| 页面与操作 | `app.py`、`templates/`、`static/` |
| 进度与现场证据 | `.agent/execplans/opportunity-workbench.md`、`docs/environment.md`、`docs/sources.md` |

开发期先改上述源码并重启对应进程。Docker 部署是后续把已验证源码与依赖固定下来，不需要为了二开先在容器里改代码。
