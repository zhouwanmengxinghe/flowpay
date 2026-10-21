# FlowPay Agentic RAG 教学项目

基于 [KamaAgenticRAG](https://github.com/youngyangyang04/KamaAgenticRAG) 进行业务化扩展，保留原 s1—s4 教学实现，并新增 Java 多租户业务后台、持久任务协调、Redis/RabbitMQ 集成与 RAG 检索优化。原项目版权及 MIT 许可见 [LICENSE](#upstream-license)；完整项目说明和量化实验索引见[项目总览](docs/FLOWPAY_RESUME_PROJECT_DOSSIER.md)。

新增 Java 业务升级入口：见 [商户支持后台说明](business/README.md)。包含独立 PostgreSQL、Redis 限流、成员权限、持久任务、工单和 LangChain 调查；缓存、双 Java 实例、租约恢复和 RabbitMQ outbox 的实测结果见[分布式升级总结](docs/verification/distributed/SUMMARY.md)。业务启动器默认真实模式，自动验收显式使用 mock 且未调用模型。原教学入口继续使用本文流程。

围绕一家虚构支付公司 FlowPay 的客服场景，分阶段（s1—s4）手写 Agentic RAG：
从一次检索一次回答，到模型自己决定检索几轮、用哪个工具。

## 前置条件

| 需要什么 | 说明 |
| --- | --- |
| Docker Desktop | 数据库、嵌入服务、后端和前端都在容器里跑 |
| 聊天模型密钥 | 任何 OpenAI 兼容服务都行，默认用 DeepSeek |
| 教学数据包 | 单独下发的 `flowpay-dataset-v1.zip` 或 `.tar.gz`，不随仓库分发 |
| 约 8 GB 磁盘 | 镜像约 6.5 GB（其中 ollama 自己占 4.5 GB）、嵌入模型 640 MB、数据包解开后约 160 MB |

本机**不需要**预装 Python、Node 或 Ollama。嵌入模型由 `make up` 自动下载。

## 快速开始

```bash
cp .env.example .env     # 然后填下面两项
make up                  # 首次视网络而定，主要花在拉 4.5 GB 的 ollama 镜像和 640 MB 的模型
```

`.env` 里只有两项必填：

- `FLOWPAY_MODEL_API_KEY`：聊天模型密钥
- `FLOWPAY_DATASET_ROOT`：数据包位置，填 `.zip` / `.tar.gz` 都行，首次启动自动解开

跑完打开 <http://127.0.0.1:5173/>，接口在 <http://127.0.0.1:8000/>。

想跑别的阶段：`STAGE=s2 make up`（可选 s1—s4）。

## `make up` 做了什么

每一步都可以重复执行，已经完成的会跳过：

1. 校验 `.env`，把数据包解开成绝对路径交给 compose
2. 起 PostgreSQL（pgvector）
3. 导入知识库：100 篇文档、712 个向量，直接从数据包的 dump 恢复，不用自己算
4. 起 Ollama，并确保嵌入模型 `qwen3-embedding:0.6b` 就位
5. 核对模型 digest 与 `backend/configs/local.toml` 的 `embedding.revision` 是否一致
6. 起后端与前端

模型存在 `flowpay_ollama` 这个 docker 卷里，`make down` 不会删，所以只下载一次。

会话事件库保存在 `flowpay_runtime` 数据卷中，避免在 Windows 挂载目录上逐片同步写入
造成流式回答延迟。普通 `docker compose down` 保留此卷；`down -v` 会删除会话、知识库和模型。
从旧版 `var/live` 切换时，先停止后端，再用 SQLite backup 迁移（保留原库，不覆盖已有目标）：

```bash
docker compose stop backend
docker compose run --rm --no-deps -T backend python scripts/migrate_runtime.py /app/var/live/sessions.sqlite /app/runtime/sessions.sqlite
docker compose up -d --no-deps --force-recreate backend
```

## 为什么必须有嵌入服务

数据包省掉的是**建库**：100 篇文档切成 712 块，每块的向量都算好了。

但用户每提一个问题，这个问题本身也要现场嵌成向量，才能和库里那 712 个向量比相似度。
这一步没法预先做——没人知道用户会问什么。所以 Ollama 是运行期的必需服务，
不是可选加速项。

两边的向量必须出自同一个模型才可比。`KnowledgeClient.search` 在嵌入之前会用
`same_vector_space` 核对集合记录的模型身份（provider / model / revision / base_url /
dimensions / request_options），对不上直接报错，不会让检索悄悄退化成噪声。

这也是为什么**嵌入服务的端口必须是 11434**：集合里的向量是在该端口建的，
端口参与向量空间身份比较（主机名不参与，`127.0.0.1`、`host.docker.internal`、
`ollama` 会被归一成同一个部署）。

## 常用命令

Docker 默认关闭后端自动重载与前端文件轮询，避免 Windows 文件共享进程持续高 CPU。
后端、前端、Ollama 分别限制为 2、1、4 个逻辑 CPU 的计算配额，给桌面保留资源。
嵌入高负载时可能需要更长时间，但不会允许该容器独占全部 CPU。
修改源码后执行 `docker compose restart backend frontend` 加载；需要前端轮询开发时，
可显式设置 `FLOWPAY_POLL_WATCH=1` 后重新创建前端容器。

```
make up        起全栈（BUILD=1 强制重建镜像）
make down      停容器，数据卷保留
make logs      跟踪后端与前端日志
make help      列出全部目标
```

## 换聊天模型

在 `.env` 里填这两项，留空则用 `backend/configs/local.toml` 的默认值：

```bash
FLOWPAY_MODEL_BASE_URL='https://api.openai.com/v1'
FLOWPAY_MODEL_NAME='gpt-4.1-mini'
```

嵌入模型不要换：数据包里的向量和它绑定，换了就得自己重建整个集合。

## 宿主机直跑（进阶）

要改 `backend/src/flowpay/core/` 或者需要断点调试时走这条，否则上面四个命令就够了。
这条路径需要本机自己装好 uv、Node >= 22.12 和 **Ollama**：

```bash
make setup SETUP_ARGS=--download-model   # 装依赖、建库，并下载嵌入模型到宿主机
make run STAGE=s1                        # 后端
make frontend                            # 前端
```

`make setup` 只起 PostgreSQL 容器，嵌入服务用宿主机那个（`127.0.0.1:11434`）。
`make doctor` 可以只做只读检查，不装东西也不调用聊天模型。

## 常见问题

**提问时报嵌入相关的错**  
`docker compose ps` 看 ollama 是否在跑，`make logs` 看后端报什么。
正常情况下 `make up` 已经确保模型就位，不该出现。

**启动时提示 digest 与 revision 不符**  
上游更新了同名模型标签。新模型和数据包里的向量不在同一空间，不能混用；
请使用数据包配套的模型版本，不要改 `revision` 绕过检查。

**端口被占**  
需要 5173、8000、15432 三个端口。Ollama 的 11434 不对外发布，
不会和你本机已装的 Ollama 冲突。

**想从头再来**  
`docker compose down -v` 删掉数据卷（会话历史、知识库和嵌入模型都会删除，下次启动需重新准备）。

## 上游代码许可声明

<a id="upstream-license"></a>

<details>
<summary>MIT 版权与许可声明（保留上游授权）</summary>

```text
MIT License

Copyright (c) 2026 程序员Carl

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

</details>
