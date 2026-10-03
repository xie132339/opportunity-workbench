# 环境操作记录

## 2026-10-03 机会中心空页排查与恢复

- 用户页面保留新人价筛选；当时正式库 879 条机会中该类型 0 条，故筛选后为空。默认页同时要求原文近 2 小时且来源持续更新；业务 worker 已停止、来源最后检查停在 10 月 2 日，历史记录仍在留档。
- 恢复 worker 后发现新插入事件的 last_seen_at 为 NULL：旧库该列通过 ALTER TABLE 加入时没有默认值，scanner 的 INSERT 又未显式赋值。修复为新事件插入时写 CURRENT_TIMESTAMP；启动时将旧 NULL 回填为原始 observed_at，不伪造为今天出现。实际重扫有明确发布时间的政府采购来源后，默认页显示 1 条符合时效门槛的公告。
- 本机 RSSHub 服务原已退出，5 个配置的 RSS 来源先报 502。使用已安装的 node_modules 与 Node 24.15.0 直接从源码启动；根页面和小米上新路由返回 200，逐个重扫后 5 个 RSS 来源均 healthy。业务服务及 worker 从修复后的源码重新运行；外部消息通道仍无启用项。
- 浏览器清除新人价筛选、保留“全部留档”后可见 923 条未忽略线索；页面标题已改为“全部留档线索”，明确警示旧记录和发布时间未知不能当成当前可买。默认页 1 条，新人价页 0 条，三个页面 HTTP 200；正式库总机会 944 条，其中 21 条忽略。商品结算价、账号资格与利润仍未验证。源码语法检查和 git diff --check 通过。终端会话退出或机器重启后，源码进程仍需重新启动。

## 2026-10-03 新人优惠识别

- 功能分支 feature/bootstrap 增加优惠类型、本人资格和限购数量；公开线索标题命中新人／新客／首单／首购时仅标为疑似。机会中心增加新人价筛选；历史现金实付价仅与同优惠类型比较，资格未知的旧样本不参与。
- py_compile 和 git diff --check 通过。源码服务运行于 127.0.0.1:5002；新人价留档筛选页、消息接入页与已有详情页均返回 HTTP 200。现有库迁移后 879 条机会均为优惠资格未知，标题规则命中 0 条；已核实新人价 0、历史现金实付样本 0。没有取得登录后个性化价格、发送消息或下单。
- 在本机浏览器实际选择“新人价（含疑似）”和“全部留档”，提交后地址包含 offer=new_user&view=all，选项保持选中，页面显示没有符合条件的线索。此处是当前数据结果，不能证明各平台不存在新人优惠。

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

## 2026-10-02 GitHub CLI 底座检索

- 执行 `gh search repos` 对通用价格追踪、闲鱼监控、京东比价、拍卖采集进行检索；`gh search code 'smzdm repo:DIYgod/RSSHub'` 核对值得买路由；`gh api repos/.../contents/...` 阅读 PriceDive 和国内采集器源码。命令和逐项目证据见 `docs/github-cli-research.md`。
- 普通沙箱联网报 `error connecting to api.github.com`；获准联网执行后只读查询成功。未克隆、安装、运行新项目，也未录入虚构价格。
- PriceDive 的核心取价是随机数模拟，明确排除为真实行情来源；ai-goofish-monitor 列为闲鱼候选，RSSHub 列为线索候选。当前工作台和纸品捡漏状态不因 GitHub 仓库存在而变化。

## 2026-10-02 多来源与消息二开阶段

- 在本仓库 `feature/bootstrap` 保留先前未提交调研文档，不覆盖用户数据。新增本机 RSSHub、闲鱼结果来源方法，同规格历史现金实付样本对比，外部消息投递队列，以及企业微信、Server酱个人微信、本机 OneBot QQ 三个可选发送适配器；配置样例为 `.env.example`，真实凭据未写入 Git，默认 `NOTIFY_ENABLED=0`。
- 上游 RSSHub SHA `5d7e6ce62d1555b09582c3267aa0fa6f1f81c660` 与 ai-goofish-monitor SHA `f85d140b6b45029d9a0925feb96dad733b41396d` 用 `git clone --no-checkout` 后切到 `feature/opportunity-workbench`，并移除 `origin/master` 跟踪。RSSHub 用 nvm Node 24.15.0、pnpm 10.34.5，`pnpm install --frozen-lockfile --ignore-scripts` 成功；`LISTEN_INADDR_ANY=0 PORT=1200 pnpm dev` 在 `127.0.0.1:1200` 监听，首页 HTTP 200。`/jd/price/526835` 返回 503，日志显示上游 `http://p.3.cn/prices/mgets` 连接被对端关闭；HTTPS 直查也 TLS 失败，此路由未启用为真实价格源。SMZDM 路由源码要求 cookie，未配置、未宣称可用。
- 业务源码 `py_compile` 成功，`git diff --check` 无错误。在 127.0.0.1:5003 以现有业务库临时运行新源码，`/health`、`/sources`、`/strategies`、`/market`、`/opportunities/1` 全部 HTTP 200；详情显示历史实付价区块和“样本不足”。迁移后只读核对：来源 16、事件/机会各 425、报价 0、交易 0、原站内提醒 112、新外部投递 0。此前后台继续产生新线索，不把 425 与先前的 275 视为迁移新增。
- QQ 官方文档说明主动推送已停止，本项目接的是用户自备本机 OneBot 网关；个人微信通过用户自备 Server酱密钥，企业微信通过群机器人 Webhook。本次没有发送外部消息，真实 QQ/微信投递尚无凭据和收件结果。

