# 多平台机会工作台

> **先读 [当前产品契约](docs/current-product-contract.md)。** 它是唯一能够驱动产品、代码和验收的文档；旧方案和运行记录已放入 [docs/history/](docs/history/README.md)，不能当作当前结论。

本地工作台当前先跑通淘宝联盟第一方优惠物料：发现官方推广价、保留官方入口和价格证据，并只在同款和市场基准充分时才判断“捡漏”。当前可展示官方优惠观察，尚未开放“已证实捡漏”结论。

## 当前能力

- 定时扫描淘宝联盟官方物料 API：`taobao.tbk.dg.material.optional.upgrade`。
- 列表和详情展示可解释的“淘宝联盟官方推广价”及优惠条件。
- SKU 详情用于规格/库存/比较增强；其受限不会隐藏官方优惠观察。
- 价格、来源和每次扫描都有审计记录；无有效价格的原始候选不会进入默认列表。

## 本地启动

要求 Python 3.12。初次安装：

```bash
bash setup.sh
```

在本机 `.env` 配置淘宝联盟凭据和推广位（不提交 Git），然后由运行管理脚本启动网页和 worker：

```bash
bash scripts/workbench.sh restart
```

脚本只会停止工作目录属于本项目、且命令为 `app.py serve` 或 `app.py taobao-worker` 的进程；不会因 5002 端口被其它应用占用而终止该应用。每次 `restart` 都从当前工作树加载代码，并自动检查 `/health`。查看状态或停止本项目进程：

```bash
bash scripts/workbench.sh status
bash scripts/workbench.sh stop
```

打开 <http://127.0.0.1:5002/>。不需要 Docker。数据库在本机 `data/workbench.sqlite3`；克隆项目不会带走账号、密钥或采集记录。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest tests.test_taobao_alliance
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s tests
git diff --check
```

## 文档入口

- [当前产品契约](docs/current-product-contract.md)：范围、证据等级、价格口径、捡漏定义。
- [当前需求](docs/requirements.md)：可实现需求和验收条件。
- [当前来源](docs/sources.md)：已启用的数据源及新增门槛。
- [当前核心验收](docs/core-acceptance.md)：已通过和未通过的能力。
- [当前实施 AC](docs/core-implementation-ac.md)：下一轮实现的硬条件。
- [运行环境](docs/environment.md)：可重复的启动和验证命令。
- [历史归档](docs/history/README.md)：只读审计材料，不能作为当前上下文。

## 代码位置

| 责任 | 位置 |
|---|---|
| 联盟 API 与价格解释 | `services/taobao_alliance.py` |
| SKU 增强核验 | `services/taobao_sku_detail.py`、`services/taobao_sync_validation.py` |
| 扫描、数据库与审计 | `scanner.py`、`db.py` |
| 准入、检索与页面 | `autoreview.py`、`services/product_search.py`、`routes/`、`templates/` |
| 自动化验证 | `tests/` |

仓库：<https://github.com/xie132339/opportunity-workbench>。开发只在非保护分支 `feature/bootstrap` 进行；不要直接修改 `main`。
