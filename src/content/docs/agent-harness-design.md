---
title: "Agent 后端架构设计指南：领域层、应用层与适配层"
date: 2026-11-04
categories:
  - "RAG 与 Agent"
tags:
  - "Agent"
  - "架构设计"
  - "依赖倒置"
  - "领域层"
  - "应用层"
  - "适配层"
  - "组装根"
---

> 本文整理自一段「如何设计可扩展的 Agent 后端」的设计讲解（口播稿 + AI 结构化总结），并用 **Dify** 后端真实源码逐条做对照实证——每一个设计主张都能落到 `api/core/` 下的具体行号，而不是停留在原则口号。
>
> 核心命题：**Agent 看起来只是一个循环，但把它做成一个能在模型快速演进中保持可扩展的系统，需要一层「系统自己的语言」**。这套三��设计（领域层 / 应用层 / 适配层 + 组装根）不只适用于 Agent，可以移植到任何「内部编排 + 外部依赖」的系统。
>
> 配套阅读：Agent 的运行时机制（FC 循环 / ReAct / 工具与 MCP / 记忆）见 `agent/ai-agent.md`；本文只讲**结构**，那篇讲**机制**。

---

## 〇、全景地图

```
┌────────────────────── 三层架构：适配层和应用层为领域层而生 ──────────────────────┐
│                                                                                   │
│  ┌───────────────────────────────────────────────────────────────────────────┐   │
│  │  领域层 Domain —— 定义系统自己的语言（什么是对话/工具/上下文）              │   │
│  │                                                                           │   │
│  │    Session          Tool              Context                            │   │
│  │    (id/messages)    (name/desc/       (instruction/tools/                │   │
│  │                     param/execute)     history/output_type)              │   │
│  │                                                                           │   │
│  │    UserMessage / AssistantMessage / ToolCall / ToolResult                │   │
│  └───────────────▲──────────────────────────────▲──────────────────────────┘   │
│                  │ 依赖（只认接口）              │ 依赖                          │
│  ┌───────────────┴──────────────────────────────┴──────────────────────────┐   │
│  │  应用层 Application —— 编排领域概念，决定何时循环、何时结束                 │   │
│  │                                                                           │   │
│  │    SessionStore(接口)      ModelClient(接口)     AgentService            │   │
│  │    Assistant(角色)         ToolCallReviewer(角色)                          │   │
│  │    Max iterations 上限                                                   │   │
│  └───────────────▲──────────────────────────────────────────────────────────┘   │
│                  │ 实现接口                                                       │
│  ┌───────────────┴──────────────────────────────┐                            │
│  │  适配层 Adapter —— 把外部事物翻译成系统语言     │                            │
│  │    JSONLSessionStore   OpenAIClient           │                            │
│  │    AnthropicClient     GoogleClient           │                            │
│  └──────────────────────────────────────────────┘                            │
└───────────────────────────────────────────────────────────────────────────┘

  组装根 Composition Root —— 唯一知道所有具体类的地方；换模型/存储/入口只改这一处
```

**一句话定义**：**领域层定义"我们说的到底是什么"，应用层决定"接下来做什么"，适配层负责"跟外面怎么打交道"，组装根负责"把它们接起来"**。

### 为什么不能一把梭

如果不分层，这个流程就是一个函数——或者更糟，是散落在七八个地方的 if。代价立刻出现：

| 需求 | 一把梭的代价 |
| --- | --- |
| 把模型从 OpenAI 换成 Anthropic 协议 | 同时修改「组织上下文」「发请求」「解析回复」多处 |
| 单元测试流程 | 必须真正调用模型——又花钱又慢，还不稳定 |
| 换一个存储（JSONL → 数据库） | 业务流程里混进序列化代码 |
| 加一个 CLI 入口 | 在业务逻辑里写命令行解析 |

分层的价值不是"好看"，而是**把"变化"关进笼子里**：模型协议变了只动适配层，存储变了只动适配层，入口变了只动适配层；应用层和领域层纹丝不动。

---

## 第一讲 领域层：先造一门自己的语言

领域层是整个系统的**本质**。"如何定义领域，就是一个系统的本质"——同一个 Agent 后端，领域层把它建模成"对话 + 消息"，还是建模成"事件流 + 状态机"，后面所有代码都不一样。

### 1.1 Session：整个系统最重要的概念

系统的表现形式就是对话，所以第一个要定义的是「对话」。材料给出的定义非常克制：

```python
class Session:
    id: str                    # 对话 ID，方便之后复用这次对话
    messages: list[Message]    # 消息列表，会随对话增长

    def append(self, message: Message) -> None: ...
```