- RSSHub `/mi/newproducts` 实测 HTTP 200、XML 126084 字节，工作台解析器取得 55 条带小米商品原链接的条目；登记正式来源 #17 并首次扫描，结果 `healthy/new=55/baseline=True`。商品列表与相应售价仍不是本人最终结算价，未计入历史买价或利润。
- ai-goofish-monitor 源码安装：Python 3.12 隔离 `.venv`、`uv pip install -r requirements-runtime.txt` 成功；`web-ui` 用 `npm ci --ignore-scripts` 和 `npm run build` 成功。忽略的 `.env` 使用随机本机管理密码、`SKIP_AI_ANALYSIS=true`，无 AI API key、无闲鱼登录状态。`uvicorn src.app:app --host 127.0.0.1 --port 8000` 已运行；`/health`、`/`、`/api/results/files` 均 HTTP 200，结果列表 `[]`。上游 API 路由未发现服务端统一认证门槛，因此只监听回环地址；当前不可称闲鱼自动线索已接入。
- 正式工作台网页与 worker 从新源码重启于 `127.0.0.1:5002`。渠道页展示 RSSHub 小米上新、RSSHub/闲鱼本机来源选项；提醒页展示可选个人微信、企业微信及 QQ 投递；机会详情展示历史实付价“样本不足”。无账号、价格或消息投递结果被凭空补造。

- 最终源码重启后再核对：`/`、`/?candidate=1`、`/sources`、`/strategies`、`/opportunities/1` 均 HTTP 200；双重证据候选筛选显示无符合线索。RSSHub `/mi/newproducts` 仍 HTTP 200 XML；闲鱼 `/api/results/files` 仍 `[]`。来源 #17 为 `healthy/enabled=1`，报价 0、交易 0、外部投递 0。`py_compile` 与 `git diff --check` 通过；三个仓库都在非保护功能分支，`.env`、数据库、虚拟环境及上游构建产物均被 Git 忽略。没有执行真实下单、外部消息或 Docker 部署。

## 2026-10-02 续作验收

- 在 feature/bootstrap 上新增四个 RSSHub 来源并实际扫描，healthy 且首次基线 141 条。
- 新增 buy_checks 表和详情页显示，当前核价快照 0；没有伪造付款。
- 源码语法检查通过，服务重启后 /health、/sources、/opportunities/1、/?candidate=1 返回 HTTP 200。正式库来源 21、事件与机会各 621，报价、快照、交易、外部投递为 0。

## 2026-10-02 时效验证

- 从真实 RSS 再次解析并回填：小米上新 55、小米众筹 25、宜家 78、酷比 20、麦当劳活动 18 条。98 条带原文时间，其中小米众筹 3 条为未来日期；宜家与酷比 98 条无原文时间，不能作为当前。
- 中国政府采购网中央与地方公开招标列表实际扫描各 14、20 条，页面明确的发布时间转 UTC 保存；近期 4 条进入默认页，其余旧记录留档。值得买公开首页没有可机器读取的发布时间，保留为时间未知。
- 新规则：默认页原文 2 小时、来源最近成功且条目最近再次出现；人工核价 15 分钟。默认页实见 4 条有时间依据的官方公告，全部留档页保留旧记录；未来/未知/旧数据不进双重证据候选或外部消息。标题与日期仍不证明最终结算价、库存或可成交利润。源码服务与 worker 已从新源码重启。

- 发现原站内提醒把新入库的旧文章也计为待查看；现仅在原文近 2 小时且来源仍健康时生成新提醒，旧待办从当前列表与首页计数排除，历史视图可追溯。当前提醒页实见 4 条近期官方公告，历史提醒仍可查；双重证据候选 0。

## 2026-10-02 消息接入界面

- 在 feature/bootstrap 新增 /messages 独立页面和导航入口，提供企业微信、Server酱、QQ OneBot 的参数状态、启用开关、凭据输入、清除与主动测试按钮；保存本身不外发。
- 消息配置写入 Git 忽略的 .env，权限保持 0600；网页不回显密钥。notifier 在网页和 worker 使用时重新读取配置，避免页面保存后后台继续使用旧设置。
- 源码语法检查与 git diff --check 通过；网页和 worker 从源码重启。/、/sources、/messages、/strategies 均 HTTP 200，导航均显示“消息接入”，页面有 3 个通道与配置表单；HTML 不包含已有密钥值。本次没有提交任何凭据，也没有发送测试消息或外部消息。
