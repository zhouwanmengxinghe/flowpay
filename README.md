# FlowPay · 商户技术支持 Agent 平台

面向支付平台商户接入与故障排查的多租户支持系统：**Java 管业务与执行状态，Python Agent 负责检索和调查**，将文档问答、业务核查、多轮追问与人工工单衔接起来。

支持先运行不需要模型密钥的模拟演示，再接入真实模型和知识库。新机器推荐使用下面的一键部署入口；后面的原开发流程以 **Windows + PowerShell 7 + Docker Desktop（Linux 容器）**为准。

**一键部署：** 只需安装 Docker（含 Compose 插件、使用 Linux 容器），下载并解压本仓库后执行部署入口。Java 编译、Python 依赖安装、数据库迁移与服务健康检查均在容器内完成，不需要宿主机安装 JDK、Maven、Python、uv 或 PowerShell 7。首次需要联网下载镜像和依赖，耗时取决于网络；这不是离线安装包。真实调查仍需自己提供 API 密钥与配套数据包。

## 新机器快速启动

Windows：在仓库目录打开 PowerShell（系统自带版本即可），运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1
```

Linux / macOS：

```sh
sh deploy.sh
```

启动器自动创建独立的 `.env.deploy`，生成随机数据库口令、内部通信密钥及演示登录密码；重新执行不会覆盖这些配置和已有数据。等待显示 `FlowPay is ready` 后访问 **http://localhost:8080**（Linux/macOS 输出 `FlowPay ready`）。账号为 `merchant_a`、`merchant_b`、`support_a`、`support_b`，登录密码查看 `.env.deploy` 的 `SUPPORT_DEMO_PASSWORD`。

默认启动四个长期运行容器：业务数据库、Redis、mock Agent、Java 业务后台及工作台。仅工作台端口映射到宿主机回环地址；不公开数据库与 Agent 内部接口。模拟演示不调用模型，真实检索与工具调查需切换下一节的 real 模式。

已有 8080 服务时，先编辑 `.env.deploy` 的 `DEPLOY_PORT=28080` 再执行部署入口，地址相应变为 `http://localhost:28080`。该部署栈使用独立的数据卷，不复用原开发环境的账号、密码和会话数据；在同一个 localhost 下切换不同端口的工作台时，需要重新登录。

### 切换真实 Agent

首次模拟部署生成 `.env.deploy` 后，在该文件填写：

```dotenv
FLOWPAY_DATASET_ROOT='D:/datasets/flowpay-dataset-v1'
FLOWPAY_MODEL_PROVIDER=anthropic-compatible
FLOWPAY_MODEL_BASE_URL=https://api.deepseek.com/anthropic
FLOWPAY_MODEL_NAME=deepseek-flash
FLOWPAY_MODEL_API_KEY=填写你的密钥
FLOWPAY_MODEL_REQUEST_OPTIONS='{"max_tokens":2048,"thinking":{"type":"disabled"}}'
```

数据包可从仓库根目录下载 [flowpay-dataset-v1.zip](./flowpay-dataset-v1.zip)，解压后将 `FLOWPAY_DATASET_ROOT` 指向解压目录（其中应直接包含 `data/`、`postgres/` 等目录）。Linux/macOS 使用该机器的实际绝对路径。随后 Windows 执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -Mode real
```

Linux/macOS 执行 `sh deploy.sh real`。流程会校验数据、按需生成业务快照、迁移和导入知识数据库、下载嵌入模型并核对 revision，全部成功后才启动真实 LangChain Agent。数据库导入失败会回滚；已有就绪集合与嵌入模型会复用。初始化不会发送聊天模型请求，在网页提交调查后才调用模型。

### 部署网络、更新与停止

新部署入口不绑定特定代理。需要代理时，在 `.env.deploy` 中填写容器可访问的地址，例如 Docker Desktop：

```dotenv
DEPLOY_HTTP_PROXY=http://host.docker.internal:7890
DEPLOY_HTTPS_PROXY=http://host.docker.internal:7890
MAVEN_PROXY_HOST=host.docker.internal
MAVEN_PROXY_PORT=7890
```

这分别配置 Python 依赖/真实模型/嵌入模型下载，以及 Maven 依赖下载；拉取基础镜像仍使用 Docker 引擎自身的网络配置。容器中的 `127.0.0.1` 指容器自己；Linux 用户需填写容器能访问的代理主机地址，不能直接照抄宿主机回环地址。默认配置适用于可直连网络，脚本不会在代理失败后自动切换连接方式。

更新代码后，重新执行相同部署命令，Docker 按变化重新构建并替换服务。部署栈运行镜像中的源码，不依赖宿主机源码挂载。停止并保留数据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -Stop
```