只有两个字段加一个方法。这个「够用即止」很重要——领域模型是为**表达业务**服务的，不是为写满字段服务的。

### 1.2 messages 里到底装什么

`messages` 是三种消息的列表，这个三分法是整套设计的承重墙：

```python
class UserMessage(Message):      # 用户发的
    content: str

class AssistantMessage(Message): # 助手回的
    content: str
    tool_calls: list[ToolCall]   # 除了文本，还可能带工具调用

class ToolCall:
    id: str            # 调用的唯一标识
    name: str          # 调哪个工具
    arguments: dict    # 传什么参数

class ToolResult:
    tool_call_id: str  # 说明这是哪次调用的结果
    content: str       # 执行结果
```

三个设计点值得单独拎出来：

**① 工具调用和工具结果必须成对，且靠 ID 关联。** 不能靠"顺序"或"最近一次"来配对——一次回复里可以并发调多个工具（材料里 Agent 支持并行工具调用），只能靠 `tool_call_id` 精确对应。少一个 ID 字段，模型并行调用时结果就会串。

**② 工具调用属于「助手消息」的一部分，不是独立消息。** 因为它由模型在同一轮里产生。把它做成独立消息类型会导致历史回放时丢失"哪几个调用是同一轮发起的"这层语义。

**③ 工具结果是一种消息，会进入历史。** 模型下一轮要看到它，否则无法基于结果继续推理。这条决定了循环的形态：结果必须回灌进上下文。

> **Dify 对照**：这个三分法在 Dify 里落到了数据库模型上——`Message` 表存对话消息（`api/models/model.py:1393`），`conversation_id` 外键指向 `Conversation`（`api/models/model.py:1044`/`:1411`），而工具调用/工具结果这类「思考过程」单独落一张 `MessageAgentThought` 表（`api/models/model.py:2316`），用 `position` 字段记录它在一轮里的序号（`:2328`），`thought` 与 `observation` 两列分别存模型的思考和工具的返回（`:2332`/`:2337`）。**一个会话两张表**，而不是把所有东西塞进一张 messages 表——这是真实系统对「三分法」的一次修正：对话正文与工具轨迹的生命周期不同，前者要展示给用户，后者只用于回放与排查。

### 1.3 Tool：可以被执行才是工具

```python
class Tool:
    name: str                    # 模型据此选择工具
    description: str             # 模型据此理解用途
    parameter: ToolParameter     # 模型据此知道传什么参数
    def execute(self, arguments) -> str: ...   # 返回 ToolResult.content
```

前三个字段是**给模型看的**（上下文里的工具清单），`execute` 是**给系统看的**（第六步真的执行它）。这个"同一对象、两种用途"的划分，正是工具能被模型发现、又能被系统执行的原因。

> **Dify 对照**：Dify 的工具定义在 `AgentToolEntity`（`api/core/agent/entities.py:9`），执行走 `ToolEngine.agent_invoke`（`api/core/tools/tool_engine.py:48`）。注意执行这一步的入参里带着 `tool_parameters`、`user_id`、`tenant_id`、`message`、`invoke_from`、`app_id`——**工具执行需要的不只是模型给的参数，还有当前会话与租户上下文**。材料里 `execute(arguments)` 只描述了最小形态，真实系统必须把上下文一起带进去。

### 1.4 Context：一次调用的全部输入

```python
class Context(Generic[OutputType]):
    instruction: str          # 指令：这次要做什么、遵守什么规则
    tools: list[Tool]         # 可用工具
    history: list[Message]    # 历史信息
    output_type: type[OutputType]   # 规定模型要返回什么类型
    # 可选：memory、agent_md 等
```

**用泛型定义输出类型**，是这个设计里最优雅的一笔。因为同一个系统有两类角色：

- 回复用户的角色，输出 `AssistantMessage`
- 审核工具调用的角色，输出 `Decision`

有了 `Context[OutputType]`，两个角色可以共用同一个 `Context` 类型、同一个 `ModelClient` 接口，输出类型由调用方指定。

> **Dify 对照**：Dify 的对应物是 `PromptMessage` 体系——`base_agent_runner.py:350` 的 `organize_agent_history` 把历史整理成 `list[PromptMessage]`，工具则被转成 `PromptMessageTool`（`base_agent_runner.py:135`/`:155`）。而「输出类型」在 Dify 里是**按策略分派**的：`CotAgentOutputParser.handle_react_stream_output`（`api/core/agent/output_parser/cot_output_parser.py:12`）解析 ReAct 的文本输出，`fc_agent_runner` 则直接读模型原生的 `tool_calls` 字段（`api/core/agent/fc_agent_runner.py:102`）。Dify 没用泛型，而是用两个 Runner 类 + 一个 `Strategy` 枚举（`api/core/agent/entities.py:73`）达到同样效果——**泛型与策略枚举是同一问题的两种解法，前者编译期检查，后者运行时可切换**。

