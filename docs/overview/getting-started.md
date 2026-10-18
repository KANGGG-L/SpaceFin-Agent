# 快速上手

> 目标：在一台新机器上把核心闭环（数据 → 引擎 → 驾驶舱）跑起来。全量采集链路（路径 B）与 VPS 部署不在本文展开，见文末指针。

## 环境要求

| 依赖      | 要求                                                                                     |
| --------- | ---------------------------------------------------------------------------------------- |
| Python    | 3.10+（仓库目标版本 3.10；作者环境为 conda `spark` 3.10.18）                             |
| 依赖安装  | 全量运行/测试：`pip install -r tools/orchestrator/requirements.txt`（含 PyMySQL、scikit-learn、scipy、redis、curl_cffi 等）；只看前端最少只需 PyMySQL |
| Docker    | 跑 MySQL 业务库必需（Linux 用 `docker`；WSL/Windows 用 Docker Desktop）                  |
| Git LFS   | **当前未启用**——AVM 模型 `output/avm/model.joblib`（约 22MB）位于被 `.gitignore` 排除的 `output/` 目录，**不随仓库分发**。使用前需本机 `python3 tools/avm/train.py` 生成（若后续为模型启用 LFS 托管，则须 `git lfs install` 后 `git lfs pull`） |
| Node（可选） | 仅提交钩子（commitlint）需要，跑项目不需要                                            |

> 仓库脚本里常见的 `tools/orchestrator/.venv/bin/python` 是作者机器上指向 conda `spark` 环境的符号链接别名（不随 git 分发）。新机器上用任意装好依赖的 `python3` 替换即可，本文命令均如此书写。

## 第一步：起数据 + 看驾驶舱（约 5 分钟）

```bash
cp .env.example .env      # .env 已被 gitignore，勿提交真实密码
make up                   # 启动 MySQL（首次自动执行 sql/init/01_schema.sql + 02_seed.sql 合成 seed）
make health               # ✅ 校验：探活通过
```

启动前端（stdlib `http.server`，零额外依赖）：

```bash
mkdir -p output/frontend
SF_DEV_MODE=1 python3 tools/frontend/app.py --host 127.0.0.1 --port 8500
# 浏览器打开 http://127.0.0.1:8500
```

- `SF_DEV_MODE=1` 时使用内置演示账号（dev-only）：`admin`/`admin`（全权限）、`postloan`/`postloan`（仅看预警，1104 报送页 403）。不设该变量则启动时生成随机强口令并持久化到 `output/frontend/credentials.json`。
- 未跑过 pipeline 时，P5/P7/P3 等依赖 `output/` 产物的页面显示「产物缺失」空态（已优雅降级，不崩溃）——要看完整数据走根 README 的路径 B。

## 第二步：开发热重载（改代码自动重启）

```bash
./dev.sh                # 确保 MySQL + Redis 起好，拉起前端(:8500) + 后端 master(:5102)，改码即重启
./dev.sh --no-backend   # 只拉前端
```

原生 master 用 5102 端口 + Redis db=1，与 Docker 采集栈（5100 / db=0）隔离，互不打架。日志在 `output/dev/`，停止用 `pkill -f dev_server.py`。

## 第三步：跑测试

```bash
python3 -m pytest tools/ -q
```

- 全仓共 **109 个** `test_*.py`（tools/ 内 108 + `deploy/superset/` 1 个）；四个核心模块（frontend 48 / risk 6 / alerting 5 / avm 4，合计 63 个文件、858 个 `def test_` 用例）覆盖核心闭环。
- 爬虫探索脚本目录 `tools/anjuke_crawler/experiments/` 已在 `pyproject.toml` 中排除出收集范围（零断言的选型记录，非测试）。
- 多数测试零 DB、零网络（用 `fakeredis` 与 fake 连接替身），无需起 Docker 即可跑。

## 第四步：7 天风险演进演示（可选，需先有路径 A 的 MySQL）

演示剧本内置在 `tools/dev/`（逐日调用**真实引擎**灌数，非 mock）：第 4 天广州挂牌价异动下探 → AVM 重估 → LTV 集体上穿预警线 → 第 5 天达峰 → 批量确认处置 → 第 7 天收口。

```bash
# 数据规模：先把合成 seed 扩到 5,000 笔并灌库
python3 seed/generate_seed.py 5000 --write-db

# 完整回填：reset（回滚扰动 + 重训基线 + 重灌 5000 笔）→ D1..D7
python3 tools/dev/backfill_7d.py --all

# 单日重跑 / 一键回滚广州扰动 / 跑验收 SQL 检查
python3 tools/dev/backfill_7d.py --day 4
python3 tools/dev/backfill_7d.py --rollback-gz
python3 tools/dev/backfill_7d.py --verify

# 逐项验收（A/C/D 组打勾清单）；标准见 tools/dev/acceptance_checklist.md
python3 tools/dev/verify.py
```

注意：回填期间请暂停 CDC 常驻进程（`tools/cdc/main.py` / `consumer.py`），否则增量消费会用当日日期并发覆写 ADS 表、破坏回填确定性（幂等，随时可重启）。广州扰动的原值记在 `ads_demo_gz_perturb`，`--rollback-gz` 一键恢复。

## 远程演示访问（可选）

前端默认只绑 `127.0.0.1:8500`。要让外部访客临时远程访问，无需开放 VPS 端口：

```bash
tools/ops/cloudflared_demo.sh    # 经 Cloudflare quick tunnel 生成 *.trycloudflare.com 临时 URL
```

URL 每次启动随机变化、进程停止即失效，仅适合临时演示（需先安装 cloudflared，脚本会给出安装命令）。详见 [tools/ops/README.md](../../tools/ops/README.md)。

## 往后走

| 想做什么                                   | 去哪看                                                          |
| ------------------------------------------ | --------------------------------------------------------------- |
| 全量本地链路（采集 + ETL + 空间 + 前端）   | 根 [README.md](../../README.md)「路径 B · 全量本地链路」（8 步，每步带校验点） |
| Airflow 每日 DAG 编排                      | [../../airflow/README.md](../../airflow/README.md)（注意：apache-airflow 未随仓库环境安装，`make up` 不起 Airflow；DAG 可编译，端到端调度未在标准环境验证，跑通记录仅来自开发机） |
| Doris + MinIO 湖仓、Superset BI（可选副本） | [../../deploy/doris-minio/](../../deploy/doris-minio/) · [../../deploy/superset/README.md](../../deploy/superset/README.md) |
| VPS 公网部署实录                           | [../../deploy/vps/DEPLOYMENT.md](../../deploy/vps/DEPLOYMENT.md)（IP/邮箱均已脱敏为占位符） |
| 各组件技术说明                             | [../tech/components/](../tech/components/)（19 篇）              |

## 常见坑（新手最易撞的三个）

1. **`output/` 权限**：该目录会被 Docker 以 root 身份创建；宿主以普通用户跑 ETL 写状态文件会 `PermissionError`——先 `chown` 或在容器内跑。
2. **P1 页 ODS 显示「空表」**：CDC 变更日志表默认就是空的（demo 数据走批处理、不经 binlog），页面标灰不标红，属预期。想填充见 `tools/cdc/bootstrap_ods.py`。
3. **AVM 页面没数据 / `predict.py` 报错**：多半是 Git LFS 未启用（模型只是指针文本）或缺 `scikit-learn`——`git lfs install && git lfs pull`，并确认用的是装过 `tools/orchestrator/requirements.txt` 的解释器。
