# 国内“捡漏／优惠线报”开源项目的实际渠道配置

> 最新家用、试用与免费活动检索及公开接口实测见文末2026-10-04补充；当前先采集与预估，旧文的个人结算前置建议已被修正。

核对日期：2026-10-03。方法：使用 GitHub CLI 搜索公开仓库，再读取配置文件、采集入口和 README。这里的“前辈”指公开维护这些项目的开发者；仓库并未提供其实际盈利凭证。代码声明支持、默认配置启用、当前可用、本人可买、能转卖获利是五件不同的事。

## GitHub CLI 核对入口

```sh
gh search repos '闲鱼 监控' --limit 10
gh search repos '什么值得买 推送' --limit 10
gh search repos '优惠券 淘宝 京东 拼多多' --limit 10
gh api repos/abinnz/xiaofendui/contents/config.json -H 'Accept: application/vnd.github.raw'
gh api repos/abinnz/xiaofendui/contents/crawler/zhidexiang.py -H 'Accept: application/vnd.github.raw'
gh api repos/lx1169732264/zdm/contents/readme.md -H 'Accept: application/vnd.github.raw'
gh api repos/why2lyj/youxiang-Itchat/contents/_config.yaml -H 'Accept: application/vnd.github.raw'
gh api repos/ChaosTechDev/xianyu-hunter/contents/config.json.example -H 'Accept: application/vnd.github.raw'
```

仓库搜索只能发现候选，以下以源码中的入口和配置为准；没有把 star 数、营销介绍或示例配置当收益证据。

## 实际配置与用途

