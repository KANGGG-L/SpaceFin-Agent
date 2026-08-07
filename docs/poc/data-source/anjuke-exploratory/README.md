# 安居客房源数据探索 PoC（公开数据获取通道 / 反爬可行性论证）

> **状态**：✅ 技术 PoC 完成，作为项目**公开数据获取通道**的实现基础
> **能力地图层级**：L0 数据源头 · 公开数据采集
> **核心命题**：爬虫（项目的公开数据获取渠道）在安居客反爬强度下能走通到哪一步？把有效技术沉淀为可复用采集包。
> **核心产出**：`tools/anjuke_crawler/` 自建采集包（详见 [docs/tech/components/anjuke-crawler.md](../../../tech/components/anjuke-crawler.md)）。

## 1. 为何做这个 PoC

项目的公开渠道数据（房产挂牌/成交、小区、POI）**以爬虫获取，数据源锁定安居客**——这是项目数据策略的明确选择（个人贷款数据则一律合成，见 §5 红线）。安居客反爬较强，是检验"公开爬虫能走多远"的硬核试金石。本 PoC 把"安居客爬虫到底能走通到哪一步"在沙箱里穷尽实测，将有效技术固化为 `tools/anjuke_crawler/`，作为后续开发持续从安居客采集房产样本的实现基础。

## 2. 起点：三种常规实现全部被拦（基线）

最初先后试过三种常规实现，均被安居客（58 系）反爬网关拦下：

| 实现 | 客户端 | 实际抓到的内容 | HTTP 层结果 |
|---|---|---|---|
| 裸 HTTP | `urllib` 标准库 | `@@xxzlGatewayUrl` JS 重定向壳（930B） | 200 |
| 真浏览器 | `selenium.webdriver` + 无头 Chrome | `callback.58.com/antibot/verifycode` 极验页 | 200 |
| 真浏览器 | `DrissionPage.ChromiumPage` | `callback.58.com/antibot/deny.do` 直接拒绝页 | 200 |

**共同点**：HTTP 层都返回 200，但 body 是反爬网关挑战页——无 JS 执行能力的客户端（urllib）连壳都看不到内容；有 JS 能力的（Selenium/DrissionPage）能渲染，但**第二次请求就被同 IP 拉黑**。

## 3. 自建采集包的突破技术（沉淀进 tools/anjuke_crawler/）

针对上面的失败，逐阶段攻克并固化为 `tools/anjuke_crawler/` 包，核心三招：

### 3.1 TLS / JA3 / HTTP/2 指纹伪装（`fetcher.py`）

```python
s = requests.Session(impersonate="chrome")   # curl_cffi
```

`curl_cffi` 用 libcurl 实现，能完整模拟 Chrome 的 TLS 握手（JA3/JA4 fingerprint）+ HTTP/2 SETTINGS 帧 + Client Hello 顺序——这是第 2 节三种实现的 requests/urllib 都做不到的，也正是被网关识别为"机器"的根因。

### 3.2 Session 预热（**关键步骤，基线实现全部漏掉**）

```python
# 步骤 1：访问首页，收获 sessid / ctid / xxzl_cid 等关键 cookie
s.get("https://guangzhou.anjuke.com/", headers={"Referer": "https://www.baidu.com/"})
# 步骤 2：带 cookie + Referer 请求列表页
s.get("https://guangzhou.anjuke.com/sale/p1/", headers={"Referer": "https://guangzhou.anjuke.com/"})
```

先拿全局 Cookie 再带 Referer 请求列表页，才能绕过 `deny.do` 网关——基线的三种实现都是"冷启动直冲列表页"，所以必被拦。

### 3.3 卡片级解析：lxml XPath + regex 兜底（`parser.py`）

输出 17 字段数值化 schema（标题 / 小区 / 户型拆解 / 单价 / 坐标 / URL 等），可直接喂给 L3 AVM 估值。

**关键 XPath 修复**（本项目踩坑后修正）：
- 错误：`//div[contains(@class, 'property')]` —— 会同时匹配 `property-content`、`property-price` 等子节点，**每套房源被重复解析 4 次**。
- 正确：`//div[@class='property']` —— 精确匹配，去重后得到 71 条独立房源。

## 4. 实测结果

### 4.1 突破网关，抓到真实数据（单页一次性成功）

本机 IP 未被标记时，curl_cffi + Session 预热**成功抓回 1.29 MB 真实列表页**（广州在售房源），解析得 71 条独立房源。该页面已作为 fixture 收进包内（`tools/anjuke_crawler/tests/sample_listing.html`），离线可复现：

```
$ python -m anjuke_crawler.main parse \
    --html anjuke_crawler/tests/sample_listing.html --district sh_pudong
[+] Saved 71 clean numeric-schema records ...
  示例: 证大家园 | 3室2厅 | 95.0㎡ | 500.0万
```

### 4.2 但每 IP 只放行 1 页（对照实验，关键边界）