Linux/macOS 执行 `sh deploy.sh stop`。不要删除 `.env.deploy` 或数据卷；重新生成密码不会自动更新已有数据库与账号。启动出错时可查看 `docker compose --env-file .env.deploy -f compose.deploy.yaml logs --tail 100`，修复后重试。该入口用于本机部署；公网提供服务还需配置域名、TLS 和实际账号策略。

---

## 项目能力

- **证据驱动调查**：LangChain Agent 结合知识检索、文档读取、受控 SQL 和日志工具，核对支付、事件与 Webhook 投递等模拟业务事实。
- **多租户业务后台**：商户/技术支持角色、账户和环境授权、成员管理、配额调整、会话、工单状态流转与审计。
- **可靠执行**：异步持久任务、幂等受理、执行批次、全局槽位、租约恢复、取消及迟到结果隔离。
- **中间件治理**：Redis 检索缓存与热点重建、Lua 固定窗口限流；RabbitMQ 事务性 Outbox 和数据库轮询兜底。
- **可复现 RAG 优化**：Dense + BM25 + RRF 混合召回、文档聚合排序、稳定证据标签及离线候选快照重放。

FlowPay 是模拟支付业务场景；本项目不执行真实收单、清结算或资金交易。

## 架构与技术栈

```text
浏览器 / 业务工作台
       │
       ├── Spring Boot 业务 API（单实例，或 Nginx + 双实例）
       │      ├── PostgreSQL：身份、会话、任务、工单、审计、Outbox
       │      ├── Redis：流量限制与配额保护
       │      └── RabbitMQ：可选的任务唤醒通知
       │
       └── Python FastAPI / LangChain Agent
              ├── Redis：可选检索缓存
              ├── PostgreSQL / pgvector：知识库
              ├── Ollama / Qwen3 embedding：本地向量化
              ├── SQLite / 日志快照：受控只读模拟业务查询
              └── 外部模型 API：OpenAI 或 Anthropic 兼容协议
```

| 层次 | 技术 |
|---|---|
| 业务服务 | Java 21、Spring Boot 3.5、Maven、Spring JDBC、Flyway |
| Agent 服务 | Python 3.12、FastAPI、Pydantic、LangChain、StructuredTool |
| 检索 | PostgreSQL 17 / pgvector、Ollama、Qwen3-Embedding 0.6B、BM25、RRF |
| 中间件与部署 | Redis 7.4、RabbitMQ、Docker Compose、Nginx |
| 界面 | Java 提供的 HTML/JavaScript 业务工作台；原 React/TypeScript/Vite 教学前端 |
| 验证 | pytest、Vitest、故障注入脚本、JSON 实验记录、Prometheus 格式指标 |

## 1. 下载到本地

安装 Git，打开 PowerShell：

```powershell
git clone https://github.com/zhouwanmengxinghe/flowpay.git
cd flowpay
```

后续命令均在这个仓库根目录运行。更新已有副本使用 `git pull`；如果是历史重写前克隆的旧副本，建议另外重新克隆，并先保存自己的本地改动。

## 2. 准备开发环境（原开发流程）

以下为原开发启动方式需要的工具；使用前面的一键部署入口可跳过本节：

| 工具 | 用途 |
|---|---|
| Docker Desktop，启用 Linux 容器 | 数据库、Redis、Agent、嵌入服务及可选 Java 容器 |
| Python 3.12 或 3.13 | 启动、准备和验证脚本；无需先装 Python 项目依赖 |
| uv | 根据锁文件准备 Agent 的 Linux 依赖 |
| JDK 21、Maven | 构建 Java 业务服务；日常开发模式也在宿主机运行 Java |
| PowerShell 7（`pwsh`） | 运行仓库的准备和构建脚本 |

检查是否可用：

```powershell
docker version
docker compose version
python --version
uv --version
java -version
mvn -version
pwsh --version
```

**网络配置：** 当前 `prepare-runtime.ps1` 和 `build.ps1` 默认使用本机 HTTP/mixed 代理 `127.0.0.1:7890`。使用这些脚本前确认代理已启动；若使用其他地址，需要调整准备脚本中的代理设置，Java 构建也可用 `build.ps1 -Proxy http://你的代理地址:端口`。脚本不会自动绕过代理直连。

