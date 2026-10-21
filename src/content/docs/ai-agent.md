---
title: "AI Agent 源码深度学习笔记"
date: 2026-10-21
categories:
  - "RAG 与 Agent"
tags:
  - "AI Agent"
  - "Dify"
  - "ReAct"
  - "Function Calling"
  - "MCP"
  - "源码"
---

> 基于对 **Dify** 后端 Agent 全链路源码的逐文件精读整理而成：`api/core/agent/`（`fc_agent_runner.py`、`cot_agent_runner.py`、`base_agent_runner.py`、`entities.py`、`prompt/template.py`）、`api/core/app/apps/agent_chat/app_runner.py`、`api/core/tools/tool_engine.py`、`api/core/mcp/mcp_client.py`、`api/core/memory/token_buffer_memory.py`、`api/core/workflow/nodes/agent/agent_node.py`。
>
> 阅读顺序即学习顺序：**Agent 是什么 → Function Calling 循环 → ReAct 循环 → 工具与 MCP → 记忆与上下文 → 工程化与边界**。
>
> 所有结论均标注了源文件位置（格式 `api/core/xxx.py:行号`），建议对照代码阅读。
>
> **版本基线**：Dify `1.14.2-128-g5c5a6e83e5`，精确 commit `5c5a6e83e5`，commit 时间 `2026-05-27 02:12:11 +0000`。
>
> 选 Dify 作为样本的理由：它同时实现了**两条主流 Agent 范式**（Function Calling 与 ReAct），并且把工具、插件、MCP、知识检索、记忆全部接进同一个循环里——读一份代码就能看清 Agent 的完整骨架。RAG 侧见 `agent/rag.md`、`agent/rrf.md`，另一种（Eino 图编排）实现见 `agent/opencoze-agent.md`。

---

## 〇、全景地图

```
┌────────────────────── 一个 Agent = 一个"带工具的循环" ──────────────────────┐
│                                                                            │
│   用户 query                                                               │
│       │                                                                    │
│       ▼                                                                    │
│   ┌────────────┐   记忆（history + scratchpad）                            │
│   │ 组织提示词  │◀──────────────────────────────────┐                       │
│   └─────┬──────┘                                    │                      │
│         ▼                                           │                      │
│   ┌────────────┐   工具列表（tools/MCP/知识库）       │                      │
│   │  LLM 推理   │◀───────────── _init_prompt_tools   │                      │
│   └─────┬──────┘                                    │                      │
│         ▼                                           │                      │
│   输出是什么？──── 有 tool_call ──▶ 执行工具 ──▶ 结果回灌 ─┘                  │
│         │                                                                  │
│         └── 无 tool_call（Final Answer）──▶ 结束，发 QueueMessageEndEvent    │
│                                                                            │
│   上限保护：max_iteration（默认 10，硬上限 99）→ AgentMaxIterationError      │
└────────────────────────────────────────────────────────────────────────────┘
```

**一句话定义**：**Agent = 大模型 + 工具 + 记忆 + 一个由模型自己决定何时退出的循环**。少了循环就是 Chatbot；少了工具就是纯 LLM；少了模型就退化成工作流。

Dify 用两个类承载两条范式，选型逻辑写在数据模型里：

```python
class AgentEntity(BaseModel):                     # api/core/agent/entities.py:68
    class Strategy(StrEnum):                      # :73
        CHAIN_OF_THOUGHT = "chain-of-thought"     # :78   ReAct：靠提示词约束输出格式
        FUNCTION_CALLING = "function-calling"     # :79   靠模型原生 tool_calls 字段
    provider: str
    model: str
    strategy: Strategy
    prompt: AgentPromptEntity | None = None
    tools: list[AgentToolEntity] | None = None
    max_iteration: int = 10                       # :86   默认最多 10 轮
```

---

## 第一讲 Agent 是什么：与大模型、工作流的边界

### 1.1 三者的区别

