# SpaceFin-Agent · VPS 部署指南

> 环境：VPS（Ubuntu 24.04），公网 IP `20.70.128.18`。
> 云防火墙当前仅放行 **22（SSH）** 与 **80（Caddy）**；其余端口（含 3306/9092/8080/6379 等）对公网不可达。
> 口径：本项目为校招作品集，按最终交付版本标准部署（见根 README「项目性质与范围说明」）。

---

## 架构

```
公网 → 云防火墙（仅 22/80）→ Caddy :80 → 127.0.0.1:8500（前端）
数据栈（MySQL/Kafka/Flink/Redis/Doris/MinIO/Airflow）仅绑 127.0.0.1 或容器网络内部
```

---

## 服务端口绑定

| 服务 | 文件 | 宿主机端口 | 绑定 |
|------|------|-----------|------|
| MySQL | `docker-compose.yml` | 3306 | `127.0.0.1` |
| Kafka | `deploy/kafka-flink/docker-compose.yml` | 9092 / 9093 | `127.0.0.1` |
| Flink | `deploy/kafka-flink/docker-compose.yml` | 8081 / 6123 | `127.0.0.1` |
| Crawler master/standby | `tools/orchestrator/docker-compose.yml` | 5100 / 5101 | `127.0.0.1` |
| Airflow webserver | 宿主 systemd 直装（非 compose） | 8080 | `0.0.0.0`（云防火墙已挡） |
| Redis | 外部/历史启动（不在本仓 compose） | 6379 | `0.0.0.0`（云防火墙已挡） |
| Doris / MinIO | — | 无宿主机发布端口 | 容器网络内部 |

> 说明：Airflow 的 `airflow/docker-compose.airflow.yml` 为备选形态（注释明确"推荐宿主 systemd 直装"）；实际运行的是宿主 systemd 安装的 Airflow，故 compose 端口绑定不作用于该进程。Redis 容器不在本仓任一 compose 内定义。以上两者虽绑 `0.0.0.0`，但云防火墙仅放行 22/80，公网不可达。

---

## 反向代理（Caddy）

- 安装：`apt install caddy`；自启：`systemctl enable --now caddy`。
- 配置：`/etc/caddy/Caddyfile`，`:80` 反代 `127.0.0.1:8500`，含安全头与 `/healthz` 探活。
- 前端 `tools/frontend/app.py` 监听 `127.0.0.1:8500`，由 Caddy 对外暴露。
- 当前访问：`http://20.70.128.18/`（明文 HTTP）。

---

## 前端进程

- `systemd --user` 单元 `spacefin-frontend.service`，`Restart=always` + `enable-linger` 常驻。

---

## 访问方式

- **URL**：`http://20.70.128.18/`
- **账号**：密码由 `tools/frontend/app.py` 在启动时生成并持久化至 `output/frontend/credentials.json`（非 dev 模式）；`SF_DEV_MODE=1` 时回退内置弱口令（`admin/admin123` 等）。环境变量 `SF_PWD_<ROLE>` 可固定口令。
- **演示路径**：驾驶舱 → P5 空间画像（地图点选）→ P7 AVM → P9 合规审计（导出脱敏）→ 切换 `postloan` 看 403。

---

## 下一步：域名 + TLS

当前为 IP-only 明文 HTTP。升级 HTTPS（Caddy 自动 ACME，改动极小）：

1. 域名 A 记录指向本机公网 IP。
2. 云防火墙放行 80（ACME http-01 挑战）与 443。
3. `Caddyfile` 首行 `:80` 改为域名：
   ```
   spacefin.example.com {
       reverse_proxy 127.0.0.1:8500
       header { -Server X-Content-Type-Options "nosniff" X-Frame-Options "DENY" Referrer-Policy "no-referrer" }
   }
   ```
   Caddy 自动申请并续期证书、强制跳转 HTTPS。
4. 校验：`sudo caddy validate --config /etc/caddy/Caddyfile` → `sudo systemctl reload caddy`。
5. 可选 `basicauth`：公网长期暴露时加 Caddy `basicauth` 保护。

---

## 运维速查

```bash
# 前端（user 级 systemd）
systemctl --user restart spacefin-frontend.service

# Caddy
sudo systemctl reload caddy

# 数据栈（按需）
docker compose up -d                                  # MySQL
cd deploy/kafka-flink && docker compose up -d         # Kafka/Flink
cd tools/orchestrator && docker compose up -d         # Crawler
```

---

## 备注

- CD 流水线：`.github/workflows/deploy.yml` 目前仅发版说明（tag `v*` 触发），未接真实服务器部署；如需自动部署，可加 SSH 部署 job（tag → 拉代码 → 重启服务 → 探活 `/healthz`）。
- 纵深防御（可选）：若希望即便云防火墙误配也不裸奔，可将宿主 Airflow 的 `web_server_host` 改为 `127.0.0.1`、将 Redis 改为绑 `127.0.0.1`；当前由云防火墙兜底。
