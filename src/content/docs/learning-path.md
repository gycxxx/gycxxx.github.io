---
title: "源码深读知识体系 · 学习路径与记忆地图"
date: 2026-10-17
categories:
  - "学习路线"
tags:
  - "思维导图"
  - "学习路径"
  - "记忆方法"
---

> **配套交互式思维导图**：[`learning-map.html`](learning-map.html)（本地同目录；线上 <https://gycxxx.github.io/learning-map/>）。
> 本文是那张图的**文字版 + 记忆科学使用说明**：图用来"看"，本文用来"讲"和"复习"。
>
> **覆盖**：19 份文档 / **111 讲** / 6 条主线。全部结论可对照源码复验（`verify_citations.py`，3561 条引用 0 问题）。

---

## 一页纸总览

```
源码深读知识体系（19 份文档 · 111 讲 · 3561 条已校验引用）
│
├─ 阶段一 · Java 基础打底（第 1–3 周）           ← 地基：数据结构 → 并发 → JVM
│  ├─ java-collections（6 讲）  List → Map → Set/Queue → 迭代器 → 散列 → 并发集合
│  ├─ java-lock（5 讲）         synchronized 升级 → AQS → ReentrantLock → 读写锁 → Condition
│  ├─ java-concurrent（5 讲）   线程池 → Future/CFF → 并发容器 → 同步器/ForkJoin → 时间工具
│  ├─ java-thread（5 讲）       Thread → ThreadLocal → 虚拟线程 → ThreadGroup → HotSpot
│  └─ jdk（5 讲）               类加载 → 运行时数据区 → JIT → GC → JMM
│
├─ 阶段二 · Spring 全家桶（第 4–6 周）            ← 一条请求从进容器到落库
│  ├─ spring-core（6 讲）       BeanFactory → 生命周期 → Context → AOP → SpEL → 事务
│  ├─ springboot（5 讲）        run 流程 → 自动配置 → 内嵌容器 → 外部化配置 → starter
│  ├─ springmvc（5 讲）         DispatcherServlet → Mapping → Adapter → 视图 → 横切
│  ├─ springcloud（4 讲）       BOM → commons/LoadBalancer → Gateway → OpenFeign
│  └─ mybatis（6 讲）           入口配置 → Executor → StatementHandler → 动态 SQL → Mapper → 插件
│
├─ 阶段三 · 存储与缓存（第 7–9 周）               ← 三种存储范式，横向对照着读
│  ├─ mysql（6 讲）             启动 → 查询生命周期 → 优化器 → InnoDB → MVCC/锁 → 两阶段提交
│  ├─ redis（6 讲）             事件驱动 → RESP → 命令生命周期 → 数据结构 → 持久化 → 集群
│  └─ elasticsearch（6 讲）     集群发现 → 索引分片 → 写入路径 → 查询 → 分布式执行 → recovery
│
├─ 阶段四 · 分布式中间件（第 10–11 周）            ← 概念密度最高
│  ├─ kafka（6 讲）             网络层 → KRaft → 存储引擎 → Producer → Consumer → Coordinator
│  └─ nacos（5 讲）             装配启动 → naming → config → AP/CP 双轨 → 客户端 SDK
│
├─ 阶段五 · 游戏与云原生（第 12–14 周，可并行）     ← 独立主线，不依赖 Java 线
│  ├─ open-match（6 讲）        拓扑 → Ticket 生命周期 → 状态存储 → 本地缓存 → 过滤匹配 → Assignment
│  └─ agones（6 讲）            三层 CRD → 控制面 → GameServer/SDK Server → Fleet 编排 → 分配 → 扩缩容
│
└─ 阶段六 · RAG 与 Agent 工程（第 15–16 周）       ← 把前五阶段原语拼成 AI 应用链路
   ├─ opencoze-agent（6 讲）    数据模型 → 构建期 → 运行期 → 工具节点 → 知识/建议 → 发布
   └─ ingestion-deep-dive（12 讲） parser → chunk → 编排 → Ingestor → LLM 抽取 → 检索 → 编译
```

**全链路一句话**：请求进来（Spring MVC）→ 用 Bean（spring-core）→ 查数据（MySQL / Redis / ES）→ 发消息（Kafka）→ 找服务（Nacos）→ 游戏场景匹配开服（Open Match / Agones）；AI 侧则是 Agent 跑业务逻辑（OpenCoze）→ 知识入库检索（RAGFlow）。

