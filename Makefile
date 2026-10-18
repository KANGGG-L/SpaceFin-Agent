# ==============================================================================
# SpaceFin Agent · 本地开发便捷入口
# ------------------------------------------------------------------------------
# 常用：make up / make sql / make seed-gen / make down
# ==============================================================================

# 加载本地 .env（不存在则忽略），并导出给子进程
-include .env
export

.PHONY: help up down destroy logs ps sql seed-gen health infra avm-diag persona-refresh pack

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

infra: ## 幂等创建 spacefin-net 网络与 spacefin-redis 容器（从零部署 orchestrator 前置）
	docker network create spacefin-net || true
	docker run -d --name spacefin-redis --network spacefin-net -p 6379:6379 redis:7 redis-server --appendonly yes || docker start spacefin-redis

avm-diag: ## 幂等生成 P7 归因产物体（rows.json + attribution.json）；模型重训或产物缺失时自动重建，否则跳过
	$(eval PYTHON := $(shell [ -x tools/orchestrator/.venv/bin/python ] && echo tools/orchestrator/.venv/bin/python || echo python3))
	$(PYTHON) tools/avm/gen_attribution.py --python $(PYTHON)

persona-refresh: ## 重新生成 P10 校准报告（output/persona/persona_report.json）；脚本依赖 scipy，取第一个带 scipy 的解释器
	$(eval PY := $(shell for p in $(SPACEFIN_PERSONA_PYTHON) tools/orchestrator/.venv/bin/python python3; do if [ -n "$$p" ] && { [ -x "$$p" ] || command -v "$$p" >/dev/null 2>&1; }; then "$$p" -c "import scipy" >/dev/null 2>&1 && { echo "$$p"; break; }; fi; done; :))
	$(PY) tools/persona/main.py

pack: ## 打包交付归档 spacefin-agent-clean.tar.gz（排除 .git / 密钥 / 产物 / 缓存）
	tar --exclude-vcs \
	    --exclude='./.env' \
	    --exclude='./.env.local' \
	    --exclude='./output' \
	    --exclude='./tools/anjuke_crawler/output' \
	    --exclude='./tools/orchestrator/.venv' \
	    --exclude='./node_modules' \
	    --exclude='__pycache__' \
	    --exclude='.pytest_cache' \
	    --exclude='.ruff_cache' \
	    --exclude='*.pyc' \
	    -czf /tmp/spacefin-agent-clean.tar.gz .
	@mv /tmp/spacefin-agent-clean.tar.gz spacefin-agent-clean.tar.gz
	@ls -lh spacefin-agent-clean.tar.gz
