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

## 5.1 2026-10-12 手机端列表"在淘宝 App 打开"按钮

- 手机 offer 卡片（`templates/_mobile_offer.html`）在"详情"旁新增"在淘宝 App 打开"按钮，仅当该行携带官方淘宝联盟推广链接（`row.affiliate_promotion`，`s.click.taobao.com`）时显示；复用 `static/mobile.js` 的 `data-app-promotion` 唤端路径（手机端 `tbopen://` + 网页备用）。无联盟入口的来源不显示该按钮。
- 验证：新增渲染测试通过；专项 60 项、全量 368 项中 1 个**既有失败**（`test_single_item_price_is_not_rendered_twice`，stash 基线同样失败，与本改动无关）；本机 5002 与 ECS `/application` 均已加载该改动；ECS HEAD `5090068`，公网登录后 `q=抽纸` 页面按钮出现 100 处、`data-app-promotion` 与 `s.click.taobao.com` 锚点各 200 处。实体手机唤端与佣金归因仍需实体机回放。

## 5.2 2026-10-12 假优惠与荒谬单价拦截

- 背景：淘宝联盟 `reserve_price` 是商家自设划线价，可任意虚高，产生大量"一包纸优惠几百元、付款几块钱"的假优惠；生活品关键词还误命中几千上万的工业设备/荒谬组合价。
- 规则 1（参考价差合理性）：`services/taobao_alliance.py::reference_drop_plausibility` 判定划线价差不可信的情形——降幅达到参考价 85% 或以上（含边界；2026-10-12 用户反馈"优惠力度大的都是假的"后由 90% 收紧至 85%，拦截恰好九折 KUB 乳霜纸 629 元、89.5% 柴火大米 626 元类）、参考价超过推广价 20 倍、或推广价低于 10 元且优惠额超过 50 元。`parse_item` 的 `price_drop` 信号、`services/product_search.py::official_promotion_discount` 的参考价差候选共用该门控；存量数据在展示层即时生效（`validated_offer` 运行时重算）。保留审计字段 `reference_drop_plausible/reference_drop_reason`，snippet 追加"参考价过高，未据此认定优惠"。
- 规则 2（生活品主题单价上界）：`services/taobao_living_topics.py::TOPIC_PRICE_CEILING_CENTS`（纸品 300/饮用水 400/大米 500/面粉 400/食用油 2000/洗衣液 300 元）+ `living_topic_ceiling_cents(source_name)`（按核心主题关键词匹配来源名，自定义来源不拦）；`promotion_admission` 淘宝分支对超上界推广价返回 `eligible=False, tier='promotion_clue'`，reason 注明"合理上界"，仅来源审计。
- 真实数据回放（100,707 条）：假优惠（优惠>50 元且付款<10 元）1661 条 **100% 拦截**；旧降价信号 58,156 条中新规则判定不可信 4,689 条（含 85–90% 区间 1,636 条与恰好 90% 边界 131 条）；超主题上界 486 条（其中 115 条原为优惠信号被拦出默认列表）。收紧后默认列表优惠额 TOP 全为真品（山茶油礼盒、五常大米 20 斤、PDO 橄榄油、低氘水 12 瓶）；coupon 形态大额假优惠 0 条。高端真品（山茶油 5L 1308 元、橄榄油 3L 616 元）在上界内不受影响。
- 测试：新增 6 项专项（合理性边界、解析信号、官方优惠额、准入拦截、真品不误伤），后按用户反馈将 90% 边界收紧至 85%（含恰 85%，新增 KUB/柴火场景断言）；全量 `unittest discover -s tests` 374 项，唯一失败为既有 `test_single_item_price_is_not_rendered_twice`（基线同样失败）。文档：`docs/current-product-contract.md` 第 3 节新增价格规则 6/7 与准入表"可解释的当前优惠信号"表述；`docs/core-acceptance.md` 增补 2026-10-12 拦截验收记录。

## 5.2b 2026-10-12 数据驱动校准替换写死阈值（假优惠自适应收尾）