### 1.5 两个角色：为什么需要"审核"这个角色

材料定义了两个角色，各自负责生成自己的 Context：

| 角色 | 有 instruction | 有 tools | 输入 | 输出类型 |
| --- | --- | --- | --- | --- |
| `Assistant` | ✅ | ✅ | `session` | `AssistantMessage` |
| `ToolCallReviewer` | ✅ | ❌ | `session` + 待审的 `tool_call` | `Decision` |

`Decision` 是新定义的一个类型，包含两样东西：**这次调用能不能执行** + **拒绝的原因**。被拒绝时，这个原因会作为 `ToolResult.content` 交还给模型——模型于是知道自己为什么没拿到结果，可以换一种方式。

**审核者不需要工具**，这一点是刻意的：审核的角色是"判断"，给它工具等于让它自己动手，越权了。

> **Dify 对照**：Dify **没有**独立的审核角色——它走的是另一条路：把工具调用直接执行，把错误当 Observation 回灌让模型自我纠正（`fc_agent_runner.py:231-235`：`"there is not a tool named {tool_call_name}"`）。两种设计的取舍很清楚：
>
> | 方案 | 优点 | 代价 | 适合 |
> | --- | --- | --- | --- |
> | 审核者模式（材料） | 危险工具在执行前被拦下；可换更便宜更快的模型当审核者 | 每轮多一次模型调用；审核者本身可能被绕过 | 有 Bash、写文件等高危工具 |
> | 事后纠错（Dify） | 少一次调用、延迟低 | 危险命令**已经执行完了** | 工具只读或沙箱内 |
>
> Dify 敢用事后纠错，是因为它的工具跑在插件沙箱里；而一个给模型 Bash 权限的系统（比如 Claude Code 这类），几乎必须做执行前审核。

---

## 第二讲 应用层：编排与接口

应用层做两件事：**编排流程**（什么时候继续循环、什么时候结束）和**定义接口**（应用层需要什么外部能力，但不关心谁来实现）。

### 2.1 Agent Service：编排者

```python
class AgentService:
    def __init__(
        self,
        model_client: ModelClient,      # 接口，不是实现
        session_store: SessionStore,    # 接口，不是实现
        assistant: Assistant,
        reviewer: ToolCallReviewer,
        assistant_settings: ModelSettings,   # 两个角色可配不同模型
        reviewer_settings: ModelSettings,    # 审核者可用更便宜更快的模型
        max_iterations: int,                 # 防死循环
    ): ...
```

注意两个角色的 `ModelSettings` 是**分开的**——因为它们干的活难度不同、成本差几个数量级。用同一个模型跑审核是浪费。

### 2.2 两个接口：依赖倒置的落点

```python
class SessionStore(Protocol):
    def list_sessions(self) -> list[Session]: ...
    def save(self, session: Session) -> None: ...
    def get(self, session_id: str) -> Session | None: ...

class ModelClient(Protocol):
    def generate(
        self,
        context: Context[OutputType],
        settings: ModelSettings,
    ) -> OutputType: ...
```

**接口放在应用层，实现放适配层**——这是材料里最容易被误解的一点，材料专门停下来解释了：

> "我们之前不是说和外部的翻译要放到适配层吗？怎么这个 Session Store 放到应用层了？答案是 Session Store 本身只是一个接口，它说明应用层需要什么，真正的实现放在适配层。"

这就是**依赖倒置**：应用层不依赖适配层，适配层依赖应用层定义的接口。带来两个直接好处：

1. **换技术实现只改适配层**——把 JSONL 换成 PostgreSQL，`AgentService` 一行不用动
2. **测试可以换假实现**——`FakeSessionStore` / `StubModelClient`，测试流程不需要真调模型

材料那句"如果还是不太理解，没有关系，往后看你会越来越理解这一点"其实是所有讲 DDD 的人都会说的话——**依赖倒置的直觉通常来自"测试时能不能塞个假的进去"**。

