# 多平台机会工作台：本机 AI 交接

> 整理时间：2026-10-12（北京时间）。这是给同一台电脑上的下一位 AI 的导航页，不代替当下的代码、数据库和服务检查。请勿把历史文档中的测试回执当成此刻的运行状态。

## 1. 从哪里接手

- **本机项目根目录**：`/Applications/work/gitRepo/opportunity-workbench`。
- **当前开发分支快照**：`feature/bootstrap`，编写本页前的代码基线为 `d3d5a8f`（手机商品结果精简），当时工作树干净。本机已有未推送提交；具体 HEAD 与超前数量须在接手时重查。GitHub 克隆和 ECS 是否含最新提交，也必须分别核对，不能从“本机已提交”推断已经推送或部署。
- **先读的文件，按顺序**：根目录 `AGENTS.md` → `docs/README.md` → `docs/current-product-contract.md` → `docs/core-acceptance.md` → `.agent/execplans/opportunity-workbench.md`。专题内容再按需读 `docs/deployment-aliyun-ecs.md`、`docs/environment.md` 和其他计划。`docs/history/` 是审计归档，不定义当前产品规则。
- **接手时先只读核对**：`git branch --show-current`、`git status --short --branch`、`git log -1 --oneline`；核对本地服务、最近扫描回执、ECS HEAD 后再下当前结论。严格遵守全局保护分支规则：只在非保护功能分支修改仓库。

## 2. 产品目标与数据口径

目前先跑通淘宝一个渠道，但产品并非永久排除其他渠道。应用使用**淘宝联盟第一方物料**定时发现有当前优惠信号的商品；用运营人员在本系统维护的具体品牌、规格、单位及四档价格带，判断其是否低于维护基准。未维护价格带或规格不确定时，官方优惠观察仍可展示，但不宣称“可捡漏”。第三方转载线报和普通目录原价不能冒充已验证优惠。

默认列表的最低准入是：官方商品身份、官方推广入口、当前快照、正数联盟推广价，以及可解释的参考价降幅、有效券或促销路径之一。`zk_final_price` 是联盟推广价；可复算的券后/促销价格是**条件试算**。两者均不是用户账户结算价、全网最低价或可转售利润。SKU 详情缺失不阻断合格物料展示，但不能凭标题猜 SKU 结算价。金额缺失的记录不应出现在默认优惠列表。

节日大促是独立菜单。2026 双11规则和官方物料列表已能同步，但“当前账号可见物料”“名称含双11”或日期重叠均不能证明商品属于今年活动。只有官方公告或运营确认归属，才能将物料建为节日定时来源。组合计算仅在用户输入的商品、优惠、资格、运费和订单分组条件下选最低**预估**，不得称账户最终付款。

## 3. 已有实现与证据边界

| 范围 | 现状 | 下一位 AI 需要保留的边界 |
|---|---|---|
| 淘宝采集 | `taobao.tbk.dg.material.optional.upgrade`；纸品、饮用水、大米、面粉、食用油、洗衣液六个主题独立配置、到期扫描与分页。ECS 文档记录 2026-10-12 一轮六主题真实回放：原始 4,491、解析 4,404、促销信号 4,157。 | 这些是当时回执；需查最新 `source_scan_runs`。促销信号不等于结算价或捡漏。 |
| 价格判断 | 本地价格带匹配、单位换算、四档判定和版本审计已实现。 | 真实运营价格带尚未完成录入与商品级验收；不能编造品牌价格。 |
| 节日专区 | 独立页面、官方规则与商品/权益物料同步、条件分类、店铺候选、优惠组合试算已实现。 | 今年双11商品归属、本人券资格和真实结算未证实；AC-09、AC-11 见当前验收文档。 |
| 手机浏览器 | 代码已将手机搜索结果收敛为标题、价格、优惠价、详情；无依据的优惠价显示“—”。“我的推广链接”在手机端优先尝试淘宝 `tbopen://`，保留原联盟网页备用。 | 真实手机的视觉、淘宝 App 唤起和佣金归因未完成实体回放。公网仍为 HTTP。 |
| 账户结算价 | 尚未获取可证明的个人领券资格、库存、运费与结算页总额。 | 不得把联盟价、条件价或本地维护阈值写成实际到手价。 |

