# 多平台机会工作台（源码开发版）

面向个人的中国市场线索工作台：公开入口与人工线索汇集、来源状态、行情证据、成本核算、库存和实际收益。**页面条目不等于有利润**。先从源码运行和二开；场景验收后再制作 Docker 部署。

## 当前组成

- 监控底座：独立源码工作区 `/Applications/work/gitRepo/changedetection-source`，changedetection.io 0.60.8，当前检出提交见 `docs/environment.md`。
- 业务层：本仓库 Python 3.12、Flask/Jinja2/SQLite。两者通过 changedetection 公开 API 连接，不直接读取监控数据库。
- 已实际提取的入口和边界见 [docs/sources.md](docs/sources.md)。产品完整范围见 [docs/requirements.md](docs/requirements.md)。
- 在页面新增渠道的具体步骤与各方式边界也见 [docs/sources.md](docs/sources.md)；公开网页解析只接受已适配站点。
- 判断低价是否可能盈利的实际步骤与证据门槛见 [docs/profit-playbook.md](docs/profit-playbook.md)。
- RSSHub、闲鱼结果、历史实付价和 QQ/微信消息的二开接入见 [docs/integrations.md](docs/integrations.md)。外部消息可在 [本机消息接入页](http://127.0.0.1:5002/messages) 配置。

## 开发期从源码启动

两仓库都必须先核对当前分支是非保护功能分支。以下命令不需要 Docker：

```bash
cd /Applications/work/gitRepo/changedetection-source
git branch --show-current
uv venv --python /Users/xdf/.local/bin/python3.12 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/changedetection.io -C -d "$PWD/datastore" -h 127.0.0.1 -p 5001 -l INFO
```

另开终端：

```bash
cd /Applications/work/gitRepo/opportunity-workbench
git branch --show-current
uv venv --python /Users/xdf/.local/bin/python3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
# 首次配置：从监控底座的设置页取得 API key，按 .env.example 建立仅本机可读的 .env
chmod 600 .env
.venv/bin/python app.py serve
```

访问 [业务工作台](http://127.0.0.1:5002/) 与 [监控底座](http://127.0.0.1:5001/)。按需单次采集：`.venv/bin/python app.py scan`。持续低频采集：另开终端运行 `.venv/bin/python app.py worker`；worker 按各来源的间隔执行。进程关闭或本机休眠时不会继续采集。

业务数据位于忽略的 `data/workbench.sqlite3`；`.env`、虚拟环境和监控 `datastore/` 都不提交 Git。首次采集保留为基线，不触发站内提醒。报价类型和成本缺失会阻止确定利润展示。交易由用户自行执行，软件不下单。

## 二开位置

| 改动 | 文件 |
|---|---|
| 新公开入口解析规则与 RSS | `scanner.py` |
| 数据对象、初始化、迁移 | `db.py` |
| 页面与操作 | `app.py`、`templates/`、`static/` |
| 进度与现场证据 | `.agent/execplans/opportunity-workbench.md`、`docs/environment.md`、`docs/sources.md` |

开发期先改上述源码并重启对应进程。Docker 部署是后续把已验证源码与依赖固定下来，不需要为了二开先在容器里改代码。
