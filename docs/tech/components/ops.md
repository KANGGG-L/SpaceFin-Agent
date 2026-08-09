# 组件技术说明 · 运维入口（systemd 用户服务 + 采集编排 + 资源管家）

> **状态**：✅ 本机 Linux 部署运行中（7 个用户级 systemd 服务常驻，linger 已开）
> **能力地图层级**：全局运维层——把「宿主常驻服务 + Docker 容器栈 + 内存水位」收口到一个入口
> **引入原则**：用 systemd 用户单元托管脆弱的裸跑进程（nohup 会话退出即丢，前端已掉线一次）；
> 用 manage.sh 做 15G/4 核主机下的内存纪律（T0/T1 分级 + 一键降级）

---

## 1. 为何引入

本机是单台 Linux 宿主（15G/4 核），链路组件却很多：Docker 容器栈（MySQL / Redis / Doris /
Kafka / Flink / Minio）、CDC 流、Airflow 调度、前端驾驶舱、宿主 Chrome 渲染服务。三个问题：

1. **裸跑进程易丢**：之前用 nohup 挂服务，SSH 会话退出即丢进程，已发生一次前端掉线；
2. **资源吃紧**：全组件常驻约 7~8G，接近 15G 上限，需要「分优先级、内存吃紧时整层停」的纪律；
3. **入口分散**：容器归 docker、服务归 nohup、状态靠人肉拼，没有一个统一的查看/启停入口。

因此引入 `deploy/systemd/` 用户单元 + `tools/ops/manage.sh`。注意：**采集编排**（master 主备 +
worker，Docker 化，见 [crawler-orchestrator.md](crawler-orchestrator.md)）与**宿主渲染服务**
（`spacefin-host-render.service`）也在此收口，Airflow DAG 的 `start_stack` 任务即调
`tools/orchestrator/start_all.sh` 幂等拉起这套（见 [airflow.md](airflow.md) §3）。

## 2. 生产对应物（诚实标注）

| 本项目（本机可跑实现） | 生产对应物 |
|---|---|
| systemd 用户单元（linger 常驻） | k8s Deployment / 托管平台的 PaaS 进程管理 |
| `manage.sh` 内存水位监控 + 手动整层降级 | HPA / 弹性伸缩 + 监控告警（Prometheus + Alertmanager） |
| 单台 15G 宿主，T0/T1 手动分级 | 资源隔离（cgroup / namespace）+ 自动驱逐策略 |
| `docker update --restart unless-stopped` 保活 | 容器编排平台自愈 / livenessProbe |

## 3. 部署形态（本机 Linux，实际运行）

| 项 | 值 |
|---|---|
| 宿主 | Linux，15G/4 核，已配 4G swap 兜底 |
| 托管方式 | 用户级 systemd（`~/.config/systemd/user/`），linger 常驻 |
| 配置目录 | `~/.config/spacefin/*.env`（cdc.env / render.env，**值勿加引号**） |
| 常驻前提 | `sudo loginctl enable-linger $USER`——无登录会话也常驻，重启宿主自动拉起 |
| 容器自愈 | 8 个核心容器 restart policy 统一 `unless-stopped` |

### 服务清单（用户级 systemd）

| 服务 | 用途 | 配置 | 备注 |
|---|---|---|---|
| `spacefin-cdc` | 宿主 MySQL binlog → ODS（I-01） | `~/.config/spacefin/cdc.env` | binlog 读取必须单点常驻（server_id 唯一、位点连续） |
| `spacefin-cdc-consumer` | ODS → DWS/ADS 增量同步（S1） | 同 cdc.env + `SPACEFIN_CDC_CONSUMER_INTERVAL` | 与 cdc 拆开：下游可独立重启/重放，不反压 binlog |
| `airflow-scheduler` | 每日 00:30 触发采集 DAG | 单元内 `AIRFLOW_HOME` / `PATH` 含 airflow-venv/bin | 单元在仓库外，见 [airflow.md](airflow.md) §4 |
| `airflow-webserver` | Airflow Web UI（:8080） | 同上 | — |
| `spacefin-host-render` | 宿主 fangyuan 渲染（Chrome + DrissionPage，:8899） | `~/.config/spacefin/render.env`（`RENDER_PORT=8899` / `RENDER_SLOTS=5`） | `LimitNOFILE=8192`：多路 Chrome + CDP 会打满默认 fd 上限（踩过 OSError(24)） |
| `spacefin-frontend` | 前端驾驶舱（S5，:8500） | ExecStart 硬编码仓库路径（无 env 文件） | 换机部署须同步改路径 |
| `spacefin-stream-producer` | （精简版已移除）读 ods_cdc_log → Kafka 实时链路 | — | 实时层 Kafka/Flink 已于精简版分支删除 |

> `deploy/systemd/` 仓库内收录 5 个单元（cdc / cdc-consumer / host-render / frontend /
> stream-producer），airflow 两个单元实际运行在 `~/.config/systemd/user/`（仓库外，未随 repo
> 交付）。单元内 `ExecStart` 一律用 `/bin/sh -c` 展开 `EnvironmentFile` 里的路径，避免把本机
> 绝对路径硬编码进仓库；frontend / stream-producer 是硬编码本机路径的两个例外（见文件注释）。

## 4. manage.sh（资源管家）

`tools/ops/manage.sh` 把「容器 + systemd 服务 + 内存」收口到一个命令。依赖约定：
容器 restart policy 为 `unless-stopped`；服务为 enable + linger 常驻。

**三级运行策略（内存纪律）**

