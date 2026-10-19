# Agent 契约

v4.2 的 1.3 要求一份 `agents/base.py` 与 `README_BASE_AGENT.md`，契约是
`run(state) -> state`。**那个契约与本仓库不兼容**，照它实现会让评审门禁拿到
整个 state、断言解析成空、直接回滚。本文件描述的是**真实**契约。

没有 `base.py`：Agent 是普通函数，不是基类的子类。要加一个 Agent，写一个符合
下面契约的函数并挂到 `default_agents()` 即可。

---

## 签名

```python
def my_agent(inputs: dict) -> dict:
    ...
```

## 输入

`chain.py:_invoke_agent` 构造 `inputs`，键固定为这七个：

| 键 | 类型 | 说明 |
|---|---|---|
| `user_input` | `str` | 原始需求 |
| `timestamp` | `str` | ISO-8601，整条链共用 |
| `state` | `dict` | **副本**。见下方「为什么是副本」 |
| `llm_client` | 可调用或 `None` | 已计量的 LLM 客户端 |
| `stage` | `str` | 当前阶段，如 `"S5"` |
| `agent` | `str` | 自己的名字，用于领域 Prompt 注入 |
| `artifacts` | `dict` | **副本**，键为已完成 Agent 的名字 |

## 输出

返回一个 `dict`。它会被放进 `context["artifacts"][agent_name]`，供后续 Agent
通过 `inputs["artifacts"]` 读取。

三条硬性要求：

1. **必须返回 `dict`**。返回别的东西会抛 `TypeError`。
2. **不能返回 `{"error": ...}`**。那会被当成失败抛出 `RuntimeError`。要降级就
   返回正常结构并在自己的字段里说明。
3. **不要依赖副作用**。`state` 与 `artifacts` 都是副本，就地修改不会传播。

## 为什么是副本

`state` 是 `dict(context["state"])`，`artifacts` 同理。这样单个 Agent 无法
悄悄改变其他 Agent 看到的世界——**跨 Agent 的通信必须走返回值**。v4.2 的
`run(state) -> state` 契约会破坏这一点：任何 Agent 都能改任意字段，失败时的
归因也就无从谈起。

副作用不传播的代价是「想加一个全局配置」不能靠改 `state`。正确做法是让链在
调用前把它放进 `state`（例如 `run_chain` 会填好 `capability_bus` 与
`registry`），Agent 只读。

## 最小示例

```python
def echo_requirements(inputs: dict) -> dict:
    text = inputs["user_input"]
    return {"requirements_md": f"## 需求\n\n{text}\n"}
```

挂载：

```python
# runtime/chain.py
def default_agents() -> dict:
    return {
        ...
        "echo_requirements": echo_requirements,
    }
```

## 出错时怎么办

- **可选 Agent**：返回正常结构，把降级信息放在自己的字段里。链会继续。
- **门禁 Agent**（`tdd_reviewer`、`e2e_validator`）：必须 fail-closed。拿不准就
  判失败——一个拿不准时放行的门禁不是门禁。
- **真的无法继续**：抛异常。链会把它记进 `errors` 并停下，而不是带着半个
  结果继续往下走。

## 领域 Prompt

如果当前领域的声明里 `prompts` 有你这个 Agent 的名字，链会在调用 LLM 前自动
把它前置。你不需要知道领域的存在，也不要自己去读 `domain_config`。

```json
{"prompts": {"tdd_coder": "面向数字 IC 设计：产出可综合的 Verilog-2001 模块。"}}
```

## 检查清单

新增一个 Agent 时：

- [ ] 返回 `dict`，且不含 `error` 键
- [ ] 不依赖对 `state` / `artifacts` 的就地修改
- [ ] 已在 `default_agents()` 挂载（`AGENTS_MANIFEST.md` 的「未挂载」应保持 0）
- [ ] 有测试覆盖，且至少有一条断言它**在真实链路上**被调用
- [ ] 若是门禁：有一条测试证明它会**真的拦下**错误输入