| | 纯 LLM（Chatbot） | Agent | 工作流 Workflow |
|---|---|---|---|
| 谁决定下一步 | 用户（下一句话） | **模型自己** | 开发者（画好的图） |
| 能否调外部能力 | 不能 | 能（工具/API/检索） | 能（节点） |
| 执行路径 | 单轮 | **不确定，循环** | 确定（DAG） |
| 可预测性 | 高 | 低 | 最高 |
| 适用 | 问答、写作 | 探索性、多步任务 | 固定 SOP、需审计 |

工程上的判断标准很朴素：**路径能画出来就用工作流，画不出来才用 Agent**。Dify 自己也是这个思路——它把 Agent 做成了工作流里的一个节点：

```python
class AgentNode(Node[AgentNodeData]):   # api/core/workflow/nodes/agent/agent_node.py:27
    def _run(self) -> Generator[NodeEventBase, None, None]:   # :72
```

即"**确定性的骨架里嵌一小段不确定的推理**"，这是当下最稳的落地形态。

### 1.2 Agent 的五件套

1. **模型（Brain）**：负责推理与决策
2. **工具（Tools）**：让模型能"动手"——API、数据库、检索、代码执行
3. **记忆（Memory）**：短期（本轮 scratchpad + 历史对话）+ 长期（向量库/数据库）
4. **规划（Planning）**：把大目标拆成子任务（ReAct 就是最朴素的规划）
5. **行动（Action）**：真正执行并回灌结果

---

## 第二讲 Function Calling 循环

模型原生支持 `tool_calls` 时，Dify 走 `FunctionCallAgentRunner`。核心就是这个 while：

```python
def run(self, message, query, **kwargs):                    # api/core/agent/fc_agent_runner.py:35
    ...
    iteration_step = 1
    max_iteration_steps = min(app_config.agent.max_iteration, 99) + 1   # :52  硬上限 99
    function_call_state = True                                          # :55
    while function_call_state and iteration_step <= max_iteration_steps: # :77
        function_call_state = False
        if iteration_step == max_iteration_steps:
            prompt_messages_tools = []                                  # :80  最后一轮没收工具
        prompt_messages = self._organize_prompt_messages()              # :91 之后
        self.recalc_llm_max_tokens(self.model_config, prompt_messages)   # :91  重算 token 预算
        chunks = model_instance.invoke_llm(..., tools=prompt_messages_tools, ...)  # :96
```

四个值得记住的设计点：

**① 循环退出条件是"模型不再要调工具"**（`:77` 的 `function_call_state`）。每轮开头置 False，只有解析出 tool_call 才置 True（`:123`/`:150`）——**模型自己决定何时结束**，这是 Agent 与流水线的本质差别。

**② 最后一轮强制收口**（`:80`）：到最大轮次时把工具列表清空，模型拿不到工具就只能输出文本。这一招同时解决两个问题：避免无限循环，避免最后一轮还在调工具却没有机会把结论说出来。

**③ 每轮重算 token 预算**（`:91` `recalc_llm_max_tokens`）：因为每轮都把工具结果塞回上下文（`:272`），prompt 会越来越长，必须动态压缩"留给模型的最大输出"。

**④ 观察回灌成一条 `ToolPromptMessage`**（`:272`）：

```python
self._current_thoughts.append(
    ToolPromptMessage(content=str(tool_response["tool_response"]),
                      tool_call_id=tool_call_id,
                      name=tool_call_name))
```

工具结果以 `role=tool` 且带 `tool_call_id` 的身份回到对话里——**这就是 Function Calling 协议的核心**：不是把结果当普通文本拼进去，而是严格对应到某一次调用 id，模型才知道"这个结果属于哪个调用"。

工具找不到的兜底也写得很直白（`:231-236`）：不抛异常，而是把 `"there is not a tool named xxx"` 当作**观察结果**回灌给模型，让它自己纠错重试。

收尾：

