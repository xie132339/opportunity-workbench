# 当前运行环境

> 当前产品范围见 [当前产品契约](current-product-contract.md)。本页只记录可重复的本地运行方式，不记录过期 PID、一次性日志或旧渠道状态。

## 依赖

- Python 3.12
- Flask / Jinja2 / SQLite
- 本机 `.env`：淘宝联盟 App Key、App Secret、推广位等敏感配置

`.env` 和 `data/workbench.sqlite3` 只留在本机，不能提交或复制到文档。

## 启动

```bash
bash scripts/workbench.sh restart
```

网页入口：<http://127.0.0.1:5002/>。脚本先安全停止本工作区自身的网页和淘宝 worker，再以当前工作树启动两者，并检查 `/health`。它只会处理命令和工作目录均匹配本项目的进程，不会为了夺取端口停止别的应用。worker 只扫描到期来源；来源扫描状态以 `/sources` 页面和数据库中的最近 `source_scan_runs` 为准。

```bash
bash scripts/workbench.sh status
bash scripts/workbench.sh stop
```

## 验证命令

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest tests.test_taobao_alliance
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s tests
git diff --check
```

历史运行记录见 [归档](history/environment-runtime-journal-before-current-contract-2026-10-11.md)，不能用来判断当前服务是否已运行。
