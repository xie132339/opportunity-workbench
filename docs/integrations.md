# 多来源与消息二开接入

更新于 2026-10-02。以下是源码接入点，不等于已取得各平台登录状态、最终结算价或真实利润。开发阶段先源码运行，业务场景验证完成后再考虑 Docker。

## 多来源能力

| 功能 | 当前入口 | 真正可用的条件 |
|---|---|---|
| 网页变化与历史快照 | 已运行的 changedetection.io | 适合单页变化；不自动生成真实商品结算价 |
| 公开 RSS/Atom | 渠道页“公开 RSS 订阅” | 真实 HTTPS feed，点击“现在检查”后有条目 |
| RSSHub 多站点路由 | 渠道页“本机 RSSHub 订阅” | 自行源码运行 RSSHub 于 `127.0.0.1:1200`，填具体路由，如 `http://127.0.0.1:1200/smzdm/keyword/...`；什么值得买相关路由要求 `SMZDM_COOKIE`；`/mi/newproducts` 已实测返回 55 条可用小米商品链接并在工作台作为首次基线入库，京东价格路由实测 503 |
| 闲鱼 AI 筛选结果 | 渠道页“本机闲鱼监控结果” | 独立源码运行 ai-goofish-monitor 于 `127.0.0.1:8000`，完成授权登录与任务运行，再填 `/api/results/<文件名>.jsonl`；采集标题、原始链接、挂牌价及 AI 推荐标记，挂牌价不进入成交依据；上游源码 UI 与结果接口已在本机启动；`/api/results/files` 当前为空，尚无登录状态和真实监控结果 |
| 国内电商采集器 | 仍为候选 | 京东/淘宝/抖音等必须逐个核对当前页面与合法访问条件；不能凭代码文件声称可用。PriceDive 随机模拟价绝不入库 |

新增本机来源只接受配置的 `127.0.0.1` 服务端口；远程或重定向地址被拒绝。RSS 条目还要有公开 HTTPS 原始链接。用户运行上游项目时在其仓库保持非保护功能分支，不把登录 cookie 或 AI key 写进本项目 Git。

## 历史低价与盈利

机会详情现在可录“历史现金实付总额（含运费）”。需完整同规格、原始 HTTPS 依据、日期和适用条件；180 天内至少 3 个不同日期或来源的样本，才显示中位数和最低样本。当前本人现金实付总额必须在 15 分钟内核实；“低于已录入历史最低样本”也只是待人工复核的低价候选。转售还要有独立成交或最终回收价、完整成本，才可显示保守价差。两个门槛同时满足且保守价差大于零，才会进入外部消息队列。

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

每条机会每通道只排队一次；发送结果和错误码在“筛选策略与提醒”显示，失败后手动重试。外部消息开启后由 `app.py worker` 处理。消息只写“待人工复核的价差线索”，不承诺必买、可卖或已经获利。当前没有配置接收凭据，也没有向任何 QQ/微信目标发送消息。修改 `.env` 后需重启网页和 worker 进程。

## 2026-10-02 核价留档

本人每次在详情页确认结算价、运费、完整规格和依据后，工作台保留单独的核验快照。快照不是已付款记录，不参与三笔历史现金实付门槛。新增四个健康 RSSHub 路线见 docs/sources.md。