- 背景：用户明确要求"假优惠判断不能写死、要用非常不错的算法解决"。固定阈值（85%/20 倍/10 元-50 元/主题上界）的 85% 收紧仍遗留：①低价箱被假货污染，85–90% 区间假优惠仍漏网；②高价箱尖刺误伤高端真品（康泽泉低氘水 358 元 61% 曾被 water 高价箱 46% 界误拦）。
- 模块 `services/discount_calibration.py`（新建，判定边界全部由存量语料统计估计，worker 每小时按数据新鲜度重算落盘）：
  - `calibrate(db)`：每主题估计 `price_ceiling_cents = median(log10 promo)+6×MAD(log10 promo)`；按推广价 0.5 log10 宽分箱（1.0–7.5），每箱取降幅与优惠额 P97（校准样本剔除降幅 >0.92 的虚高尾段防污染；箱样本 <30 回退全局 P97）；`high_log = P97 所在箱上界`（高价豁免线）。落盘值：paper ceiling ¥356 high ¥100；water ¥457/¥316；rice ¥1142/¥316；flour ¥489/¥316；cooking_oil ¥2555/¥1000；laundry ¥646/¥316。
  - `judge_drop(topic, reserve_cents, promotion_cents)`：荒谬单价超上界 → 拦（"推广价超出…合理上界"）；推广价 ≥ high_log → 豁免（只受上界约束）；否则降幅/优惠额任一超所在箱界 → 拦。topic 可 None（来源外主题回退全局/种子）。`price_ceiling_cents(topic)` 供 `promotion_admission` 复用（有校准用校准上界，否则回退 `living_topic_ceiling_cents`）。
  - 接入：`taobao_alliance.py::reference_drop_plausibility` 改为委托 `judge_drop(None,…)`（REFERENCE_* 常数保留为种子注释）；`product_search.py::official_promotion_discount`/`promotion_admission` 改用 `topic_key(source_name)+judge_drop()/price_ceiling_cents()`；`db.py::initialize()` 建 `discount_calibration` 表并 `load_calibration`；`app.py` 两个 worker 循环每轮 `refresh_if_stale(connection)`（60 分钟重算）。种子回退：无校准/样本不足时 0.85、20 倍、10 元/50 元、原主题上界。
- 真实数据回放（六主题 103,135 条，有划线 57,511 条）：拦 4,169（高价 63 + 降幅 3,709 + 优惠额 397；纸 2,563/水 371/米 162/面 408/油 109/洗衣液 556）。关键假货全拦：KUB 69.9→699、柴火 73→699、袁谷 188.3→1016、百岁山 37.1→300、42 元面粉 88%、碾米机 1380、威露士 2997。关键真品全保留：康泽泉低氘水 358、山茶油礼盒 1498、依云 108.9、赣江 2L 茶油 208、食刻橄榄油 54.9。本机页面回放（`q=抽纸/大米/食用油/饮用水/洗衣液&sort=discount_high`）：KUB/柴火优惠从排序消失，各主题 TOP 优惠额全为真品（纸 596.9/米 499/油 960/水 169.2/洗衣液 293.5）。
- 测试：`tests/test_discount_calibration.py` 8 项（quantile/bin 映射、calibrate 估计、箱界判定、premium 豁免、种子回退与旧规则等价、价格上界种子、refresh 重算、topic 映射）；全量 `unittest discover -s tests` 382 项，唯一失败仍为既有 `test_single_item_price_is_not_rendered_twice`（基线失败）。文档：契约第 3 节规则 6/7 重写为自适应描述；`core-acceptance.md` 增补 2026-10-12 自适应收尾验收。提交与部署：`git diff --check` 通过后提交推送，ECS `/application` 快进并重启 web+worker，公网登录回放同本机。

## 5.2c 2026-10-12 生产部署修正（自适应校准落地三连修）

- **Gunicorn(wsgi:app) 不经过 app.py main()**：ECS web 进程从未建表/校准，公网实际是固定种子兜底。修复：`create_app()` 启动即 `refresh_if_stale(connection)`（表空/过期则自行校准落盘并加载）；worker 每小时刷新不变。触发方式：`systemctl restart` 后 web 首次启动即全量校准。
- **生产库路径**：systemd 注入 `Environment=WORKBENCH_DB=/application/.runtime/workbench.sqlite3`（**不是** `/application/data/workbench.sqlite3`）。手工 exec 脚本若不 source `.runtime/workbench.env` 会连错库（data/ 是开发/旧数据 57MB，生产库约 33MB）；验证/回放脚本必须显式带 `WORKBENCH_DB=...` 或 `set -a; . .runtime/workbench.env; set +a`。
- **官方来源命名**：生产库来源名为"淘宝生活品·纸品优惠搜索"（本地开发为"淘宝店抽纸优惠搜索"）。`topic_key` 与 `calibrate` 的 LIKE 收集均改为 name（纸品）+keyword（抽纸）双匹配；否则 paper 主题永远无校准回退种子。
- **校准快照对账（prod `.runtime/workbench.sqlite3`）**：6 主题 sample paper 11252 / water 3440 / rice 2691 / flour 5531 / cooking_oil 3605 / laundry 6515；paper global_ratio_cap 0.8995、high 豁免线 ¥100。生产判定复核（WORKBENCH_DB 注入后）：KUB 拦（86% 界）、柴火拦（81%）、袁谷拦（81%）、百岁山拦（80%）、威露士荒谬高价拦、低氘水 358 放、5 折放。公网回放：五主题 TOP 优惠额全为真品，KUB/柴火无优惠。
- 部署操作：ECS `git fetch && git merge --ff-only origin/feature/bootstrap` → `sqlite3 delete from discount_calibration`（强制重建；表新鲜时 refresh 会跳过）→ `systemctl restart opportunity-workbench-web opportunity-workbench-worker` → health + 公网回放。

