# FlowPay 常用开发命令。在仓库根目录执行：make <目标>
#
# 三种 Python 调用，按"虚拟环境存不存在"分：
#   $(PYTHON)  系统 python3，只给 setup / doctor 用，它们要在 venv 建好之前跑
#   $(VENV)    backend/.venv/bin/python，其余全部用它
#   uv         只出现在 sync 里，负责建出上面那个 venv
# 曾经混用 `uv run pytest` 导致仓库根不在 sys.path，scripts 包整包导不进来。

.DEFAULT_GOAL := help

PYTHON ?= python3
VENV   := backend/.venv/bin/python
CONFIG := backend/configs/local.toml

STAGE      ?= s1
STAGES     ?= s1,s2,s3,s4
PER_TYPE   ?= 2
SETUP_ARGS ?=

# Python 不自动读 .env。这里在同一条 shell 里加载它，再派生两类变量：
# 数据集路径，以及三个角色的连接串。口令固定，与 compose.yaml 里那份同一个默认值；
# 容器那份走 postgres:5432，这里是宿主机那份，走发布出来的端口。
DB_HOST := 127.0.0.1:15432/flowpay_knowledge
define with-env
	set -a; [ -f .env ] && . ./.env; \
	[ -n "$$FLOWPAY_DATASET_ROOT" ] && FLOWPAY_DATASET_PATH="$$FLOWPAY_DATASET_ROOT/data"; \
	FLOWPAY_DB_PASSWORD="$${FLOWPAY_DB_PASSWORD:-flowpay_local}"; \
	FLOWPAY_KNOWLEDGE_ADMIN_DSN="postgresql://flowpay_admin:$$FLOWPAY_DB_PASSWORD@$(DB_HOST)"; \
	FLOWPAY_KNOWLEDGE_DSN="postgresql://flowpay_knowledge_reader:$$FLOWPAY_DB_PASSWORD@$(DB_HOST)"; \
	FLOWPAY_KNOWLEDGE_BUILDER_DSN="postgresql://flowpay_knowledge_builder:$$FLOWPAY_DB_PASSWORD@$(DB_HOST)"; set +a;
endef

# 目标全部是 .PHONY：没有一个是在生成同名文件。
.PHONY: $(shell sed -n 's/^\([a-zA-Z0-9_-]*\):.*##.*/\1/p' $(MAKEFILE_LIST))

help: ## 列出可用目标
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@ / {printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo

##@ 教程路径

up: ## 起全栈：校验 .env、备好数据库、知识库与嵌入模型，再起全部容器
	$(PYTHON) scripts/start.py $(if $(BUILD),--build)

down: ## 停止全栈容器，数据卷保留（知识库与嵌入模型不会丢）
	docker compose down

logs: ## 跟踪后端与前端日志
	docker compose logs -f backend frontend

##@ 宿主机直跑

# 要改 core/ 或者需要断点时才走这条；只用容器的话上面四个就够。
setup: ## 一键准备本地配置、依赖、数据库与知识集合（含 uv sync）
	$(PYTHON) scripts/setup.py $(SETUP_ARGS)

doctor: ## 只读检查基础环境，不安装依赖也不调用聊天模型
	$(PYTHON) scripts/doctor.py $(SETUP_ARGS)

db-up: ## 只起 PostgreSQL（make up 会自己带上它）
	docker compose up -d --wait postgres

db-setup: ## 执行知识库结构迁移与角色授权（setup 已包含，升级旧库时单独跑）
	@$(with-env) $(VENV) scripts/database.py setup

run: ## 课程后端（STAGE 可选 s1—s4）
	@$(with-env) backend/.venv/bin/flowpay --config $(CONFIG) --stage $(STAGE)

frontend: ## 前端开发服务器
	npm --prefix frontend run dev

##@ 开发

test: ## 只跑 backend pytest（未配置数据集时相关用例自动跳过）
	@$(with-env) $(VENV) -m pytest backend/tests -q

quality: ## 代码质量门禁：ruff、basedpyright、pytest、前端构建
	@$(with-env) $(VENV) scripts/check_quality.py

benchmark: ## 数据集 300 题分层对照（PER_TYPE=0 表示全量）
	@$(with-env) $(VENV) scripts/benchmark/run.py --stages $(STAGES) --per-type $(PER_TYPE)

blueprints: ## 把 300 题渲染成可读 Markdown，落在数据集的 benchmark/readable/
	@$(with-env) $(PYTHON) scripts/benchmark/render.py

demo: ## 起确定性契约后端，只验协议不跑课程阶段（需先 make setup）
	$(VENV) scripts/demo.py --config $(CONFIG)

smoke: ## 对着已在跑的后端验 HTTP/SSE：断线回放、取消与继续
	@$(with-env) $(VENV) scripts/smoke_runtime.py
