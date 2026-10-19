# 225 · Nexent 参赛平台部署取证（v2.7.0）

> 目的：赛题三「基于 Nexent 智能体平台」的唯一前置条件——**能不能把 Nexent 装起来、跑起来**。本文件记录实际部署过程、可复跑命令、全部本地偏离、验证证据与发现的真问题。
> 记录时刻：2026-10-10（本机 CST）。所有结论均来自本次实跑的命令输出，未从记忆或摘要转述。

## 0. 结论（一句话）

**部署成功且模型通路已打通**：10 个容器全部 Up，Web `http://localhost:3000` 返回 200 且可登录，Nexent 侧已注册 `workbuddy` provider 并拉到 **15 个 LLM**，容器内经转发器对上游模型完成过一次**真实生成**。
**已完成补测（10-10，见 §11–§13）**：平台内已建智能体 `ict-demo-agent`（`agent_id=1`）并端到端跑通对话（`final_answer` 正确）；**自建 MCP 服务已注册并被智能体调用**（§12）；**官方技能已装 4 个、绑定并被实际读取执行**（§13）。**仍未做**：Web UI 里人工点击建智能体（本机无浏览器）、装回 supabase 取完整权限栈（§6.4）。

## 1. 源码与环境

| 项 | 实测值 |
|---|---|
| 源码目录 | `/home/endmin/nexent-ict` |
| 来源 | `git clone --depth 1 --branch v2.7.0 https://gitcode.com/ModelEngine/nexent.git`（10.5 s，193 MB） |
| HEAD | `519b9b17c7f4cbc623256ac318ea96a20f33772e` |
| `VERSION` | `v2.7.0`（选发布 tag 而非 `develop`，材料里可引用确切版本） |
| 内核 | `6.18.33.2-microsoft-standard-WSL2`（**mirrored networking**，见 §6.1） |
| CPU / 内存 / 磁盘 | 24 核 / 11 GiB 总（7 GiB 可用）/ 913 G 可用 |
| Docker | 29.8.0 + Compose v5.5.1，daemon 可用，当前用户属 `docker` 组 |
| GPU | NVIDIA RTX 5070 Laptop GPU（Nexent 非必需，模型走 OpenAI 兼容 API） |
| 官方要求 | CPU 4（推荐 8）/ 内存 8 GiB（推荐 16 GiB）/ 磁盘 40 G（推荐 100 G），见源码 `README_CN.md:29-36` |

内存 11 GiB 低于官方推荐的 16 GiB ⇒ 本次通过降 ES 堆（§3 偏离清单第 3 条）压到可跑。

## 2. 可复跑部署命令

```bash
cd /home/endmin/nexent-ict
bash deploy/docker/deploy.sh \
  --components infrastructure,application,data-process \
  --image-source general \
  --defaults
```

- 组件默认值是 `infrastructure,application,data-process,supabase`；本次**去掉 `supabase`**（其 kong 容器占宿主 `8000:8000`，本机 8000 已被 `python3` pid 397 占用），也**未部署 `monitoring`**。
- 部署产物：`deploy/env/.env`（约 400 行）、`deploy/docker/deploy.options`。
- 部署成功标志（脚本自身输出）：`🎉 Deployment completed successfully!`

## 3. 本地偏离清单（相对 v2.7.0 原样，共 6 条）

> 全部为**本地文件改动或运行时选择**，未改任何上游代码逻辑；逐条给出文件与行号，便于复现与向上游反馈。

1. **ES 宿主端口 9210 → 9220**（三处）
   - `deploy/docker/compose/docker-compose.yml:38`：`"9210:9200"` → `"9220:9200"`
   - `deploy/common/common.sh:1804`：`ports+=(9210 …)` → `ports+=(9220 …)`
   - `deploy/docker/generate_env.sh:91`：`http://localhost:9210` → `http://localhost:9220`
   - 原因：宿主 9210 被 **Windows 侧 QQ** 占用（§6.1）。ES 在 docker 网络内的地址不受影响（`deploy/env/.env:33` `ELASTICSEARCH_HOST=http://nexent-elasticsearch:9200`）。
2. **`deploy/common/common.sh:743`**：`deployment_update_monitoring_env_var "OTEL_EXPORTER_OTLP_PROTOCOL" "http"` → **`"http/protobuf"`**
   - 原因：该硬编码值会让 `nexent-runtime` 的 FastAPI 启动即崩（§6.2）。**这是上游真 bug。**
3. **ES 堆**：`deploy/env/.env:37` `ES_JAVA_OPTS="-Xms2g -Xmx2g"` → `-Xms1g -Xmx1g`（11 GiB 机器的内存压降）。
4. **不部署 supabase 组件**（宿主 8000 冲突；代价见 §6.4）。
5. **镜像经可达源拉取后 retag 成 canonical 名**（§5，不改仓库文件）。
6. **不部署 monitoring 组件**（无 OTEL collector；但遥测环境变量仍会被注入，这正是 §6.2 的触发条件）。

## 4. 端口占用（部署前实测）