| 项目及源码 | 实际入口／默认配置 | 筛选与推送 | 对本项目的意义与边界 |
|---|---|---|---|
| [xiaofendui 配置](https://github.com/abinnz/xiaofendui/blob/master/config.json)、[采集器](https://github.com/abinnz/xiaofendui/blob/master/crawler/zhidexiang.py) | 从“值得享”一个聚合 API 取列表，按 `resource/resourceTag/userName` 分出 0818tuan、小屁屁挖白菜、蘑菇小牙牙、披着羊毛的魔鬼等内容源；按 `mall` 标记京东、淘宝、天猫 | 每内容源 `INCLUDE/EXCLUDE/MALLS/NONMALLS`，30 秒轮询，发微信群 | 借鉴“聚合内容源 × 实际商城 × 关键词”三级标识。四个内容名和三个商城标签**不是七个独立实时采集器**。项目老旧，聚合 API 当前可用性未验证。 |
| [zdm README](https://github.com/lx1169732264/zdm/blob/master/readme.md) | 什么值得买“好价排行榜”一个上游；可选 Cookie，README 也提 Selenium 获取 Cookie | `minVoted/minComments`、标题黑白名单、SQLite 去重，邮箱／WxPusher | 借鉴榜单质量门槛与去重。README 更新日志记录了 Cookie 和反爬变动；投票高不等于本人最终付款低，也不等于转卖收益。 |
| [youxiang-Itchat 配置](https://github.com/why2lyj/youxiang-Itchat/blob/master/_config.yaml)、[README](https://github.com/why2lyj/youxiang-Itchat/blob/master/README.md) | 推广素材接口：淘宝 `is_open: True`，素材组覆盖综合、服饰、零食、餐饮券；京东 `False`，9.9 专区；拼多多 `True`，素材组 1、2；苏宁 `False`，素材组 2179。唯品会在 README 中尚未完成 | 各平台推广 API key／推广位、分群定时发微信 | 属于**优惠券导购佣金**，与囤货转卖是两种账。默认 True 但样例凭据为空；README 提到部分接口需更新，不能据此认定现可采集或可赚佣金。 |
| [ai-goofish-monitor README](https://github.com/Usagi-org/ai-goofish-monitor/blob/master/README.md) | 闲鱼单平台，按搜索任务设关键词、价格、地区、新发布、包邮、账号状态、定时与 AI 规则 | 多通知出口；须有闲鱼登录态 | 借鉴按具体品类／SKU 建任务和时效校验。本机项目已源码启动，但没有登录任务和结果，不能把闲鱼算作已产出渠道。 |
| [xianyu-hunter README](https://github.com/ChaosTechDev/xianyu-hunter/blob/main/README.md)、[示例配置](https://github.com/ChaosTechDev/xianyu-hunter/blob/main/config.json.example) | 闲鱼单平台：关键词、最低／最高价、地区、个人卖家、包邮、发布窗口、翻页；另有关注商品降价、下架、重上架与日价报表 | 通知和账号健康监测 | 借鉴“搜索任务＋关注商品生命周期＋价格分布”。示例里最低价 8000、高价 2000，区间颠倒，不能直接照搬；运行成功率和盈利仍需实测。 |
| [N95-watcher README](https://github.com/westnestling/N95-watcher/blob/master/README.md) | 指定口罩 SKU 的京东、淘宝／天猫、苏宁库存监控 | 缺货补货提醒 | 借鉴精确 SKU 的库存和价格事件；作者说明已停止维护，不能直接当现在可用的渠道适配器。 |
| [IKEA low price README](https://github.com/Mayandev/ikea-low-price/blob/main/README.md) | 宜家低价清单，约每周更新 | 清单展示 | 垂直品类可保留，但周级更新不适合作为抢实时低价的主信号。 |
| [RSSHub 代码](https://github.com/DIYgod/RSSHub/tree/master/lib/routes) | 值得买关键词／排行榜、逛丢 9.9、宜家低价／会员优惠、小米上新等是**不同路由** | 各路由参数、Cookie／会员态和源站稳定性不同 | 路由存在不代表本机成功。本机京东价格、部分逛丢／宜家会员路由此前失败；只把实际返回商品且有可核对发布时间的路由升级为可用来源。 |

## 对照当前工作台

截至本次只读核对，本机登记 21 个来源、机会记录 956 条，但 `verified` 状态为 0。启用来源包括什么值得买公开线索、Apple 翻新、小米商城、孔夫子旧书网、苏宁、联想、荣耀、政府采购中央／地方公告，以及小米、宜家、酷比、麦当劳等 RSS。闲鱼人工和京东拍卖人工仍是待核验／未自动接入。这个结构重“能列出公开链接”，轻“同一款的本人结算价、历史价、库存和退出价”。政府采购公告属于另一类服务商机，不应挤占零售捡漏的默认优先级；品牌资讯与麦当劳活动也不应仅因更新就被标成低价。

数据模型建议把 `内容来源`（值得买／逛丢／聚合作者）、`购买商城`（京东／淘宝等）、`账号价格条件`（新人／会员／券／地区／限购）、`商品/SKU`、`实际结算证据` 和 `出售退出渠道` 分列。聚合作者和商城标签不能累加为真实独立渠道数；同一优惠跨站转载需要去重。微信／QQ 是**通知出口**，不算货源。

## 按价值调整顺序（建议，尚未改动配置）

1. **线索层**：保留值得买，增加可实测的排行榜、关键词和逛丢 9.9 路由；按发布时间、值／评论和关键词筛选，并显示 Cookie、路由健康、最近成功检查。先确认每条路由真实出货，再启用。
2. **二手层**：把闲鱼从“人工来源”细化成任务：类目或精确 SKU、成色、价格区间、地区、卖家、发布时间、包邮、关注商品变价。需本人登录态；当前零结果要如实显示。
3. **购买层**：按具体 SKU 做京东、淘宝／天猫、拼多多、苏宁等商城的最终结算核验，记录账号资格、券、运费、数量、限购和失效时间。公开标价只算线索；新人价必须本人账号确认。不要把推广 API 价格当所有人可买价。
4. **变现层**：若走导购，单列推广素材 API、授权推广位、可结算佣金和退佣风险；若走转卖，单列近 30 天同规格已完成交易／最终回收报价、费用和库存风险。这两个模式应分别计算净收益。
5. **降权层**：政府采购、品牌新闻、周更清单保留在对应类别或留档，不列入“实时低价必买”默认流；只有同规格历史低价、本人新鲜结算价与可执行退出价同时成立，才进入强提醒。

## 核验边界

本次验证的是公开仓库**实际写出的渠道配置**与工作台当前来源结构；未使用他人平台账号、未验证这些项目当前能稳定抓取、未核验个人新人资格、未取得真实结算价或项目作者盈利数据。配置的 `is_open`、README 宣称“支持”和 GitHub 活跃时间都不是成交证据。当前工作台仍没有已证实“低价必买”的纸。

## 2026-10-03 接入结果

本机首先落地逛丢单平台的纸品、手机、新客三个关键词任务；每个实取 30 条，带原文时间分别为 20、28、9。RSSHub 的风云榜虽然返回 12 条，但全部无原文时间，故暂停；宽泛“新人”入口因婚礼商品误报暂停。RSSHub 源码已修正空时间被当成当前时间的问题。什么值得买榜单／关键词与逛丢九块九本机 503，没有标为已接入。闲鱼源码 UI 已恢复，但未有登录任务与结果。详情和命令见 `docs/environment.md`。这些是线索，不是最终买价或利润。


## 2026-10-04 家用优惠、试用与免费资源补充检索（当前结论）

本次按用户要求用GitHub CLI搜索和阅读源码，仅调查渠道与公开数据，没有安装执行脚本、申请试用或新增正式渠道。当前阶段依用户最新纠正：先来源/商品/价格/行情/费用完整采集与预估，后续才本人结算和交易。前文2026-10-03“购买层先个人核验”的优先级已被核心台账修正。

### 当前为什么覆盖不足

正式来源29条。日用品专项只有逛丢“纸品”关键词，另有宜家低价及通用商城页面；没有免费申领、试用、积分兑换专项入口。纸品、手机、宜家和麦当劳等部分来源检查时为failed。重复商城首页/品牌新品/政府采购入口无法替代家清洗护、厨房用品、母婴宠物、餐饮出行及免费活动清单。问题关联C01来源覆盖、C03条件采集、D06无效与噪声来源。

### GitHub CLI核实到的真实入口

| 用途 | 项目/代码 | 已核实的入口或能力 | 当前可用性与采用方式 |
|---|---|---|---|
| 日用家清及跨平台活动线索 | [RSSHub xianbao](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/xianbao/index.ts)、[xbk_monitor](https://github.com/LiuRJ99/xbk_monitor/blob/main/main.py)、[xianbaoku](https://github.com/edsionxuanxuan/xianbaoku/blob/main/push.py) | /plus/json/push.json；含title/content/url/catename/shijianchuo；RSSHub分类有赚客吧、新赚吧、微博、豆瓣、酷安等 | **公开JSON实测200有20条**；本地/xianbao为502。可复用数据格式在现有采集器适配，不需新管理站。分站分类和两个域名都不是独立交易证据 |
| 家居日用关键词与九块九 | [SMZDM keyword](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/smzdm/keyword.ts)、[逛丢cheaps](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/guangdiu/cheaps.ts) | /smzdm/keyword/:keyword明确需要SMZDM_COOKIE；/guangdiu/cheaps/:query?为九块九 | 路由存在不等于运行成功；当前尚未证明新增关键词路由可用。应覆盖洗衣液、洗洁精、垃圾袋、保鲜袋、清洁用品等，不能只搜“纸” |
| 京东试用/小样 | [jdtry任务源码](https://github.com/ZCY01/jdtry/blob/master/static/tasks.js) | 试用列表try.jd.com/activity/getActivityList；申请和搜索是不同任务 | 仓库pushed_at=2023-01-09，GPL-3.0，旧接口未实测，适合作为字段参考。当前[京东官方试用页](https://pro.m.jd.com/mall/active/3mpGVQDhvLsMvKfZZumWPQyWt83L/index.html?activityId=158888)写明申请/公布名单/下单领取，部分中选商品需6元运费；不是人人0元可得 |
| 跨平台积分/权益 | [Unify-Sign核心目录](https://github.com/TonyJiangWJ/Unify-Sign/tree/master/core) | 有京东、淘宝、支付宝、叮咚、饿了么、网上国网、小米等实现 | Android Auto.js执行项目，非通用优惠数据API；代码推送2025-05-31、GPL-2.0。用于梳理权益来源，不直接移植整个手机执行环境；本次未验证当前奖励 |
| 店铺会员/0元试用活动 | [jdm试用脚本](https://github.com/6dylan6/jdm/blob/main/jd_wx_zeroTrial.js) | 输入店铺活动URL、店铺ID/活动ID；源码注释区分是否入会、是否推送 | 拿到活动链接后的执行脚本，不是发现所有活动的数据源；pushed_at=2025-07-08。未运行；README自述无审查，不整库导入 |
| 淘宝/京东/拼多多/苏宁券与推广素材 | [youxiang-Itchat配置](https://github.com/why2lyj/youxiang-Itchat/blob/master/_config.yaml) | 明确四个平台、素材分组、API key与推广位；含零食和餐饮券分组 | 本轮重读配置，未取得授权接口数据。旧素材ID不可直接认定有效；券后价、返佣、返现各自存储，不当作同一利润 |

xbk_monitor README声称MIT，但根目录未见LICENSE且GitHub元数据license=null；不得仅凭README认定复制许可。xianbaoku、jdm同样未取得清晰许可证元数据。优先独立适配公开接口与必要字段，不复制整库。仓库updated_at和stars不当作维护证明，上表日期使用pushed_at。

### 本次真实数据检查

检查时间2026-10-04 10:05:17北京时间。公开入口 https://new.ixbk.net/plus/json/push.json 返回200和20条JSON；旧域名http://new.xianbao.fun/plus/json/push.json也曾返回同一份内容，不重复算源。本机http://127.0.0.1:1200/xianbao返回502。本次再次读取的20条源时间距检查时36—296秒；顺序和站内ID不保证原文时间顺序，应读shijianchuo，不把抓取时间当原文时间。

以下只是当时来源标题，**未核对商家报价、活动资格或费用，不是购买建议**：

| 类别 | 接口返回的实例 | 原始线报 |
|---|---|---|
| 家庭日用 | 美丽雅纸碗560ml 50只赠手套50只13.9元；同一条还含手套、水杯两个商品，必须拆SKU | https://new.ixbk.net/haodan/7140245.html |
| 卫生用品 | 高洁丝6包37.34元 | https://new.ixbk.net/haodan/7140247.html |
| 宠物用品 | 豆腐猫砂6包50元 | https://new.ixbk.net/haodan/7140256.html |
| 餐饮 | 沪上阿姨美式0.1元 | https://new.ixbk.net/weibo/7140251.html |
| 出行权益 | 10元打车立减金，标题未写完整门槛 | https://new.ixbk.net/xiaodigu/7140250.html |
| 会员权益 | 爱奇艺V7等级红包，未证实人人可领 | https://new.ixbk.net/zuankeba/7140244.html |

公开聚合返回数据已验证，领取有效性没有验证。同一聚合源内的频道不独立计数；官方商城、活动发布者和聚合渠道分别标记。

### 优先补齐什么

第一优先：在现有工作台适配已返回真实JSON的线报入口，保留聚合ID、原渠道、商城、标题、正文、原文时间及商家/活动链接；以家清洗护、厨房日用、母婴宠物、食品餐饮、出行权益分组。跨站同活动去重。

活动机制必须分开：直接免费、付邮领取、申请试用/中选、抽奖、积分兑换、先付后返、低价购买。零售价/试用价/券面值/预计返利不得混为买价；未标明现金支出与门槛的“0元”标缺条件。需要最小支付、邮费、资格、截止时间、名额/次数、领取方式和是否含任务，不能给所有资源强塞商品利润算法。

其次补日用关键词与当前试用目录，实测每个来源再登记。未装脚本，未使用账号，未发消息，未改正式sources；本轮结果是可用入口与采集需求的验证，不宣称接入已完成。

### 可复现CLI命令

```sh
gh search repos '"线报酷"' --limit 10 --json fullName,description,url
gh search repos '"羊毛" in:name,description' --limit 15 --json fullName,description,url
gh api repos/DIYgod/RSSHub/contents/lib/routes/xianbao/index.ts -H 'Accept: application/vnd.github.raw'
gh api repos/LiuRJ99/xbk_monitor/contents/main.py -H 'Accept: application/vnd.github.raw'
gh api repos/ZCY01/jdtry/contents/static/tasks.js -H 'Accept: application/vnd.github.raw'
gh api repos/TonyJiangWJ/Unify-Sign/contents/core --jq '.[].name'
```


## 2026-10-04 优惠资源实际接入验收（C01/C03/C07/C08、D06）

在 feature/bootstrap 完成增量表字段迁移（events.metadata_json、opportunities.resource_kind/topic），修改前数据库备份至 /private/tmp/workbench-before-resources.sqlite3。新增独立 xianbao.py 适配器，只访问6个限定公开JSON地址，不运行第三方领券/签到脚本。保留来源栏目、原始正文、原文时间与HTTPS活动链接；同一原文跨栏目按最近观察快照去重，历史保留。

| 新增入口 | 首轮真实记录 | 验收 |
|---|---:|---|
| 线报酷综合优惠 | 20 | 提取正常 |
| 线报酷赚客吧 | 9 | 提取正常 |
| 线报酷新赚吧 | 1 | 提取正常，但当前条目旧且噪声较多 |
| 线报酷微博线报 | 20 | 提取正常 |
| 线报酷豆瓣线报 | 4 | 提取正常 |
| 线报酷酷安线报 | 10 | 提取正常 |
| 逛丢洗衣液关键词 | 30 | 原文时间全部缺失，默认留档 |
| 逛丢洗洁精关键词 | 30 | 原文时间全部缺失，默认留档 |
| 逛丢垃圾袋关键词 | 30 | 22条有原文时间，8条缺失 |

合计新增9个入口，来源配置由29变38，首轮新增154条原始快照。这不是9个独立上游：6栏目同属线报酷聚合，3关键词同属逛丢。首轮线报64条中23条可提取报价、5条活动、24条缺唯一报价、8条过期、4条重复等排除；不是64条已核实最低价。后台下一轮又新增15条，持续采集实跑通过，后续计数会变化。

恢复 RSSHub：使用已有依赖执行 `NODE_ENV=dev LISTEN_INADDR_ANY=0 PORT=1200 NODE_OPTIONS=--max-http-header-size=32768 node_modules/.bin/tsx lib/index.ts`，仅监听127.0.0.1:1200。pnpm dev曾因包管理器自动安装确认无法在无TTY运行而退出，未执行安装/清理；改用已安装源码执行器。旧纸品、手机、新客、宜家、麦当劳、酷比和小米众筹来源均实扫恢复提取。修复本机RSSHub请求误经环境代理7890超时：loopback请求独立session禁用环境代理，外部HTTPS采集不变。

规则/页面：区分商品报价、免费领取线索、付邮、试用、抽奖、积分、返后及优惠券权益；明确0.01元不是免费，券面额/奖品/返后报价不写商品现金价；主题与方式可组合筛选。仅初步分类，没有据此认定活动有效或可领取。高时效JSON来源检查间隔1分钟并优先调度，后台单轮串行执行后等待60秒，实际频率取决于该轮耗时，不保证零延迟。

验证：`python -m unittest discover -s tests`53项通过，`git diff --check`通过。真实浏览器家用页先显示46条、刷新后47条，家用+线报酷组合4条；权益页显示爱奇艺V7等级红包且没有虚构买价；渠道页显示6个线报栏目最近成功更新与1分钟设置。截图 /private/tmp/opportunity-home-resources.jpg。源码serve与worker已重启，仅本机5002；外发通道为空，未领券、购买或发送消息。

剩余边界：当前多为聚合爆料而非商家一手报价，尚无完整跨平台同款/数量/资格匹配、运费及叠加条件、退出行情和独立利润区间。京东试用旧项目未证实现行活动列表接口；跨平台联盟API仍需平台授权；Unify-Sign/jdm等执行脚本不是市场数据源。新赚吧等栏目存在讨论噪声，口语化数字/谐音金额不猜测填价。未接通能力继续列为缺口，不以虚构0元或旧记录充数。D06保持处理中、整体目标PARTIAL。


## 2026-10-04 市场比价方法调研、实际应用与反例验证

本轮使用GitHub CLI实际检索price tracker、京东比价、商品匹配，读取源码而非仅依赖星数/README；同时核查官方产品标识和优化方法文档。星数不作为效果证明。

| 资源与源码依据 | 真实方法 | 本地采用与限制 |
|---|---|---|
| [Google产品标识](https://developers.google.com/search/blog/2021/02/product-information)、[idealo商家资料](https://partner.idealo.com/uk/learning-center) | GTIN/EAN及品牌/MPN用于归集商品，避免只靠标题 | 本轮先采用已存在原文链接中的明确京东商品ID；不猜测淘宝item ID已确定具体颜色容量SKU。其他标识/跨平台映射仍待来源字段 |
| [PriceGhost提取实现](https://github.com/clucraft/PriceGhost/blob/main/backend/src/services/scraper.ts) | JSON-LD Product.offers、商店选择器、多种提取候选；部分代码取lowPrice或offers第一项 | 借鉴结构化优先；不照搬lowPrice当选中规格价。该仓库GitHub许可证元数据为空，本轮未复制或执行代码，未完成通用商家JSON-LD接入 |
| [Discount-Bandit历史实现](https://github.com/Cybrarist/Discount-Bandit/blob/master/app/Http/Controllers/Actions/GetChartForCurrentLinkAndItsRelatedLinksForTheUserProducts.php) | 通过product/link关系关联多店链接，再读取一年价格历史 | 借鉴“先有商品身份，再比较多来源历史”的顺序。本地新增标识优先归组；本轮不声称已有长期历史曲线或独立历史最低 |
| [国内Shopee同款匹配实现](https://github.com/yangjianxin1/Shopee-Price-Match-Guarantee/blob/main/model.py) | BERT/SimCSE对比学习及余弦相似度；README为34250条训练数据，以label_group构造同款标签 | 适合召回不同标题候选，不能替代规格冲突规则；本轮没有训练、安装或运行该模型，不把论文/比赛成绩当本地识别效果 |
| [OR-Tools约束优化](https://developers.google.com/optimization/cp)、[背包示例](https://developers.google.com/optimization/pack/multiple_knapsack) | 给定目标、变量和约束后搜索可行组合；MIP/CP-SAT可处理数量、互斥、满减约束 | 目前规则不足，不虚构约束求“最优”。对已有明确方案独立实现精确枚举的Pareto筛选：总支出与折合单件价两个目标，保留不被同时压低的方案。没有安装或运行OR-Tools，也未实现多店混合凑单求解器 |

实际变更：comparison.merchant_identity只接受明确HTTPS京东商品路径/wareId，拒绝商铺/券ID、相似域名、任意追踪查询；同一原文多个JD商品ID时标记歧义不合并。有明确ID时覆盖标题差异，同标题却ID不同保持分开；无ID时仍为同标题候选。来源里的链接仅是原文商品关联证据，并非商家对规格或有效报价的独立确认。

quantity_options对已采集方案比较最低整单与最低折合单件，金额用整数分及Fraction；已知全部运费时含运费，否则明确只比较商品金额。数量/资格分层；缺字段、复杂扣减、凑单/返现/积分、过期/未来/失效来源不参与数量推荐。PLUS加入资格识别。没有生成可无限重复下单的方案，没有把互斥券强行叠加，也没有以未知运费为0。

验证：最终96项独立回归通过（88旧项+8新项）；git diff --check通过。曾因直接导入TestCase使发现机制重复计数，已修正为模块导入，最终96不含重复测试。3032条真实快照全量回放耗时0.276秒（当次快照）。5条历史记录含直接JD商品ID，最新去重且为商品候选的4条进入ID匹配依据；没有证明已有跨平台SKU组合。实际Flask请求首页、商品ID样例、数量比较样例及反例均200，源码serve/worker重启后浏览器验收：

- #2334从原文商品链接提取京东商品ID10162014388331，明确显示匹配依据；该条来源超时仍被排除，不因有ID变成当前优惠。
- #2932归集3条当前同口径来源报价，商品总额6.42、6.42、6.43元，数量选择保留两个并列6.42来源，不把重复/转载解释为两个独立市场证据；运费仍未知。
- #2924洗洁精5件112.20元/折22.44元，源正文要求额外凑单儿童乐器且发布时间缺失；与1件28.16元的PLUS方案资格也不同。浏览器显示0条可推荐方案及具体拒绝原因，未误推低单价。
- 合成隔离回归验证1件10元、2件15元、2件18元仅保留前两种（少花钱与低单价目标不同）；已知运费能改变赢家；不同资格、旧报价、不同SKU和多SKU歧义拒绝。合成场景未写正式数据库，不能当真实优惠实例。

截图 /private/tmp/opportunity-market-methods.jpg。新能力在同一工作台，未新增服务平台/依赖/消息外发。工程与上述实际流程局部PASS；整体最优惠/盈利目标仍PARTIAL：公开数据的SKU覆盖低，商家券规则和地区运费未完整，凑单商品与跨平台可比报价仍不足。本轮借鉴与应用具体能力，不宣称已运行全部第三方项目或已找到全网最低。