> **Dify 对照**：Dify 的 `ModelManager.get_model_instance`（`api/core/model_manager.py:469`）扮演的就是 `ModelClient` 适配器的角色，而 `ProviderManager`（`api/core/provider_manager.py:64`）的文档注释把设计意图写得很直白：
>
> > "The runtime adapter is injected by the composition layer so this class stays focused on configuration assembly instead of constructing plugin runtimes."
> > （运行时适配器由组合层注入，所以这个类只专注于配置装配，而不是构造插件运行时。）
>
> 这正是「适配层实现接口、组装根负责注入」的白纸黑字版。Dify 的 `ModelManager` 还额外说明了依赖倒置的一个**陷阱**：它默认关闭凭证缓存（`enable_credentials_cache=False`，`model_manager.py:459`），因为缓存会在 API Key 变更后返回**过期凭证**；注释明确警告「不要把它当成进程级长生命周期对象共享」（`model_manager.py:442-447`）。**接口干净不代表实现可以随意复用**——实现自身的生命周期约束同样要写进契约。

### 2.3 ModelSettings：把"思考深度"收成自己的概念

```python
class ModelSettings:
    model: str        # 用哪个模型
    effort: int       # 思考多深（内部定义 6 档）
```

两个参数总是成对出现，所以包在一起。**关键在于 `effort` 是系统自己的概念**——材料定义 6 档，翻译到各厂商时是：

| 内部概念 | OpenAI | Anthropic | Google |
| --- | --- | --- | --- |
| `effort`（6 档） | `reasoning_effort` | `output_config.effort` | `thinking_level` |

**"内部一个 `effort`，外部三个名字"** 是适配层的典型职责。如果不翻译，模型协议一变，领域层和应用层全要跟着改——分层就白分了。

> **Dify 对照**：Dify 的模型参数体系放在 `api/core/entities/parameter_entities.py` 与 `api/models/agent_config_entities.py`，但思考深度的落地并不在 Agent 主链路上——而是在工作流的 Agent v2 节点里，由 `runtime_request_builder.py:280` 的 `_build_output_config` 构造输出配置。这说明一个现实：**当"内部概念 → 厂商参数"的映射涉及深度思考这类新特性时，它往往会先在旁路（新节点）落地，再回填到主链路**。统一抽象是长期工作，不是一次重构能完成的。

### 2.4 循环的收口条件

材料给出的循环退出条件有两个：

1. 模型的响应中**不再有工具调用** → 正常结束
2. **循环次数达到上限** → 强制结束

此外还有一条材料没细说但实现上必须有的：**审核不通过时，把拒绝原因作为工具结果回灌**，然后继续循环——因为模型可能换个方式重试。

> **Dify 对照**：Dify 的三道闸是本仓库里最值得反复看的实现（`api/core/agent/fc_agent_runner.py`）：
>
> | 闸 | 实现 | 行号 |
> | --- | --- | --- |
> | 轮次上限 | `max_iteration_steps = min(app_config.agent.max_iteration, 99) + 1` | `:52` |
> | 最后一轮强制收口 | `if iteration_step == max_iteration_steps:` 时清空工具 | `:80` |
> | 超轮抛错 | `AgentMaxIterationError` | `:8`/`:225` |
>
> 注意 `min(..., 99)` 这个细节：即使业务配了 `max_iteration=1000`，**框架层也硬砍到 99**。这是"领域概念"与"外部现实"必须在适配层做翻译的另一个例子——和 `effort` 翻译是同一类动作。

---

## 第三讲 适配层：翻译，而不是包装

适配层的职责一句话说清：**把系统外部的东西翻译成我们自己的语言**。

### 3.1 存储适配：JSONLSessionStore

```python
class JSONLSessionStore(SessionStore):
    def __init__(self, path: str): ...   # 相比接口多一个「文件路径」字段
    def save(self, session: Session) -> None:
        # Session 对象 → 一行 JSON
```

翻译的方向是双向的：系统里是 `Session` 对象，存到 JSONL 文件里是一行行 JSON，存进数据库是表里一条条记录。**同一份领域对象，在不同存储里形态完全不同**——这正是需要适配层的原因。

> **Dify 对照**：Dify 的持久化是关系库，但领域对象与库表之间同样隔着一层映射：`Conversation`（`api/models/model.py:1044`）与 `Message`（`:1393`）是两个独立的模型类，`Message.conversation_id` 用外键指向会话（`:1411`），消息内容用 `LongText` 存文本、`_inputs` 用 JSON 列存结构化输入（`:1412`）。而应用层拿到的是 `TokenBufferMemory`（`api/core/memory/token_buffer_memory.py:30`）这样的**内存视图对象**，不是 ORM 实体。三层之间的关系是：**ORM 模型（适配） ←→ 领域对象（领域） ←→ 内存视图（应用）**。

### 3.2 模型适配：一个内部概念，三种协议

```python
class OpenAIClient(ModelClient):
    def generate(self, context, settings) -> OutputType:
        # Context → OpenAI 请求格式；OpenAI 响应 → OutputType

class AnthropicClient(ModelClient): ...
class GoogleClient(ModelClient): ...
```