## 5.2d 2026-10-12 保真优先：划线虚高商品保留展示（不误伤真实捡漏）

- 背景：用户强调"一定不要误伤真实的要优惠捡漏的产品"。此前 judge_drop 判定不可信的参考价差使 `price_drop_candidate=False`，商品若无券/无路径则促销信号全空 → 从默认列表消失——真实便宜货（推广价可信）被误伤。
- 修复：`services/taobao_alliance.py::has_current_promotion_signal` 增加兜底——**存在参考价差（reserve>promo>0）本身即准入信号**，与可信性解耦。划线虚高商品：以官方推广价保留在默认列表，**不产生优惠额、不参与优惠额排序、不显示"优惠 X 元"**，snippet 保留"参考价过高，未据此认定优惠"；荒谬单价（推广价超校准上界）仍仅来源审计；划线=推广价且无券无路径的普通商品仍不准入。
- 真实数据回放（本机 10.4 万条）：被拦但保留 纸 2634 / 水 360 / 米 155 / 面 423 / 油 114 / 洗衣液 569；荒谬移除 纸 229 / 水 71 / 米 24 / 面 31 / 洗衣液 6；无信号普通商品仍排除（纸 1539 等）。页面回放：KUB 乳霜纸在列表（推广价展示、无优惠标签、含"参考价过高"说明），威露士 2997 不在列表，`sort=discount_high` TOP 优惠额全为真品（596.9/595.99/595.9）。
- 测试：`test_parse_item_inflated_reference_drop_is_not_a_promotion_signal` 扩展断言被拦后仍有准入信号；新增 `test_has_current_promotion_signal_keeps_reference_gap_but_excludes_plain_items`；全量 383 项唯一失败仍为既有基线。文档：契约规则 6 增补"保真优先"条目。

## 5.2e 2026-10-12 品类词过滤解霸榜 + 豁免区全局降幅界 + 移动端折扣率标签

- 背景：用户最新痛点"找不到真正优惠的数据"。诊断（/tmp/diag.txt）发现头号原因是**主题关键词误命中非本品类商品霸榜**：大米主题 TOP 优惠额第一是"科颜氏大米小麦洗发露"（优惠 ¥5601、推广 ¥399、降幅 93%）、"Reiss 大米色条纹服饰"（¥828）、另一科颜氏（¥600）——品类与主题不符，却因标题含主题词被采集、因高价豁免区放行产生巨额优惠额霸榜，用户看不到真优惠。
- 修复（提交 `bf2f4dc`，已推 GitHub 并部署 ECS）：
  - `services/discount_calibration.py`：新增 `TOPIC_CATEGORY_TERMS`（六主题 include 强词/exclude 干扰词）+ `_GENERIC_INTERFERING_TERMS` + `category_matches(topic,title)`（无主题/未知/空标题返回 True 不设卡；管理主题需命中 include 且不含 exclude/通用干扰词）；
  - `services/product_search.py::official_promotion_discount`：参考价差候选新增 `category_ok` 条件（import 已在第 8 行）；
  - `judge_drop` 豁免区（high_log 以上）：新增全局降幅界——`ratio > global_ratio_cap` 即拦（reason"降幅超过该主题全局可信上界"），消灭豁免区 90%+ 划线虚高（拦洗发露 93%，保留低氘水 61%/山茶油 55-68%）；
  - `routes/search.py`：`build_row` 新增 `mobile_discount_rate`（参考价差 basis 直接用官方 rate_basis_points，券/路径按 before/after 现算）；`templates/_mobile_offer.html` 移动端卡片显示"省 X%"。