本机原有服务：`3080/3081` = `dsh web`（pid 978，同时绑两个）；`8100`、`8000` = 其他 python 进程（pid 397 占 8000）。
Nexent 默认宿主端口：`3000`（web）`5010`（config）`5011/5015`（mcp）`5012/5555/8265`（data-process）`5013`（northbound）`5014`（runtime）`5434`（postgresql）`6379`（redis）`9010/9011`（minio）`9210/9310`（elasticsearch）`3100`（nginx）`2222`（openssh）`8000`（supabase-kong，已去除）。
除 8000 外**全部实测空闲**；9210 的冲突在 Linux 侧看不见（§6.1）。

## 5. 镜像来源矩阵（本机网络条件下的实际结果）

| 镜像 | 结果 |
|---|---|
| `docker.m.daocloud.io/library/*` | ✅ 只放行官方 library：`redis:alpine` 18.8 s 成功、`elasticsearch:8.17.4`、`postgres:15-alpine` manifest 可用 |
| `docker.m.daocloud.io/minio/*`、`nexent/*` | ❌ `403 Forbidden denied` |
| `quay.io/minio/minio:RELEASE.2023-12-20T01-00-02Z`（直连） | ❌ `401 UNAUTHORIZED` |
| `quay.m.daocloud.io/...`（`--image-source mainland`） | ❌ `error from registry: denied` |
| `hub.rat.dev` / `docker.1ms.run` 的 minio 旧 tag | ❌ `not found`（该 tag 已被 Docker Hub 官方删除） |
| **华为 SWR quay 代理** `swr.cn-north-4.myhuaweicloud.com/ddn-k8s/quay.io/minio/minio:…` | ✅ 可用 |
| **腾讯云** `ccr.ccs.tencentyun.com/nexent-hub/{nexent,nexent-web,nexent-data-process,nexent-mcp,nexent-sandbox}:v2.7.0` | ✅ 可用（1.78 GB / 3.7 min） |
| `hub.rat.dev/nexent/*` | ✅ 可用（备用腿） |

做法（`/tmp/nexent-pull.sh`）：从可达源 `docker pull` → `docker tag` 成 compose 期望的 canonical 名 → 三组并发。20:20:43 → 20:28:50 全部完成，9 个镜像合计约 11.5 GB。

## 6. 发现的真问题（三个可上报上游 + 两条部署代价）

### 6.1 部署脚本的端口检查是**假绿**（WSL2 镜像网络盲区）
- 现象：`docker compose up` 反复失败，逐字 `failed to bind host port 0.0.0.0:9210/tcp: address already in use`（容器停在 Created）。
- 诊断：`/proc/net/tcp*`、`ss -lnt`、`ss -tan`、`docker-proxy` **全无 9210 踪迹**，但 python 直接 `bind(0.0.0.0:9210)` 仍 `[Errno 98]`（9310 可绑）。
- 真因：`netstat.exe -ano`（Windows 侧）显示 **`127.0.0.1:9210 LISTENING PID 23872`**，进程为 `D:\Program Files\Tencent\QQNT\QQ.exe`。WSL2 **mirrored networking** 把 Windows 的监听透传进 WSL，但 **Linux 侧 socket 表看不到它**。
- 附带：脚本自带端口检查（`deploy/common/common.sh:1804`）依赖 `lsof`，本机未安装 ⇒ 检查静默通过（假绿）。
- 教训：**WSL2 mirrored networking 下判断端口占用必须用 `netstat.exe`/`powershell.exe` 对查**；`lsof` 缺失时端口检查必须报"未检测"而不是"通过"。

### 6.2 `common.sh:743` 硬编码 OTLP 协议，让 runtime 启动即崩（上游真 bug）
- 现象：`nexent-runtime` crash-loop（restarts 递增），日志逐字：
  `[ERROR uvicorn.error] FastAPI automatic telemetry requires the OTLP http/protobuf protocol. Configure other transports explicitly and pass telemetry={'auto_configure': False} to FastAPI().` → `Application startup failed. Exiting.` ⇒ web 的 nginx 对 `/api/health` 返回 `502 {"detail":"Backend unavailable"}`。
- 容器环境实测：`OTEL_EXPORTER_OTLP_PROTOCOL=http`、`ENABLE_TELEMETRY=false`、`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` ⇒ **没部署 monitoring 组件，遥测变量照样注入**（compose 的 app 服务硬写 `env_file: ../../env/monitoring.env`）。
- 两次无效尝试（教训：改错层）：① 改生成物 `deploy/env/monitoring.env:10` → 每次部署被重新生成覆盖；② 改模板 `deploy/env/monitoring.env.example:12` → 脚本不使用模板值。
- 根因：`deploy/common/common.sh:743` 每次部署**硬写** `"http"`。改为 `"http/protobuf"` 后即通。
- **附加教训**：compose 不会因 `env_file` 内容变化自动重建容器 ⇒ 改完必须 `docker rm -f nexent-runtime` 再跑部署，否则看不到任何变化。

