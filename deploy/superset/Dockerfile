# SpaceFin Agent · Superset 镜像（基于官方 4.1.2，补 Doris 连接所需的 PyMySQL 驱动）
# Doris FE 兼容 MySQL 协议，Superset 用 mysql+pymysql 驱动接入；官方镜像未预装 PyMySQL。
FROM apache/superset:4.1.2

USER root
RUN pip install --no-cache-dir pymysql
USER superset
