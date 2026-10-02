# 环境操作记录

## 2026-10-02 渠道配置实测

- 用临时 SQLite 数据库运行源码 `initialize()`、Flask `test_client()` 的 `/sources` 表单流程；验证新增苏宁网页解析来源、重复入口拦截、解析规则与域名不一致时拦截、检查间隔改为 15 分钟、暂停及恢复，均通过。测试数据库退出后删除，正式库没有“网页表单验证”来源。
- 通过新增来源的“现在检查”实际抓取苏宁 42 条；同一隔离库直接检查孔夫子旧书网 27 条、联想商城 53 条、荣耀商城 11 条，四者均为 `healthy`，且保存具体商品链接。结果只证明当前页面可提取条目。
- 本机浏览器实际打开 `/sources`，可见配置字段及四个新增健康来源；HTTP `/sources` 返回 200。正式库只读核对：启用网页解析入口 9 个、平台 8 个，验证记录 0 条，报价 0 条、交易 0 笔。实际到手价、库存、卖出成交和后台完整检查周期仍未验证。

## 2026-10-02 扩充公开商品渠道

- 在 `feature/bootstrap` 增加孔夫子旧书网、苏宁易购、联想商城、荣耀商城的公开商品链接解析规则及默认来源；渠道管理页面开放已适配站点的“公开网页解析”配置，并说明 RSS、页面变化监控、人工方式的边界。
- 使用源码 `initialize()` 登记新来源，随后逐个执行 `scan_source()`；首次分别得到 27、42、53、11 条真实商品／在售链接，状态 `healthy`，全为基线。用户页面 `/sources` 返回 HTTP 200，4 个新来源及解析规则均可见。真实付款价、库存和利润未验证。
- 京东、1688、唯品会、华为商城公开首页也做了只读探查；当前响应没有可按现有规则提取的具体商品链接，故未启用自动采集。没有尝试绕过登录或反爬。


## 2026-10-02 捡漏判定门槛修正

- 在 `feature/bootstrap` 上修改源码与项目文档；业务服务从源码重新启动于 `127.0.0.1:5002`。首次尝试用后台 `nohup` 启动没有保持运行，随后改用托管终端会话直接运行 `.venv/bin/python app.py serve`，启动输出确认监听成功。
- `PYTHONPYCACHEPREFIX=/private/tmp/opportunity-pycache .venv/bin/python -m py_compile app.py db.py` 成功；`git diff --check` 无错误。服务 `/health`、纸品筛选页均返回 HTTP 200，详情页显示“保守价差测算／待核实”。
- 数据库只迁移新增证据列，原记录保留。迁移后读到机会 275、报价 0、交易 0；没有真实卖价证据，故没有可确认的正价差或盈利。未录入示例交易，也未执行购买。
- 新流程见 `docs/profit-playbook.md`；原有 `.env` 凭据和业务数据仍保持忽略，不进入 Git。

## 2026-10-02 初始化

- `git init -b feature/bootstrap /Applications/work/gitRepo/opportunity-workbench`：成功；功能分支 feature/bootstrap。
- `open -a Docker`：引擎随后可用；项目没有创建镜像或容器。现有 Dependency-Track 容器属于其他工作，未触碰。
- Git、gh、Docker CLI、uv 已安装，系统 Python 3.9.6。
- 环境和业务进度继续记录于本文件与执行计划；凭据不进入记录。

## 2026-10-02 开发方式调整

- 用户明确要求从源码启动和二开，场景完成后再 Docker 部署。
- 中断后检查：本机没有 changedetection 镜像或本项目容器。现有 Dependency-Track 容器属于其他工作，未更改。
- 后续操作：独立检出上游源码至非保护功能分支，建立 Python 虚拟环境，从源码运行监控服务和业务服务。

## 2026-10-02 源码环境与实际启动