- 验证：本机真实回放——品类错配过滤 339 条，可信价差 TOP12 全真品（山茶油 5L/低氘水/秋味坊/赣江/PDO 橄榄油/KUB 整箱）；大米主题官方优惠 TOP 十月稻田 50kg ¥1000/33%、龙稻 5kg ¥300/62.5%、柴火 5kg ¥200/71.4% 全真米。公网 ECS 回放：科颜氏/Reiss 0 出现、"省 X%"与"在淘宝 App 打开"按钮正常。测试：新增 `test_category_matches_blocks_look_alike_products` + 豁免区全局界扩展用例；全量 384 项唯一失败仍为既有基线 `test_single_item_price_is_not_rendered_twice`。部署：`git merge --ff-only origin/feature/bootstrap` + `systemctl restart opportunity-workbench-web opportunity-workbench-worker`；品类过滤不改变校准表数据，refresh_if_stale 照常管新鲜度。
- 词表维护：品类词是维护性词典（非业务阈值），主题关键词新增采集词时需同步检查 `TOPIC_CATEGORY_TERMS` 的 include/exclude 是否覆盖新品类边界。

## 5.2f 2026-10-13 低价小件/引流 SKU 去优惠化（百岁山 348ml 4 块多不算优惠）

- 背景：用户打开"【尝鲜】百岁山 348ml 2 瓶"（推广 ¥10.01、划线 ¥19.9、省 50%）明确"不算优惠"；price_low/优惠排序仍被低价小件霸屏（¥2.01 抽纸大量精确同价划线虚高省 70-85%、植护 ¥2.39 省 84.8%、洁柔 ¥2.99 省 69.8%）。诊断：引流小规格/首单统一补贴价不是真优惠，却走参考价差/单券/完整路径任一候选产生优惠额霸榜。
- 修复（提交 `54396d3`，已推 GitHub、已部署 ECS、生产库已重校准）：
  - `services/discount_calibration.py`：
    - `TRIAL_GATING_TERMS`（尝鲜/首单/新人/新客/试饮/试用/小样/引流/体验装/一分购/元购）+ `has_trial_gating_marker(title)`（空/None→False）；
    - `GATED_MIN_COUNT=5` + `_gated_promo_prices(promo_values)`：主题内低于中位数的精确同价（分）聚集 ≥5 → 引流统一价；`calibrate` 落盘 `gated_prices`；
    - `low_price_floor` = 主题推广价 P15（数据驱动低价小件线）；`is_gated_price(topic,cents)`、`is_low_value_small_item(topic,cents)` 查询函数；
    - `judge_drop` 新增 gated 拦截（reason"低价引流统一价（首单/新人/试饮等小规格引流 SKU），不作为优惠证据"）。
  - `services/product_search.py::official_promotion_discount`：
    - **函数级守卫**：`has_trial_gating_marker(title)` 命中即 return None（覆盖参考价差/单券/完整路径全部候选——初稿只拦参考价差，尝鲜装走促销路径仍产 ¥9.89）；
    - 单券候选按券后价 `quote_price_cents`、完整路径候选按 `estimate_cents` 分别过 `is_gated_price` + `is_low_value_small_item`；参考价差候选过 `judge_drop`（含 gated）+ `is_low_value_small_item(promotion)`。
- 验证：本机重校准（DELETE discount_calibration→calibrate→save→load）：paper gated ¥1.41-2.20 等、water ¥2.11-6.30、laundry ¥2.01-3.61；`judge(paper 990→201)=False`、`judge(water 1990→1001)=False`；尝鲜百岁山 official=None。页面回放：搜"百岁山"23 条全整箱真记录（24 瓶 ¥92.52 省 53.5% 等）；price_low 小件引流清零、剩 ¥7.9 大提整箱；discount_high TOP8 全真品。公网 ECS：搜百岁山 10 条全整箱（旗舰店 570ml×24 ¥74.9 省 61.4%）、price_low 无小件、TOP 全真品大件。全量 388 项唯一失败仍为既有基线。
- 部署要点：本改动新增 `gated_prices`/`low_price_floor` 校准字段，**生产部署必须重校准**（`DELETE FROM discount_calibration` + `WORKBENCH_DB=/application/.runtime/workbench.sqlite3 PYTHONPATH=/application .venv/bin/python -B -u <脚本>`，base64 通道；重启服务后 refresh_if_stale 在表新鲜时不会再重算）。生产口径 floor：paper 4.00/water 21.00/rice 17.76/flour 9.99/oil 35.88/laundry 10.90。

## 5.2g 2026-10-13 第4层市场价带判据 + 引流形态词扩展（跟其他平台价格一样不算数）