### 6.3 批量建模型接口 500：`skip_default_backfill` 未在批量路径 pop（上游真 bug）
- 请求：`POST /api/model/provider/batch_create`（用 provider 拉到的 15 条模型投递）
- 响应：`500 {"message":"Failed to batch create models: Unconsumed column names: skip_default_backfill"}`
- 根因：`skip_default_backfill` 是**不入库**的流程控制字段（`backend/consts/model.py:581-586`）。单条路径在 app 层 pop 掉（`backend/apps/model_managment_app.py:274`：`skip_backfill = bool(model_data.pop("skip_default_backfill", None))`），**批量路径没有 pop**，字段一路传到 ORM，而 DB 表 `nexent.model_record_t` 没有该列（`docker exec nexent-postgresql psql -U root -d nexent -c "\d nexent.model_record_t"` 实测列清单确认）。
- 绕行：改用单条 `POST /api/model/create`（本次 3 条模型均由此建成）。

### 6.4 去掉 supabase 的代价：mock 用户管理 ⇒ 部分管理端点 403
- 登录响应 `Set-Cookie: nexent_access_token=mock_access_token`（另有 `nexent_refresh_token=mock_refresh_token`，Max-Age 315360000）⇒ 本部署走 **mock 用户管理**。
- DB 实测 `nexent.user_tenant_t`：`user_id="user_id"`、`tenant_id="tenant_id"`、`user_role="SPEED"`。
- 受影响端点（实测）：`GET /api/repository/agent/official/management` → 403 `Super admin role is required`；`POST /api/model/manage/healthcheck` → 403 `This operation requires SU or tenant ADMIN role`。
- ⇒ 想要完整权限栈（官方智能体同步、模型健康检查、批量模型），需要把 supabase 组件装回来；前置是解决宿主 8000 端口冲突（`python3` pid 397）。

### 6.5 `nexent-minio` 会掉出 docker 网络 ⇒ 所有智能体运行失败（2026-10-15 复发并修复）
- **现象**：UI 里任何提问都以 `Agent execution failed. Please try again later.` 结束（前端仍显示 `success` / 已完成，**是假绿**）；耗时约 126 秒后失败。
- **日志证据**（`docker logs nexent-runtime`）：
  ```
  botocore.exceptions.EndpointConnectionError: Could not connect to the endpoint URL: "http://nexent-minio:9000/nexent-artifacts"
  consts.exceptions.MemoryPreparationException: Could not connect to the endpoint URL: "http://nexent-minio:9000/nexent-artifacts"
  [ERROR management.services.agent.run] Agent stream producer exited without terminal state conversation=10
  ```
- **根因**：`docker inspect nexent-minio` 的 `NetworkSettings.Networks` **为空** —— 10 个容器里只有它不在 `nexent_network` 上（其余 9 个都在；`nexent-runtime` 另挂 `nexent_sandbox_control`）。容器内解析主机名报 `[Errno -3] Temporary failure in name resolution`。失败发生在 **memory 准备阶段**（`run.py:1395`），与模型通路无关。
- **判据**：`docker exec nexent-runtime python3 -c "import urllib.request; print(urllib.request.urlopen('http://nexent-minio:9000/minio/health/live', timeout=8).status)"` → 修前 DNS 失败、修后 **`200`**。
- **修法（一行，无需重启任何容器）**：`docker network connect nexent_network nexent-minio`。
- **成因推断**：`docker compose up` 重建网络后 minio 未重新接入（本次修复后需观察是否复发）。
- **演示前检查（建议每次跑）**：
  ```bash
  for c in $(docker ps --format '{{.Names}}'); do
    echo "$c [$(docker inspect $c --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}')]"
  done
  ```
  任何容器的网络列为空 ⇒ `docker network connect nexent_network <容器名>`。

## 7. 模型通路设计（宿主网关 → WSL 转发器 → 容器）

背景：用户既有模型网关是 **Windows 侧**的 `C:\wb\gateway-serve.bat`（python pid 24524，日志 `C:\wb\gateway.log`），**只绑 Windows 的 `127.0.0.1:8788`**；key 在 `C:\wb\keys.txt`（`intl`/`cn` 两个，与 `~/.dsh/gateway-keys.env` 的 `WB_INTL_KEY`/`WB_CN_KEY` 同值，48 字符）。
- WSL 侧直连 `127.0.0.1:8788/v1/models` **通**（无 key → 401 `invalid api key`）。
- Docker 容器**不通**：`172.17.0.1:8788`、`host.docker.internal:8788` 均连不上（Windows 只绑回环）。

解法：新增 WSL 侧纯 asyncio TCP 转发器
- 文件：`/home/endmin/dsh/research/tools/nexent-gw-forward.py`（sha256[:32] `42c6f5018d549c89fc506f9aedf95dcb`）
- 启动：`setsid nohup python3 /home/endmin/dsh/research/tools/nexent-gw-forward.py 8790 8788 </dev/null >/tmp/nexent-gw-forward.log 2>&1 &`
- 监听 `0.0.0.0:8790` → 转发 `127.0.0.1:8788`
- 容器侧 base_url = **docker bridge 网关 IP**：`nexent_network` 网关 `172.20.0.1` ⇒ `http://172.20.0.1:8790/v1`
- 注意：容器**重启后 bridge 网关 IP 可能变**，若 provider 不通先 `docker network inspect nexent_network | grep Gateway` 复核。

## 8. 验证证据（逐条命令 → 结果）

