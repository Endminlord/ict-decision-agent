# 235 · 平台 UI 示例问答截图（自动化取证）

**目的**：补齐赛题三初赛材料逐字要求里的「**示例问答截图**」。此前材料只有 SSE 事件流日志，
没有任何平台界面截图。本文记录：如何在没有 Linux 浏览器的机器上，用 Windows Chrome +
CDP 自动化产出**真实 UI 截图**，以及路上踩到的四个坑与一个真 bug。

## 1. 环境与方法链

- WSL 内**无任何浏览器**（chromium / firefox / msedge 全无），但 Windows 侧有
  `/mnt/c/Program Files/Google/Chrome/Application/chrome.exe`（实测 Chrome/155）。
- 截图方式：**Windows Chrome headless + CDP 驱动**。
  - 从 WSL 直接执行 exe 无输出 ⇒ 必须写成 `.bat` 再 `cmd.exe /c` 调用。
  - 一次性截图：`chrome --headless=new --screenshot=C:\temp\home.png --window-size=1440,1000 --virtual-time-budget=9000 URL`。
  - 交互式（填表、点击、等待回答）：`--remote-debugging-port=9222 --remote-debugging-address=0.0.0.0`，
    再用 `tools/ui-chat-shot.py` 走 CDP。
- 本机 `modlens_read_image` 不可用（缺 `@liustack/modlens`），无 tesseract ⇒
  **截图内容不靠视觉工具确认**，而是同时抓 `document.body.innerText` 作为「页面上有什么」的文本证据，
  两者一并归档。

## 2. 四个坑

| # | 现象 | 根因 | 解法 |
|---|---|---|---|
| 1 | 从 WSL 调 `chrome.exe` 无任何输出 | cmd 层路径/转义 | 写 `.bat` + `cmd.exe /c` |
| 2 | CDP WebSocket 握手 403 `Rejected an incoming WebSocket connection from the http://127.0.0.1:9222 origin` | Chrome 校验 Origin | 客户端 `suppress_origin=True`（或服务端 `--remote-allow-origins=*`） |
| 3 | 脚本被 timeout 杀掉后**一行输出都没有** | python stdout 缓冲 | `python3 -u` / `PYTHONUNBUFFERED=1` |
| 4 | `--screenshot` 一次性截图能出图，但页面停在登录页 | 需要登录态 | 见 §3 |

## 3. 登录：speed 模式下的 mock 会话

去 supabase 部署（`DEPLOYMENT_VERSION=speed`）下，鉴权由 mock 实现接管：

- `POST /api/user/signin`（config 服务 5010，`mock_user_management_app.py:64`）**接受任意邮箱/密码**，
  返回固定 `MOCK_USER`（`id="user_id"`, `role="admin"`）与 `MOCK_SESSION`
  （`access_token="mock_access_token"`，有效期 10 年）。
- 前端把会话存在 localStorage 的 `session` / `user_info` 两个键（Next.js bundle 里的
  `STORAGE_KEYS = {SESSION:"session", USER_INFO:"user_info"}`）。
  ⇒ 直接注入这两个键即可获得登录态，**不必模拟表单交互**：

```js
localStorage.setItem('session', JSON.stringify({access_token:'mock_access_token',
  refresh_token:'mock_refresh_token', expires_at:2107256544, expires_in_seconds:315360000}));
localStorage.setItem('user_info', JSON.stringify({id:'user_id', email:'ict@example.com', role:'admin'}));
```

## 4. 真 bug：智能体在 UI 里「消失」

现象：API `GET /api/agent/list` 有 1 条智能体，但 UI 新对话页显示「**共 0 个智能体**」。

排查链（逐层证伪）：

1. 前端列表接口不是 `/api/agent/list`，而是 **`/api/agent/published_list`**（CDP 抓包确认）；
   未发布的智能体不在其中 ⇒ 先 `POST /api/agent/{id}/publish` 发布（返回 `version_no`）。
2. 发布后接口返回 1 条，但 UI 仍是 0 条 ⇒ 前端按 **`is_available`** 过滤。
3. 接口返回 `"is_available": false, "unavailable_reasons": ["model_unavailable"]`。
4. 判定代码 `management/services/agent/read.py:83-110 check_agent_availability`：
   `model_ids` 非空时逐个 `is_model_available(record)`；而
   `management/services/model/resolver.py:24 is_model_available` 要求
   `connect_status == ModelConnectStatusEnum.AVAILABLE.value`（`"available"`）。
5. 查库 `nexent.model_record_t`：4 条模型 `connect_status` 全是 **`not_detected`** ——
   模型从未做过连通性检测。