- 背景：用户强调"一定要去除引流的假优惠，最后算下来跟其他平台价格一样、相差就一两块钱，肯定不算数"。引流假优惠本质=到手价在市场正常价带内（划线虚高造优惠）。
- 修复（提交 `a3f7243`，已推 GitHub、已部署 ECS、生产库已重校准）：
  - `services/discount_calibration.py`：
    - `MARKET_BAND_MAD_K=1.0` + `DEEP_DROP_QUANTILE=0.90`；`calibrate` 落盘 `market_band_low/high`（推广价中位数 ± 1.0×MAD，**用分值中位/MAD，勿用 log 值**——初版误用 log 中位数导致市场带≈0，已修）与 `deep_drop_threshold`（主题降幅 P90）；
    - `is_market_band_fake_discount(topic, final_cents, ratio)`：`market_band_low <= final <= market_band_high 且 ratio >= deep_drop_threshold` → 假优惠（未知主题/非法入参返回 False）；
    - `TRIAL_GATING_TERMS` 扩展：首购/首件/试吃/试喝/随身装/便携/迷你/U先/新人礼/首单礼金/免费领/1分钱；**移除 0.01/0.1元 数字子串词**（会误伤"¥10.01"标题）。
  - `services/product_search.py::official_promotion_discount`：参考价差候选按推广价、单券按券后价、完整路径按路径预估，三个候选均新增 `is_market_band_fake_discount` 检查（与 is_gated_price / is_low_value_small_item 并列）。
- 验证：真品全放（KUB ¥188.9 带外、整箱水 ¥92.52 带外、五常米 ¥200 带外、悬挂纸 ¥7.9 降幅 79%<paper P90 界 91.6%）；深划线+市场价全拦（¥8.5 省95%、¥40 省80%、¥35 省82%）。本机库新增拦截 0（组合已被前层消化），判据单测保障、生产漂移后自动生效。公网回放：百岁山 7 条全整箱、price_low 全量大整箱、TOP 全真品。全量 389 项唯一失败仍为既有基线。
- 部署要点：新增 `market_band_low/high`/`deep_drop_threshold` 校准字段，**生产部署必须重校准**（DELETE discount_calibration + WORKBENCH_DB/PYTHONPATH 注入 + base64 通道）。生产口径：paper 带 ¥4.01-11.79 深降幅 0.916；water ¥23.9-54.1 0.717；rice ¥19.7-59.9 0.800；flour ¥12.93-38.27 0.708；oil ¥49-150.8 0.667；laundry ¥15.8-44 0.799。

## 5.2h 2026-10-13 默认列表准入四层排除（所有产品不要出现引流假优惠）

- 背景：用户"是针对所有产品不要出现上面的数据"——引流/划线虚高假优惠不只是去掉优惠标签，**整个商品都不应出现在默认列表**。已核实本机 114843 条/生产 36178 条联盟记录 100% 归属六主题（非主题=0），"所有产品"即全部联盟商品；默认 view=offers 已按 `public_offer_eligible` 过滤列表。
- 修复（提交 `9dc433f` + `8412587`，已推 GitHub、已部署 ECS、生产库已重校准）：
  - `services/product_search.py::promotion_admission` taobao_alliance 分支在荒谬单价上界检查后新增**四层排除**，任一命中 → `eligible=False, tier='promotion_clue'`（仅来源审计，默认列表消失）：
    ① `has_trial_gating_marker(title)`（引流形态词）；② `is_gated_price(topic, promo)`（低价统一价聚集）；③ `is_low_value_small_item(topic, promo, title)`（低价小件）；④ `is_market_band_fake_discount(topic, promo, (reserve-promo)/reserve)`（深划线+市场价带）。
  - `services/discount_calibration.py`：**规格量判据**（数据驱动）——`SMALL_SPEC_LIMITS` 按量纲（抽/张/片<200、包/提<6、瓶/支/粒<6、ml<500、L/升<1.5、kg/斤<2，物理商品属性非优惠阈值）识别便携小件：`small_spec_limits(title)` 提取标题规格量，**所有量纲均小于上限**才算小规格（任一大量纲如 400抽30包/24瓶/1000张 → 不拦，量大真品永不误伤）；`is_low_value_small_item` 双通道：**有规格 → 小规格且低于主题推广价 P50（`price_p50_cents`）**；**无规格 → 低于 P15 线**（原行为不变）。`calibrate` 落盘 `price_p50_cents`。
  - 去优惠化层（official_promotion_discount 参考价差/单券/完整路径）同步传 title，两层行为一致。
