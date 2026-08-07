# 反爬攻克实验脚本（阶段一 / 阶段二的探索记录）

本目录是安居客反爬攻克过程中的**实验脚本归档**——记录在确定"curl_cffi + Session 预热
+ 持久化 profile"主路线之前，对各种绕过手段的逐一尝试与结论。它们大多被验证为**不足以
稳定突破**，但作为决策证据保留（说明"为什么最终选了主路线"）。

| 脚本 | 尝试的手段 | 对应阶段 | 结论 |
|------|-----------|---------|------|
| `test_58_antibot.py` | curl_cffi + Session 预热打 58 antibot 网关 | 阶段三 | ✅ 通过网关（但每 IP 仅放行第 1 页） |
| `test_uc.py` | undetected-chromedriver（无头） | 阶段二 | ❌ Canvas/WebGL 指纹 + IP 即时拉黑 |
| `test_selenium_stealth.py` | selenium-stealth 插件（无头） | 阶段二 | ❌ 无头特征仍被识别 |
| `test_selenium_headed.py` | selenium-stealth（有头）+ 首页预热 + 人工过验证 | 阶段二→三 | ⚠️ 人工过验证后可取，但无法自动化、吞吐极低 |
| `test_parse_houses.py` | 列表页卡片解析探针（lxml） | 解析 | ✅ 验证 XPath 可抽出结构化字段（早期版，含重复 bug） |

> 这些脚本各自独立运行，依赖见各文件头部（selenium / undetected-chromedriver /
> selenium-stealth / curl_cffi / lxml）。它们不进入主采集链路——主链路见 `../fetcher.py`
> （阶段三）与 `../stealth.py`（持久化 profile）。

⚠️ 合规：仅用于反爬可行性论证，采集公开非 PII 数据；数据源锁定安居客（已知悉并接受其
TOS/反爬风险，见 docs/product/01/数据现状摸底.md §3.3）。