三家的请求格式都不太一样，翻译点至少有四处：

| 翻译维度 | 内部 | OpenAI | Anthropic | Google |
| --- | --- | --- | --- | --- |
| 思考深度 | `effort`（6 档） | `reasoning_effort` | `output_config.effort` | `thinking_level` |
| 工具声明 | `Tool(name, description, parameter)` | `tools: [{type:"function", function:{...}}]` | `tools: [{name, description, input_schema}]` | `function_declarations` |
| 历史消息 | `list[Message]` | `messages: [{role, content}]` | `messages: [{role, content}]` + 独立 `system` | `contents: [{role, parts}]` |
| 工具结果 | `ToolResult` | `role:"tool", tool_call_id` | `tool_result` content block | `function_response` part |

**消息角色的映射也不是一一对应**：Anthropic 把 system 提为顶层参数而非消息，Google 把内容拆成 `parts` 数组。这是适配层最脏也最必要的活。

> **Dify 对照**：Dify 的模型适配被推到了**插件运行时**那一层——`api/core/plugin/` 下有 `entities/`、`impl/`、`endpoint/`，模型由插件 daemon 承载。这意味着 Dify 面对的正是"新协议不断出现"的问题，它的答案是**把协议差异关进插件机制，而不是写进 Agent 主链路**。这比材料里"再加一个 `GoogleClient` 类"更进化了一步：**适配层的扩展点本身也被做成了可插拔的**。`api/core/mcp/mcp_client.py:48-49` 是另一个同类证据——MCP 的传输协议（`streamablehttp_client` / `sse_client`）被收进一张字典 `connection_methods`，按 URL 路径末段自动选择（`:57-64`），新传输方式只需往字典里加一项。

### 3.3 前端适配：应用层不关心入口长什么样

前端要的功能（新建对话、列出对话、发送信息）都在 `AgentService` 里定义。至于前端是命令行还是网页，**应用层并不关心**：

- 网页前端 → 适配层提供一个 HTTP 接口，把请求转成对 `AgentService` 的调用
- 后来新增 CLI → 再加一个适配器

同一个 `AgentService`，N 个前端适配器。

---

## 第四讲 组装根：唯一知道具体类的地方

三层写完之后还差最后一步：**把它们组装起来**。

现在各层还只通过抽象打交道——`AgentService` 只认识 `ModelClient` 和 `SessionStore`，并不知道有 `AnthropicClient` 和 `JSONLSessionStore`。需要有一个地方把具体的类创建起来、接到一起。这个地方叫**组装根**（Composition Root），**也是启动服务的地方**。

```python
# main.py —— 组装根
def build_agent_service() -> AgentService:
    session_store = JSONLSessionStore(path="./sessions.jsonl")
    model_client = AnthropicClient()
    return AgentService(
        model_client=model_client,
        session_store=session_store,
        assistant=Assistant(),
        reviewer=ToolCallReviewer(),
        assistant_settings=ModelSettings(model="claude-sonnet-5", effort=4),
        reviewer_settings=ModelSettings(model="claude-haiku-4.5", effort=1),
        max_iterations=20,
    )
```

四条性质：

1. **唯一知道所有具体类的地方**——整个系统里只有这一个文件 import 了 `AnthropicClient` 和 `JSONLSessionStore`
2. **也是启动服务的地方**——`main()` 里第一行就调它
3. **换任何具体实现只改这一个文件**——想从 Claude 换成 GPT，只改 `model_client` 那一行
4. **可测试**——测试时组装根换成假实现，整个系统就能离线跑

材料那句"你可以把这一大堆你的设计结论都放进和 AI 的对话框里，让它帮你实现"是个很实际的建议：**这套设计的价值之一是它可以被 AI 编码**——因为接口都定死了，AI 只需要实现功能，代码质量不会差到哪去。再配上 TDD（先写测试再写实现），AI 辅助编码的产出就相当可控。