以上状态以 `docs/core-acceptance.md` 的 AC-01 至 AC-12 为准；它包含具体回放和仍未通过的项目。旧测试项数、旧 PID、旧源数量都只具有当时效力。

## 4. 代码和运行入口

| 职责 | 位置 |
|---|---|
| Web 入口、登录和调度入口 | `app.py`、`routes/auth.py`、`services/local_auth.py` |
| 官方 API、价格解释、主题配置 | `services/taobao_alliance.py`、`services/taobao_living_topics.py` |
| 扫描、SQLite、回执 | `scanner.py`、`db.py`；表 `sources`、`source_scan_runs` |
| 搜索/详情与手机结果 | `routes/search.py`、`routes/opportunities.py`、`templates/index.html`、`templates/_mobile_offer.html`、`static/style.css`、`static/mobile.js` |
| 节日规则/物料/计算 | `services/double11_sync.py`、`services/festival_promotions.py`、`services/festival_optimizer.py`、`routes/festival_promotions.py` |
| 本机启停与 ECS 部署 | `scripts/workbench.sh`、`deploy/aliyun/`、`docs/environment.md`、`docs/deployment-aliyun-ecs.md` |

本机为 Flask/Jinja2/SQLite，Python 虚拟环境在 `.venv`；数据库和密钥均不在 Git 中。按 `docs/environment.md`，本机入口是 `http://127.0.0.1:5002/`，网页和淘宝 worker 由 `bash scripts/workbench.sh restart` 管理。接手时不要因为历史文档写“运行中”便假定进程仍在。整理当次曾观察到 5002 的监听，但在当前执行环境请求 `/health` 未能连接，因此本交接**未确认本机 HTTP 可用**。

ECS 项目根目录为 `/application`，实例 `i-uf693wtee99tze11foca`（上海），公开入口 `http://8.159.128.186/`；Nginx 对外 80，Gunicorn 本机 5002，网页与采集分别是 `opportunity-workbench-web` 和 `opportunity-workbench-worker`。运行配置位于 `/application/.runtime/workbench.env`，SQLite 位于 `/application/.runtime/workbench.sqlite3`。这些路径来自部署文档；**远端当前提交、服务和最新数据需现场核对**。公网尚未配置 HTTPS，正式从公网输入密码前应先解决 TLS。

## 5. 下一位 AI 的工作顺序

1. 先确认用户当前要做的具体工作；不要把本交接当成自动执行清单。核对本机分支、HEAD、工作树，阅读现行产品契约与 AC。
2. 涉及数据正确性时，先从真实淘宝官方响应、最近 `source_scan_runs`、数据库记录和页面同一商品追溯身份、价格、优惠条件与入口；明确“事实、推断、未知”。不要以旧截图或模型常识替代当次证据。
3. 涉及本机或 ECS 功能时，分别核对代码提交、进程、HTTP 页面和采集回执；本机、GitHub、ECS 三处可能不同步。不要覆盖运行数据库、`.env`、服务器配置或用户未提交改动。
4. 涉及页面时，以真实手机场景确认标题、价格、优惠价和详情可读；推广链接唤端和佣金归因需要实体手机及联盟回执，不以协议代码推定成功。
5. 每个修复关联 `docs/core-acceptance.md` 的 AC/缺陷，记录实际验证与未验证边界；产品规则变化先改 `docs/current-product-contract.md`。未完成核心闭环时，不把局部修复宣布为全部完成。

## 6. 安全与交付

不要将本机 `.env`、服务器 `.runtime/workbench.env`、App Secret、管理员密码、会话密钥或数据库写进交接文档、终端输出或 Git。远端操作优先按已安装的 `alibabacloud-workbench-cli` Skill 和 `docs/deployment-aliyun-ecs.md`；仅在用户当前明确授权的范围内变更外部系统。GitHub 分支当前比本机落后，下一位 AI 不能只拉取远端再宣称拿到最新代码。若需发布，先核对当前凭据、分支、差异、ECS 状态和用户意图。