- 验证：本机/公网 price_low 引流小件消失（¥4.40 30抽便携装、尝鲜百岁山 348ml×2 从列表消失），剩余 ¥5.29 400抽30包/¥6.19 1000张/¥7.9 悬挂纸/¥6.66 500张全为量大真品；discount_high TOP8 全真品（KUB ¥188.9、龙稻五常米 ¥300、十月稻田 ¥1000、格兰杰洗衣液）；搜百岁山 6 条全整箱。新增测试：AllianceTrafficAdmissionTests（准入四层正反例）+ SmallSpecLimitsTests（便携 vs 量大、双通道）；全量 391 项唯一失败仍为既有基线 `test_single_item_price_is_not_rendered_twice`。
- 部署要点：新增校准字段 `price_p50_cents`，**生产必须重校准**（DELETE discount_calibration + WORKBENCH_DB/PYTHONPATH 注入 + base64 通道；`echo $B64` 不要写成 `\$B64`，否则远端收到空脚本）。生产口径（10-13 重校准）：paper floor ¥4.00 p50 ¥7.90；water ¥21.05/¥39.00；rice ¥16.90/¥39.00；flour ¥9.90/¥25.00；oil ¥35.20/¥99.90；laundry ¥10.80/¥29.90。

## 5.2i 2026-10-13 品牌标准市场价判据（优惠价小于等于品牌单品市场价才算真优惠）

- 背景：用户"如果能增加品牌的产品进行参考，一件/一瓶/一包这样的数据进行参照……优惠价金额只要按照优惠小于等于这些，小于市场价就行，这个你是知道的要进行维护"——用**品牌标准售卖单位的市场价**做参照：折算标准单位到手价 > 品牌标准市场价 = 划线虚高/引流小规格。
- 修复（提交 `7d2ba24`，已推 GitHub、已部署 ECS；**纯常量表改动，无需重校准**）：
  - `services/discount_calibration.py`：
    - `BRAND_TERMS` 品牌词表（六主题主流家用品牌，维护性词典）；
    - `BRAND_STANDARD_PRICES` 品牌标准市场价表（分；water=500ml瓶、paper=100抽包、rice/flour=kg、oil=L、laundry=kg；**用户可直接增改**，初始值按主流零售常识）；
    - `spec_unit_price_cents(title, cents)` 规格折算引擎：`_BOX_RE/_BAG_RE` 乘法（箱×瓶/包、袋×kg、瓶×升），优先级 kg > L > 500ml瓶 > 瓶 > 100抽包 > 包；**纯单瓶描述（570ml/瓶）不折算**（否则把整箱价当单瓶价误判）；
    - `is_brand_market_overpriced(topic, title, final_cents)`：折算单价 > 标准市场价×`BRAND_OVERPRICE_K(1.2)` → 真；未知品牌/无规格/无标准价永不拦。
  - `services/product_search.py`：`promotion_admission` 第 5 层排除（reason 优惠价高于品牌标准市场价）+ `official_promotion_discount` 参考价差/单券/完整路径三候选接入。
- 验证：尝鲜百岁山 348ml×2 ¥10.01 → 500ml 折算 ¥7.19 > ¥2.80×1.2 拦（准入层+去优惠化层双拦）；整箱真品全放——百岁山 570ml×24 ¥23.59/¥63.9/¥92.52、维达 100抽24包 ¥49.9（单包 ¥2.08 < ¥3.00×1.2）、KUB ¥13.11/包（≈可优比 ¥12×1.2 边界放）、柴火五常米 ¥40/kg、十月稻田 ¥20/kg、龙稻 ¥30/kg、金龙鱼 5L、蓝月亮 3kg×2。公网回放：price_low 全量大品、discount_high 全真品、搜百岁山 5 条全整箱、搜维达 57 条全整箱/大规格。
- 测试：`BrandMarketReferenceTests` 12 项（折算乘法/优先级/纯单瓶描述 None + 品牌判据正反例）；全量 394 项唯一失败仍为既有基线 `test_single_item_price_is_not_rendered_twice`。
- 维护说明（配置全面化 `BRAND_REFERENCE_CONFIG`，95 品牌，2026-10-13 ⑩/⑪ 后）：**唯一配置源** `services/discount_calibration.py::BRAND_REFERENCE_CONFIG`——每品牌 `{"terms": (品牌词/别名…), "unit": 标准单位, "price_cents": 市场价分, "note": 说明}`。新增/改品牌只改这张表：terms 越长者优先（"华润五丰"先于"五丰"）；unit 必须与折算引擎输出一致（bottle500/bottle/kg/liter/pack100/pack），商品折算单位与配置单位不同则不比（保守放行）；price_cents 单位分。标准单位口径：water=500ml 瓶(bottle500)、paper=100 抽包(pack100)、rice/flour=公斤(kg)、cooking_oil=升(liter)、laundry_detergent=公斤(kg)。