> **Dify 对照**：Dify 的组装根在 `api/core/app/apps/agent_chat/app_runner.py`——`AgentChatAppRunner`（`:27`）的 `run()`（`:32`）里按模型能力选策略（`:199`/`:207`，不认识的策略直接 `raise ValueError`），并在 `:161-162` 从 `app_config.agent` 取出领域配置 `agent_entity`。整个 `run()` 干的事正是材料说的：**取配置 → 建模型实例 → 建记忆 → 建工具 → 选 Runner → 跑**。
>
> 更能说明问题的是 `app_runner.py:186` 那行：
>
> ```python
> agent_entity.strategy = AgentEntity.Strategy.FUNCTION_CALLING
> ```
>
> **这是"组装根"边界的精确体现**：框架在组装阶段做了一次**领域对象的翻译**——把"这个模型支不支持原生 tool_calls"这个外部事实，翻译成领域层的 `strategy` 字段。组装根是唯一允许"外部事实 → 领域概念"这种翻译发生的地方，因为只有它同时知道两边。
>
> 再往前看，`ProviderManager` 的注释（`api/core/provider_manager.py:66-68`）说 runtime adapter 由组合层注入——Dify 确实有一层专门的组合逻辑在 `api/core/plugin/`，而不是把 `new XxxClient()` 散落在业务代码里。

---

## 第五讲 两个扩展练习

材料留了两个练习，答案就在 Dify 的源码里。

### 5.1 分叉对话（fork / branch）

**需求**：用户可以从一个已有对话的某条消息开始，分支出一个新对话。新对话带着这条消息之前的全部历史，之后各走各的，互不影响。

**设计要点**：

| 层 | 要加什么 |
| --- | --- |
| 领域层 | `Session` 增加 `parent_session_id` + `fork_from_message_id`（或等价设计） |
| 应用层 | `SessionStore` 增加 `fork(session_id, message_id) -> Session`；`AgentService` 暴露 `fork_session` |
| 适配层 | 存储实现 fork 的物理操作（复制历史行、写新 id、建父子关系） |

**关键判断**：分叉是**领域概念**，不是存储细节。领域层必须能表达"这条对话是从哪条消息分出来的"，否则上层无法做"列出我的所有分支""回到分叉点重试"这类操作。

> **Dify 对照**：Dify 的 `Message` 表里有一个 `parent_message_id` 字段（`api/models/model.py:1426`，外键指向 `messages.id`）。**一条消息可以记录自己的父消息**——这正是分叉能力的存储基础：从某条消息派生出的新消息，`parent_message_id` 指向它。配合 `MessageAgentThought.position`（`:2328`）记录轮次序号，分支内的工具轨迹也能完整回放。
>
> 值得强调的是 Dify 的选择：**它把 `parent_message_id` 放在 Message 上，而不是 Conversation 上**。也就是说，**分叉点粒度是消息而不是整个会话**——这比"会话级分叉"精细，能实现"重试某一步"这类操作。

### 5.2 对话压缩（compaction / summarization）

**需求**：对话越长，发给模型的上下文就越大，费用和延迟都会更高。压缩是在对话变长之后，让模型把较早的消息总结成一段摘要，之后发给模型的就是**摘要 + 最近的消息**。

**设计要点**：

| 层 | 要加什么 |
| --- | --- |
| 领域层 | `Context.history` 的构造逻辑要支持"摘要 + 尾部"两种来源；摘要本身是一种特殊消息（角色/类型要能标记） |
| 应用层 | 一个 `Compactor` 角色（类似 `Assistant`/`Reviewer` 的第三种角色，输入历史输出摘要）；`AgentService` 在构造 context 前判断是否触发压缩 |
| 适配层 | 摘要用哪个模型跑（通常用便宜的小模型——又一个 `ModelSettings` 独立配置的理由） |

**关键判断**：压缩的**触发时机**和**摘要的存储形态**是两个独立决策。触发可以是"token 数超阈值"或"轮次超阈值"；摘要可以**原地替换**历史消息，也可以**作为一条特殊的 summary 消息**保留在历史里（可追溯、可回看原文）。

> **Dify 对照**：Dify 的历史窗口策略在 `api/core/memory/token_buffer_memory.py`——`get_history_prompt_messages(max_token_limit=2000, message_limit=None)`（`:122`），超限处理在 `:202-203`：
>
> ```python
> if curr_message_tokens > max_token_limit:
>     while curr_message_tokens > max_token_limit and len(prompt_messages) > 1:
>         # 从最旧的一条开始丢
> ```
>
> **这是"丢弃"而不是"摘要"**：Dify 选择了更简单的方案——超预算就从最旧的消息开始丢，保留最近的消息。它对**当轮上下文**做了压缩（保证不超预算），但**没有保留被丢内容的语义**。
>
> 两者的取舍很清楚：
>
> | 方案 | 优点 | 代价 | 适合 |
> | --- | --- | --- | --- |
> | 丢弃最旧（Dify） | 零额外成本、零延迟 | 丢失早期信息，模型可能"忘了"约束条件 | 短任务、上下文预算充裕 |
> | 摘要压缩 | 保留早期语义 | 每次压缩要调一次模型（贵、慢）；摘要本身可能失真 | 长对话、多轮约束 |
>
> 如果要做摘要压缩，正确的落点就是**在 `TokenBufferMemory` 那一层把"截断"替换成"摘要"**——领域层和应用层的接口完全不用改，这正是分层的红利。