---

## 为什么按这个顺序排

排课表不是按"知名度"，是按**依赖方向**：

| 顺序 | 依赖关系 | 不按这个顺序的代价 |
|------|---------|-------------------|
| Java 基础 → Spring | spring-core 大量复用 AQS、`ConcurrentHashMap`、类加载 | 读不懂线程池拒绝策略、三级缓存、CGLIB 增强的前提 |
| Spring → 存储 | mybatis 二级缓存复用 collections 的装饰器模式；事务管理复用 spring-core | 看不懂 `CachingExecutor` 为什么是两层 |
| 存储 → 中间件 | Kafka/Nacos 的持久化与一致性都建立在存储范式之上 | 把"日志"和"数据库"混为一谈 |
| 中间件 → 游戏云原生 | Agones 的 K8s 编排理念与 Nacos 的服务发现同源 | 看不出 ownerReference 与 ServiceInstance 的对应 |
| 全部 → RAG/Agent | Ingestion 是把前面所有原语拼起来的一条完整工程链路 | 只能看懂单点，拼不出系统 |

**阶段五可与前四阶段并行**：它是 K8s/Go 技术栈，与 Java 线无依赖，且内部顺序（匹配 → 开服）与线上调用链一致。

---

## 各主线明细（文档 → 讲次 → 记忆钩子）

"钩子"= 该讲**能脱口而出的一句话**。复习时先复述钩子，卡住再回原文。

### 阶段一 · Java 基础打底

**java-collections**
1. List 家族 — ArrayList 扩容 1.5×；LinkedList 双链表；`Arrays.asList` 返回的是 Arrays 内部类，不能 add/remove
2. Map 家族核心 — HashMap 链表 + 红黑树；LinkedHashMap 覆写 `afterNodeInsertion` 即得 LRU；TreeMap 走红黑树有序
3. Set 与 Queue — HashSet 就是 HashMap 包壳；PriorityQueue 数组小顶堆；ArrayDeque 循环数组
4. 迭代器与工具类 — fail-fast = modCount 校验；`iterator.remove` 会同步 expectedModCount；Collections 三类包装全是装饰器
5. 散列原理 — 容量 2 次幂 → 位与代替取模；树化阈值 8 来自泊松分布；loadFactor 0.75 是时空权衡
6. 并发集合 — CHM 的 `sizeCtl` 一个 int 四态 + 协作扩容；COW 写时复制；CLQ 无锁；BlockingQueue 四兄弟

**java-lock**
1. synchronized 与锁升级 — JDK 21 移除偏向锁，新增 LM_LIGHTWEIGHT；ObjectMonitor 是 LIFO cxq + FIFO EntryList 混合队列
2. AQS 核心 — JDK 21 大改：status 替代 waitStatus、节点子类化、`acquire` 收 6 个参数
3. ReentrantLock — 公平与非公平只差一个 `hasQueuedPredecessors` 判断
4. 读写锁 — ReentrantReadWriteLock 用高低 16 位分别计数；StampedLock 乐观读靠 stamp 校验
5. Condition 与 LockSupport — Condition 是单链表等待队列；LockSupport 是 permit 语义

**java-concurrent**
1. Executor 与线程池 — `ctl` 高 3 位存状态低 29 位存计数；Worker 继承 AQS 做不可重入锁；四种拒绝策略
2. Future 与 CompletableFuture — FutureTask 七态状态机；Completion 继承 ForkJoinTask 实现零成本复用
3. 并发容器 — CHM 用 CAS + synchronized；ReservationNode 占位解决 compute 冲突；COW / BlockingQueue
4. 同步器与 ForkJoin — AQS 五兄弟；ForkJoinPool work-stealing 偷任务
5. 时间工具与杂项 — TimeUnit 换算；ThreadLocalRandom 避免竞争；ScheduledThreadPoolExecutor

**java-thread**
1. Thread 基础与生命周期 — Thread.State 枚举；interrupt 是协作式；`start0` 是 native
2. ThreadLocal — 挂在 Thread 上的弱 key 强 value 哈希表 → value 泄漏的根源
3. 虚拟线程 — continuation + ForkJoinPool + 九态状态机；注意 carrier 钉住；State 只暴露 WAITING
4. ThreadGroup — JDK 21 已退化为逻辑分组 + 异常派发链
5. HotSpot 线程实现 — JavaThread 反射调 Java；OSThread 薄壳；SMR 安全回收

