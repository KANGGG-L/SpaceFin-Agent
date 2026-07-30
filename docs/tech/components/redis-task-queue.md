# 组件技术说明 · Redis 任务队列（anjuke_crawler 外部依赖）

> **状态**：✅ 代码集成并验证（沙箱以 fakeredis 替身跑通）／ ⏳ 生产需真实 Redis 服务
> **能力地图层级**：anjuke_crawler 分布式采集的中间件
> **引入原则**：按需接入。Redis 在安居客采集链路承担两个角色——分布式任务队列 + proxy_pool 的代理存储后端，是"多页/多区可持续抓取"的必需中间件。

---

## 1. 为何引入

安居客列表页按 **IP 频次**拦截（每个新 IP 约只放行第 1 页），多页/多区抓取必须分布式 + 代理轮换。Redis 在此承担：

- **任务队列**：`anjuke_task_queue`（List），producer 推入 城市×页 任务，worker 副本 `lpop` 并行消费；
- **结果聚合**：`anjuke_results`（Hash，以 url 去重），worker 写入、exporter 汇总导出；
- **proxy_pool 后端**：jhao104/proxy_pool 用 Redis 存储/验证代理（见 [proxy-pool.md](proxy-pool.md)）。

## 2. 生产对应物（诚实标注）

| 本项目（沙箱可跑） | 生产对应物 |
|--------------------|-----------|
| fakeredis（内存 Redis 协议实现，`scripts/demo_pipeline.py`） | 常驻 Redis 服务（k8s `redis-master`，见 `k8s_manifests/redis-deployment.yaml`） |

沙箱无 redis-server / 编译器 / sudo，以 fakeredis 验证**队列编排逻辑**（真 lpop/hset/hgetall/去重/导出）；生产换真实 Redis，代码零改动。

## 3. 数据结构与队列流

```
producer ──rpush──> anjuke_task_queue (List) ──lpop──> worker×N (asyncio)
                                                        │ parse + 去重
                                                        ▼ hset
                                              anjuke_results (Hash, key=url)
                                                        │
                                              exporter ──hgetall──> CSV
```

## 4. 如何运行

```bash
# 沙箱（fakeredis 替身，一键复现三段流水线）
python -m anjuke_crawler.scripts.demo_pipeline

# 生产（真实 Redis + k8s）
kubectl apply -f anjuke_crawler/k8s_manifests/redis-deployment.yaml
export REDIS_HOST=redis-service REDIS_PORT=6379
python -m anjuke_crawler.distributed.producer --cities gz,sz --pages 3
python -m anjuke_crawler.distributed.exporter
```

## 5. 合规

Redis 仅作中间件，不存储任何个人数据；流经它的房产样本均为公开非 PII 数据。数据红线见 [anjuke-crawler.md](anjuke-crawler.md) §6。