---

## 六、这套设计的可移植性

材料最后强调：「你可以将这一套思路用在任何系统上，而不只是 Agent 系统」。确实如此——三层架构的适用条件只有两条：

1. **系统有外部依赖**（存储、模型、第三方服务）需要被翻译
2. **核心流程有多种实现方式**（不同存储、不同协议、不同入口）

典型同构场景：

| 场景 | 领域层 | 应用层 | 适配层 |
| --- | --- | --- | --- |
| 支付系统 | 订单/支付单/退款单 | 下单编排、状态机流转 | 渠道对接（微信/支付宝/银联） |
| 数据同步 | 源表/目标表/映射规则 | 同步任务编排、增量推进 | 连接器（MySQL/Kafka/文件） |
| 通知系统 | 模板/收件人/投递记录 | 发送编排、重试策略 | 通道（短信/邮件/推送） |
| 权限系统 | 用户/角色/资源/策略 | 鉴权编排、决策流程 | 身份源（LDAP/OAuth/自建） |

**迁移步骤**是同一套四步：

1. 先梳理流程，建立一套系统自己的语言作为**领域层**
2. 然后在**应用层**编排流程
3. 外部服务在**应用层定义接口**，然后在**适配层实现**
4. 最后通过**组装根**把具体的类接起来

---

## 七、面试高频 Q&A

14 道题。「先给结论」是本站惯例，深入列指向对应讲次。

| # | 问题 | 答案（先给结论） | 深入 |
| --- | --- | --- | --- |
| 1 | 设计一个 Agent 后端，核心难点是什么？ | 不是循环本身，而是**在模型快速演进时保持可扩展**——需要一层自己的语言隔离外部协议变化 | 〇 |
| 2 | 为什么 Agent 后端不能写成一个大函数？ | 换模型协议要改多处；测试必须真调模型（贵、慢、不稳）；换存储要在业务里写序列化 | 〇 |
| 3 | 三层架构分别负责什么？ | 领域层定义系统语言（是什么）、应用层编排流程（做什么）、适配层翻译外部（怎么打交道） | 〇 |
| 4 | 依赖倒置在这里体现在哪？ | **接口定义在应用层，实现在适配层**；`AgentService` 只依赖 `ModelClient`/`SessionStore` 抽象，换实现不用改业务 | 第二讲 |
| 5 | 领域层应该包含什么？ | 只有业务概念本身：`Session`、`Message`、`Tool`、`Context`——**不含任何外部 SDK 的类型** | 第一讲 |
| 6 | `Context` 为什么要用泛型？ | 同一套上下文要服务两类角色（输出 `AssistantMessage` / 输出 `Decision`），泛型让输出类型由调用方指定 | 第一讲 |
| 7 | 工具调用和工具结果怎么配对？ | 必须靠 `tool_call_id` 显式关联，不能靠顺序——一次回复可能并发调多个工具 | 第一讲 |
| 8 | 为什么工具结果要进历史？ | 模型下一轮要基于结果继续推理；不回灌就断了循环 | 第一讲 |
| 9 | 审核工具调用这个角色为什么不需要工具？ | 它的职责是"判断"不是"执行"；给工具等于让它自己动手，越权 | 第一讲 |
| 10 | 审核不通过怎么办？ | 把拒绝原因作为 `ToolResult.content` 回灌模型，模型可以换方式重试——拒绝是**信息**不是终止 | 第一讲 |
| 11 | 两个角色为什么用不同的模型？ | 难度与成本差几个数量级；审核者用更便宜更快的模型是必然选择 | 第二讲 |
| 12 | `effort` 这种参数为什么要自己定义？ | 内部一个概念、外部三个名字（`reasoning_effort`/`output_config.effort`/`thinking_level`）；不翻译则协议一变全链路要改 | 第二讲 |
| 13 | 组装根是什么？ | 唯一知道所有具体类的文件，也是启动入口；换模型/存储/入口只改这一处 | 第四讲 |
| 14 | 分叉对话怎么设计？ | 分叉点是**领域概念**：`Session` 要记 `parent_session_id` + `fork_from_message_id`；存储层只是物理复制 | 第五讲 |

---

## 八、易错点清单