**jdk**
1. 类加载机制 — 双亲委派；ClassFileParser 流式拆包；InstanceKlass 是运行时类
2. 运行时数据区 — 堆 / Metaspace / 栈帧 / oop 对象头
3. JIT 编译器 — C1/C2 分层编译；CompileBroker 派发；OSR 栈上替换
4. GC 家族 — G1 = region + SATB；ZGC = 染色指针 + 读屏障；另 Serial/Parallel/Shenandoah/Epsilon
5. JMM — x86 TSO 下 volatile 几乎免费；OrderAccess acquire-release；DCL 的正确性证据

### 阶段二 · Spring 全家桶

**spring-core**
1. BeanFactory 与容器基础 — DefaultListableBeanFactory 三级缓存；`preInstantiateSingletons` 预初始化
2. Bean 生命周期与依赖注入 — `createBean` 五步；Aware 回调链；`@Autowired` 注入点解析
3. ApplicationContext 与资源加载 — `refresh()` 十二步；`@Configuration` full/lite 与 CGLIB 增强
4. AOP 代理体系 — JDK 动态代理 vs CGLIB 选型；AbstractAutoProxyCreator 织入；Advisor/Pointcut 链
5. SpEL 与切面解析 — 表达式求值链路 vs AspectJ 切点链路，注意能力边界
6. 声明式事务 — AbstractPlatformTransactionManager 传播行为；TransactionInterceptor；六大失效场景

**springboot**：run 全流程（监听器时序 / BootstrapContext）→ 自动配置（imports / 条件注解 / Binder 松弛绑定）→ 内嵌容器（ServletWebServerFactory / 优雅停机）→ 外部化配置（ConfigDataEnvironmentPostProcessor / 优先级）→ starter 生态

**springmvc**：DispatcherServlet（doDispatch 九步 / HandlerExecutionChain / 异常解析链）→ HandlerMapping（PathPattern）→ HandlerAdapter（HttpMessageConverter 协商 / 数据绑定）→ 视图与内容协商 → 异步 / CORS / 拦截器 / 异常

**springcloud**：release train 与 BOM → commons 抽象与 LoadBalancer（ServiceInstanceListSupplier 过滤器链）→ Gateway（Route/Predicate/Filter 链 / RateLimiter / Reactor Netty）→ OpenFeign（动态代理 / Encoder/Decoder / LB 集成）

**mybatis**
1. 入口与配置体系 — SqlSessionFactoryBuilder → XMLConfigBuilder → Configuration → DefaultSqlSession
2. Executor 执行器体系 — BaseExecutor 一级缓存 + queryStack 防递归；CachingExecutor + TransactionalCache 两阶段提交
3. StatementHandler 与参数/结果 — DefaultResultSetHandler 决策树；TypeHandler 查找链；延迟加载
4. SQL 映射与动态 SQL — MappedStatement / SqlSource 三兄弟；SqlNode 节点树；`#{}` vs `${}`
5. Mapper 接口绑定 — JDK 动态代理；MapperMethod 分派；ParamNameResolver
6. 插件/缓存/日志 — Interceptor 拦四大对象；CacheBuilder 装饰链；JDBC 日志代理

### 阶段三 · 存储与缓存

**mysql**：mysqld 启动与连接线程模型 → 查询生命周期（COM_QUERY 到结果包）→ 优化器（JOIN::optimize / AccessPath）→ InnoDB（buffer pool / B+ 树 / 行格式 / double write）→ 事务 MVCC 与锁（read view / undo 版本链 / Next-Key / 死锁检测）→ redo/undo/binlog 两阶段提交

**redis**：事件驱动核心（aeMain / beforeSleep / serverCron / io-threads）→ 网络层与 RESP（RESP3 / 输出缓冲限制）→ 命令生命周期（processCommand 检查链 → call → propagate）→ 对象与数据结构（encoding 演化 / listpack / dict rehash / zset 双结构）→ 持久化（RDB fork / AOF 三策略 / 混合）→ 复制与集群（PSYNC2 / gossip / failover）

