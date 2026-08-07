"""tools/lake —— Doris + MinIO 湖仓接入（L0 数据底座）。

模块：
  config.py     连接参数（Doris / MinIO / MySQL）
  schema.py     分层表定义与 DDL 生成（ODS / DWD / DWS / ADS）
  minio_sync.py MinIO 对象上传（SigV4 签名，无额外依赖）
  sync.py       主入口：建表 → 导入 → 分层 → 湖上传 → 对账验证
"""