```python
if iteration_step == max_iteration_steps and tool_calls:        # :224
    raise AgentMaxIterationError(app_config.agent.max_iteration) # :225
...
self.queue_manager.publish(QueueMessageEndEvent(...))            # :310
```

---

## 第三讲 ReAct 循环（Chain-of-Thought）

模型不支持原生 tool_calls 时，只能靠**提示词约束输出格式 + 解析器**模拟同样的循环。这就是 ReAct。

模板把格式写死：

```
Respond to the human as helpfully and accurately as possible.   # api/core/agent/prompt/template.py:1
...
Valid "action" values: "Final Answer" or {{tool_names}}        # :10
...
Question: input question to answer
Thought: consider previous and subsequent steps
Action:
```
$JSON_BLOB
```
Observation: action result
... (repeat Thought/Action/Observation N times)
Thought: I know what to respond
Action:
```
{
  "action": "Final Answer",
  "action_input": "Final response to human"
}
```                                                            # :1-42
```

循环结构与 FC 几乎一致，差别在"怎么从输出里抠出动作"：

```python
while function_call_state and iteration_step <= max_iteration_steps:              # api/core/agent/cot_agent_runner.py:103
    if iteration_step == max_iteration_steps:
        self._prompt_messages_tools = []                                          # :107  同样最后一轮收口
    chunks = model_instance.invoke_llm(..., tools=[], stream=True, ...)            # :126  注意 tools=[] ！
    react_chunks = CotAgentOutputParser.handle_react_stream_output(chunks, usage_dict)  # :136  流式解析 JSON blob
    scratchpad = AgentScratchpadUnit(agent_response="", thought="", action_str="",
                                     observation="", action=None)                  # :137
    ...
    self._agent_scratchpad.append(scratchpad)                                      # :173
    if iteration_step == max_iteration_steps and scratchpad.action:                # :176
        if scratchpad.action.action_name.lower() != "final answer":                # :177
            raise AgentMaxIterationError(app_config.agent.max_iteration)           # :178
```

**关键差别**：FC 走 `tools=prompt_messages_tools`（模型返回结构化的 `tool_calls`），ReAct 走 `tools=[]` 并靠 `CotAgentOutputParser` 从**流式文本**里边收边解析出 JSON blob。

每轮的 scratchpad 最终被格式化回提示词，形成"思路链"：

```python
def _format_assistant_message(self, agent_scratchpad):   # api/core/agent/cot_agent_runner.py:372
    for scratchpad in agent_scratchpad:
        if scratchpad.is_final():
            message += f"Final Answer: {scratchpad.agent_response}"
        else:
            message += f"Thought: {scratchpad.thought}\n\n"
            if scratchpad.action_str:
                message += f"Action: {scratchpad.action_str}\n\n"
            if scratchpad.observation:
                message += f"Observation: {scratchpad.observation}\n\n"     # :372-387
```

`AgentScratchpadUnit` 的"是否结束"判定也很朴素——看动作名里有没有 final + answer：

```python
class AgentScratchpadUnit(BaseModel):     # api/core/agent/entities.py:31
    def is_final(self) -> bool:           # :59
        return self.action is None or (
            "final" in self.action.action_name.lower()
            and "answer" in self.action.action_name.lower())
```

### 策略是自动选的

```python
model_schema = llm_model.get_model_schema(model_instance.model_name, ...)   # api/core/app/apps/agent_chat/app_runner.py:181
if {ModelFeature.MULTI_TOOL_CALL, ModelFeature.TOOL_CALL}.intersection(model_schema.features or []):
    agent_entity.strategy = AgentEntity.Strategy.FUNCTION_CALLING           # :186
...
if agent_entity.strategy == AgentEntity.Strategy.CHAIN_OF_THOUGHT:          # :199
    runner_cls = CotChatAgentRunner / CotCompletionAgentRunner              # :202/:204
elif agent_entity.strategy == AgentEntity.Strategy.FUNCTION_CALLING:        # :207
    runner_cls = FunctionCallAgentRunner                                    # :208
```