修法（**不是改状态，而是让平台自己去测**）：

```bash
curl -s -X POST "http://localhost:5010/api/model/healthcheck?display_name=DeepSeek%20V4.1%20Flash&model_type=llm" \
     -H 'Authorization: Bearer mock_access_token'
# → {"message":"Successfully checked model connectivity","data":{"connectivity":true,"model_name":"deepseek-v4.1-flash"}}
curl -s -X POST "http://localhost:5010/api/model/healthcheck?display_name=bge-m3%20(本地%20ONNX%20%2F%20宿主%20GPU)&model_type=embedding" \
     -H 'Authorization: Bearer mock_access_token'
# → connectivity: true
```

注意 `display_name` 参数要传**显示名**（`DeepSeek V4.1 Flash`），传 `model_name` 会返回
`{"message":"Model configuration not found for deepseek-v4.1-flash"}`。
检测后 `connect_status` 由 `not_detected` → `available`，UI 立刻出现智能体卡片
（`is_available: true`，`unavailable_reasons: []`）。

## 5. 两张截图（本轮产物）

| 图 | 问题 | 结果 | 耗时 | 文件 |
|---|---|---|---|---|
| 正例 | 营商环境优化方面，国家有哪些上位文件？地方是怎么落实的？请给出可回查的出处。 | 跨层级链条 + 引用块清单 + 三段证据边界说明 | **73.44 s** | `evidence/09-ui-qa-cross-doc.png`（39,058 B）+ `.txt`（19,621 字符） |
| 负例 | 量子计算芯片的光刻胶配方工艺参数是多少？ | 拒答「语料库中没有找到足够依据」+ 判定表 + 可改问方向 | **33.31 s** | `evidence/10-ui-qa-insufficient.png`（200,980 B）+ `.txt`（5,074 字符） |

正例答案要点（UI 里真实渲染）：河北省优化营商环境条例（2017-11-30，省级先行立法）→
国办函〔2018〕46 号 → 国办函〔2019〕89 号 → 常州市钟楼区人大决定（2020-11-01）→
国办发〔2022〕35 号 → 媒体解读（2023-03-23，**并主动标注「该篇为媒体层级，不能作为制度依据使用」**）；
末尾附 `chunk_id / 标题 / 日期` 引用块清单，并自述边界：
「语料库中未检索到地方文件明确援引上述国务院办公厅文件条款的原文，因此本文只能按『层级 + 时间』
给出制度沿革关系，不能断言『某一地方文件即某一国家文件的直接落实文件』」。

负例答案要点：`insufficient_evidence=true`、`max_score 0.7349 < 0.75`、`grounded_rings=0`，
三项判定均不通过 ⇒ 不附引用标记、不做推测性作答，并给出可改问方向（集成电路产业政策、
关键核心技术攻关、出口管制等）。

## 6. 复跑命令

```bash
# 1) 启动带调试端口的 Windows Chrome（headless）
printf '@echo off\r\nstart "" "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --headless=new --disable-gpu --no-sandbox --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0 --user-data-dir=C:\\temp\\cdpprof about:blank\r\n' > /mnt/c/temp/cdp.bat
setsid nohup cmd.exe /c "C:\\temp\\cdp.bat" >/tmp/cdp-start.log 2>&1 &
# 2) 模型连通性（否则 UI 不显示智能体）
curl -s -X POST "http://localhost:5010/api/model/healthcheck?display_name=DeepSeek%20V4.1%20Flash&model_type=llm" -H 'Authorization: Bearer mock_access_token'
# 3) 跑一次问答并截图
uv run --python 3.10 --with websocket-client python3 tools/ui-chat-shot.py \
  --q "营商环境优化方面，国家有哪些上位文件？地方是怎么落实的？请给出可回查的出处。" \
  --shot /mnt/c/temp/chat-answer.png --text-out /tmp/chat-answer.txt --wait 240
```

## 7. 诚实边界

- 截图是**平台 UI 的真实渲染结果**（Chrome headless 访问 `http://localhost:3000/zh/newchat`），
  不是拼图、不是手绘、不是把日志排版成图片。
- 由于本机没有可用的视觉工具，截图的「内容正确性」由同一次运行抓取的 `innerText` 文本佐证，
  两者必须一起看：图证明「界面上是这样」，文本证明「文字是什么」。
- 依赖 Windows 侧 Chrome 与 CDP 调试端口；换机器需重跑 §6 第 1 步。
- 登录用的是 speed 模式的 mock 会话（无真实鉴权），这在参赛环境说明里必须如实写明。