## 5.2j 2026-10-13 品牌判据误伤修复（真实数据全量审查：拦截 4183→358，-91%）

- 背景：品牌判据上线后用户"继续检查与优惠对比"——本机 11.8 万联盟记录全量重放发现**误伤 4183 条**：乳霜纸/悬挂式/婴儿柔巾（paper 499）、苏打/迷你瓶装（water 846）、五常/礼盒米（rice 191）、烘焙/全麦/新疆面粉（flour 2348）、花生/橄榄/菜籽/玉米油（oil 1497）、浓缩/除菌洗衣液（laundry 365）等**细分品类单价系统性高于普通标准价**，被单一标准价判据误拦真品。
- 修复（提交 `029405a`，已部署 ECS，无需重校准）：
  1. **BRAND_EXEMPT_TERMS 细分品类豁免词表**（六主题扩充，命中即跳过品牌判据；假优惠由③低价小件/④市场价带层兜底，真品不误伤）。
  2. **规格引擎增强**：g/克 单位（500g*10→5kg）、斤→kg 0.5 系数（1斤=0.5kg）、×N 泛乘法（10kg*2/5KG*4/2.5kg*4/1.8l×2桶）、N包×M（24包*3）、×N 与 N袋 去重、重复计量取 max（"2.5kg 5斤"是同一量不叠加）。
  3. **标准价按真实市场价校准**：鲁花 24→36/L、胡姬花 20→30/L、屈臣氏 3→4/500ml、泉阳泉 2→2.5、金沙河 5→6/kg、五得利 5→8/kg、古船 5→6、天山 10→13/kg、中裕 6→8、香满园面粉 7→9、汰渍 15→25/kg、白猫 10→13、斑布 2→3.5/100抽、洁云 2→2.8、维达 3→3.5、妮飘 4→12。
  4. **溢价系数**：water 小瓶×1.4（280-410ml 单 ml 价高）、paper 抽数<100 ×1.3（70抽 3 包装）、flour 总量<5kg / laundry 总量<2kg 小包装豁免（`bulk_kg_or_none`）。
- 验证：误伤样本 33 例全放行（乳霜/悬挂/花生油/菜籽油/新疆面粉/礼盒米/浓缩洗衣液/尝鲜外的小瓶装）；假优惠仍拦（尝鲜百岁山 348ml×2 ¥10.01、心相印茶语 110抽3包 ¥14.01 真贵）；全库重放：品牌拦截 4183→358（paper 49/water 35/rice 21/flour 177/oil 35/laundry 41），准入 55611→59470（+6.9%）。公网回放：搜菜籽油 9 条全真品、搜面粉 100 条全真品、百岁山 6 条全整箱、price_low 全量大品。全量 394 项唯一失败仍为既有基线。
- 维护口径：豁免词 = 细分品类/高端线（单价系统性高于普通标准品类的词），新发现误伤先加豁免词或校标准价，勿放宽系数。

## 5.2k 2026-10-13 品牌捡漏基准（AC-05 生效：从"过滤假优惠"升级为"标注真捡漏"）

- 核心产品需求：用户多次痛点"找不到真正优惠的数据"；此前六层负向过滤已闭环，但品牌标准市场价表（95 品牌，用户要求全面维护的配置数据）只用于拦截、未用于正向标注。本轮让该配置直接驱动真捡漏结论与排序，AC-05 从"实现待配置"变为实际生效。
- 实现（提交 `b7432e3`，已部署 ECS，纯常量+逻辑，无需重校准）：
  1. **`assess_brand_baseline`**（discount_calibration）：品牌折算标准单价 / 配置标准市场价 → ratio 分档：≤0.6 可捡漏(rank5)/≤0.85 优惠(4)/≤1.0 低于常规(3)/≤1.2 常规(2)/>1.2 偏高(1)；ratio<0.3 标"异常偏低待复核"(rank0，防假货/规格解析异常霸榜)。schema 与人工维护基准完全一致，列表/详情/comparison 排序零改动复用。
  2. 三处接入（search 列表 / 缓存分支 / opportunity 详情）：人工基准未命中（no_match/unit_unavailable/not_official_promotion）→ 品牌基准回退；**主题用 `topic_key(source_name)`**——`display_topic` 只有 food/home 粗分类（首版标签不出的根因）。
  3. 豁免词只作用于拦截侧，捡漏侧忽略（柴火五常米/新疆面粉等真品才能获得标注）；新增 `BRAND_BASELINE_EXCLUDE_TERMS` 非本品类混品守卫（rice: 小米/黄小米/杂粮/糯米/黑米/糙米/藜麦/燕麦米——混品不能用大米标准价误标）。
  4. comparison"维护基准可捡漏优先"排序实际生效（rank 置顶）；人工 maintained_price_baselines 仍优先于品牌基准。