在当前终端使用 7890 代理可设置：

```powershell
$env:HTTP_PROXY='http://127.0.0.1:7890'
$env:HTTPS_PROXY='http://127.0.0.1:7890'
$env:NO_PROXY='localhost,127.0.0.1,::1'
```

Docker 拉镜像使用 Docker Desktop 自身的网络/代理配置；上面的终端变量不代表 Docker 引擎已经使用同一个代理。首次下载镜像、依赖和嵌入模型可能需要较长时间。

## 3. 首次构建与模拟演示：不需要 API 密钥

推荐先用 mock 模式走通登录、调查、取消、工单和权限流程。mock 使用模拟 Agent，不调用聊天模型，也不需要外置数据包、知识库或 Ollama。

### 3.1 创建业务环境配置

首次运行下面的代码生成 `.env.support`；已有文件不会被覆盖：

```powershell
if (-not (Test-Path .env.support)) {
    $agentKey = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    @"
SUPPORT_DB_PASSWORD=support-local-only
SUPPORT_AGENT_KEY=$agentKey
SUPPORT_AGENT_MODE=mock
SUPPORT_AGENT_ENGINE=langchain
SUPPORT_DEMO=true
SUPPORT_DEMO_PASSWORD=FlowPay-Demo-2026!
SUPPORT_ORIGIN=http://localhost:8080
SUPPORT_RABBIT_USER=support
SUPPORT_RABBIT_PASSWORD=flowpay-rabbit-local-only
"@ | Set-Content -Encoding utf8 .env.support
}
```

这些口令用于本机演示，不用于公网部署。`SUPPORT_AGENT_KEY` 是 Java 与 Python 之间的内部共享密钥，**不是模型 API Key**。RabbitMQ 口令在此一并准备，避免 Compose 解析可选 MQ 服务时缺少变量。

### 3.2 拉取基础镜像，构建项目镜像和 Java 包

```powershell
docker pull pgvector/pgvector:0.8.2-pg17
docker pull redis:7.4-alpine

docker build -f infra/backend/Dockerfile -t flowpay-teaching-backend .
pwsh -File scripts/business/prepare-runtime.ps1
pwsh -File scripts/business/build.ps1
```

三个构建步骤的作用：

1. `docker build` 创建 Python 基础运行镜像。
2. `prepare-runtime.ps1` 按 `backend/uv.lock` 准备 Linux 依赖，创建 `flowpay-support-agent` 镜像。
3. `build.ps1` 使用 Maven 生成 `business/target/support-business-0.1.0.jar`。

本仓库目前没有可直接 `docker pull` 的公开 FlowPay 成品镜像，**自定义镜像需要本地构建**。镜像与容器不同：镜像是运行模板，容器才是实际启动的进程。

### 3.3 启动日常业务工作台

```powershell
python scripts/business/start.py --skip-build --mode mock
```

启动器使用已有镜像，启动业务 PostgreSQL、流量 Redis 和 Python Agent，并在宿主机运行 Java。它不会自动下载缺失镜像，也不会自动初始化真实知识库。

等待输出 `Business workbench ready` 后，访问 **http://localhost:8080**。

### 3.4 登录账号

默认演示密码为 **`FlowPay-Demo-2026!`**，实际取 `.env.support` 中的 `SUPPORT_DEMO_PASSWORD`。

| 账号 | 角色和默认范围 |
|---|---|
| `merchant_a` | 商户 A 管理员；调查、建单、管理本商户成员与查看用量 |
| `merchant_b` | 商户 B 管理员；用于验证商户隔离 |
| `support_a` | 技术支持；默认授权商户 A，并有支持授权管理和观测权限 |
| `support_b` | 普通技术支持；默认授权商户 A，可接单处理 |

建议体验：`merchant_a` 创建调查 → 转工单 → `support_a` 接单处理并提交解决结果 → `merchant_a` 确认关闭。

演示账号只在首次初始化时创建。已有数据库中的密码不会因为修改环境文件自动更新。

## 4. 配置 API，切换真实 LangChain 调查

真实模式还需要：**模型密钥、单独提供的 FlowPay 数据包、已导入的 pgvector 知识集合，以及本地 Ollama 嵌入模型**。

### 4.1 数据包准备

数据包不随本仓库分发，目前仓库没有提供公开的数据包下载地址。请使用配套课程/项目的数据包；没有数据包时可以运行前面的 mock 演示，但不能仅靠克隆仓库启动完整真实调查。

解压后，数据集根目录应包含：