- 监控源码独立检出 `/Applications/work/gitRepo/changedetection-source`，在取出文件前切到非保护分支 `feature/opportunity-workbench`；HEAD `0e0566721b1c483dcf7ae548210ee10532d9b181`，版本 `0.60.8`，工作区干净。
- 两仓库均用已安装的 Python `3.12.13` 与 `uv venv` 创建忽略的 `.venv`。监控源码执行 `uv pip install --python .venv/bin/python -e .`，业务仓库安装 Flask、BeautifulSoup、feedparser、requests；均成功。没有修改系统 Python。
- 监控进程：`.venv/bin/changedetection.io -C -d /Applications/work/gitRepo/changedetection-source/datastore -h 127.0.0.1 -p 5001 -l INFO`，首页 HTTP 200。监控数据在其忽略的 `datastore/` 中。
- 本机监控 API 创建什么值得买首页任务，UUID `4d1581a9-8062-4ba6-8b66-34e8b2d6dda4`，提交检查后 `last_checked` 有值、`last_error=false`、`history_n=1`；快照已通过 API 消费到业务库。API 凭据只写入本仓库被忽略且权限 `0600` 的 `.env`，文档不保存凭据。
- 业务进程：`.venv/bin/python app.py serve`，监听 `127.0.0.1:5002`；`/health` 和五个主页面均 HTTP 200，真实记录详情页也 HTTP 200。两个进程当前为开发终端会话，机器休眠或结束会话后不保证继续运行。
- 首次 `.venv/bin/python app.py scan`：什么值得买 56、Apple 翻新 3、小米商城 80、中国政府采购网中央公告 20 条，均为基线，站内提醒 0 条。解析规则修正后中央公告复查仅 14 条公开招标；地方公开招标入口首次 20 条。价格标签与非可投标公告共 20 条保留原始记录、状态标记为忽略。
- 另核对 Apple Mac/iPad 翻新子栏目与政府采购中央/地方公开招标子栏目，具体见 `docs/sources.md`。义乌购库存尾货页面虽 HTTP 200，未提取到条目，不启用自动抓取。
- SQLite 业务库为忽略的 `data/workbench.sqlite3`，启用 WAL；监控底座与业务库互相独立。未执行购买、下单、邮件、付费 API 或 Docker 部署。

## 2026-10-02 源码运行复查与知识库同步

- 新增按来源检查间隔运行的 `app.py worker` 源码进程；当前间隔默认 60 分钟，worker 每分钟检查哪些来源到期。进程已启动，尚未等到一个完整间隔验证长期周期。
- 工作台已支持平台、类别、关键词和已核实买入预算筛选；未填写买价的条目不会凭空满足预算条件。暂停来源会显示“已暂停”。
- 监控底座同一个历史快照再次同步，结果 `healthy/new=0`，避免“无变化”被误判为抓取失败；只请求未处理快照，单次最多 100 条以便逐次补取。
- `Obsidian Vault` 中新增 `Notes/多平台机会工作台-实施日志.md`，原需求笔记改为“实施中-源码开发”并双向链接。`Start Here.md` 可到达需求笔记；CLI 搜索返回两篇笔记，Obsidian 内已打开实施日志并目视检查标题、属性、链接和表格。
- UI 现场复查发现 SQLite 使用 UTC 时间，已增加界面层北京时间转换；Obsidian/业务数据的底层时间不改写。首页默认优先零售与小额类别，保持所有类别可筛选。
- 已验证的“什么值得买首页整体变化”演示监控因不是具体商品线索，保留原始快照但在监控底座和业务来源均暂停，首个非商品机会标记为忽略。五个具体网页解析入口仍启用。
- 首次 Git 提交 `3db71c6` 位于 `feature/bootstrap`；暂存内容未包含本机 API token，`.env`、`.venv`、`data/` 已核对为 Git 忽略。
- 监控底座首次安装自带的 Hacker News 与 changedetection 更新日志两个示例任务已通过本机 API 暂停；本项目的首页演示任务也已暂停。当前有效自动采集由业务 worker 的 5 个具体公开入口承担。
