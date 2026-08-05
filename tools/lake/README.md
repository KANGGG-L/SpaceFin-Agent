# tools/lake —— Doris + MinIO 湖仓接入

L0 数据底座：把 MySQL 业务库/房产库 + `data_lake/housing` Parquet 数据湖接入
Doris 分层仓（ODS / DWD / DWS / ADS），并在 MinIO 上建立对象存储底座。

## 运行

复用 `tools/orchestrator/.venv`（唯一 Python 环境）：

```bash
cd tools/lake
../orchestrator/.venv/bin/python sync.py                 # 全量：建表+导入+分层+湖上传+对账
../orchestrator/.venv/bin/python sync.py --recreate      # 表结构变更后强制重建表再同步
../orchestrator/.venv/bin/python sync.py --skip-minio    # 湖文件不变时跳过上传
../orchestrator/.venv/bin/python sync.py --verify-only   # 只做对账验证
```

可重复执行：所有表先 TRUNCATE 再导入/加工，结果与执行次数无关。
对账不过（MISMATCH/CHECK）时退出码非 0，可接入 CI/调度。

## 模块

| 文件 | 职责 |
|------|------|
| `config.py` | Doris / MinIO / MySQL 连接参数（读仓库根 `.env`，风格同 `tools/risk/config.py`） |
| `schema.py` | 分层表定义与 DDL 生成（ODS/DWD/DWS/ADS 共 24 张表） |
| `minio_sync.py` | Parquet 数据湖文件上传 MinIO（requests 手写 SigV4，无额外依赖） |
| `sync.py` | 主入口：建表 → MySQL→ODS Stream Load → 分层加工 → 湖上传 → 对账验证 |

详见组件文档 `docs/tech/components/doris-lake.md`。