```text
flowpay-dataset-v1/
├── data/
│   └── knowledge/
├── benchmark/
│   └── design/
└── postgres/
    └── flowpay_knowledge.dump
```

首次导入知识库需要 `postgres/flowpay_knowledge.dump`。业务启动器挂载数据时，请使用**解压后的绝对目录**，不要把 `.zip` 或 `.tar.gz` 文件直接填给业务容器。

### 4.2 创建模型配置文件

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑根目录 `.env`。下面使用本项目已验收过的 DeepSeek Anthropic 兼容配置；模型名应按你的账户实际可用型号调整：

```dotenv
FLOWPAY_DATASET_ROOT='D:/datasets/flowpay-dataset-v1'

FLOWPAY_MODEL_PROVIDER='anthropic-compatible'
FLOWPAY_MODEL_BASE_URL='https://api.deepseek.com/anthropic'
FLOWPAY_MODEL_NAME='deepseek-flash'
FLOWPAY_MODEL_API_KEY='填写你的真实模型密钥'
FLOWPAY_MODEL_REQUEST_OPTIONS='{"max_tokens":2048,"thinking":{"type":"disabled"}}'

OLLAMA_API_KEY=ollama
FLOWPAY_RETRIEVAL_MODE=hybrid-document
```

- `FLOWPAY_MODEL_API_KEY`：聊天模型供应商签发的密钥，自己填写，不提交到 Git。
- `FLOWPAY_MODEL_BASE_URL`：供应商基础地址，SDK 会追加接口路径，不要填写完整的 `/v1/messages` 或 `/chat/completions`。
- `OLLAMA_API_KEY=ollama`：本地嵌入服务占位配置，不是付费聊天模型的密钥。
- `hybrid-document`：Dense + BM25 加权融合，并且每篇来源文档只保留一个代表片段。

如果使用 OpenAI 兼容接口，将模型部分改为：

```dotenv
FLOWPAY_MODEL_PROVIDER='openai-compatible'
FLOWPAY_MODEL_BASE_URL='https://api.deepseek.com'
FLOWPAY_MODEL_NAME='填写该接口支持的模型名称'
FLOWPAY_MODEL_API_KEY='填写你的真实模型密钥'
FLOWPAY_MODEL_REQUEST_OPTIONS='{"max_tokens":2048}'
```

