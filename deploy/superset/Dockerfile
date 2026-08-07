# SpaceFin Agent · Superset 镜像（基于官方 4.1.2，补 Doris 连接所需的 PyMySQL 驱动）
# Doris FE 兼容 MySQL 协议，Superset 用 mysql+pymysql 驱动接入；官方镜像未预装 PyMySQL。
FROM apache/superset:4.1.2

USER root
# PyMySQL：Doris 走 MySQL 协议接入必需。
RUN pip install --no-cache-dir pymysql
# psycopg2-binary：元数据库改 PostgreSQL 必需，否则 PG URI 启动报驱动缺失。
RUN pip install --no-cache-dir psycopg2-binary
USER superset