即：**模型能力声明里带 `TOOL_CALL`/`MULTI_TOOL_CALL` 就自动升级成 FC，否则退回 ReAct**。所以"要不要自己写 ReAct prompt"的问题答案是——能升级就升级，FC 的稳定性显著高于文本解析。

---

## 第四讲 工具系统：从 Tool 到 MCP

### 4.1 工具是怎么塞给模型的

```python
def _init_prompt_tools(self) -> tuple[dict[str, Tool], list[PromptMessageTool]]:   # api/core/agent/base_agent_runner.py:185
    for tool in self.app_config.agent.tools or []:                                  # :192
        prompt_tool, tool_entity = self._convert_tool_to_prompt_message_tool(tool)  # :196 → :135
    for dataset_tool in self.dataset_tools:
        prompt_tool = self._convert_dataset_retriever_tool_to_prompt_message_tool(dataset_tool)  # :155  知识库也是工具
    return tool_instances, prompt_messages_tools
```

注意最后一类：**知识库检索在 Agent 眼里就是一个普通工具**（`:155`）——这正是"RAG 是 Agent 的一个工具子集"这句话的实现依据。

工具执行统一走：

```python
class ToolEngine:                      # api/core/tools/tool_engine.py:42
    def agent_invoke(...)              # :48
```

调用点分别在 `fc_agent_runner.py:239`（FC）与 `cot_agent_runner.py:222`（ReAct 走 `_handle_invoke_action`，`:287`）。

### 4.2 MCP：工具的"USB-C 接口"

没有 MCP 时，N 个 Agent 框架 × M 个工具 = N×M 套适配代码。MCP 把它变成 N+M：工具方按协议暴露一次，所有支持 MCP 的客户端都能直接用。

Dify 的 MCP 客户端：

```python
class MCPClient:                                        # api/core/mcp/mcp_client.py:17
    session_context = ClientSession(*streams)           # :88   建立会话
    self._session.initialize()                          # :90   握手（能力协商）
    def list_tools(self) -> list[Tool]:                 # :92   拉取工具清单
    def invoke_tool(self, tool_name, tool_args) -> CallToolResult:   # :99   调用
```

**MCP 的三个原语**：`tools`（可被模型调用的函数）、`resources`（可被读取的数据）、`prompts`（预置提示词模板）。传输层用 JSON-RPC over stdio 或 HTTP+SSE。

对 Agent 的意义：**工具发现变成动态的**（`list_tools` 拉回来再转成 `PromptMessageTool`），不再需要为每个工具写死 Schema。

---

## 第五讲 记忆与上下文

### 5.1 短期记忆：token 预算制

```python
class TokenBufferMemory:                                       # api/core/memory/token_buffer_memory.py:30
    def get_history_prompt_messages(self, max_token_limit: int = 2000, ...):   # :122  默认 2000 token
        ...
        if curr_message_tokens > max_token_limit:              # :202
            while curr_message_tokens > max_token_limit and len(prompt_messages) > 1:  # :203
                prompt_messages.pop(0)                          # 从最旧的消息开始丢
```

三个结论：

1. **上下文是预算，不是无限容器**：默认只保留 2000 token 的历史
2. **溢出策略是从旧到新丢**（`:203`），代价是"忘了开头说了什么"——这就是"多轮对话聊久了就失忆"的根因
3. **每轮 `recalc_llm_max_tokens`**（`fc_agent_runner.py:91`）与它配合，保证 prompt + 预留输出不超模型窗口

`AgentEntity` 侧还有一份"本轮的思考记录"（`_current_thoughts` / `_agent_scratchpad`），它是**跨轮次的工作记忆**，只活在一次请求内，请求结束即丢弃。

### 5.2 长期记忆与上下文工程