**elasticsearch**：节点启动与集群发现（Coordinator / Raft 改良选主 / PreVote）→ 索引与分片（IndexService / AllocationService / routing）→ 写入路径（TransportBulkAction / Translog / Refresh / Flush）→ 查询与搜索（QueryPhase / 聚合同一次 Lucene search）→ 分布式执行（两阶段 query-then-fetch / scroll）→ recovery 与故障转移（PeerRecovery / SeqNo / Snapshot）

### 阶段四 · 分布式中间件

**kafka**：网络层与请求处理（Acceptor / Processor / Purgatory）→ KRaft 元数据与 Controller（raft 日志 + snapshot）→ 存储引擎（UnifiedLog / LogSegment / LogCleaner）→ Producer（RecordAccumulator / Sender / 幂等事务）→ Consumer（Fetcher / 再均衡 eager vs cooperative）→ Group Coordinator 与事务（状态机 / 两阶段标记）

**nacos**：模块装配与启动（NacosBootstrap 三段式点火）→ naming（v2 模型 / distro / gRPC 推送 / 健康检查）→ config（ConfigCacheService MD5 / 长轮询 30s）→ 一致性协议（AP distro vs CP JRaft）→ 客户端 SDK（双客户端韧性设计）

### 阶段五 · 游戏与云原生（独立主线）

**open-match**
1. 组件拓扑与进程模型 — v2 砍成单二进制 om-core + 6 个 RPC
2. Ticket 生命周期 — Ticket ID ≡ Redis Stream entry ID；新票恒为 inactive
3. 状态存储抽象 — XADD / XREAD BLOCK / XTRIM MINID；三个 sync.Map 本地缓存
4. ReplicatedTicketCache — setDifference 求活跃票，读路径整个下沉到内存
5. 过滤与匹配编排 — 4 种硬编码 filter 而非 CEL；4MB 分块；MMF 双向流
6. Assignment 与观测 — WatchAssignments 1s 轮询；指标名笔误 `om_core.ticket.assigment.watch`

**agones**
1. 数据模型：三层 CRD — Fleet → GameServerSet → GameServer，ownerRef + label 双轨；11 状态机
2. 控制面骨架 — 单进程 5 runner；三条队列按状态分流；链式 syncHandler
3. GameServer 生命周期与 SDK Server — :9357 gRPC；JSON Patch 反向写 CRD；三层健康检查；端口位图
4. Fleet 与 GameServerSet 编排 — MaxSurge 对 Spec.Replicas、MaxUnavailable 对 ReadyReplicas 双基数
5. GameServerAllocation — 聚合 API + 独立 allocator 双路径；失败返回 201 + UnAllocated
6. FleetAutoscaler 与观测 — 7 种策略；Buffer 以 Allocated 为基数；OpenCensus 双 exporter

### 阶段六 · RAG 与 Agent 工程

**agent/opencoze-agent**
1. 数据模型 — Draft / Version / Publish 三形态；SingleAgent 只是跨域薄壳
2. 构建期 — BuildAgent 装配 5 类工具；按有无工具决定 ReAct / 裸 LLM；AllPredecessor 触发
3. 运行期 — StreamExecute 建 Pipe(10)；`runner.Stream` 返回值被丢弃，输出只经 callback
4. 五类工具节点 — 数据库工具运行期校验 SQL 表名防越权；`newKnowledgeTool` 是死代码
5. 知识检索与建议 — RAGFlow 直连双通道，硬编码 0.3/0.7/8；`suggestParser` 空函数
6. 发布与端到端 — 七步发布；`publish.go:100` 并发共享 err 的 bug；三层流 100→10→20

**agent/ingestion-deep-dive**（12 讲）：internal/parser 解析分块基础 → chunk 三阶段流水线 → GeneralChunker 消费现场 → Parser 组件边界与规范化 → 任务编排（Pipeline 与落库）→ Ingestor（NATS 消费）→ LLM 抽取链路（Extractor / KnowledgeCompiler）→ 检索侧 → 知识编译与 Agentic RAG 原理 → Agentic RAG 全流程 → 知识编译全流程 → 演进史与速查

---

## 记忆科学：这套笔记怎么用才不白读

七条策略，全部可在这套文档的结构里落地：

