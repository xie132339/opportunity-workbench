# GitHub CLI 检索：国内多平台机会工作台

日期：2026-10-02。本次核对公开仓库源码、许可证和中国市场适配条件；未对新仓库做本地实时运行。

## 检索命令

```sh
gh search repos 'price tracker' --sort stars
gh search repos 'goofish' --match name,description --sort stars
gh search repos '闲鱼 监控' --limit 10
gh search repos '京东 比价' --limit 10
gh search repos 'auction scraper china' --limit 10
gh search code 'smzdm repo:DIYgod/RSSHub'
gh api repos/DAILtech/PriceDive/contents/src/pricedive.py -H 'Accept: application/vnd.github.raw'
gh api repos/wangdw495/ecommerce-price-analysis/contents/src/ecommerce_price_monitor/collectors/jd_collector.py -H 'Accept: application/vnd.github.raw'
```

`gh repo view` 的 `licenseInfo` 本次均为空；许可证以 `gh api repos/<owner>/<repo> --jq .license.spdx_id` 和 LICENSE 文件核对。普通沙箱中的 gh 网络访问失败，获准联网执行后成功。

## 候选项目与边界

| 项目 | 源码事实 | 使用判断 |
|---|---|---|
| [changedetection.io](https://github.com/dgtlmoon/changedetection.io) | Python，Apache-2.0，监控页面变化和快照；本项目已源码运行 | 保留为监控底座，不把页面变化视为结算价或利润 |
| [RSSHub](https://github.com/DIYgod/RSSHub) | TypeScript，AGPL-3.0；有[值得买关键词路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/smzdm/keyword.ts)和[商品路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/smzdm/product.ts)，需 `SMZDM_COOKIE`，商品路由过滤过期文章 | 补充线索，逐路由实测；不能充当完整历史低价库；复用须考虑 AGPL |
| [ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor) | Python/Playwright/SQLite，MIT；闲鱼关键词、价格、地区、定时、AI 分析、价格历史；需要登录状态和 AI 配置 | 中国二手适配器首选候选，尚未本机实跑，仅覆盖闲鱼 |
| [ecommerce-price-analysis](https://github.com/wangdw495/ecommerce-price-analysis) | Python，MIT；有京东、淘宝、抖音、小红书采集器源码；最近推送 2025-09 | 参考字段与解析器；真实页面、登录和最终到手价待实跑 |
| [PriceGhost](https://github.com/clucraft/PriceGhost) | TypeScript/React/Node/PostgreSQL；价格历史、人审价格；主要适配海外零售站，GitHub 未识别许可证且根目录未见 LICENSE | 借鉴交互与价格证据设计；代码复用须先明确授权 |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | Python，Apache-2.0，网页采集与内容提取 | 特定公开动态页确有必要时引入；不提供商品利润判断 |

## 明确排除

- [PriceDive 的取价函数](https://github.com/DAILtech/PriceDive/blob/main/src/pricedive.py#L156-L200) 在源码中明确标注 `SIMULATION`，用 `random.uniform` 生成价格，虽然 README 宣称支持淘宝、京东、拼多多，不能当真实价格源。
- [pricewatch](https://github.com/juhao10086/pricewatch)、[price-compare-skill](https://github.com/hopkdj/price-compare-skill) 的 README 包含演示或回退示例数据，不能把它们当真实行情。
- 仓库 star 数、README 的“全平台”描述、采集器文件存在，都不证明中国平台当下能取得最终付款价。

## 当前工程决策

保留独立的多平台工作台统一来源、证据与交易账本；再接 RSSHub 路由和闲鱼候选适配器。每条“捡漏”要同时证明：原始线索与时效、同规格真实买入到手价和历史比较、独立成交或可执行最终回收报价、扣完运费手续费损耗后的保守价差。当前纸品记录缺本人结算价、完整同规格历史和真实退出价，仍没有已验证的“低价必买”纸。

本次未克隆或启动新仓库，未使用登录态访问闲鱼、淘宝或京东，未验证其当前持续采集成功率、到手价或商业收益。先用一款纸打通证据闭环，再复制到更多 SKU 和渠道；整体范围仍是多平台、多类型机会。