- **长期记忆**：把历史摘要/用户画像/关键事实落到数据库或向量库，按需检索注入
- **上下文工程（Context Engineering）**：比提示词工程更上位的概念——**在有限的 token 预算里，动态决定"这一轮该把什么放进上下文"**（历史消息、工具描述、检索结果、示例）
- 常见手段：滑动窗口（Dify 的做法）、摘要压缩、向量检索按需注入、工具描述按需加载（工具多了也吃 token）

---

## 第六讲 工程化：上限、可观测、成本与安全

### 6.1 必须设的三道闸

| 闸 | 实现 | 位置 |
|---|---|---|
| 轮次上限 | `min(max_iteration, 99) + 1`，超出抛 `AgentMaxIterationError` | `fc_agent_runner.py:52`、`:224` |
| 最后一轮收口 | 清空 tools 强制输出文本 | `fc_agent_runner.py:80`、`cot_agent_runner.py:107` |
| Token 预算 | 每轮重算 max_tokens；历史按 2000 token 窗口截断 | `fc_agent_runner.py:91`、`token_buffer_memory.py:202` |

**没有闸的 Agent 在生产里一定会烧钱**：一个死循环就能把几万 token 烧光。

### 6.2 可观测：每一步都要能回放

Dify 把每一次"思考/动作/观察"都落库并推事件：

```python
agent_thought_id = self.create_agent_thought(...)   # api/core/agent/fc_agent_runner.py:85
self.save_agent_thought(..., observation={...}, llm_usage=current_llm_usage)  # :206 / :281
self.queue_manager.publish(QueueAgentThoughtEvent(agent_thought_id=...))       # :174 / :297
```

`agent_thought` 表就是 Agent 的"调用链追踪"：哪一轮调了什么工具、入参是什么、返回了什么、花了多少 token。**没有它，Agent 出问题只能靠猜**。

### 6.3 成本：钱花在哪

- **每轮都重发完整上下文**（历史 + scratchpad + 工具描述）→ 轮次越多越贵，近似 O(轮次²)
- **工具描述本身吃 token**：接了 20 个工具，光 Schema 就上千 token
- 优化：减少轮次（更好的提示词/更好的工具描述）、按需加载工具、缓存检索结果、小模型做路由

### 6.4 安全四条

1. **工具最小权限**：只给 Agent 完成任务必需的工具（Dify 的 `AgentToolEntity` 逐工具配凭证，`api/core/agent/entities.py:10`）
2. **参数校验到运行时**：模型生成的 SQL/命令必须在执行前校验表名、库名、路径白名单
3. **防提示词注入**：检索回来的网页/文档内容是不可信输入，必须当作数据而非指令
4. **人工确认（Human-in-the-loop）**：高风险动作（付款、删数据）必须二次确认

---

## 七、面试高频 Q&A

24 道最高频题的「一句话答案」。先报结论占住阵地，面试官真追问时，按「深入」列展开到对应讲次。

