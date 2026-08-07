# tools/ops —— 运维加固：容器恢复 + 资源管家

## 三级运行策略（15G/4 核主机的内存纪律）

| Tier | 定位 | 组件 | 约占用 | 何时停 |
|------|------|------|--------|--------|
| T0 恒驻 | 链路命脉 | MySQL / Redis / CDC / Consumer / Airflow(scheduler+webserver) / 前端驾驶舱 | ~2.5G | 不主动停，停机=业务中断 |
| T1 可降级 | 计算/队列 | Doris FE+BE / Minio / Kafka / Flink(JM+TM) / stream-producer | ~4.7G | 内存吃紧时一键 `stop t1` 释放 |
| T2 按需 | 任务型 | 爬虫、离线渲染等 | 不定 | 用完即弃，本脚本不管理 |

内存预算（参考）：T0+T1 常驻约 7~8G；主机总 15G，`available` 低于 2.5G 即为吃紧。

## 容器自动恢复

8 个容器 restart policy 已统一为 `unless-stopped`（宿主重启后 docker daemon 自动拉起，
除非显式 `docker stop`）：

```bash
docker update --restart unless-stopped \
  spacefin-mysql spacefin-redis spacefin-doris-fe spacefin-doris-be \
  spacefin-minio spacefin-kafka flink-jobmanager flink-taskmanager
```

注意：`docker update` 只改运行中容器的策略，新建容器要遵循 `docker-compose.yml` 里的定义。

## 脆弱服务托管 systemd（用户级）

nohup 裸跑的会话退出即丢进程，已发生前端掉线。现托管为用户级 systemd unit：

- `spacefin-frontend.service` —— 前端驾驶舱（端口 8500）
- `spacefin-stream-producer.service` —— 实时链路 producer（读 ods_cdc_log → Kafka）

unit 文件入库于 `deploy/systemd/`，安装/验证步骤见各文件顶部注释；
先 `sudo loginctl enable-linger $USER` 保证无登录会话也常驻。

## manage.sh 用法

```bash
./manage.sh status              # 容器 + systemd 服务 + 内存水位
./manage.sh stop t1             # 内存吃紧一键降级，释放约 4.7G
./manage.sh start t1            # 恢复计算/队列层
./manage.sh watch [seconds]     # 每 N 秒(默认30)监控内存，available<2.5G 时 stderr 告警
./manage.sh help
```
