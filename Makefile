# ==============================================================================
# SpaceFin Agent · 本地开发便捷入口
# ------------------------------------------------------------------------------
# 常用：make up / make sql / make seed-gen / make down
# ==============================================================================

# 加载本地 .env（不存在则忽略），并导出给子进程
-include .env
export

.PHONY: help up down destroy logs ps sql seed-gen health

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## 启动 MySQL 业务源库（首次会自动执行 schema + seed）
	docker compose up -d mysql

down: ## 停止服务（保留数据卷）
	docker compose down

destroy: ## 停止并删除数据卷（下次 up 将重新初始化 schema + seed）
	docker compose down -v

logs: ## 查看 MySQL 日志
	docker compose logs -f mysql

ps: ## 查看服务状态
	docker compose ps

sql: ## 进入 MySQL 交互终端（spacefin 库）
	docker compose exec mysql mysql -uroot -p"$(MYSQL_ROOT_PASSWORD)" spacefin

seed-gen: ## 重新生成合成 seed SQL（sql/init/02_seed.sql）
	python3 seed/generate_seed.py

health: ## 探活 MySQL
	docker compose exec mysql mysqladmin ping -h localhost -uroot -p"$(MYSQL_ROOT_PASSWORD)"