| # | 问题 | 答案（先给结论） | 深入 |
| --- | --- | --- | --- |
| 1 | 什么是 AI Agent？ | **大模型 + 工具 + 记忆 + 由模型自己决定何时退出的循环**；少了循环是 Chatbot，少了工具是纯 LLM | 〇 |
| 2 | Agent 和大模型的区别？ | 大模型只能"说"，Agent 能"做"——调用工具改变外部世界，并根据结果继续推理 | 〇 |
| 3 | Agent 与工作流的区别？ | 工作流路径确定（DAG），Agent 路径由模型决定；能画出流程图就用工作流 | 第一讲 |
| 4 | Agent 有哪几个核心模块？ | 模型、工具、记忆（短/长期）、规划、行动 | 第一讲 |
| 5 | 什么是 ReAct？ | **Reasoning + Acting 交替**：Thought → Action → Observation 循环，直到输出 Final Answer | 第三讲 |
| 6 | Function Calling 是什么？ | 模型原生输出结构化 `tool_calls`，框架执行后把结果以 `tool_call_id` 对应回灌 | 第二讲 |
| 7 | FC 与 ReAct 哪个好？ | 能用 FC 就用 FC：结构化字段比从文本里解析 JSON blob 稳定得多；Dify 会按模型能力自动升级（`app_runner.py:186`） | 第三讲 |
| 8 | Agent 怎么知道该结束了？ | 模型不再产出工具调用（FC 的 `function_call_state`，`fc_agent_runner.py:77`）或产出 Final Answer（`:177-178` 判定） | 第二讲 |
| 9 | Agent 死循环怎么办？ | 三道闸：轮次上限（默认 10、硬上限 99，`fc_agent_runner.py:52`）、最后一轮清空工具强制收口（`:80`）、超轮抛 `AgentMaxIterationError`（`:225`） | 第六讲 |
| 10 | 工具调用失败怎么处理？ | 不抛异常，把错误信息当作 **Observation** 回灌给模型让它自我纠错（`fc_agent_runner.py:231-236`） | 第二讲 |
| 11 | 什么是 MCP？为什么需要它？ | 模型上下文协议，工具的"USB-C"：把 N×M 适配变成 N+M；三个原语 tools/resources/prompts（`mcp_client.py:92`/`:99`） | 第四讲 |
| 12 | MCP 相比普通 Function Calling 多了什么？ | **动态发现**（`list_tools`）与跨框架复用；FC 的 Schema 是写死在每个应用里的 | 第四讲 |
| 13 | A2A / 多 Agent 协作是什么？ | Agent 之间用协议互调（一个 Agent 把另一个当工具）；适合复杂任务拆分，但调试与成本翻倍 | 第一讲 |
| 14 | 知识库检索在 Agent 里是什么角色？ | 就是一个普通工具——Dify 把 dataset retriever 转成 `PromptMessageTool`（`base_agent_runner.py:155`） | 第四讲 |
| 15 | RAG 和 Agent 的区别？ | RAG 是"检索增强生成"，解决知识问题；Agent 是"循环 + 工具"，解决行动问题。RAG 常作为 Agent 的一个工具 | 第四讲 |
| 16 | Agent 的记忆怎么做？ | 短期：本轮 scratchpad + 历史窗口（Dify 默认 2000 token，超限从旧到新丢，`token_buffer_memory.py:202`）；长期：摘要/画像落库按需检索 | 第五讲 |
| 17 | 什么是上下文工程？ | 在有限 token 预算里动态决定"这一轮放什么进上下文"（历史、工具、检索、示例），比提示词工程更上位 | 第五讲 |
| 18 | 为什么多轮对话会"失忆"？ | 历史按 token 窗口截断，超出部分从最旧的消息开始丢（`token_buffer_memory.py:203`） | 第五讲 |
| 19 | Agent 为什么贵？钱花在哪？ | 每轮重发完整上下文 → 近似 O(轮次²)；工具 Schema 本身也吃 token | 第六讲 |
| 20 | 怎么降低 Agent 成本？ | 减少轮次、按需加载工具、检索缓存、小模型路由、更短的工具描述 | 第六讲 |
| 21 | 什么是提示词注入？怎么防？ | 外部内容（网页/文档）被当作指令执行；防御：外部内容一律当数据、工具最小权限、敏感操作人工确认 | 第六讲 |
| 22 | Agent 输出不稳定怎么排查？ | 靠 `agent_thought` 记录回放每一步（工具、入参、返回、token），`fc_agent_runner.py:85`/`:206`/`:281` | 第六讲 |
| 23 | 幻觉能根治吗？ | 不能，只能缓解：接检索给证据、要求引用来源、工具校验事实、降低 temperature、关键结论二次校验 | 第六讲 |
| 24 | 什么时候不该用 Agent？ | 路径确定、要求可审计/低延迟/低成本的场景——直接上工作流或普通代码 | 第一讲 |