| # | 命令 | 结果 |
|---|---|---|
| 1 | `docker ps -q \| wc -l` | `10` |
| 2 | `docker ps --format '{{.Names}}'` | `nexent-config nexent-data-process nexent-elasticsearch nexent-mcp nexent-minio nexent-northbound nexent-postgresql nexent-redis nexent-runtime nexent-web` |
| 3 | `curl -s -o /dev/null -w '%{http_code}' http://localhost:3000/zh` | `200`（`<title>AI Agent Platform</title>`） |
| 4 | `POST /api/user/signin {"email":"suadmin@nexent.com","password":"Nexent@123"}` | `200` + mock cookie |
| 5 | `POST /api/model/provider/create {provider:"workbuddy", api_key:<WB_CN_KEY>, base_url:"http://172.20.0.1:8790/v1", ssl_verify:false}` | `200`，拉回 **15 个 LLM**（deepseek-v4.1-flash / deepseek-v4-pro / glm-5.3 / glm-5.3-flash / glm-5.2 / glm-5.1 / glm-5v-turbo / hy4-preview-f / hy3 / kimi-k3-1 / kimi-k2.8-preview / kimi-k2.7 / kimi-k2.6 / minimax-m3 / space-bunny） |
| 6 | `POST /api/model/create` ×3 | `200`；`deepseek-v4.1-flash`(id=1，自动占默认槽 `auto_configured_defaults:[{config_key:"LLM_ID",model_id:1}]`)、`glm-5.3`(id=2)、`glm-5v-turbo`(id=3) |
| 7 | 容器内 `curl -X POST http://172.20.0.1:8790/v1/chat/completions`（model=`deepseek-v4.1-flash`，`max_tokens:20`） | **真实生成**：`usage.total_tokens=61`、`completion_tokens_details.reasoning_tokens=20`、`finish_reason":"length"`，reasoning 段非空 |
| 8 | ES：`curl -u elastic:<ELASTIC_PASSWORD> http://localhost:9220/_cluster/health` | `{"cluster_name":"docker-cluster","status":"green",…}` |
| 9 | `docker logs nexent-runtime`（修复后） | `Application startup complete.` / `Uvicorn running on http://0.0.0.0:5014` / `restarts=0` |

**未通过 / 未验证**
- `GET /api/agent/list` → `[]`（无智能体）；`POST /api/agent/run {"query":…,"model_id":1}` → SSE `{"type":"error","content":"Agent execution failed. Please try again later."}`，runtime 日志 `File "/opt/backend/database/agent_db.py", line 62, in search_agent_info_by_agent_id / raise ValueError("agent not found")` → `consts.exceptions.MemoryPreparationException: agent not found`。
- 建智能体的 API 路都不通：`GET /api/agent/workbench/bootstrap` → `404 {"message":{"code":"WORKBENCH_DISABLED"}}`（`ENABLE_AGENT_WORKBENCH=false`）；`POST /api/repository/agent/internal/official/sync` → 经 nginx 403 `Official agent synchronization is only available locally`（handler 校验 `request.client.host in {"127.0.0.1","::1"}`），容器内直调 404。⇒ **建智能体留给 Web UI**（`http://localhost:3000`，Windows 浏览器可直开）。
- `/api/health` → `404 {"detail":"Not Found"}`：该路由在此版本不存在，**不代表服务未起**（应用已 `Application startup complete`）。

## 9. 访问入口与运维

| 项 | 值 |
|---|---|
| Web UI | `http://localhost:3000`（WSL2 镜像网络下 Windows 浏览器直接打开） |
| 默认账号 | `suadmin@nexent.com` / `Nexent@123`（mock 用户管理） |
| ES | `http://localhost:9220`，用户 `elastic`，口令见 `deploy/env/.env:34` 的 `ELASTIC_PASSWORD` |
| 容器重启 | `docker compose -f deploy/docker/compose/docker-compose.yml restart <name>`；改 env 后须 `docker rm -f <name>` 再跑部署 |
| 停止 | `bash deploy/docker/deploy.sh --stop`（或 `docker compose … down`） |
| 转发器 | **systemd 用户服务** `nexent-gw-forward.service`（`~/.config/systemd/user/`），`enabled + active`，`Restart=always`，日志 `/tmp/nexent-gw-forward.log`；`systemctl --user status nexent-gw-forward` 查看 |
| 取证文件 | 本文件；原始 provider 响应 `/tmp/nexent-provider.json`；cookie jar `/tmp/nexent-cookie.txt` |

## 10. 未完成清单（下一步）

1. ~~在 UI 建一个智能体并跑通对话~~ ✅ **已用 API 路径完成**：建 `ict-demo-agent`（§11.1）→ 跑通（§11.2 / §12.4 / §13）；UI 点击路径未走（本机无浏览器），留作可选。
2. ~~MCP / Skills 端到端实测~~ ✅ **已完成**：自建 MCP 服务注册 + 智能体调用（§12）、官方技能安装 + 绑定 + 执行闭环（§13）。（原始待办描述见下：Nexent 把 MCP 与 Skills 做成一等公民
3. ~~转发器开机自启~~ ✅ 已做：`~/.config/systemd/user/nexent-gw-forward.service`（`systemctl --user enable --now`，`Restart=always`）+ `loginctl enable-linger`。注意：10 个容器本身已是 `restart: always`，但**容器 bridge 网关 IP 在重建后可能变化**，届时需复核 provider 的 base_url。
4. **是否装回 supabase** 以拿到完整权限栈（需先解决 8000 端口冲突）。
5. 向上游反馈 §6.2 / §6.3 两个 bug（可附最小复现）。