| Tier | 定位 | 组件 | 约占用 | 何时停 |
|---|---|---|---|---|
| T0 恒驻 | 链路命脉 | 容器 spacefin-mysql / spacefin-redis；服务 spacefin-cdc / spacefin-cdc-consumer / airflow-scheduler / airflow-webserver / spacefin-frontend | ~2.5G | 不主动停，停机 = 业务中断 |
| T1 可降级 | 计算/队列 | 容器 spacefin-doris-fe / be、spacefin-minio / spacefin-kafka / flink-jobmanager / taskmanager；服务 spacefin-stream-producer | ~4.7G | 内存吃紧时 `stop t1` 一键释放 |
| T2 按需 | 任务型 | 爬虫、离线渲染等 | 不定 | 用完即弃，脚本不管理 |

内存预算（参考）：T0+T1 常驻约 7~8G；`available` < 2.5G 视为吃紧。

**命令**

```bash
./manage.sh status               # 容器 docker ps + systemd 服务 + 内存水位 free -h
./manage.sh start <t0|t1>        # 整层启动（先容器后服务，含 preflight 前置检查）
./manage.sh stop  <t0|t1>        # 整层停止（先服务后容器；stop t1 释放约 4.7G）
./manage.sh watch [seconds]      # 每 N 秒(默认30)监控内存，available<2.5G 时 stderr 告警
./manage.sh help
```

**实现要点**（`manage.sh`）

- `XDG_RUNTIME_DIR` 兜底：cron / 非交互 shell 里 `systemctl --user` 会报 "Failed to connect to
  bus"，脚本开头兜底为 `/run/user/$(id -u)`；
- `mem_avail_mb` 取 `free -m` Mem 行第 7 列 `available`——系统按可回收性估算的"真可用"，比
  `free` 列更接近实际可分配量；
- `crawl_window`：Asia/Shanghai 00:30~04:00 是 DAG 采集窗口，采集集群 + 渲染进程是内存大头，
  `preflight` 检测到该窗口会提示 T1 实时层避让（爬虫与实时链同时满载有 OOM 风险，优先保数据源）；
- `preflight`：启动 T1 前检查内存水位（< 2.5G 告警）与采集窗口；
- 启停顺序刻意区分：`start` 先容器后服务（服务依赖数据库/队列先就绪，避免连接失败反复重启）；
  `stop` 先服务后容器（先把生产者/消费者拉下线再停存储，避免写半截数据或刷告警）；
- `watch` 告警走 **stderr**——非交互守护场景 stdout 可能被丢弃，stderr 便于管道/cron 捕获。

## 5. 已知坑

1. **docker 需 sudo**：本机 docker 命令需 sudo 前缀（或先把当前用户加入 docker 组并重新登录）。
   注意 `manage.sh` / `start_all.sh` 内部**直接裸调 `docker`**，未授权 shell 下会 permission
   denied——需 `sudo bash` 再执行，或加组后重开 shell。
2. **npx 不在 PATH**：pre-commit 的 commitlint 钩子 entry 是 `npx --no-install commitlint --edit`
   （`.pre-commit-config.yaml:25`），本机 Node v24 装在 `~/node24`，新 shell / 非交互 shell 的
   PATH 里没有它，提交时报 `npx: command not found`。解决：
   `export PATH=/home/azureuser/node24/bin:$PATH` 后再 `git commit`（或写进 `~/.bashrc`）。
3. **Chrome 150 需 chmod -R +x**：宿主渲染服务（DrissionPage 驱动）用
   `/opt/chrome150/chrome-linux64`，解压后二进制无执行位，Chrome 起不来。首次使用须
   `chmod -R +x /opt/chrome150`（WidevineCdm 等子目录一并给执行位）。
4. **systemd 把引号当值的一部分**：`~/.config/spacefin/*.env` 里值**勿加引号**，否则 ExecStart
   展开时引号会进值导致路径错（见 `spacefin-cdc.service:4` 注释）。
5. **frontend / stream-producer 单元硬编码了本机仓库路径** `/home/azureuser/SpaceFin-Agent`，
   换机部署需同步改 ExecStart（或用 cdc 的 EnvironmentFile 模式重写）。
6. **容器自愈只改运行中策略**：`docker update --restart unless-stopped` 只对已运行容器生效，
   新建容器要遵循 `docker-compose.yml` 里的 restart 定义。

## 6. 目录

| 路径 | 用途 |
|---|---|
| `deploy/systemd/spacefin-cdc.service` | CDC binlog → ODS 用户单元（模板） |
| `deploy/systemd/spacefin-cdc-consumer.service` | CDC 下游增量同步用户单元（模板） |
| `deploy/systemd/spacefin-host-render.service` | 宿主 fangyuan 渲染用户单元（模板） |
| `deploy/systemd/spacefin-frontend.service` | 前端驾驶舱用户单元（硬编码路径） |
| `deploy/systemd/spacefin-stream-producer.service` | 实时 producer 用户单元（硬编码路径） |
| `tools/ops/manage.sh` | 资源管家：status / start / stop / watch |
| `tools/ops/README.md` | 运维手册（三级策略 + 恢复 + 托管说明） |
| `tools/orchestrator/start_all.sh` | 采集编排一键启动（Airflow start_stack 调用） |
| `~/.config/spacefin/*.env` | 托管配置（cdc.env / render.env，本机，不入库） |
| `~/.config/systemd/user/` | 实际生效的用户单元（airflow 两个 + 上面五个） |

## 7. 合规

运维层本身不触碰业务数据，只管理进程生命周期、容器策略与内存水位；配置目录
`~/.config/spacefin/*.env` 中的连接口令等敏感项仅存本机且不入库，属作品集可接受范围；
投产前应移入 secret 管理（systemd `LoadCredential=` 或托管密钥）。CDC/渲染/采集各组件对
数据的使用口径与 [crawler-orchestrator.md](crawler-orchestrator.md) §6 / [cdc-downstream.md](cdc-downstream.md) 一致。