---

## 八、易错点清单

1. **把 Agent 当万能**：能用确定性代码/工作流解决的别上 Agent，成本与不确定性都高一个量级
2. **不设轮次上限**：一个循环烧掉几万 token（`fc_agent_runner.py:52` 的 99 是最后防线，业务侧应更小）
3. **把工具错误直接抛给前端**：应回灌成 Observation 让模型纠错（`fc_agent_runner.py:231-236`）
4. **工具 Schema 写得含糊**：模型靠描述决定调不调、怎么调，描述质量直接决定命中率
5. **工具接太多**：Schema 吃 token 且模型选择困难，按需加载
6. **忽略 `tool_call_id` 对应关系**：多工具并发调用时结果会串
7. **以为历史消息会一直保留**：实际按 token 预算从旧到新丢弃（`token_buffer_memory.py:203`）
8. **把检索内容当可信指令**：提示词注入的主要入口
9. **没有 `agent_thought` 级别的可观测**：线上问题无法复现
10. **盲目追求 FC**：模型不支持时 ReAct 是可用退路，但要做好解析容错（`cot_agent_runner.py:136`）

---

## 九、数值速记

| 数值 | 含义 | 出处 |
| --- | --- | --- |
| `10` | `AgentEntity.max_iteration` 默认值 | `api/core/agent/entities.py:86` |
| `99` | 轮次硬上限（`min(max_iteration, 99) + 1`） | `api/core/agent/fc_agent_runner.py:52` |
| `2000` | 历史消息默认 token 上限 | `api/core/memory/token_buffer_memory.py:122` |
| `2` | Agent 策略数（chain-of-thought / function-calling） | `api/core/agent/entities.py:78-79` |
| `5` | Agent 五件套（模型/工具/记忆/规划/行动） | 概念 |

---

## 十、复习卡

- [ ] 能一句话定义 Agent，并说清它与 Chatbot、工作流的边界
- [ ] 能默写出 FC 循环的骨架（while → 组织提示词 → 调 LLM → 有 tool_call 就执行并回灌 → 否则结束）
- [ ] 能说出"最后一轮清空工具"这一招解决了什么问题
- [ ] 能说出 ReAct 的 Thought/Action/Observation 格式与解析器位置
- [ ] 能说出 FC 与 ReAct 的本质差异与自动切换的判定条件
- [ ] 能说出 MCP 解决了什么规模问题、三个原语是什么
- [ ] 能说出短期记忆的 token 窗口策略与溢出行为
- [ ] 能说出 Agent 成本的来源与四种降本手段
- [ ] 能说出三道安全闸（最小权限、运行时校验、注入防御、人工确认）
- [ ] 能说出可观测需要记录哪些字段

---

## 十一、文件索引

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `api/core/agent/entities.py` | 94 | `AgentEntity`（策略、max_iteration）、`AgentScratchpadUnit` |
| `api/core/agent/fc_agent_runner.py` | 469 | Function Calling 主循环 |
| `api/core/agent/cot_agent_runner.py` | 443 | ReAct 主循环与 scratchpad 格式化 |
| `api/core/agent/base_agent_runner.py` | 478 | 工具初始化、历史组织、thought 落库 |
| `api/core/agent/prompt/template.py` | 105 | ReAct 提示词模板 |
| `api/core/app/apps/agent_chat/app_runner.py` | 220+ | 按模型能力选择 FC / CoT runner |
| `api/core/tools/tool_engine.py` | — | `ToolEngine.agent_invoke`（`:48`） |
| `api/core/mcp/mcp_client.py` | — | MCP 会话、list_tools（`:92`）、invoke_tool（`:99`） |
| `api/core/memory/token_buffer_memory.py` | — | 历史窗口与 token 截断（`:122`/`:202`） |
| `api/core/workflow/nodes/agent/agent_node.py` | — | 工作流里嵌 Agent 节点（`:27`/`:72`） |