---

## 11. 平台能力实测（10-10 补测：智能体 / MCP / Skills）

> 承接 §8 的"未通过"清单：本节把当时缺的三块证据补齐。全部为实跑输出。

### 11.1 建智能体（API 路径，非 UI）

`POST /api/agent/update` 在 **`agent_id` 为 null 时走创建分支**（`backend/management/services/agent/service.py:705` 起，注释逐字 `# If agent_id is None, create a new agent; otherwise, update existing`），`AgentInfoRequest` 全部字段可选。

```bash
curl -X POST http://localhost:3000/api/agent/update -b <cookie> -H 'Content-Type: application/json' -d '{
  "name":"ict-demo-agent","display_name":"ICT 演示智能体",
  "description":"验证平台端到端可用性的最小智能体",
  "business_description":"回答简单问题，验证模型通路与平台编排",
  "model_ids":[1],"business_logic_model_id":1,"max_steps":5,
  "duty_prompt":"你是演示智能体，回答要简短。","enabled":true}'
# → {"agent_id":1}   code=200
```

`GET /api/agent/list` → 1 条（`ict-demo-agent`）。注意：list 返回里 `model_ids` 显示为 `[]`，但运行时不缺模型（见 11.2，模型走 `model_id` 参数与默认槽 `LLM_ID=1`）。

### 11.2 智能体端到端运行（本文件最关键的一条证据）

`POST /api/agent/run`（SSE）：

```bash
curl -sN -X POST http://localhost:3000/api/agent/run -b <cookie> \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"query":"只回答两个字：通了","agent_id":1,"model_id":1,"history":[]}'
```

事件序列（实测）：`tool`(search_memory) → `execution_logs` → `other`(uncertainty_reserve_active) → `agent_new_run` → `step_count` → `model_attempt_control(begin)` → `model_output_thinking` → `token_count` → `model_attempt_control(commit)` → **`final_answer`**。

- **结果：`{"type":"final_answer","content":"通了","unit_index":7}`** —— 模型经"容器 → WSL 转发器 → Windows 网关"回答正确，平台编排（记忆工具调用 + 步进 + 预算控制）全程走通。
- 非 debug 运行同时落库：`conversation_created` 事件 + `Auto-created conversation_id=…` 日志 ⇒ **会话持久化可用**。
- `token_count` 实测 `provider_cache_hit: false`、`provider_uncached_input_tokens: 3526`。
- 运行工作区：`/mnt/nexent/workdir/<user_id>/<run_hash>`（日志中给出，产出文件结束运行时上传 MinIO）。

### 11.3 上游 bug #4：省略 `history` 字段会让 `/api/agent/run` 500

- **最小复现**：`POST /api/agent/run {"query":"只回答两个字：通了","agent_id":1,"model_id":1}`（**不带** `history`）⇒ `500 {"message":"Agent run error."}`；runtime 日志逐字 `[ERROR agent_app] Agent run error: 'NoneType' object is not iterable` + `[ERROR apps.app_factory] HTTPException: Agent run error.`。
- **同一请求加上 `"history": []` ⇒ 完全正常**（11.2 的结果就是带 `[]` 跑出来的）。
- 已定位的传递点：`backend/management/services/agent/run.py:1182` 把 `agent_request.history`（此时为 `None`）交给下游构建运行信息，下游对 history 做迭代 ⇒ `NoneType` 不可迭代。**未取到完整栈**（应用只记录 `str(e)`，无 `exc_info`），故只报症状与绕过法，不臆断具体行。
- 影响面：凡是不显式传 `history` 的 API 调用方都会 500；Web UI 本身会带 `history`，故 UI 不受影响。
- 修复建议：在消费处 `history or []`（或在 `AgentRequest` 上给 `history` 默认空列表）。

### 11.4 MCP 协议实测（内置 MCP 服务，走标准协议）

- 内置 MCP 服务 = `NEXENT_MCP_SERVER`（容器内实测 `http://nexent-mcp:5011`），租户级 SSE 端点为 **`/mcp/{tenant_id}/sse`**（`backend/utils/mcp_url_utils.py:14`；`backend/mcp_service.py:539` 注释逐字 "API remains on 5015; MCP clients use /mcp/{tenant_id}/sse on 5011"）。
- 鉴权（`backend/mcp_service.py:453-471`）：无 `Authorization` 头时必须同时带 `X-Tenant-Id: <tenant_id>` 与 `X-Nexent-Internal-Token: <TOKEN>`；容器内 `TOKEN=token`（实测 `docker exec nexent-mcp printenv | grep TOKEN`）。
- **探针脚本**：`/home/endmin/dsh/research/tools/mcp-probe.py`（可复跑，只走标准 MCP：SSE 握手 → `initialize` → `tools/list` → `tools/call`）：

```bash
python3 /home/endmin/dsh/research/tools/mcp-probe.py   # 默认 http://localhost:5011 tenant_id token
```

