# 让工作台可从手机浏览器使用

This ExecPlan is a living document maintained according to `~/.codex/PLANS.md`.

## Purpose and observable outcome

手机通过现有阿里云 ECS 公网入口打开工作台并登录；商品搜索、节日大促、渠道管理等主要页面呈现手机布局，底部导航可触达，长表格以卡片阅读和操作，不需要整页横向滚动。桌面布局继续可用。

## Scope, constraints, and assumptions

复用现有 ECS 的 Nginx 公网入口和 Gunicorn 本机监听，不扩张本机 5002 或服务器安全组；更新响应式前端并部署到已有服务器。项目仍须登录、保留 CSRF；不把 API 密钥或数据库复制到手机。当前公网只有 HTTP，HTTPS 尚未配置，需明确作为真实访问边界。功能分支 `feature/bootstrap`，遵守受保护分支规则。

## Context and orientation

`app.py:main` 本机源码监听 `127.0.0.1:5002`，不用于手机公网访问。已有 ECS `8.159.128.186` 的 Nginx 对外提供 HTTP，公网 GET `/` 返回登录跳转；远端 `feature/bootstrap` 干净，但部署提交仍为 `61e072f`。`templates/base.html` 的十一项顶部导航在 390px 手机视图换成四行；`templates/index.html` 的长说明和多项筛选挡住商品列表。`static/style.css` 已有部分商品列表卡片样式，但其它表格仍以 1120px 宽度横向滚动。

## Progress

- [x] (2026-10-12) 确认功能分支、实际服务监听、手机视图和本机 Wi-Fi 地址。
- [x] (2026-10-12) 验证已有 ECS 公网 HTTP 入口、远端干净功能分支及网页/worker服务；保留本机监听边界。
- [x] (2026-10-12) 完成手机顶部、底部导航、折叠说明/筛选和常用表格卡片布局。
- [x] (2026-10-12) 以 390px / 320px 浏览器视口检查搜索、详情、节日、渠道、价格基准及导航，均无整页横向溢出；登录和公网待部署后检查。
- [ ] 更新访问说明、验收记录和实施日志；提交非保护分支，并将同一版本部署 ECS。

## Plan of work

1. 保留本机回环监听。核对远端部署脚本和当前 Git 状态，选择不覆盖运行库与凭据的更新方式；网页服务重启后用公网 IP 请求确认。
2. `templates/base.html` + `static/style.css` 增加手机底部导航和“更多”菜单，固定安全区及触控尺寸。首页将长说明和高级筛选默认折叠，保留完整内容。
3. 表格在手机视图转为带字段标签的纵向卡片，保留原有表单提交和链接；桌面仍为表格。
4. 对关键路由和安全失败路径做现有框架回归，在 390px 与较窄视图检查真实浏览器页面与操作。

## Validation and acceptance

预期：ECS 公网 IP 的 `/login` 可访问且未登录业务页跳转登录；服务器应用仅监听 127.0.0.1；登录后手机底部导航可跳转；关键页面 `scrollWidth <= innerWidth`，商品详情、渠道操作和节日卡片可阅读；全量回归通过。真实手机终端和网络仍需用户设备回放才能完全证明。

## Surprises and discoveries

- 远端仍运行旧提交，不能只交付本机 CSS；本轮使用 Workbench CLI 传输 Git bundle 更新远端功能分支，避免复制运行库与密钥。
- 本机浏览器 390px 和 320px 视口均无整页横向溢出；通用表格的移动端标签卡片已在价格基准页实际显示。

## Decision log

- 2026-10-12：手机入口复用现有 ECS 公网 IP；本机与 Gunicorn 保持回环监听，避免扩张网络暴露。

## Idempotence and recovery

前端仅增强布局；原始数据和业务记录不迁移。远端更新前检查工作树，保存上一源码提交以便恢复；只重启本项目的 web/worker 单元。

## Outcomes and retrospective

待验收。