- 验证：本机 comparison 100 条 87 可捡漏+13 优惠全真品（新疆天山面粉 ¥5.99/kg、河套全麦 ¥8.97/kg、乔府大院 ¥23.6/kg vs 市场 ¥30）；黄小米误标 0；公网 70 可捡漏+30 优惠（鲁花葵花仁油 ¥19.98/L vs 市场 36 = 5.5 折、新良蛋糕粉 ¥4.54/kg）。单测 13 项；全量 408 项唯一失败仍为既有基线。
- 维护口径：品牌标准市场价表既是拦截基准也是捡漏基准；捡漏档位阈值在 BRAND_BASELINE_RANKS（0.6/0.85/1.0/1.2），发现误标先加 EXCLUDE_TERMS 或校标准价，勿放宽档位。

## 5.2l 2026-10-13 需求落实收尾（移动端捡漏标签 + 用户样本防回归 + 品牌别名 bug）

- 用户要求"产品的需求肯定要落实并且要把遇到的问题规避"。
- 需求落实补缺：品牌捡漏结论此前只在桌面表格/卡片显示，**移动端 _mobile_offer 漏掉**——补 `mobile-offer-baseline` 标签行（与桌面同 state 判定，bargain 高亮）。公网实测：搜鲁花，移动端+桌面均显示"按品牌基准：可捡漏 · ¥19.98/liter"等 10 条。
- 问题规避（防回归）：新增 `tests/test_acceptance_samples.py`（25 项），把用户多轮明确认可/否定的真实样本固化为自动化回归：必须拦截（尝鲜百岁山 348ml×2、茶语 110抽3包 ¥14.01、维达 1包 ¥10、五得利 5kg ¥123.58/10斤 ¥123.71）、必须放行（百岁山整箱×2、KUB 乳霜纸、柴火五常米、维达整箱、悬挂纸、多力菜籽油、新疆面粉、蓝月亮整箱）、捡漏标签正反例（天山/鲁花可捡漏、黄小米混品不标、真贵偏高）、六主题 topic_key 映射、95 品牌配置完整性。
- **顺带发现并修复真实 bug**：`brand_from_title` 命中英文别名（vinda）后返回 term 而非配置 key → 别名标题查不到标准价、**绕过品牌判据**。改为返回配置 key，别名标题同样参与贵过市场价判据（新增回归用例）。
- 验证：防回归 25 项通过；全量 433 项唯一失败仍为既有基线；本机移动端 25 条标签、公网 10 条。
- 维护口径：以后每轮改动跑 `python -m unittest discover -s tests`（433 项），唯一允许失败 = 既有基线 `test_single_item_price_is_not_rendered_twice`；用户新增"认可/要拦"样本先加进 acceptance_samples。

## 5.3 2026-10-12 登录页"保持登录状态"

- `templates/login.html` 登录表单新增"保持登录状态（30 天）"复选框（`name="remember"`）；`routes/auth.py::login` 在凭据校验通过后按复选框设置 `session.permanent`；`app.py` 配置 `PERMANENT_SESSION_LIFETIME = timedelta(days=30)`。
- 勾选时登录 cookie 带 `Expires`（30 天后过期），不勾选则维持浏览器会话 cookie（关闭浏览器即失效）。密码本身不落盘，仅保持登录态；`/logout` 仍即时清除会话。
- 测试：新增 2 项（勾选→permanent+Expires、不勾选→会话 cookie）；全量 374 项仅剩既有失败 `test_single_item_price_is_not_rendered_twice`。本机 5002 登录页实测复选框与两种 cookie 行为正确。

## 6. 安全与交付

不要将本机 `.env`、服务器 `.runtime/workbench.env`、App Secret、管理员密码、会话密钥或数据库写进交接文档、终端输出或 Git。远端操作优先按已安装的 `alibabacloud-workbench-cli` Skill 和 `docs/deployment-aliyun-ecs.md`；仅在用户当前明确授权的范围内变更外部系统。GitHub 分支当前比本机落后，下一位 AI 不能只拉取远端再宣称拿到最新代码。若需发布，先核对当前凭据、分支、差异、ECS 状态和用户意图。
