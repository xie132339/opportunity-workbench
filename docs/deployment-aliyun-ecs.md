# 阿里云 ECS 部署

本部署使用源码、Python 虚拟环境、Gunicorn、systemd、Nginx 与 SQLite；不使用 Docker。目标是将网页服务限制在 `127.0.0.1:5002`，只由 Nginx 对外提供 HTTP。淘宝同步进程独立于网页服务运行。

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

安装脚本只安装依赖、创建运行用户、虚拟环境、systemd 单元和 Nginx 配置，不启动网页服务、不启用 Nginx，避免使用示例密码对公网暴露服务。

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
