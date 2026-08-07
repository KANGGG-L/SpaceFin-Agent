# 组件技术说明 · 安居客房源采集器（公开数据获取通道 / 反爬可行性论证）

> **状态**：✅ 技术验证完成，作为项目**公开数据获取通道**的实现基础
> **能力地图层级**：L0 数据源头 · 公开数据采集
> **引入原则**：按需接入（Just-in-Time）。本组件回答一个决策问题——"公开房源数据能不能用爬虫稳定拿到"——并把它沉淀为项目后续从公开渠道获取房产样本数据的可复现链路。

---

## 1. 为何此时引入

项目的数据分两类，获取策略不同（数据合规红线见 §6）：

- **公开渠道数据**（房产挂牌/成交、小区、POI、地理坐标）：**爬虫即公开渠道，数据源锁定安居客**——本组件就是这条通道的实现，后续开发的房产样本**持续从安居客爬取**，喂给 L3 AVM 估值。
- **个人贷款数据**（客户/征信/贷款台账等 PII）：**一律合成 populate**（`seed/generate_seed.py`），绝不使用真实个人信息。

本组件针对安居客（58 系）反爬网关逐阶段攻克，验证"安居客房源能否稳定爬取"，并把有效技术固化为可复用的采集包 `tools/anjuke_crawler/`——安居客既是验证目标，也是后续开发的**既定数据源**，本组件即其采集实现。

## 2. 生产对应物（诚实标注）

| 本项目（可跑实现） | 生产对应物 |
|--------------------|-----------|
| 单 IP 一次性抓取 + 代理池/k8s 接口（已留） | 代理池轮换 + k8s 分布式，对**安居客**持续爬取挂牌/成交样本，承载真实抵押物估值所需数据 |

本组件是项目**真实的公开数据获取通道**（非占位 mock）：安居客是项目锁定的房产数据源，本组件已完成反爬攻克与链路验证；生产化时按 §6 补齐代理轮换与分布式采集即可持续供数。房产样本走安居客爬虫，个人贷款数据走合成——两条路径并行，见 `数据现状摸底.md`（D3 信贷业务数据 / 抵押物估值样本）。

## 3. 自建的六阶段反爬攻克链路

`tools/anjuke_crawler/` 是本项目的核心产出。面对安居客（58 系）反爬网关，逐阶段尝试并固化有效技术：

| 阶段 | 解决的问题 | 采用的技术 | 对应模块 | 实测结论 |
|------|-----------|-----------|---------|---------|
| 一 | 普通 HTTP 请求拿不到内容 | requests / urllib 裸请求 | `experiments/` | ❌ 被 `@@xxzlGatewayUrl` JS 网关拦截 |
| 二 | 需要真实浏览器渲染 | Selenium / undetected-chromedriver / selenium-stealth | `experiments/` | ❌ Canvas/WebGL 指纹 + IP 即时拉黑 |
| **三** | **绕过 TLS/HTTP2 指纹检测** | **curl_cffi `impersonate=chrome` + Session 预热** | `fetcher.py` | ⚠️ **每个新 IP 仅放行第 1 页** |
| **三+** | **被标记的陌生机器（带登录态抓取）** | **DrissionPage 持久化 profile 会话重放 + 代理自动切换** | `stealth.py` | ✅ 人工登录一次后带登录态持续抓（真正把数据抓下来的组件） |
| 四 | IP 频次限制（多页被封） | 动态代理池（每请求换 IP） | `proxy.py` | ✅ 多页/可持续的唯一出路（接口已留） |
| 五 | 在线地理编码太慢（200ms/次） | 本地离线 O(1) 哈希词典 | `geocoder.py` | ✅ 单条 ~1.2ms |
| 六 | 单机吞吐不足 | k8s + Redis 队列 + asyncio 分布式 | `k8s/` + `k8s_manifests/` | ⏸ 架构完整，需 k8s 集群方可运行 |

解析层有两套 schema：`parse/numeric.py` 输出 **17 字段纯数值 schema**（户型拆解 / 房龄 / 车位 / 总价 / 单价 / 经纬度 / URL，喂给 L3 AVM 估值）；`parse/advanced.py` 输出 **18 字段增强 schema**（额外含户型串 Layout、是否含车位 Has_Parking、车位描述 Parking_Desc，供更细粒度分析）。

**外部组件**（阶段四/六的运行依赖，各自单独登记）：

- [Redis 任务队列](redis-task-queue.md)——分布式任务队列 + 结果聚合 + proxy_pool 后端
- [jhao104/proxy_pool 代理池](proxy-pool.md)——IP 轮换，突破"每 IP 1 页"频次限制
- [Kubernetes 分布式集群](k8s-crawler-cluster.md)——worker 副本集横向扩展吞吐

## 4. 关键实测发现

阶段三"curl_cffi + Session 预热"能突破网关，但对照实验给出了**精确边界**：

| 请求 | 结果 |
|------|------|
| 全新 session · 广州 `/sale/p1/` | ✅ 71 条真实房源（1.29 MB） |
| 同一 session · 广州 `/sale/p2/` | ❌ 610 B deny 拦截页 |
| 另一全新 session · 深圳 `/sale/p1/` | ❌ 610 B deny 拦截页 |

由此得出三个判断，直接支撑"走商业 API"的决策：