| 请求 | 结果 |
|---|---|
| 全新 session · 广州 `/sale/p1/` | ✅ 71 条（1.29 MB） |
| 同一 session · 广州 `/sale/p2/` | ❌ 610 B deny 拦截页 |
| 另一全新 session · 深圳 `/sale/p1/` | ❌ 610 B deny 拦截页 |

即列表页闸门是 **IP 频次**：第 1 页放行，第 2 页起立刻封，跨 session 也封。curl_cffi + Session 预热扛不住——要可持续必须每请求换 IP（代理池）。

### 4.3 cookie 登录态对列表抓取无帮助（证伪实验）

曾导出真实登录 cookie 注入 session 复测，但**零 cookie 的裸 session 同样能拿到第 1 页**——说明列表页闸门认 IP 不认登录态，cookie 管的是登录用户功能而非反爬。

### 4.4 本地离线地理编码（`geocoder.py`）

```python
OFFLINE_COMMUNITY_DB = {
    "天河": (23.1246, 113.3612),     # 广东 5 城区中心
    "陆家嘴": (31.2394, 121.4912),   # 上海板块
    "证大家园": (31.2849, 121.5921), # 真实小区样本
    ...
}
```

`geocoder.geocode(community, full_text)` 单条 ~1.2 ms（dict O(1) 查找），规避在线 API 200ms/次延迟——为 L2 空间风控提供经纬度。

### 4.5 代理池 + k8s 分布式（`proxy.py` + `k8s/`，接口已留）

`proxy.py` 封装代理池 REST 客户端（用坏即扔），`k8s/` 提供 Redis 队列 + asyncio 多 worker 的分布式骨架。这是把"每 IP 1 页"扩展为"可持续多页"的必经之路，但需常驻代理池 + k8s 集群方可运行，本 PoC 仅论证 + 留接口。

## 5. 数据合规红线 与 生产化前提

**数据分层红线**：

| 数据类型 | 策略 |
|---|---|
| 公开渠道数据（房产挂牌/成交、小区、POI、坐标） | **爬虫即公开渠道，数据源锁定安居客**，本 PoC 沉淀的采集包即其实现 |
| 个人贷款数据（客户/征信/贷款台账等 PII） | **一律合成 populate**（`seed/generate_seed.py`），绝不采集真实个人信息 |

**安居客采集生产化的前提**（诚实标注，知悉并接受以下风险）：

| 维度 | 决策 / 已知风险与缓解 |
|---|---|
| **合规** | **数据源锁定安居客**，采公开、非 PII 的房源数据。已知风险：安居客（58 系）TOS 限制批量抓取、绕过其反爬网关有法律风险——缓解：控制访问频次与规模、代理轮换、仅取公开展示信息、不碰个人数据，并在正式投产前做法律审查 |
| **稳定性** | 每 IP 1 页 + 滑块验证码 → 可持续采集需代理池（`proxy.py`）+ k8s 分布式（`k8s/`），接口已留 |
| **数据质量** | 挂牌价非成交价，含重复/虚假房源 → 入 AVM 前需去重、清洗、与成交样本校准 |

**结论**：爬虫是本项目的公开数据获取通道，**安居客是锁定的房产数据源**，反爬攻克链路已验证可复现；生产化按上表落实代理轮换 + 分布式采集 + 清洗。本 PoC 留下的资产：

1. `tools/anjuke_crawler/` —— 自建采集包，后续开发从安居客持续采集房产样本的实现基础；
2. 17 字段数值 schema —— 给 AVM 估值（`docs/poc/core-prototype/` 的 `dwd_enriched.csv` 已用同样字段形态）；
3. 离线 fixture（`tests/sample_listing.html`）—— 给回归测试用，无网也能跑。

## 6. 运行示例

```bash
conda activate spark
cd tools

# 1. 离线解析随包样本（可复现，应得 71 条）
python -m anjuke_crawler.main parse \
    --html anjuke_crawler/tests/sample_listing.html \
    --district sh_pudong --out anjuke_crawler/output/anjuke_parse.csv

# 2. 在线抓取单页（依赖本机 IP 未被标记；通常一次后即被限速）
python -m anjuke_crawler.main crawl --city gz --pages 1 --out anjuke_crawler/output/anjuke_gz.csv

# 3. 多页抓取需先起代理池，再设环境变量
export SPACEFIN_PROXY_BASE=http://127.0.0.1:5010
python -m anjuke_crawler.main crawl --city gz --pages 5 --out anjuke_crawler/output/anjuke_gz.csv
```

## 7. 文件清单

| 路径 | 用途 |
|---|---|
| `tools/anjuke_crawler/{fetcher,parser,geocoder,proxy,main}.py` | 采集包：抓取 / 解析 / 地理编码 / 代理 / CLI |
| `tools/anjuke_crawler/tests/sample_listing.html` | 随包真实列表页样本（离线解析 71 条） |
| `tools/anjuke_crawler/k8s/` + `k8s_manifests/` | 分布式骨架（Redis 队列 + worker 副本集） |
| `docs/tech/components/anjuke-crawler.md` | 组件技术说明（为何引入 / 生产对应物 / 接入运行） |