实测输出（10-10）：

| 步骤 | 结果 |
|---|---|
| ① SSE 握手 | `endpoint=/mcp/tenant_id/messages/?session_id=<uuid>` |
| ② `initialize` | `serverInfo={"name":"nexent_mcp_tenant_id","version":"2.14.7"}`，`capabilities=[experimental, prompts, resources, tasks, tools]` |
| ③ `tools/list` | **7 个工具**：`local_test_tool_name`、`nl2a_search_installed_mcp_tools`、`nl2a_search_installed_resources`、`nl2a_search_uninstalled_resources`、`nl2a_recommend_resources`、`nl2a_save_agent_draft_fields`、`nl2a_wrapper` |
| ④ `tools/call` | `local_test_tool_name{para_1:"hello-mcp",para_2:7}` → `{"content":[{"type":"text","text":"success"}],"structuredContent":{"result":"success"},"isError":false}` |

⇒ **MCP 协议（SSE 传输 + JSON-RPC 2.0 + 工具发现与调用）端到端可用**，不是"能连上"而是"能调用出结果"。
- 传输判定规则（`backend/agents/create_agent_info.py:2889`）：URL 以 `/sse` 结尾 ⇒ `transport: "sse"`，否则 `streamable-http`。

### 11.5 Skills 实测（官方技能库 + 安装 + 落地）

- `GET /api/skills` → `{"skills":[]}`（初始为空）；`GET /api/skills/official` → **28 个官方技能可装**：`analyze-image, analyze-text-file, canvas-design, create-docx, create-excel, create-file-directory, delete-file-directory, docx, email-utils, frontend-design, list-directory, mcp-builder, move-file-directory, pdf, pptx, read-file, run-shell-ssh, search-datamate, search-dify, search-idata, search-knowledge-base, search-web-exa, search-web-linkup, search-web-tavily, skill-creator, slack-gif-creator, web-artifacts-builder, xlsx`。
- 安装命令（实测 200）：`POST /api/skills/install {"skill_names":["analyze-text-file","read-file","mcp-builder","skill-creator"],"locale":"zh"}` → `{"installed":[…4 项],"total":4}`。
- 落地验证：`GET /api/skills` → 4 条（id 1–4，`source:"official"`）；容器内 `/mnt/nexent-data/skills/tenant_id/` 出现实体目录；`GET /api/skills/analyze-text-file/files` → `{"name":"analyze-text-file","type":"directory","children":[{"name":"examples.md",…},{"name":"SKILL.md",…}]}`。
- 注意：**装了技能 ≠ 技能被用上**。技能要生效需绑定到智能体（`AgentInfoRequest.enabled_skill_ids` / `skill_instances`）并由运行时的 skill 执行链调用；本次仅验证"安装 + 落地 + 可读"，**绑定与调用未测**（列入未完成清单）。

### 11.5.1 五个自建 Skill 已装并绑定（2026-10-19 补）

官方要求「深度集成 Nexent 的 MCP 协议与 Skills 编排能力」，此前只验证了「官方技能安装 + 落地 + 可读」，**自建 Skill 既没装也没绑**。本轮补齐：

| 步骤 | 命令 | 结果 |
|---|---|---|
| 上传自建 Skill | `POST /api/skills/upload`（multipart，`file=@skills/<name>/SKILL.md`，`source=custom`） | `citation-verify` → id 7 · `ontology-migrate` → id 8 · `table-extract` → id 9（各返回 201） |
| 绑定到智能体 | `POST /api/skills/instance/update {"agent_id":1,"skill_id":N,"version_no":0}` | 5 个自建全部 `enabled: true`（id 5 / 6 / 7 / 8 / 9） |
| 复查 | `GET /api/skills` | **9 个**：4 官方（`analyze-text-file` / `read-file` / `mcp-builder` / `skill-creator`）+ **5 自建**（`workbench-status-report` / `governance-policy-trace` / `citation-verify` / `ontology-migrate` / `table-extract`） |

**注意**：`POST /api/skills/upload` 支持单个 `SKILL.md` 或 ZIP（ZIP 可带 `scripts/` 与 `assets/`）；`source=custom` 与官方技能同目录落地（容器内 `/mnt/nexent-data/skills/tenant_id/`）。绑定接口要求 `version_no=0`（草稿版），已发布版本的绑定需走版本升级路径 —— 本次绑的是 `ict-demo-agent` 的草稿版。

**仍未验证**：绑定之后智能体在真实对话里**是否真的会调用**这些自建 Skill（`05` 原文 §11.5 的「装了 ≠ 用上」同理）。要验需跑一轮对话并检查 trace 里的 skill 调用记录。

### 11.6 本节新增的上游问题（合计 4 个）

| # | 问题 | 位置 | 状态 |
|---|---|---|---|
| 1 | 部署脚本端口检查假绿（WSL2 镜像网络 + lsof 缺失） | `deploy/common/common.sh:1804` | 已绕过（ES 改 9220） |
| 2 | 硬编码 OTLP 协议致 runtime 启动崩溃 | `deploy/common/common.sh:743` | 已修（`http/protobuf`） |
| 3 | 批量建模型 500：`skip_default_backfill` 未 pop | `backend/apps/model_managment_app.py`（单条路径 274 行有 pop） | 已绕过（单条建模型） |
| 4 | 省略 `history` 致 `/api/agent/run` 500 | 传递点 `backend/management/services/agent/run.py:1182` | 已绕过（显式 `history: []`） |

