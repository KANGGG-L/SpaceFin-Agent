# SpaceFin-Agent · VPS 部署指南（生产级）

> 面向：在 VPS（当前 `20.70.128.18`，Ubuntu 24.04）上以正常应用标准部署，使面试官通过 URL 访问。
> 口径：本项目为校招作品集，但按**最终交付版本**标准部署（见根 README「项目性质与范围说明」）。

---

## 已落地（本仓库提交内容）

### 1. 公网暴露收敛（compose 端口绑定 `127.0.0.1`）
下列服务的宿主机端口已从 `0.0.0.0` 改为仅绑 `127.0.0.1`，**不再对公网直接可达**，仅本机 / 反代可访问：

| 服务 | 文件 | 端口 |
|------|------|------|
| MySQL | `docker-compose.yml` | 3306 |
| Kafka | `deploy/kafka-flink/docker-compose.yml` | 9092 / 9093 |
| Flink | `deploy/kafka-flink/docker-compose.yml` | 8081 / 6123 |
| Airflow webserver | `airflow/docker-compose.airflow.yml` | 8080（见下方「已知问题」） |
| Crawler master/standby | `tools/orchestrator/docker-compose.yml` | 5100 / 5101 |

> 注：Doris / MinIO 未发布宿主机端口（仅容器网络内部），无需处理。

### 2. 反向代理（Caddy，IP-only 测试版）
- 已安装 Caddy（`apt install caddy`），`systemctl enable --now caddy` 自启。
- 配置 `/etc/caddy/Caddyfile`：`:80` 反代到 `127.0.0.1:8500`，加安全头，探活 `/healthz`。
- 前端 `tools/frontend/app.py` 监听 `127.0.0.1:8500`，由 Caddy 对外暴露。
- 当前访问：`http://<公网IP>/`（明文 HTTP）。

### 3. 前端进程
- `systemd --user` 单元 `spacefin-frontend.service`，`Restart=always` + `enable-linger` 常驻。

---

## 访问方式（当前）

- **URL**：`http://20.70.128.18/`（HTTP 明文，IP-only 测试版）
- **账号**：`admin/admin123`、`risk/risk123`、`da/da123`、`postloan/post123`（dev-only 弱口令，见待办）
- 演示路径：驾驶舱 → P5 空间画像（地图点选）→ P7 AVM → P9 合规审计（导出脱敏）→ 切换 `postloan` 看 403

---

## 下一步：域名 + TLS（待执行）

当前为 IP-only 明文 HTTP。生产过程中应升级为 **HTTPS**。Caddy 原生支持自动 ACME 签发，改动极小：

1. **准备域名**：将域名（如 `spacefin.example.com`）的 A 记录指向本机公网 IP。
2. **放开 80/443**：云防火墙放行 80（ACME http-01 挑战）与 443。
3. **改 Caddyfile**：将首行 `:80` 改为域名，并显式监听 443：
   ```
   spacefin.example.com {
       reverse_proxy 127.0.0.1:8500
       header { -Server X-Content-Type-Options "nosniff" X-Frame-Options "DENY" Referrer-Policy "no-referrer" }
   }
   ```
   Caddy 会自动申请并续期证书、强制跳转 HTTPS，无需其他改动。
4. **校验**：`sudo caddy validate --config /etc/caddy/Caddyfile` → `sudo systemctl reload caddy`。
5. **（可选）basicauth**：公网长期暴露时，在 Caddyfile 加 `basicauth` 保护，避免弱口令直暴露于公网。

---

## 待办 / 已知问题

- **[安全] 强口令**：`USERS` 写死在 `tools/frontend/app.py`（dev-only）。上域名/公网长期暴露前应改为环境变量 + 强口令，或加 Caddy `basicauth`。
- **[已知问题] Airflow 8080 仍绑 `0.0.0.0`**：`airflow-init` 一键初始化容器因预先存在的权限 bug（`/opt/airflow/logs/scheduler` Permission denied）失败，导致 webserver 容器未被 recreate，compose 端口改动未生效。与本次部署改动无关。临时处置：在云防火墙阻断 8080（Airflow 为运维工具，非面试面向上）。
- **[待办] Redis 6379**：不在本仓 compose 内定义，无法在 repo 层收敛；由云防火墙按需阻断。
- **[增强] 匿名探活**：`/healthz` 当前由前端返回 404（Caddy 探测块转给后端），建议前端加一个匿名 200 端点供存活探针。
- **[增强] CD 流水线**：`.github/workflows/deploy.yml` 目前仅发版说明，未接真实部署；后续可加 SSH 部署 job（tag `v*` → 拉代码 → 重启服务 → 探活）。

---

## 重启 / 运维速查

```bash
# 前端（user 级 systemd）
systemctl --user restart spacefin-frontend.service

# Caddy
sudo systemctl reload caddy

# 数据栈（按需）
docker compose up -d                                  # MySQL
cd deploy/kafka-flink && docker compose up -d         # Kafka/Flink
cd tools/orchestrator && docker compose up -d         # Crawler
cd airflow && docker compose -f docker-compose.airflow.yml up -d
```