1. **阶段三对"单页一次性"有效**，对"多页/多区/可持续"无效——列表页闸门是 **IP 频次**。
2. **cookie 登录态对列表抓取无帮助**（实测零 cookie 裸 session 也能拿到第 1 页）——cookie 管的是登录用户功能，不是反爬闸门。
3. **代理池不是锦上添花，是必经之路**：要抓多页必须每请求换 IP。这正是阶段四/六要解决的问题，也说明把爬虫做到"可持续"的运维成本极高。

## 5. 接入与运行

依赖已在 conda env `spark`：`curl_cffi` / `lxml`（`redis` 仅分布式需要）。

```bash
conda activate spark
cd tools

# A. 离线解析（fixture 模式，不联网，可复现）——解析随包附带的真实列表页样本，应得 71 条
python -m anjuke_crawler.main parse \
    --html anjuke_crawler/tests/sample_listing.html \
    --district sh_pudong --out anjuke_crawler/output/anjuke_parse.csv

# B. 本地地理编码自测
python -m anjuke_crawler.main geocode --name 证大家园

# C. 在线抓取（单页一次性，每 IP 约仅第 1 页成功）
python -m anjuke_crawler.main crawl --city gz --pages 1 --out anjuke_crawler/output/anjuke_gz.csv

# D. 多页抓取：先起代理池（Redis + 调度 + API :5010），再设环境变量
export SPACEFIN_PROXY_BASE=http://127.0.0.1:5010
python -m anjuke_crawler.main crawl --city gz --pages 5 --out anjuke_crawler/output/anjuke_gz.csv

# E. k8s 分布式（需可用 k8s 集群）
kubectl apply -f anjuke_crawler/k8s_manifests/redis-deployment.yaml
kubectl apply -f anjuke_crawler/k8s_manifests/worker-deployment.yaml   # hostPath 先改为本仓库路径
python -m anjuke_crawler.k8s.producer --cities gz,sz --pages 3
python -m anjuke_crawler.k8s.exporter
```

抓取产物落 `tools/anjuke_crawler/output/`（已 gitignore，**不入库**）。

## 6. 数据合规红线 与 生产化前提

**数据分层红线**（本项目数据策略的硬约束）：

| 数据类型 | 策略 |
|---------|------|
| 公开渠道数据（房产挂牌/成交、小区、POI、坐标） | **爬虫即公开渠道**，本组件是其实现；可持续获取 |
| 个人贷款数据（客户/征信/贷款台账等 PII） | **一律合成 populate**（`seed/generate_seed.py`），绝不使用真实个人信息，也绝不通过爬虫采集 |

**安居客采集的生产化前提**（诚实标注，知悉并接受以下风险）：

| 维度 | 决策 / 已知风险与缓解 |
|------|----------------|
| 合规 | **数据源锁定安居客**，采集**公开、非 PII** 的房源数据。已知风险：安居客（58 系）TOS 限制批量抓取、绕过其反爬网关存在法律风险——缓解：控制访问频次与规模、代理轮换、仅取公开展示信息、不碰任何个人数据，并在正式投产前做法律审查 |
| 稳定性 | 每 IP 1 页 + 滑块验证码 → 生产化需常驻代理池（`fetch/proxy.py`）+ k8s 分布式（`distributed/`），接口已留 |
| 数据质量 | 挂牌价非成交价，含重复/虚假房源 → 入 AVM 前需去重、清洗、与成交样本校准 |

**结论**：爬虫是本项目的公开数据获取通道，**安居客是锁定的房产数据源**，反爬攻克链路已验证可复现；生产化时按上表落实代理轮换 + 分布式采集 + 数据清洗。可复用资产：17 字段数值 schema、离线地理编码词典、六阶段反爬攻克链路。

## 7. 文件清单

| 路径 | 用途 |
|------|------|
| `tools/anjuke_crawler/fetch/fetcher.py` | 抓取层（阶段三）：curl_cffi + Session 预热 |
| `tools/anjuke_crawler/fetch/stealth.py` | 隐身抓取层（阶段三+）：DrissionPage 持久化 profile 会话重放 + 代理自动切换 |
| `tools/anjuke_crawler/fetch/proxy.py` | 代理层（阶段四）：proxy_pool REST 客户端（用坏即扔） |
| `tools/anjuke_crawler/parse/numeric.py` | 解析层：17 字段纯数值 schema |
| `tools/anjuke_crawler/parse/advanced.py` | 解析层（增强）：18 字段 schema（户型串/车位描述） |
| `tools/anjuke_crawler/geocoder.py` | 地理编码：本地离线 O(1) 哈希（含 save/add 增量沉淀） |
| `tools/anjuke_crawler/distributed/{producer,worker,exporter}.py` | 分布式层（阶段六）：Redis 任务生产 / asyncio 消费 / 汇总导出 |
| `tools/anjuke_crawler/main.py` | CLI：parse / parse-advanced / geocode / crawl / stealth |
| `tools/anjuke_crawler/setup.py` | 包定义（curl_cffi / lxml / redis / DrissionPage） |
| `tools/anjuke_crawler/tests/sample_listing.html` | 随包附带的真实列表页样本（离线解析可复现，71 条） |
| `tools/anjuke_crawler/k8s_manifests/*.yaml` | Redis + Worker 副本集 k8s 清单 |
| `tools/anjuke_crawler/scripts/demo_pipeline.py` | 端到端流水线演示（分布式队列 + 代理池对接 + 抓取解析，替身环境可复现） |
| `tools/anjuke_crawler/experiments/` | 阶段一/二反爬绕过手段的实验脚本与结论归档 |
| `docs/poc/data-source/anjuke-exploratory/README.md` | 数据源探索 PoC 总文档（反爬演进的完整论证） |