---

## 12. 自建 MCP 服务端到端集成（10-10 补测 · 赛题三核心证据）

### 12.1 自建 MCP 服务端（工作台能力以标准 MCP 暴露）

| 项 | 值 |
|---|---|
| 服务端文件 | `/home/endmin/dsh/research/tools/workbench-mcp-server.py`（78 行，sha256 `8a25cabfccb1ec9f07a7c16c1e897a5c9e9e25db0331fd1c353eee30c7f4afd6`） |
| 框架 / 传输 | fastmcp **2.14.7**（与容器内同版本），streamable-http，`0.0.0.0:8791`，路径 `/mcp` |
| 工具 1 | `workbench_task_status()` → 读 `TASK_PLAN_STATUS.md`，数 ✅ / 🟨 / ⬜ 与文档元信息 |
| 工具 2 | `workbench_recent_commits(limit)` → `git log --oneline` |
| 工具 3 | `workbench_run_demo()` → 跑 `python3 run.py --demo`（实测 3.19 s / rc=0 / `model_calls:22` / `model_mode:offline_fixture_NOT_live_LLM`） |
| 运行方式 | systemd 用户服务 `~/.config/systemd/user/workbench-mcp.service`（`enabled + active`，`Restart=always`，`ExecStart=/home/endmin/.local/bin/uv run --python 3.11 --with fastmcp==2.14.7 python …`，日志 `/tmp/workbench-mcp.log`，MainPID 133216） |

客户端侧实测（fastmcp `Client`，与本平台用的是同一个库）：
`TOOLS: ['workbench_task_status', 'workbench_recent_commits', 'workbench_run_demo']`；
`workbench_task_status` → `{"file":"/home/endmin/project-ict/TASK_PLAN_STATUS.md","bytes":81097,"mtime":"2026-10-09 22:44:19","done":94,"partial":23,"todo":18}`。

### 12.2 容器 → 宿主可达性（部署前必查）

```
docker exec nexent-runtime curl -X POST http://172.20.0.1:8791/mcp \
  -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```
→ **http_code=200**，返回合法 `initialize` 结果（`protocolVersion:"2025-06-18"`，`capabilities` 含 tools/prompts/resources/tasks）。⇒ 容器经 docker bridge 网关 `172.20.0.1` 可达宿主服务（与 §7 模型通路同一机制）。

### 12.3 注册 → 工具入库 → 绑定智能体

| 步骤 | 命令 | 结果 |
|---|---|---|
| 注册 MCP 服务 | `POST /api/mcp/add {"name":"workbench","server_url":"http://172.20.0.1:8791/mcp","source":"local","enabled":true}` | `200 {"message":"Successfully added MCP service"}` |
| 平台侧健康检查 | `GET /api/mcp/list` | `mcp_id=1`，**`status:true`**、`enabled:true`，`registry_json._toolNames = ["workbench_task_status","workbench_recent_commits","workbench_run_demo"]`（= Nexent 自己连上去发现的结果） |
| 扫描入库 | `GET /api/tool/scan_tool` | `200 {"message":"Successfully update tool"}`；`/api/tool/list` → **tool_id 34 / 35 / 36**（`usage:"workbench"`，`is_available:true`） |
| 绑定智能体 | `POST /api/agent/update {"agent_id":1,"enabled_tool_ids":[7,20,27,32,34,35,36],"enabled_skill_ids":[1,2,3,4]}` | `200 {"agent_id":1}` |

注：`/api/agent/list` 不投影 `enabled_tool_ids`（返回 `null`）——**绑定是否生效要看运行行为，不能看 list 投影**。

### 12.4 端到端证据：智能体调用自建 MCP 工具并答对真实数据

```
curl -sN -X POST http://localhost:3000/api/agent/run -b <cookie> \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"query":"用已启用的工作台工具查一下 TASK_PLAN_STATUS.md：已完成、部分完成、待办各多少项？只报三个数字。","agent_id":1,"model_id":1,"history":[]}'
```
事件序列（实测）：`conversation_created` → `tool`(**search_memory**) → `execution_logs` → `other`(uncertainty_reserve_active) → `agent_new_run` → … → `tool`(**workbench_task_status**) → … → `final_answer`。

**最终回答（逐字）：`已完成 94 项，部分完成 23 项，待办 18 项。`**
与 12.1 里直接调用工具的输出（done=94 / partial=23 / todo=18）**逐项一致** ⇒ 数字确实来自工作台项目文件，不是模型编的。

## 13. Skills 绑定与执行（10-10 补测 · 赛题三另一半）

