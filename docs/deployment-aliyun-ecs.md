# 阿里云 ECS 部署

本部署使用源码、Python 虚拟环境、Gunicorn、systemd、Nginx 与 SQLite；不使用 Docker。目标是将网页服务限制在 `127.0.0.1:5002`，只由 Nginx 对外提供 HTTP。淘宝同步进程独立于网页服务运行。

## 当前远程实例与访问（2026-10-12 实测）

| 项目 | 当前值 |
|---|---|
| ECS 实例 | `i-uf693wtee99tze11foca` |
| 地域 / 配置 | 华东 2（上海），2 vCPU / 2 GiB，Ubuntu 24.04 64 位 |
| 公网 IPv4 | `8.159.128.186` |
| 浏览器入口 | <http://8.159.128.186/>（HTTP；当前未配置域名、TLS 证书或 443） |
| 项目根目录 | `/application` |
| Git 分支 / 部署提交 | `feature/bootstrap` / `61e072fb1125d26b7573f900a361ef00fdffc889` |
| Web 进程 | `opportunity-workbench-web`；Gunicorn 监听 `127.0.0.1:5002` |
| 淘宝采集进程 | `opportunity-workbench-worker`；独立 systemd 服务 |
| 数据库 / 备份 | `/application/.runtime/workbench.sqlite3`；`/application/.runtime/backups` |
| 服务器运行配置 | `/application/.runtime/workbench.env`，属主 `workbench:workbench`，权限 `0600` |

**访问与凭据边界：**公网只经 Nginx 的 TCP 80 访问；应用的 5002 端口只监听本机，不应开放到安全组。`/health` 仅允许 ECS 本机访问，可用 `curl -fsS http://127.0.0.1/health` 检查。网页登录用户为 `admin`；管理员密码、`WORKBENCH_SECRET`、淘宝联盟 App Secret 和推广位 ID 均只从服务器环境文件读取，文档与 Git 不保存其实际值。环境变量名包括 `WORKBENCH_ADMIN_USERNAME`、`WORKBENCH_ADMIN_PASSWORD`、`WORKBENCH_SECRET`、`TAOBAO_ALLIANCE_APP_KEY`、`TAOBAO_ALLIANCE_APP_SECRET`、`TAOBAO_ALLIANCE_ADZONE_ID`。

健康接口的 `monitor_api_configured` 字段只检查 `CHANGED_API_TOKEN`，不代表淘宝联盟凭据状态；判断淘宝采集是否成功，应查看 `/sources` 和数据库 `source_scan_runs` 的最新完成回执。

### 最近一次真实采集验收

2026-10-12 北京时间 16:32:19–16:32:53，使用淘宝联盟官方物料 API `taobao.tbk.dg.material.optional.upgrade` 对纸品、饮用水、大米、面粉、食用油、洗衣液六个入口各执行一轮。六个扫描均为 `complete`，共取得 4,491 条原始结果，解析 4,404 条，识别 4,157 条促销信号，87 条未通过解析准入；同步后验收已为 4,404 条写入处理结果。本轮 20 条 SKU 详情检查中，SKU 证据完整数为 0，因此该回执只证明淘宝联盟物料已采集与解析，不能证明每条商品均已完成 SKU、库存、券资格或结算价验证，也不能单凭促销信号宣称捡漏。

验收时 Web 与 Worker 均为 `active`，六个来源均配置为 60 分钟间隔。采集是按每个来源独立的到期时间执行；服务进程活跃不替代来源最近成功时间与扫描回执。

## 前置条件

- Ubuntu 24.04 ECS，至少 2 vCPU / 2 GiB。
- 本机已通过 Workbench CLI 连接目标 ECS。
- GitHub 可访问 `feature/bootstrap` 分支。
- 已为服务器单独准备管理员密码和 `WORKBENCH_SECRET`；不能复用本机 `.env`、SQLite 数据库或淘宝密钥。

## 首次安装

在服务器以 root 用户执行：

```bash
git clone --branch feature/bootstrap --single-branch https://github.com/xie132339/opportunity-workbench.git /application
cd /application
bash deploy/aliyun/install-server.sh
```

安装脚本只安装依赖、创建运行用户、虚拟环境、systemd 单元和 Nginx 配置，不启动网页服务、不启用 Nginx，避免使用示例密码对公网暴露服务。首次 WSGI 启动时会从已提交的默认配置自动幂等创建六个淘宝生活品主题（抽纸、饮用水、大米、面粉、食用油、洗衣液）；它们是查询配置，不包含伪造的商品或历史采集数据。若现有数据库缺少配置，可在“渠道管理”页执行“初始化 / 补齐核心生活品主题”恢复。

编辑 `/application/.runtime/workbench.env`，至少设置：

```dotenv
WORKBENCH_SECRET=<仅服务器保存的随机值>
WORKBENCH_ADMIN_USERNAME=admin
WORKBENCH_ADMIN_PASSWORD=<运营者自定的强密码>
```

需要淘宝官方同步时，再填写 `TAOBAO_ALLIANCE_APP_KEY`、`TAOBAO_ALLIANCE_APP_SECRET` 和 `TAOBAO_ALLIANCE_ADZONE_ID`。密钥只保存在该环境文件中。

## 激活与验证

```bash
rm -f /etc/nginx/sites-enabled/default
ln -s /etc/nginx/sites-available/opportunity-workbench /etc/nginx/sites-enabled/opportunity-workbench
systemctl enable --now opportunity-workbench-web opportunity-workbench-worker opportunity-workbench-backup.timer nginx
curl -fsS http://127.0.0.1/health
systemctl --no-pager --full status opportunity-workbench-web opportunity-workbench-worker
```

只在本机验证通过后，才在 ECS 安全组中添加 TCP 80 入站规则。不要开放 5002。配置域名和 HTTPS 前，公网暂通过 IP 使用；中国大陆域名上线前需完成适用的备案流程。

## 更新

```bash
cd /application
git fetch origin feature/bootstrap
git switch feature/bootstrap
git reset --hard origin/feature/bootstrap
/application/.venv/bin/python -m pip install -r requirements.txt
systemctl restart opportunity-workbench-web opportunity-workbench-worker
```

更新前先确认没有直接在服务器源码目录修改文件；运行数据、备份与环境文件不在 Git 工作树中。

## 数据与恢复

SQLite 位于 `/application/.runtime/workbench.sqlite3`。每日 03:15 UTC 由 `opportunity-workbench-backup.timer` 执行在线备份，备份目录为 `/application/.runtime/backups`。每个备份执行 `PRAGMA integrity_check` 并输出 SHA-256 摘要。

```bash
systemctl start opportunity-workbench-backup.service
systemctl list-timers opportunity-workbench-backup.timer
```
