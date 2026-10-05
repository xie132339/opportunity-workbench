# 多来源与消息二开接入

更新于 2026-10-03。以下是源码接入点，不等于已取得各平台登录状态、最终结算价或真实利润。开发阶段先源码运行，业务场景验证完成后再考虑 Docker。

## 多来源能力

| 功能 | 当前入口 | 真正可用的条件 |
|---|---|---|
| 网页变化与历史快照 | 已运行的 changedetection.io | 适合单页变化；不自动生成真实商品结算价 |
| 公开 RSS/Atom | 渠道页“公开 RSS 订阅” | 真实 HTTPS feed，点击“现在检查”后有条目 |
| RSSHub 多站点路由 | 渠道页“本机 RSSHub 订阅” | 自行源码运行 RSSHub 于 `127.0.0.1:1200`，填具体路由，如 `http://127.0.0.1:1200/smzdm/keyword/...`；什么值得买相关路由要求 `SMZDM_COOKIE`；`/mi/newproducts` 已实测返回 55 条可用小米商品链接并在工作台作为首次基线入库，京东价格路由实测 503 |
| 闲鱼搜索与结果 | 工作台 [闲鱼任务页](http://127.0.0.1:5002/xianyu) | ai-goofish-monitor 仅作为 `127.0.0.1:8000` 本机后台服务；在工作台导入本人登录态、创建／编辑／启停关键词任务，并在有结果后点“接入并检查”。账号状态保存在上游被 Git 忽略的 `state/` 且文件权限 0600，工作台不回显；当前账号、任务、结果均为空。只将明确格式的商品发布时间用于实时筛选；挂牌价不当成交价。 |
| 国内电商采集器 | 仍为候选 | 京东/淘宝/抖音等必须逐个核对当前页面与合法访问条件；不能凭代码文件声称可用。PriceDive 随机模拟价绝不入库 |

新增本机来源只接受配置的 `127.0.0.1` 服务端口；远程或重定向地址被拒绝。RSS 条目还要有公开 HTTPS 原始链接。用户运行上游项目时在其仓库保持非保护功能分支，不把登录 cookie 或 AI key 写进本项目 Git。

## 授权优惠接口的标准化接入

`benefits.import_authorized_record()` 是平台适配器的统一入库边界。京东联盟、淘宝联盟、拼多多或其它平台的授权客户端负责各自 OAuth/签名、限频和响应字段映射；这里只接受已获得授权的数据，不能代替平台授权，也不会请求脚本作者的私有查券 API。标准化结果按 JSON Lines 导入：

```json
{"provider":"平台授权接口名称","platform":"jd","scope_type":"item","title":"满99减10优惠券","coupon_url":"https://pro.m.jd.com/mall/active/example/index.html","source_url":"https://open.example.com/promotion/rule","eligible_product_keys":["jd:123456"],"discount_cents":1000,"threshold_cents":9900,"eligibility":"会员条件未确认","region":"未提供","stackable":"unknown","observed_at":"2026-10-05T10:00:00+08:00","valid_from":"2026-10-05T00:00:00+08:00","valid_until":"2026-10-06T00:00:00+08:00","raw_rules":"接口返回的券规则原文"}
```

```sh
WORKBENCH_DB=/path/to/workbench.sqlite3 .venv/bin/python app.py benefits-import /path/to/authorized-benefits.jsonl
```

此命令只导入接入客户端响应，不从样例或文件名生成线上数据。导入器不验证开发者授权、API签名或提供方声明，部署时只能让受信任、已获许可的客户端调用。商品详情只按明确的 `platform:item_id` 列表显示新鲜候选；目标商品ID必须已由 `comparison.merchant_identity()` 的对应平台 URL 解析器识别。SKU专属券目前因目标SKU未结构化而拒绝匹配。店铺/品类/平台券只有接口同时返回参与商品ID清单时，才会列为对应商品候选。优惠金额、账号资格、地区、有效期或叠加规则缺失时不扣商品价、不计利润。当前环境未配置任何商城授权 API 凭据，因此尚无真实提供方轮询器或线上导入数据；这份格式与接入命令是可测试的适配边界，不是平台接通声明。

## 历史低价与盈利

机会详情现在可录“历史现金实付总额（含运费）”。需完整同规格、原始 HTTPS 依据、日期和适用条件；180 天内至少 3 个不同日期或来源的样本，才显示中位数和最低样本。当前本人现金实付总额必须在 15 分钟内核实；“低于已录入历史最低样本”也只是待人工复核的低价候选。转售还要有独立成交或最终回收价、完整成本，才可显示保守价差。2026-10-04 修正：历史低价独立判断；转售须证据与时效满足，且同一条启用策略的正数最低净利和含运费预算同时达标，才可进入外部消息队列。发送前重读策略；无完整门槛不放行。仍不是自动可执行盈利证明，见 docs/core-acceptance.md。

## 消息通道

在忽略的 `.env` 中配置，默认 `NOTIFY_ENABLED=0`。启用前先选择自己拥有的接收目标，不要把密钥写进页面或仓库。

```dotenv
NOTIFY_ENABLED=1
NOTIFY_CHANNELS=wecom,serverchan,qq_onebot
WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<your-key>
SERVERCHAN_SENDKEY=<your-SCT-key>
QQ_ONEBOT_BASE=http://127.0.0.1:3000
QQ_ONEBOT_TOKEN=<local-gateway-token>
QQ_GROUP_ID=<your-group-id>
# 或 QQ_USER_ID=<your-user-id>，二选一；群 ID 优先
```

- 企业微信群机器人使用腾讯的 Webhook；仅向配置的群发送。凭据必须由群所有者提供。
- 个人微信使用第三方 Server酱 Turbo。按其[官方文档](https://sct.ftqq.com/docs/)注册并绑定自己的微信；免费额度和消息内容转交该服务，用户自行选择是否启用。
- QQ 使用自行运行的本机 OneBot v11 HTTP 网关。[QQ 官方机器人文档](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/send.md)说明主动推送已停止；本项目不把官方 QQ 机器人写成能主动发送。OneBot 网关兼容性和账号使用条件由所选实现决定。参照[OneBot 发送接口](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)。

每条机会每通道只排队一次；发送结果和错误码在“筛选策略与提醒”显示，失败后手动重试。外部消息开启后由 `app.py worker` 处理。消息只写“待人工复核的价差线索”，不承诺必买、可卖或已经获利。当前没有配置接收凭据，也没有向任何 QQ/微信目标发送消息。消息接入页面保存后，网页和 worker 会动态读取本机配置；保存本身不发送消息。

## 2026-10-02 核价留档

本人每次在详情页确认结算价、运费、完整规格和依据后，工作台保留单独的核验快照。快照不是已付款记录，不参与三笔历史现金实付门槛。新增四个健康 RSSHub 路线见 docs/sources.md。

## 2026-10-02 工作台消息接入页面

导航新增“消息接入”：http://127.0.0.1:5002/messages。页面显示企业微信群机器人、个人微信 Server酱和本机 OneBot QQ 的选择、参数是否齐全、自动开关、投递统计；可保存自己的接收凭据并清除旧值。已有密钥不回显，配置仅写入被 Git 忽略且权限 0600 的本机 .env，保留监控 API 等其他设置。网页和 worker 每次读取最新消息设置，无需重启。

保存配置不发送消息。用户主动点击“发送测试消息”才会尝试向所选目标发送，接口接受仍需到接收端确认实际到达。自动投递继续受原文时效、本人 15 分钟核价、同规格历史实付和保守退出价共同约束；总开关关闭时自动发送被再次拦截。当前没有接收凭据，尚无真实 QQ／微信送达。
