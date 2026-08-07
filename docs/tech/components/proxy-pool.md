# 组件技术说明 · jhao104/proxy_pool 动态代理池（anjuke_crawler 外部依赖）

> **状态**：✅ 客户端集成并验证 REST 契约（沙箱以 mock 替身跑通）／ ⏳ 生产需常驻 proxy_pool + 代理源
> **能力地图层级**：anjuke_crawler 反爬攻克阶段四（IP 轮换）
> **引入原则**：按需接入。安居客列表页按 IP 频次拦截，多页/可持续抓取必须每请求换 IP——动态代理池是唯一出路。

---

## 1. 为何引入

实测安居客列表页闸门是 **IP 频次**而非登录态：第 1 页放行，第 2 页起跨 session 即封（见 [anjuke-crawler.md](anjuke-crawler.md) §4）。`curl_cffi` 指纹伪装 + 登录态都扛不住 IP 维度封锁，**必须每请求轮换出口 IP**。采用 GitHub 热门开源项目 [jhao104/proxy_pool](https://github.com/jhao104/proxy_pool) 作为代理池。

## 2. 生产对应物（诚实标注）

| 本项目（沙箱可跑） | 生产对应物 |
|--------------------|-----------|
| `scripts/demo_pipeline.py` 里复刻 `/get//pop//delete/` 契约的 mock 服务 | 常驻 jhao104/proxy_pool（Flask API :5010 + Redis 后端 + 代理爬取调度） |

沙箱无 redis-server / 公网代理源，以 mock 验证本项目 `fetch/proxy.py` 的 `ProxyClient` 与 proxy_pool 的 **REST 契约对接**（取代理、用坏即扔闭环）；生产起真实 proxy_pool，客户端零改动。

## 3. 对接契约

`fetch/proxy.py` 的 `ProxyClient` 调用 proxy_pool 三个端点：

| 端点 | 用途 | 客户端方法 |
|------|------|-----------|
| `GET /get/` | 取一个可用代理 | `get_proxy()` |
| `GET /pop/` | 取出并移除一个代理 | `pop_proxy()` |
| `GET /delete/?proxy=ip:port` | 剔除失效代理 | `delete_proxy()` |

**用坏即扔闭环**：crawler 抓取失败/被拦时自动 `delete_proxy()` 剔除该代理，避免坏代理反复使用。

## 4. 如何运行

```bash
# 生产：起 proxy_pool（需先有 Redis）
docker run -d --name proxy_pool -p 5010:5010 jhao104/proxy_pool:latest
# 或按其 README 本地跑：python proxyPool.py schedule & python proxyPool.py server

# 本项目启用代理池
export SPACEFIN_PROXY_BASE=http://127.0.0.1:5010
python -m anjuke_crawler.main crawl --city gz --pages 5   # 多页抓取走代理轮换

# 沙箱：契约对接演示（mock proxy_pool）
python -m anjuke_crawler.scripts.demo_pipeline
```

## 5. 合规

代理池仅用于轮换出口 IP 以采集**公开、非 PII** 房源数据；数据源锁定安居客（其 TOS 限制批量抓取、绕过反爬存在法律风险，已知悉并接受）。缓解：控制访问频次与规模、仅取公开展示信息、投产前法律审查。详见 [anjuke-crawler.md](anjuke-crawler.md) §6。