1. **在领域层引入外部 SDK 类型**（比如直接用 `openai.types.Message`）——协议一升级，领域层跟着炸；领域层只能有自己定义的概念
2. **接口定义在适配层**——依赖方向反了，换实现就得改业务代码
3. **组装根散落**——`new AnthropicClient()` 出现在五个文件里，就等于没有组装根
4. **工具结果靠顺序配对**——并发工具调用时结果会串，必须 `tool_call_id`
5. **把工具调用做成独立消息类型**——丢失"同一轮发起"的语义，历史回放对不齐
6. **审核者给全套工具**——越权；审核者的输出应该是判断，不是动作
7. **拒绝即终止**——把拒绝原因回灌，模型才有自我修正的机会
8. **两个角色共用一个 `ModelSettings`**——成本浪费
9. **忘了 `max_iterations`**——模型反复调工具会烧掉大量 token
10. **抽象时"顺手"把实现的坑也抽象掉了**——如 Dify 的凭证缓存失效问题，该写进实现契约而不是藏起来（`model_manager.py:442-447`）
11. **压缩策略直接改业务代码**——正确落点是历史构造那一层，换掉"截断"即可

---

## 九、数值速记

| 数值 | 含义 | 出处 |
| --- | --- | --- |
| `6` | 内部 `effort` 档位数 | 概念设计 |
| `2` | 角色数（Assistant / ToolCallReviewer） | 概念设计 |
| `3` | `messages` 的消息类型数（user / assistant / tool result） | 概念设计 |
| `3` | 三层 + 组装根 | 概念设计 |
| `10` | Dify `max_iteration` 默认值 | `api/core/agent/entities.py:86` |
| `99` | Dify 轮次硬上限（`min(max_iteration, 99) + 1`） | `api/core/agent/fc_agent_runner.py:52` |
| `2000` | Dify 历史消息默认 token 上限 | `api/core/memory/token_buffer_memory.py:122` |
| `3` | 模型协议适配数（OpenAI / Anthropic / Google） | 概念设计 |

---

## 十、复习卡

- [ ] 能默画出三层架构图，并说清每层的职责边界
- [ ] 能说清「接口在应用层、实现���适配层」为什么是依赖倒置，以及它带来的两个好处
- [ ] 能列出领域层的五个核心概念（Session / Message×3 / Tool / Context）
- [ ] 能解释 `Context` 用泛型的理由，以及两个角色各自的输入输出
- [ ] 能说清审核者模式 vs 事后纠错模式的取舍
- [ ] 能说出 `effort` 翻译这件事的本质：内部概念与外部概念的隔离
- [ ] 能描述组装根的四条性质，并说明它为什么是唯一允许"外部事实→领域概念"翻译的地方
- [ ] 能设计分叉对话：分叉点放在领域层的哪个字段上
- [ ] 能设计对话压缩：触发时机、摘要存储形态两个独立决策
- [ ] 能说出这套四步法如何迁移到支付/同步/通知/权限系统

---

## 十一、文件索引

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `api/core/agent/entities.py` | 94 | `AgentEntity`（`:68`）、`Strategy`（`:73`）、`AgentToolEntity`（`:9`）、`AgentScratchpadUnit`（`:31`） |
| `api/core/agent/base_agent_runner.py` | 478 | 应用层基类：工具初始化、历史组织（`:350`）、thought 落库（`:220`/`:262`） |
| `api/core/agent/fc_agent_runner.py` | 469 | FC 主循环：三道闸（`:52`/`:80`/`:225`）、工具执行（`:239`）、错误回灌（`:235`） |
| `api/core/agent/cot_agent_runner.py` | 443 | ReAct 主循环 |
| `api/core/agent/output_parser/cot_output_parser.py` | — | ReAct 输出解析（`:12`） |
| `api/core/app/apps/agent_chat/app_runner.py` | 242 | **组装根**：策略分派（`:186`/`:199`/`:207`） |
| `api/core/model_manager.py` | 716 | 模型适配（`:439`/`:469`）、凭证缓存失效警告（`:442-447`） |
| `api/core/provider_manager.py` | 1244 | "适配器由组合层注入"的注释（`:64`/`:66-68`） |
| `api/core/tools/tool_engine.py` | 376 | 工具执行（`:42`/`:48`） |
| `api/core/mcp/mcp_client.py` | 115 | 协议适配的"字典式"扩展（`:48-49`/`:57-64`） |
| `api/core/memory/token_buffer_memory.py` | 249 | 历史窗口与截断策略（`:30`/`:122`/`:202-205`） |
| `api/core/plugin/` | — | 插件化的模型运行时（协议差异关进插件机制） |
| `api/models/model.py` | 2589 | `Conversation`（`:1044`）、`Message`（`:1393`）、`parent_message_id`（`:1426`）、`MessageAgentThought`（`:2316`） |
