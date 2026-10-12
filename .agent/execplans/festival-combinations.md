# 计算节日优惠组合

This ExecPlan is maintained according to `~/.codex/PLANS.md`.

## Purpose and observable outcome
节日大促菜单允许将已采集商品加入购买清单，补齐数量、运费、实际券条件后，计算已确认规则内总付款最低的组合。展示用哪些券、怎么扣、备选差额和不适用原因，提供明确标为模拟的例子。

## Scope, constraints, and assumptions
本地 feature/bootstrap；金额整数分。固定购买清单与订单分组，不擅自增购、不下单。限定 8 个商品、12 条规则以穷举准确求解；不读取账户券包。规则缺少适用性/有效期/证据时不参与优惠。真实商品从已有库读取，示例不落商品库。来源价格均为待核 SKU 的官方观察。

## Context and orientation
services/festival_promotions.py 原来仅解释单条 API 路径。新增独立组合计算函数，经现有节日路由提供表单，复用登录/CSRF 和 Jinja。已有 worker 不改。

## Progress
- [x] 2026-10-12 核对源码、AC-11、官方文档。
- [x] 实现组合枚举、互斥/叠加/门槛/时间/作用范围和金额分摊。
- [x] 接入节日页，真实商品导入与本地草稿、简单示例。
- [x] 数值反例、路由和真实本机 HTTP 验证，更新文档并提交。

## Plan of work
1. 计算器校验输入，穷举可行优惠子集。按明确层级及输入顺序扣款，原金额/优惠后门槛分别计算。店铺优惠在相同子订单组内互斥；其余重叠优惠须明确确认可叠。超级红包按今年官方上限/顺序处理。
2. 节日页维护购买清单和优惠规则，计算结果以三步指引呈现，细节折叠。
3. 回归与本机服务验证；既有采集数据不做测试写入。

## Validation and acceptance
专项 unittest 验证官方 200→180 例子、后续红包、门槛仅一张、过期/未来、重复券、作用范围、运费、未知条件、贪心反例及输入上限。HTTP 验证登录/CSRF/实际 POST 和静态脚本。最优仅限当前清单/规则/顺序和固定运费；账户结算另行核验。

## Surprises and discoveries
当前 docs/core-acceptance.md 已有 2026 超级红包规则 URL，前一轮回答遗漏。官方页面标题为 26年双11超级红包消费者规则，二阶段部分信息仍待更新。

## Decision log
2026-10-12：复用标准库枚举，不加求解器依赖。官方路径不能拆成可任意叠加的独立券，仅完整单品路径作为独立方案保留。
官方来源：https://developer.alibaba.com/support/announcementDetail.htm?id=25828；https://1111.alimama.com/alimamamain/page_20211227_144813；https://developer.alibaba.com/docs/api.htm?apiId=64759。

## Idempotence and recovery
计算只读，不新增数据库表。浏览器草稿可清空，模拟例子仅在表单中。必要时恢复本轮文件并重启本地服务。

## Outcomes and retrospective
实现及本地验收完成：363项回归通过，真实浏览器POST175/运费6后181，商品87744只读导入。个人权益需录入，未验证真实结算/全局最低。验收文件随本次功能一起提交；本地网页与采集worker均运行，health正常。