| 步骤 | 命令 / 证据 | 结果 |
|---|---|---|
| 已装技能 | `GET /api/skills` | 4 条（id 1–4：`analyze-text-file`、`read-file`、`mcp-builder`、`skill-creator`，`source:"official"`） |
| 绑定 | `enabled_skill_ids:[1,2,3,4]`（与 §12.3 同一次 update） | `200` |
| 提示注入 | `/api/agent/run` 问「你现在有哪些技能可用？只列名字，一行一个」 | `final_answer` 逐行列出 4 个技能名 ⇒ 技能目录已进提示（对应 `backend/agents/create_agent_info.py` 的 `_get_skills_for_template`） |
| **执行闭环** | `/api/agent/run` 问「创建工作区文件 ict-note.txt 三行…再用 read-file 技能读回」 | 工具事件依次：**`read_skill_md`（读技能说明）→ `create_file` → `read_file`** → `final_answer` 原样报出三行 + 「编码 utf-8，**43 字节**」 |

43 字节 = `workbench-ict`(13) + `\n` + `skill-and-tool-ok`(17) + `\n` + `2026-10-10`(10) + `\n` —— 逐字节核对一致。
⇒ **Skills 不是"装了就算"**：绑定后进入提示、被 `read_skill_md` 实际读取、并指导工具调用，形成闭环。

使用注意（非 bug）：运行结束后 `/mnt/nexent/workdir` 为空（工作区不保留），故技能产物证据只能取**运行期事件与回答**；要留盘需在运行中抓或另接持久化。

---

## 14. 自建 MCP + 自建 Skill 工作流模板（2026-10-10）

### 14.1 自建 MCP 服务（赛题三第一半）

- 源码：`/home/endmin/dsh/research/tools/workbench-mcp-server.py`（78 行，sha256 `8a25cabfccb1ec9f07a7c16c1e897a5c9e9e25db0331fd1c353eee30c7f4afd6`），
  `fastmcp==2.14.7`，`mcp.run(transport="http", host="0.0.0.0", port=8791, path="/mcp")`。
- 常驻：systemd 用户单元 `~/.config/systemd/user/workbench-mcp.service`（enabled+active，`Restart=always`，日志 `/tmp/workbench-mcp.log`）。
- 三工具：`workbench_task_status` / `workbench_recent_commits(limit)` / `workbench_run_demo`。
- 容器可达：`docker exec nexent-runtime curl -X POST http://172.20.0.1:8791/mcp` → 200 + 合法 `initialize`（`protocolVersion 2025-06-18`）。
- 注册：`POST /api/mcp/add` → 200；`GET /api/mcp/list` → `mcp_id=1, status:true`；`GET /api/tool/scan_tool` → 200；
  `GET /api/tool/list` → `tool_id = 34/35/36`（`source="mcp"`, `usage="workbench"`）。
- 端到端调用：`/api/agent/run` 问计数 → 事件 `tool(search_memory)` → `tool(workbench_task_status)` → `final_answer`
  「已完成 94 项，部分完成 23 项，待办 18 项」（原始流 `/tmp/agent-mcp-run.log`，归档 `evidence/01-mcp-tool-call.sse.log`）。

### 14.2 自建 Skill 工作流模板（赛题三第二半）

- 文件：`ict-material-20261010/skills/workbench-status-report/SKILL.md`（52 行，sha256 `e174e05c92b7228018670fe363940c5535a5dccda6089e3bf7846548758c80c3`）。
- 安装：`POST /api/skills/upload`（multipart `file=SKILL.md`, `source=custom`）→ **201**，`{"skill_id":5,...,"tool_ids":[34,35,36]}`
  ⇒ `allowed-tools` 里的工具名被平台**自动解析并绑定**。
- 绑定：`POST /api/agent/update {"agent_id":1,...,"enabled_skill_ids":[1,2,3,4,5],"enabled_tool_ids":[7,20,27,32,34,35,36]}` → 200。
- 端到端：提问「请使用 workbench-status-report 技能，生成一份工作台状态简报。」→ 事件
  `read_skill_md` → `parallel_executor` → `workbench_task_status` → `workbench_recent_commits` → `workbench_run_demo` → `final_answer`；
  回答严格按技能模板，并**主动保留**技能里要求的两条限定（计数口径、`model_mode=offline_fixture_NOT_live_LLM`）。
  原始流 `/tmp/agent-custom-skill.log`（123,041 B），归档 `evidence/04-custom-skill-report.sse.log`。

### 14.3 口径更正（重要，本文档前文若引用请以此为准）

`workbench_task_status` 返回的 `done/partial/todo` = **`TASK_PLAN_STATUS.md` 中状态标记的出现次数**
（94/23/18，含图例与说明文字）；**表格行内**计数为 **77/16/12**。
`grep -c '✅' file` 数的是**行数**（同一文件得 66），不是出现次数——必须用 `grep -o '✅' file | wc -l`。
引用时必须写明口径，不得把「出现次数」称作「任务项数」。

### 14.4 初赛材料包（本次交付）

`/home/endmin/dsh/research/ict-material-20261010/`：
`01-开发设计文档.md/.docx`、`02-Nexent平台智能体设计说明.md/.docx`、`复核命令.md`、`待补充清单.md`、
`skills/workbench-status-report/SKILL.md`、`mcp/workbench-mcp-server.py` + `mcp/workbench-mcp.json`、
`evidence/01..05`。`.docx` 由 `/home/endmin/dsh/research/tools/md2docx.py` 生成
（本机无 pandoc/libreoffice；`python-docx 1.2.0` 可用）。