| 策略 | 在本套文档里的落地方式 |
|------|----------------------|
| **先全景后细节** | 每份文档都有「速览：各讲做了什么」+ 全流程图。**先读它再进讲次** —— 大脑对已有框架的信息吸收率远高于碎片 |
| **组块化（Chunking）** | 6 条主线 = 6 个组块；每块 2–6 份文档。一次只攻一条主线，不要东一本西一本 |
| **双编码（Dual Coding）** | 同一信息有文字 + mermaid 图两种表征，回忆时多一条线索 |
| **精细提问（Elaborative Interrogation）** | 每讲读完问三个"为什么"：为什么是这个阈值？为什么不是另一种结构？为什么这样取舍？ |
| **提取练习（Retrieval Practice）** | 合上书，在纸上重画思维导图。画不下去的地方就是没学会的地方，**只回去补那一块** |
| **间隔重复（Spaced Repetition）** | 按下面的 D1/D3/D7/D14/D30 节奏回看各文档末尾的「复习卡」 |
| **交错学习（Interleaving）** | 同层组件横向对比（见下方题组），比顺序递进更抗遗忘 |

---

## 间隔重复复习日程

| 节点 | 时间 | 动作 |
|------|------|------|
| **D1** | 当天晚上 | 只看思维导图叶子钩子，合书复述；卡住的位置标星 |
| **D3** | 第 3 天 | 回看该文档「面试速通」高频 Q&A，**先答后对** |
| **D7** | 第 7 天 | 做文档末尾「复习卡」，错的抄进错题本 |
| **D14** | 第 14 天 | 与同阶段另一份文档做交错对比（见下方题组） |
| **D30** | 第 30 天 | 全图重画一次，能脱稿讲完一条主线即过关 |

**16 周计划对照**（每天 1.5–2h）：

| 周次 | 主线 | 周末验收 |
|------|------|---------|
| W1–W3 | 阶段一 Java 基础 | 能白板画出 HashMap 扩容 + CHM 协作扩容 |
| W4–W6 | 阶段二 Spring | 能从头讲清一个 `@Transactional` 请求的完整生命周期 |
| W7–W9 | 阶段三 存储 | 能对比 MySQL / Redis / ES 的写路径与一致性 |
| W10–W11 | 阶段四 中间件 | 能讲清 Kafka ISR 与 Nacos distro 的取舍 |
| W12–W14 | 阶段五 游戏云原生 | 能画出"匹配 → 开服"两段链路 |
| W15–W16 | 阶段六 RAG/Agent | 能讲清一次文档入库到被检索回来的全过程 + Agent 一次调用的完整链路 |

---

## 交错对比题组（D14 用）

| 题组 | 对比对象 |
|------|---------|
| **写路径对照** | MySQL（buffer pool + redo）· Redis（aof_fsync 三策略）· ES（Translog + Refresh/Flush） |
| **一致性对照** | Kafka（ISR + 水位）· Nacos（distro AP vs JRaft CP）· Agones（乐观并发 + status 非 subresource） |
| **锁与并发对照** | synchronized（ObjectMonitor 混合队列）· AQS（CLH 变体）· CHM（synchronized 桶锁 + CAS） |
| **请求处理对照** | Spring MVC（doDispatch 九步）· Redis（processCommand 链）· Kafka（Processor + Purgatory） |
| **状态机对照** | Thread 九态 · FutureTask 七态 · GameServer 十一态 |

---

## 提取练习模板（每次复习用）

```
今天复习：______（文档 + 讲次）

1. 不看资料，3 分钟内写下该讲的"钩子"：____________________
2. 该讲回答了哪个"为什么"？____________________
3. 它和已学过的哪个组件是同构的？（如：二级缓存 = 装饰器模式）
   ____________________
4. 画出最小流程图（只画主干，不画细节）：____________________
5. 对照原文，只补写错的地方，用红笔标注：____________________
```

---

## 文件与工具

| 文件 | 用途 |
|------|------|
| `learning-map.html` | 交互式思维导图（可折叠/缩放/搜索，右侧含复习节奏与记忆法面板） |
| `build_mindmap.py` | 生成上面那个 HTML 的脚本；文档增删讲次后改数据重跑即可 |
| `learning-path.md` | 本文件：思维导图文字版 + 记忆科学使用说明 |
| `verify_citations.py` | 引用复验（期望 3561 OK + 3 AMBIGUOUS-OK，0 真实问题） |
| `README.md` | 全库总索引（文档清单 / 内容速览 / 目录结构 / 学习顺序） |

> 配套站点：`https://gycxxx.github.io/`（Hexo + indigo 主题，全部文档已上线）
