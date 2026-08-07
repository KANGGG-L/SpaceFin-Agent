# 组件技术说明 · Kubernetes 分布式采集集群（anjuke_crawler 外部依赖）

> **状态**：✅ 清单与 worker 逻辑齐备，本地以单进程 asyncio 验证； ⏳ 真实多副本需可用 k8s 集群
> **能力地图层级**：anjuke_crawler 反爬攻克阶段六（横向扩展吞吐）
> **引入原则**：按需接入。单机单 IP 抓取吞吐受"每 IP 1 页"限制；要大规模采集，需多 worker 副本横向扩展、每 Pod 轮换出口 IP。

---

## 1. 为何引入

单机抓取受 IP 频次限制，吞吐极低。k8s 提供：

- **横向扩展**：worker 副本集（默认 3 副本）并行消费 Redis 任务队列，吞吐随副本数线性扩展；
- **每 Pod 出口 IP 独立**：配合代理池进一步分散 IP 频次压力；
- **可观测**：导出 CSV 带 `pod_name` 列，标记每条记录来自哪个 worker。

## 2. 生产对应物（诚实标注）

| 本项目（沙箱可跑） | 生产对应物 |
|--------------------|-----------|
| 单进程 asyncio worker + fakeredis（`scripts/demo_pipeline.py`） | k8s `k8s-crawler-worker` 副本集 + 真实 Redis（`k8s_manifests/`） |

沙箱无 k8s 集群，以单进程 asyncio 验证 worker 消费循环与 Redis 队列编排（真实代码路径）；生产部署到 k8s，`worker.py` 逻辑零改动，仅靠副本集横向扩展。

## 3. 架构

```
kubectl apply redis-deployment.yaml   →  redis-service (任务队列 + 结果聚合)
kubectl apply worker-deployment.yaml  →  k8s-crawler-worker ×3
                                            │ python -m anjuke_crawler.distributed.worker
                                            │ asyncio 协程池 lpop 任务 → curl_cffi 抓取 → 解析 → hset
producer (城市×页 任务 rpush)  ──────────▶  anjuke_task_queue
exporter (hgetall 汇总去重)    ◀──────────  anjuke_results
```

## 4. 如何运行

```bash
# 生产（需可用 k8s 集群）
kubectl apply -f anjuke_crawler/k8s_manifests/redis-deployment.yaml
kubectl apply -f anjuke_crawler/k8s_manifests/worker-deployment.yaml   # hostPath 先改为本仓库在节点上的路径
export REDIS_HOST=redis-service REDIS_PORT=6379
python -m anjuke_crawler.distributed.producer --cities gz,sz,fs,dg,zh --pages 3
python -m anjuke_crawler.distributed.exporter   # 导出 anjuke_k8s_cluster_results.csv

# 沙箱（无 k8s，单进程 + fakeredis 验证编排）
python -m anjuke_crawler.scripts.demo_pipeline
```

> **hostPath 注意**：`worker-deployment.yaml` 用 hostPath 挂载代码，`path` 需改为 k8s 节点上本仓库的真实绝对路径（生产建议改用镜像构建）。

## 5. 合规

集群仅采集公开、非 PII 房源数据；数据源锁定安居客（TOS/反爬风险已知悉并接受）。每 Pod 须配合代理轮换并控制总访问频次，投产前完成法律审查。详见 [anjuke-crawler.md](anjuke-crawler.md) §6。