其他 OpenAI 兼容供应商按其文档填写基础地址，例如某些服务需要以 `/v1` 结尾。协议必须与供应商接口匹配。参考：[项目环境变量模板](.env.example)、[DeepSeek Anthropic 兼容接口文档](https://api-docs.deepseek.com/guides/anthropic_api/)。

### 4.3 首次初始化知识库与 Ollama

确认 `.env` 的数据集目录和模型密钥已经填写，然后执行：

```powershell
python scripts/start.py
```

该脚本会检查数据集、启动知识 PostgreSQL、执行迁移与角色授权、恢复知识向量、启动 Ollama、下载并校验 `qwen3-embedding:0.6b`，最后启动原教学前后端。初始化本身不发起聊天模型问答。

数据包中的文档向量不能替代 Ollama：每次新查询仍需要生成问题向量。模型 revision 与知识集合不匹配时脚本会停止，不能忽略该错误继续检索。

如果最终使用上面的 Anthropic 配置，后续只使用业务版 LangChain 入口。原教学 s1—s4 使用 OpenAI 兼容适配器；初始化结束后可停掉它们的界面与 HTTP 服务，保留数据库和 Ollama：

```powershell
docker compose stop backend frontend
```

### 4.4 启动真实业务模式

如果日常 Java 尚未运行：

```powershell
python scripts/business/start.py --skip-build --mode real --engine langchain
```

如果前面的 mock 工作台已经在运行，只更新 Agent：

```powershell
python scripts/business/start.py --skip-build --mode real --engine langchain --agent-only
```

访问 **http://localhost:8080**，新建调查。发送问题才会使用模型 API 额度；真实调查会将问题和相关的模拟业务/文档片段发送给供应商。

当前真实模式 Compose 将容器外部 HTTP/HTTPS 请求指向 `host.docker.internal:7890`，数据库、Redis 和 Ollama 走内部网络。请确保该代理可用；如果换代理，需要相应调整 `compose.business.real.yaml`。

可尝试：

- “Production 被暂停后，已有的 live key 是否仍能正常调用？”
- “支付成功但商户没有收到 Webhook，你需要检查哪些证据？”
- 提供当前账户下的 `payment_id` 或 `request_id`，继续进行具体调查。

不提供必要业务编号时，Agent 可能先查询授权范围内的信息，再要求补充定位条件。

## 5. 可选：Java 也放进 Docker

完成第 3 节的镜像和 JAR 构建后，可以使用 `container` profile；这样宿主机不再运行 Java 进程，但当前构建流程仍需要 JDK/Maven。

如果日常 Java 已运行，先停止它，避免占用 8080：

```powershell
python scripts/business/stop.py
```

启动完整业务容器演示：

```powershell
docker compose --env-file .env.support -f compose.business.yaml --profile container up -d --build support-db support-redis support-agent support-api
```

Java 镜像使用 `eclipse-temurin:21-jre`，首次构建需要下载。服务启动后可检查 `http://localhost:8080/api/health`，再打开工作台。

真实模式仍需先完成第 4 节的数据、知识集合和 Ollama 初始化，然后使用：

```powershell
docker compose --env-file .env --env-file .env.support -f compose.business.yaml -f compose.business.real.yaml --profile container up -d --build support-db support-redis support-agent support-api
```

不要同时运行宿主机 Java 和 `support-api` 容器；二者都会使用 8080，并可能争用单实例调度器。

### 镜像与容器分别做什么

| 镜像/服务 | 作用 | 是否需要自己构建 |
|---|---|---|
| `pgvector/pgvector:0.8.2-pg17` | 业务数据库；真实检索还会启动单独的知识数据库实例 | 拉取现成镜像 |
| `redis:7.4-alpine` | 流量限制，以及可选检索缓存 | 拉取现成镜像 |
| `flowpay-teaching-backend` | Python 基础环境、知识初始化与教学服务 | 本地构建 |
| `flowpay-support-agent` | 业务版 Python/LangChain 执行服务 | 本地构建 |
| `ollama/ollama:0.12.3` | 本地嵌入服务，真实模式需要 | 拉取镜像，另下载嵌入模型 |
| 业务 `support-api` 镜像 | Java JAR 与业务工作台，`container` profile 使用 | Compose 本地构建 |
| RabbitMQ / Nginx | 双实例升级验收的通知与统一入口 | 仅对应可选模式启动 |

Docker Desktop 的 Images 页面列出镜像，不表示这些镜像都正在运行。用 Containers 页面或 `docker compose ps` 查看实际服务。初始化服务完成后退出属于正常行为。

## 6. 入口、更新与停止

### 常用地址

| 地址/端口 | 内容 |
|---|---|
| `http://localhost:8080` | 日常商户/技术支持工作台 |
| `http://127.0.0.1:8010/health` | Python Agent 健康与运行模式 |
| `http://127.0.0.1:5173` / `:8000` | 原教学前端 / Python 教学 API |
| `http://127.0.0.1:28080` | 隔离的双 Java + MQ mock 验收栈，非日常真实模型入口 |
| `127.0.0.1:15433` / `:15432` | 业务 PostgreSQL / 知识 PostgreSQL |
| `127.0.0.1:16379` | 日常流量 Redis |

Ollama 默认只在 Docker 内提供 `11434`，不要求宿主机也监听这个端口。

### 修改后怎么更新

| 修改内容 | 更新方式 |
|---|---|
| Java 或业务工作台 HTML/JS | 先停止宿主机 Java，再运行 `build.ps1`，然后重新启动；容器模式还需重建 `support-api` |
| Python 源码 | 源码由卷挂载，重启/重建 `support-agent` 加载；无需每次重新装依赖 |
| Python 依赖或锁文件 | 重新运行 `prepare-runtime.ps1`，然后重建 Agent 容器 |
| `.env` 中 API/模型配置 | 用业务启动器更新 Agent，或 Compose 重新创建服务；仅 `docker restart` 不会加载新环境变量 |
| 数据库结构 | 由增量 Flyway 迁移管理，不编辑已经应用的历史迁移 |

Java 开发模式更新示例：

```powershell
python scripts/business/stop.py
pwsh -File scripts/business/build.ps1 -Offline
python scripts/business/start.py --skip-build --mode real --engine langchain
```

`-Offline` 仅适用于依赖已下载完整的情况。切换 mock/real、s4/LangChain、供应商或检索模式后，新建调查，避免旧检查点与配置混用。

### 停止并保留数据

```powershell
python scripts/business/stop.py
docker compose --env-file .env.support -f compose.business.yaml --profile container down
docker compose stop
```

第一条停止启动器管理的宿主机 Java，第二条停止业务容器，第三条停止教学/知识/嵌入栈。日常停止不要添加 `-v`：命名卷中保存着数据库和嵌入模型。

## 7. 测试与量化结果

| 实验 | 结果 | 口径 |
|---|---|---|
| 文档聚合检索 | 文档级 Hit@5：66.07% → **83.93%** | 复用 56 题迭代，37 → 47 题；不是回答准确率 |
| 真实检索缓存 | 嵌入调用 30 → 6，减少 **80%** | 6 条查询各重复 5 次 |
| 真实热点并发 | 同键 20 并发，嵌入调用 **20 → 1** | 本地 Ollama/pgvector；耗时约 18.19 s → 3.06 s |
| 分布式与 MQ 故障验证 | **10/10** 场景通过 | 真实中间件 + mock Agent |
| 有限真实模型验收 | **5/5** 运行完成 | 11 次模型、14 次工具调用 |

以上为本地实验结果；学习文档与历史实验记录不随代码仓库分发。可在完成真实模式的数据与服务初始化后，重新采集候选并离线重放检索实验；采集调用本地嵌入服务，两步都不调用聊天模型：

```powershell
docker compose --env-file .env --env-file .env.support -f compose.business.yaml -f compose.business.real.yaml run --rm --no-deps -v "${PWD}/scripts:/app/scripts:ro" -v "${PWD}/var:/app/var" -e PYTHONPATH=/opt/business-deps:/app/backend/src:/app support-agent python scripts/business/experiment_document_ranking.py collect --snapshot /app/var/rag-document-candidates.json
docker compose --env-file .env --env-file .env.support -f compose.business.yaml -f compose.business.real.yaml run --rm --no-deps -v "${PWD}/scripts:/app/scripts:ro" -v "${PWD}/var:/app/var" -e PYTHONPATH=/opt/business-deps:/app/backend/src:/app support-agent python scripts/business/experiment_document_ranking.py evaluate --snapshot /app/var/rag-document-candidates.json --output /app/var/replay-document-ranking.json
```

双实例 + MQ 隔离演示，仍使用 mock：

```powershell
uv run --project backend python scripts/business/upgrade_stack.py start
# 工作台：http://127.0.0.1:28080
```

这条命令需要前面的业务镜像和 JAR 已准备好，并会构建/拉取额外镜像。清理命令 `upgrade_stack.py stop` 会删除**该隔离验收栈**的容器与数据卷，不用于停止日常业务数据。

## 8. 常见问题

**`Image ... not found` 或 `pull access denied`**：`flowpay-*` 是本地构建镜像，先完成第 3.2 节；启动器的 `--pull never` 不会替你从网上找这些镜像。

**端口已占用**：用 `Get-NetTCPConnection -State Listen -LocalPort 8080` 等命令确认占用进程。不要同时开启宿主机 Java 与 Java 容器；修改端口时需同步入口和 `SUPPORT_ORIGIN`。

**模型 401/403**：检查实际 `.env` 的密钥、供应商基础地址、协议和模型权限。内部 `SUPPORT_AGENT_KEY` 不能代替模型密钥。

**模型 429 或调用失败后想继续**：等待供应商恢复，在原会话继续；失败不应覆盖上一次成功检查点。不要通过无限重试绕过额度。

**Ollama 模型 revision 不匹配**：知识向量绑定了嵌入模型版本。不要随意更换模型标签或忽略校验；更换模型需要配套重建知识集合。

**只看到 mock 回答**：查看工作台的 Agent 模式，按第 4.4 节切换真实模式。28080 的隔离验收栈不会因为修改 `.env` 自动变成真实模式。

**改了演示密码却无法登录**：初始化不会重写已有用户密码；环境变量只影响首次创建。保留数据库时应通过实际账号管理或受控维护更新。

**API 调用卡住或电脑负载高**：先查看 Agent、Ollama 的日志和资源限制。模型推理、问题向量化与 Java 接口读取是不同链路，不能用普通 GET 的吞吐代表真实 Agent 问答能力。

## 代码目录

```text
business/                    Java 业务后台、工作台、Flyway 迁移
backend/src/flowpay/business/ Python 内部业务执行接口
backend/src/flowpay/workflows/ LangChain 业务编排与原 s1—s4 教学阶段
backend/src/flowpay/core/     检索、工具、数据源、缓存与执行基础能力
infra/                       Dockerfile、PostgreSQL 初始化、Nginx
scripts/business/            准备、启停、故障验证与评测脚本

```
