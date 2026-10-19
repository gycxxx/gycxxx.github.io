---
title: "Sentinel 源码深度学习笔记"
date: 2026-10-19
categories:
  - "中间件"
tags:
  - "Sentinel"
  - "限流"
  - "熔断"
  - "源码"
---

> 基于对 Alibaba Sentinel 源码（`com.alibaba.csp.sentinel`）的逐文件精读整理而成。
>
> 源码位于 `sentinel-core/`（核心：SlotChain + Node 统计 + 全部内置 Slot）、`sentinel-extension/`（数据源、参数流控、注解）、`sentinel-cluster/`（集群限流 server/client）、`sentinel-adapter/`（Web/Dubbo/gRPC 等适配）、`sentinel-dashboard/`（控制台）。
>
> 阅读顺序即学习顺序：责任链总览 → 统计与节点 → 限流 → 熔断 → 系统/授权/热点 → 规则推送与集群限流。
>
> 所有结论均标注了源文件位置（格式 `模块/相对路径:行号`），行号均经 Read/Grep 实际核对。
>
> **版本基线**：`1.8.11-SNAPSHOT`（根 `pom.xml:43`），commit `a3f40ba`（`chore: bump version to 1.8.11-SNAPSHOT (#3623)`）。
> **注意**：Sentinel 的 slot 通过 SPI（`@Spi(order=...)`）装配，本文引用的是**默认顺序**；`ParamFlowSlot` 在 `sentinel-extension/sentinel-parameter-flow-control`（非 core），`ORDER` 常量在 `Constants.java:76-84`。

---

## 目录

- [〇、全景地图](#〇全景地图)
- [速览：六讲各自做了什么（先看这个再进细节）](#速览六讲各自做了什么先看这个再进细节)
- [面试速通：Sentinel 高频问题一页纸](#面试速通sentinel-高频问题一页纸)
- [第一讲：SlotChain 责任链 —— 一次 entry 的完整旅程](#第一讲slotchain-责任链--一次-entry-的完整旅程)
- [第二讲：Node 与统计 —— LeapArray 滑动窗口](#第二讲node-与统计--leaparray-滑动窗口)
- [第三讲：FlowSlot 限流 —— 三种 TrafficShapingController](#第三讲flowslot-限流--三种-trafficshapingcontroller)
- [第四讲：DegradeSlot 熔断 —— CircuitBreaker 状态机](#第四讲degradeslot-熔断--circuitbreaker-状态机)
- [第五讲：SystemSlot / AuthoritySlot / ParamFlowSlot](#第五讲systemslot--authorityslot--paramflowslot)
- [第六讲：规则推送与集群限流 —— DataSource / Transport / TokenService](#第六讲规则推送与集群限流--datasource--transport--tokenservice)
- [附：学习路线与复习卡汇总](#附学习路线与复习卡汇总)
- [涉及文件索引](#涉及文件索引)

---

## 〇、全景地图

Sentinel 的核心可以压缩成**一条责任链 + 一棵统计树 + 三种规则**：

- **责任链（SlotChain）**：每个资源一条 `ProcessorSlotChain`，`CtSph.entry()` 触发依次执行 8~9 个 slot——先选节点（NodeSelector）、建集群节点（ClusterBuilder）、再统计（Statistic），然后才轮到各种规则检查（Authority → System → Flow → 熔断 → Degrade → 热点）。
- **统计树（Node）**：`DefaultNode`（按上下文分）→ 共享一个 `ClusterNode`（按资源名）→ `StatisticNode` 用 `LeapArray` 滑动窗口统计秒级/分钟级 QPS、RT、线程数、异常数。**所有规则的数据源都是这棵树**。
- **规则（Rule）**：`FlowRule`（限流）/ `DegradeRule`（熔断）/ `SystemRule`（系统自适应）/ `AuthorityRule`（授权）/ `ParamFlowRule`（热点参数）+ 集群规则。规则由 `RuleManager` 持有，通过 `SentinelProperty` 监听数据源变更。

```
┌──────────────── SphU.entry(resource) ────────────────┐
│                                                       │
│  CtSph.entryWithPriority:132                          │
│    ├─ NullContext？→ 只建 CtEntry 不检查（:118）        │
│    ├─ Constants.ON？→ 全局开关关闭则直接放行（:129）     │
│    ├─ lookProcessChain:194（chainMap 缓存，CopyOnWrite  │
│    │   式换 Map；超 MAX_SLOT_CHAIN_SIZE 返回 null 放行）│
│    └─ chain.entry(...)  ↓（slot 按 @Spi order 排序）    │
└───────────────────────────────────────────────────────┘
                          │
   -10000 NodeSelectorSlot   ── 按 context 建 DefaultNode + 挂调用树
   -9000 ClusterBuilderSlot  ── 按资源名建/取 ClusterNode（全局唯一）
   -8000 LogSlot             ── 打日志
   -7000 StatisticSlot       ── entry 记账（pass/thread），exit 记账（rt/exception）
   -6000 AuthoritySlot       ── 来源黑白名单
   -5000 SystemSlot          ── 系统负载/QPS/线程/CPU/RT 自适应（BBR）
   -3000 ParamFlowSlot       ── 热点参数（extension 模块）
   -2000 FlowSlot            ── 限流：DefaultController / WarmUp / Throttling
   -1500 DefaultCircuitBreakerSlot ── 熔断
   -1000 DegradeSlot         ── 熔断规则检查
                          │
                          ▼
              Node 统计树（LeapArray 秒级 + 分钟级双窗口）
                          ▲
              规则数据源（SentinelProperty ← DataSource）
```

一句话记住全景：**"链定顺序、树记数据、规则读树"**——slot 顺序由 SPI order 决定，统计全在 Node 树，规则检查只读统计不下写。

---

## 速览：六讲各自做了什么（先看这个再进细节）

| 讲次 | 主题 | 核心类 | 要回答的问题 | 不学会怎样 |
|---|---|---|---|---|
| 第一讲 | SlotChain 总览 | `CtSph`/`DefaultSlotChainBuilder` | 一次 entry 走几个 slot、顺序凭什么 | 自定义 slot 插不进去 |
| 第二讲 | Node 与统计 | `LeapArray`/`StatisticNode` | 滑动窗口怎么做到无锁高并发 | 看不懂 dashboard 数字 |
| 第三讲 | FlowSlot 限流 | `DefaultController` 家族 | 三种 controller 差异、匀速怎么实现 | 限流配置只会抄 |
| 第四讲 | DegradeSlot 熔断 | `ResponseTimeCircuitBreaker` | 半开探测、慢调用比例怎么算 | 熔断策略选错 |
| 第五讲 | 系统/授权/热点 | `SystemSlot` 家族 | BBR 自适应是什么、热点参数限流 | 系统保护配不来 |
| 第六讲 | 规则与集群 | `NacosDataSource`/`TokenService` | 规则怎么热更新、集群限流走什么协议 | 生产只会单机 file |

---

## 面试速通：Sentinel 高频问题一页纸

### 高频 Q&A（先结论）

| # | 问题 | 一句话结论 | 出处 |
|---|---|---|---|
| 1 | Sentinel 和 Hystrix 差异？ | Sentinel 是**控制台+规则热更+多维度流控**（并发不隔离、用信号量做隔离）；Hystrix 是**线程池隔离+熔断器先行者**（已停更） | 设计对比 |
| 2 | slot 执行顺序？ | 固定顺序：NodeSelector → ClusterBuilder → Log → Statistic → Authority → System → ParamFlow → Flow → CircuitBreaker → Degrade | `Constants.java:76-84` |
| 3 | slot 顺序怎么定制？ | 实现 `ProcessorSlot` + `@Spi(order=...)`，或自定义 `SlotChainBuilder`（SPI 覆盖 `DefaultSlotChainBuilder`） | `DefaultSlotChainBuilder.java:37` |
| 4 | 限流有几种效果？ | `DefaultController`（快速失败）+ `WarmUpController`（冷启动预热）+ `ThrottlingController`/`WarmUpRateLimiterController`（**匀速排队**，基于令牌桶+未来预支） | `slots/block/flow/controller/` |
| 5 | 流控有几种策略？ | 按调用方（`STRATEGY_DIRECT`/`RELATE`/`CHAIN`）与按阈值类型（QPS/线程数）组合；集群模式由 TC 的 TokenService 裁决 | `FlowRule.java` |
| 6 | 熔断有哪几种？ | 慢调用比例（`DEGRADE_GRADE_RT`）、异常比例（`DEGRADE_GRADE_EXCEPTION_RATIO`）、异常数（`DEGRADE_GRADE_EXCEPTION_COUNT`） | `DegradeRule.java` |
| 7 | 熔断半开怎么探测？ | `HALF_OPEN` 放行**一个**探测请求：成功 → `fromHalfOpenToClose`（关闭熔断），失败/慢 → `fromHalfOpenToOpen`（重新打开） | `ResponseTimeCircuitBreaker.java:82-95` |
| 8 | 熔断和限流的区别？ | 限流是"入口控制流量"（对**所有**请求生效）；熔断是"出口保护"（对**满足统计条件**的资源暂停放行，有自动恢复） | `DegradeSlot.java:41` |
| 9 | 统计窗口多大？ | 秒级 `ArrayMetric(SAMPLE_COUNT=2, 1000ms)` + 分钟级 `ArrayMetric(60, 60000ms)`，底层 `LeapArray` 循环桶 | `StatisticNode.java:96` |
| 10 | `LeapArray` 并发安全吗？ | 桶数组用 `AtomicReferenceArray` + **CAS 换桶**（`array.compareAndSet(idx, null, window)`），统计值用 `LongAdder`——无锁 | `LeapArray.java:116` |
| 11 | 规则怎么热更新？ | `RuleManager` 监听 `SentinelProperty`；`NacosDataSource` 等实现把远程配置推成 property 更新，`loadRules` 重建 | `NacosDataSource.java:71` |
| 12 | 集群限流原理？ | 每个 TokenClient 向 TokenServer 申请配额；`TokenServer` 是独立进程（或嵌入），本地规则只做兜底；超限返回 `TokenResultStatus.BLOCKED` | `TokenService.java:36` |
| 13 | `@SentinelResource` 两个属性？ | `value`（资源名）+ `blockHandler`（限流降级方法，**方法签名要与原方法一致**）+ `fallback`（业务异常） | `annotation/SentinelResource.java` |
| 14 | 上下文超限会怎样？ | 超过 `Constants.MAX_CONTEXT_NAME_SIZE`（默认 2000）后新入口进 `NullContext`，**只建 entry 不做规则检查** | `CtSph.java:118` |
| 15 | 热点参数限流限什么？ | 按**参数值**分别统计：同一资源下 `paramIdx=0` 的 `userId=10086` 单独一个计数器，可设单参数值阈值 + 全局阈值 | `ParamFlowSlot.java:50` |
| 16 | 系统自适应 BBR 是什么？ | 参考 Google BBR：当 load 超阈值时，若并发线程 > `maxSuccessQps × minRt / 1000`（理论最优并发）则拒绝——**过载时只放进"最优并发数"的请求** | `SystemRuleManager.java:337` |
| 17 | `entry` 抛了非 BlockException 怎么办？ | `CtSph:155-159` 只记录日志继续返回 entry——**Sentinel 自身异常不拖垮业务**（fail-open） | `CtSph.java:155` |
| 18 | `SphU.entry` 忘了 exit 会怎样？ | 线程数统计泄漏（`increaseThreadNum` 加了不减），最终所有请求被线程数规则拒——**必须 try-finally** | `StatisticSlot.java:62` |

### 易错点（最容易讲错的 8 条）

1. **slot 顺序不是"先 Flow 后 System"**：System（-5000）在 Flow（-2000）**之前**——系统级保护优先于资源级限流。
2. **StatisticSlot 在规则之前**：统计先行，规则读到的才是本次请求之前的数据。
3. **`DegradeSlot` 在链尾但熔断器在链中**：`DefaultCircuitBreakerSlot`（-1500）只负责"探测请求恢复"，真正的规则检查在 `DegradeSlot`（-1000）。
4. **熔断的 `minRequestAmount` 不是请求数下限**：是"统计窗口内至少这么多请求才计算比例"，防止 1/1 异常 = 100% 误熔。
5. **异常数熔断（`DEGRADE_GRADE_EXCEPTION_COUNT`）在 1.8 已废弃**，用异常比例替代——写 `count` 语义要小心。
6. **匀速排队会 sleep**：`ThrottlingController` 超过阈值的请求不是拒绝而是 `sleep(waitInMs)`（`DefaultController.java:60` 里 prioritized 分支同理）——**高并发下会堆积业务线程**。
7. **集群限流的 `flowId` 必须全局唯一**：不同资源同一个 flowId 会互相抢配额。
8. **`ContextUtil` 入口名不是资源名**：一个入口下多个资源共享 context；`NullContext` 时所有规则静默失效。

### 数值速记

| 数值 | 含义 | 出处 |
|---|---|---|
| `-10000 / -9000 / -8000 / -7000` | 前四个 slot 的 order | `Constants.java:76-79` |
| `-6000 / -5000 / -3000 / -2000` | Authority/System/ParamFlow/Flow 的 order | `Constants.java:80-82` |
| `-1500 / -1000` | CircuitBreaker/Degrade 的 order | `Constants.java:83-84` |
| `2000` | 最大上下文数 `MAX_CONTEXT_NAME_SIZE` | `Constants.java` |
| `1000ms` | 秒级统计窗口 | `StatisticNode.java:96` |
| `60000ms` | 分钟级统计窗口 | `StatisticNode.java:103` |
| `2` | 秒级窗口桶数（`SAMPLE_COUNT`） | `StatisticNode.java:96` |

---

## 第一讲：SlotChain 责任链 —— 一次 entry 的完整旅程

> **本讲问题**：`SphU.entry("demo")` 这行代码进去以后，到底依次敲了哪些门？门的顺序凭什么这么定？

> 🎬 配套动画：[Sentinel 责任链过闸](/animations/sentinel-entry/) —— 18 步单步播放，十个 slot 按 `@Spi(order)` 依次点亮，含放行 / 拒绝 / fail-open 三条结局。

### 1.1 入口：CtSph 的三道闸

```java
// CtSph.java:132（entryWithPriority 节选）
Context context = ContextUtil.getContext();
if (context instanceof NullContext) {
    // 上下文数量超限 → 只建 entry，不检查规则
    return new CtEntry(resourceWrapper, null, context);
}
if (context == null) {
    context = InternalContextUtil.internalEnter(Constants.CONTEXT_DEFAULT_NAME);
}
// Global switch is close, no rule checking will do.
if (!Constants.ON) {
    return new CtEntry(resourceWrapper, null, context);       // 全局开关关闭
}
ProcessorSlot<Object> chain = lookProcessChain(resourceWrapper);
if (chain == null) {                                           // 资源数超 MAX_SLOT_CHAIN_SIZE
    return new CtEntry(resourceWrapper, null, context);
}
Entry e = new CtEntry(resourceWrapper, chain, context, count, args);
try {
    chain.entry(context, resourceWrapper, null, count, prioritized, args);
} catch (BlockException e1) {
    e.exit(count, args);                                       // 被拒也要 exit 一次（补统计）
    throw e1;
} catch (Throwable e1) {
    RecordLog.info("Sentinel unexpected exception", e1);        // ★ fail-open：内部异常不抛给业务
}
return e;
```

三道闸（NullContext / 全局开关 / 资源数上限）的哲学一致：**Sentinel 自己出问题时静默放行，绝不拖垮业务**。这也是它和 Hystrix 设计哲学的差异之一。

### 1.2 链的构建与缓存：CopyOnWrite 式换 Map

```java
// CtSph.java:194（lookProcessChain）
ProcessorSlot<Object> chain = chainMap.get(resourceWrapper);
if (chain == null) {
    synchronized (LOCK) {
        chain = chainMap.get(resourceWrapper);                 // 双检
        if (chain == null) {
            if (chainMap.size() >= Constants.MAX_SLOT_CHAIN_SIZE) {
                return null;                                   // 资源数上限 → 放行
            }
            chain = SlotChainProvider.newSlotChain();          // SPI 构建新链
            Map<ResourceWrapper, ProcessorSlotChain> newMap = new HashMap<>(chainMap.size() + 1);
            newMap.putAll(chainMap);
            newMap.put(resourceWrapper, chain);
            chainMap = newMap;                                 // ★ 整体换 Map，读无锁
        }
    }
}
```

`chainMap` 用**不可变替换**而不是 `ConcurrentHashMap`——读路径完全无锁，写路径才加 `LOCK`。**同一个资源全局共享一条链**（注释原文：no matter in which Context）。

`DefaultSlotChainBuilder.build()`（`slots/DefaultSlotChainBuilder.java:36`）通过 `SpiLoader.of(ProcessorSlot.class).loadInstanceListSorted()` 按 `@Spi(order)` 排序装配，非 `AbstractLinkedProcessorSlot` 的直接 warn 跳过——**想插自定义 slot，实现 `ProcessorSlot` 接口 + 打 `@Spi` 注解即可，不用改任何源码**。

### 1.3 默认链的九个环节（按 order）

| order | Slot | 位置 | entry 干什么 | exit 干什么 |
|---|---|---|---|---|
| -10000 | `NodeSelectorSlot` | `slots/nodeselector/` | 按 context 建 `DefaultNode`、挂调用树、`setCurNode` | 无 |
| -9000 | `ClusterBuilderSlot` | `slots/clusterbuilder/` | 按资源名建/取全局唯一 `ClusterNode` | 无 |
| -8000 | `LogSlot` | `slots/logger/` | 打印 entry 日志 | 打印 exit 日志 |
| -7000 | `StatisticSlot` | `slots/statistic/` | `increaseThreadNum` + `addPassRequest` | 记 RT、exception、complete |
| -6000 | `AuthoritySlot` | `slots/block/authority/` | 来源黑白名单 | 无 |
| -5000 | `SystemSlot` | `slots/system/` | `SystemRuleManager.checkSystem` | 无 |
| -3000 | `ParamFlowSlot` | extension | 热点参数检查 | 无 |
| -2000 | `FlowSlot` | `slots/block/flow/` | `FlowRuleChecker.checkFlow` | 无 |
| -1500 | `DefaultCircuitBreakerSlot` | `slots/block/degrade/circuitbreaker/` | 注册熔断器回调 | 无 |
| -1000 | `DegradeSlot` | `slots/block/degrade/` | `cb.tryPass(context)` | `cb.onRequestComplete(context)` |

### 1.4 三个值得记住的顺序理由

1. **NodeSelector 最先**：没有 Node 后面的统计和规则都无从谈起；
2. **Statistic 在规则之前**：规则读到的永远是"不含本次请求"的历史数据，避免自触发；
3. **System 在 Flow 之前**：机器都快挂了，先做系统级削减（全局），再谈单资源限流；
4. **Degrade 在最后但熔断统计在 exit**：`DegradeSlot.exit` 里调 `onRequestComplete` 把本次 RT/异常喂给熔断器——**熔断判断发生在请求结束时，影响的是下一个请求**。

### 1.5 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `CtSph.entryWithPriority:132` 三道闸 → `lookProcessChain:194` 缓存链 → `chain.entry` 依次过 slot |
| 关键实现 | 链按 `@Spi(order)` 装配（`DefaultSlotChainBuilder.java:37`），`chainMap` 用整体换 Map 实现无锁读 |
| 并发 | 写链加 `LOCK` 双检；读完全无锁；统计值用 `LongAdder` |
| 协议 | 被拒时 `e.exit(count, args)` 补记账；内部异常 fail-open 只记日志 |
| 陷阱 | `MAX_SLOT_CHAIN_SIZE` 超限后新资源**直接放行不检查**；`NullContext` 同样静默失效 |

**一句记忆钩子**：*"三道闸放行为王，一条链按 order 排队；统计先行、系统优先、熔断垫后。"*

---

## 第二讲：Node 与统计 —— LeapArray 滑动窗口

> **本讲问题**：dashboard 上的 QPS/RT/线程数从哪来？为什么统计不给业务加锁？

> 🎬 配套动画：[LeapArray 滑动窗口](/animations/leap-array/) —— 11 步单步播放，看 CAS 换桶、双桶接力与时钟回拨各发生在哪一刻。

### 2.1 三级节点树

```
EntranceNode（入口，Constants.ENTRY_NODE）
 └─ DefaultNode（每个 context 一个：NodeSelectorSlot 按 context name 建，:127 @Spi）
      └─ ClusterNode（每个资源名一个，全局唯一：ClusterBuilderSlot）
           └─ StatisticNode（统计实现）
                ├─ rollingCounterInSecond   = ArrayMetric(2, 1000ms)   // 秒级双桶
                └─ rollingCounterInMinute   = ArrayMetric(60, 60000ms) // 分钟级 60 桶
```

`NodeSelectorSlot` 的注释解释了为什么要区分 DefaultNode 和 ClusterNode（`slots/nodeselector/NodeSelectorSlot.java:136`）：**同一资源在不同 context 下要分开统计（调用链分析），又要能合并出资源级总量**——后者就是 `ClusterNode` 的职责，所有同资源的 DefaultNode 共享它。

### 2.2 StatisticNode：六个指标

```java
// StatisticNode.java:90-103
public class StatisticNode implements Node {
    private transient volatile Metric rollingCounterInSecond =
        new ArrayMetric(SampleCountProperty.SAMPLE_COUNT, IntervalProperty.INTERVAL);   // 默认 2 桶 × 500ms
    private transient volatile Metric rollingCounterInMinute =
        new ArrayMetric(60, 60 * 1000, false);                                          // 60 桶 × 1s
```

每个桶（`MetricNode`）用 `LongAdder` 记六组数：`pass`（放行）、`block`（拦截）、`success`、`exception`、`rt`（总耗时）、`thread`（并发线程）。`StatisticSlot` 的写入点：

| 时机 | 方法 | 记什么 |
|---|---|---|
| entry 通过 | `entry:55` → `node.increaseThreadNum()` + `node.addPassRequest(count)`（`:62-63`） | 线程数 +1、pass +count |
| entry 被拒 | `:82` `node.increaseThreadNum()`；`:101` `node.increaseBlockQps(count)` | 线程数 +1（然后减）、block +count |
| exit 正常 | `exit:126` → 减线程数、加 RT、success +count | rt、success |
| exit 异常 | `:163` `node.increaseExceptionQps(batchCount)` | exception |

`passQps()`（`StatisticNode.java:166`）= `rollingCounterInSecond.pass() / windowInterval`——**秒级窗口总数除以窗口长度**，这就是 dashboard 上 QPS 的算法。

### 2.3 LeapArray：无锁循环桶

```java
// LeapArray.java:116（currentWindow 节选）
public WindowWrap<T> currentWindow(long timeMillis) {
    int idx = calculateTimeIdx(timeMillis);
    long windowStart = calculateWindowStart(timeMillis);
    while (true) {
        WindowWrap<T> old = array.get(idx);
        if (old == null) {
            WindowWrap<T> window = new WindowWrap<>(windowLengthInMs, windowStart, newEmptyBucket(timeMillis));
            if (array.compareAndSet(idx, null, window)) {   // ★ CAS 换桶，只有一线程成功
                return window;
            }
        } else if (windowStart == old.windowStart()) {
            return old;                                     // 当前桶
        } else if (windowStart < old.windowStart()) {
            return new WindowWrap<>(windowLengthInMs, windowStart, newEmptyBucket(timeMillis));  // 时钟回拨
        } else {
            // 桶过期 → CAS 换新桶
            if (array.compareAndSet(idx, old, window)) { return window; }
        }
    }
}
```

三个设计点：

1. **`idx = (time / windowLength) % sampleCount`**——时间戳直接算出桶下标，天然循环；
2. **CAS 换桶**解决并发建桶；失败方自旋重试；
3. **时钟回拨**（`windowStart < old.windowStart()`）不覆盖旧桶，直接丢弃统计返回空桶——防 NTP 校时把数据打乱。

秒级用 2 个桶 × 500ms 而不是 1×1000ms，是为了**窗口切换时旧桶数据还能被 `previousWindow` 读到**（`StatisticNode.java:171` `previousWindowBlock`），避免边界跳变。

### 2.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | 三级树：DefaultNode（按 context）→ ClusterNode（按资源，全局唯一）→ StatisticNode（双窗口） |
| 关键实现 | `LeapArray.currentWindow:116` CAS 换桶；桶值 `LongAdder`；QPS = 窗口 pass / 窗口秒数 |
| 并发 | 全程无锁（CAS + LongAdder）；时钟回拨走空桶不覆盖 |
| 协议 | 秒级 2×500ms + 分钟级 60×1s；`previousWindow` 解决边界跳变 |
| 陷阱 | `entry` 后不 `exit` → 线程数只增不减，最终被线程规则全拒 |

**一句记忆钩子**：*"时间戳算下标、CAS 换桶、LongAdder 记账；同一资源一个 ClusterNode，同一入口多个 DefaultNode。"*

---

## 第三讲：FlowSlot 限流 —— 三种 TrafficShapingController

> **本讲问题**：`QPS > 10` 到底怎么判断超没超？匀速排队和快速失败在源码层差在哪？

### 3.1 FlowSlot → FlowRuleChecker → Controller

```java
// FlowSlot.java:160
public void entry(Context context, ResourceWrapper resourceWrapper, DefaultNode node, int count,
                  boolean prioritized, Object... args) throws Throwable {
    checkFlow(resourceWrapper, context, node, count, prioritized);
    fireEntry(context, resourceWrapper, node, count, prioritized, args);
}

// FlowRuleChecker.java:44
public void checkFlow(Function<String, Collection<FlowRule>> ruleProvider, ResourceWrapper resource,
                      Context context, DefaultNode node, int count, boolean prioritized) throws BlockException {
    Collection<FlowRule> rules = ruleProvider.apply(resource.getName());
    if (rules != null) {
        for (FlowRule rule : rules) {
            if (!canPassCheck(rule, context, node, count, prioritized)) {
                throw new FlowException(rule.getLimitApp(), rule);
            }
        }
    }
}
```

**规则遍历是"与"语义**：同一资源多条规则，任何一条不过就抛 `FlowException`。`canPassCheck:64` 按 `rule.isClusterMode()` 分本地/集群两条路，本地走 `passLocalCheck:78`——按 `strategy`（调用方维度）选出对应的 Node，再交给 `rule.getController().canPass(node, count)`。

### 3.2 三种 Controller

```java
// DefaultController.java:49（快速失败）
public boolean canPass(Node node, int acquireCount, boolean prioritized) {
    int curCount = avgUsedTokens(node);
    if (curCount + acquireCount > count) {
        if (prioritized && grade == RuleConstant.FLOW_GRADE_QPS) {
            // 高优先级请求：尝试占用未来令牌，等一小会儿再进来
            long waitInMs = node.tryOccupyNext(currentTime, acquireCount, count);
            ...
            sleep(waitInMs);                                   // ★ 优先级请求是"等"不是"拒"
        }
        return false;
    }
    return true;
}
```

| Controller | 算法 | 超限行为 | 场景 |
|---|---|---|---|
| `DefaultController` | 当前 QPS + count > 阈值 | **直接拒绝** | 大多数场景 |
| `WarmUpController` | 阈值按 `coldFactor` 从低往高爬 | 冷启动阶段阈值低，逐步升到配置值 | 服务刚启动防流量打死 |
| `ThrottlingController` / `WarmUpRateLimiterController` | 令牌桶 + **未来时间预支**（`tryOccupyNext`） | 拒绝（或 prioritized 时 sleep 等待） | 严格匀速 |

`WarmUpController`（`slots/block/flow/controller/WarmUpController.java:64`）的核心是 `warningToken` 曲线：阈值从 `count / coldFactor` 开始，经过 `warningToken / (count - count/coldFactor)` 时间爬到 `count`——**冷启动 10 秒内限流阈值只有配置的 1/3，防止缓存未热被打死**。

匀速排队的实现细节在 `Node.tryOccupyNext`（`node/Node.java` 的 `OccupySupport`）：算出"要等多少毫秒才有令牌"，`addWaitingRequest` 记账，然后 `sleep(waitInMs)` 再当次请求放行——**业务线程会真的睡**，所以匀速队列长度受业务线程数约束。

### 3.3 调用方维度：limitApp 三种策略

`FlowRule.limitApp`（`strategy`）决定统计哪个 Node：

| 策略 | 含义 | 选中的 Node |
|---|---|---|
| `STRATEGY_DIRECT`（默认） | 按调用方来源 | 本资源的 caller Node（按 origin） |
| `STRATEGY_RELATE` | 关联资源 | 关联资源的 Node |
| `STRATEGY_CHAIN` | 入口链路 | 从 EntranceNode 往下按链路找 |

`default` 表示不限调用方。集群模式下 `limitApp` 变成"按服务分组"，由 TokenServer 裁决。

### 3.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `FlowSlot.entry:160` → `FlowRuleChecker.checkFlow:44`（多条规则"与"）→ Controller |
| 关键实现 | `DefaultController.canPass:49` 快速失败；`WarmUpController:64` 冷启动爬坡；`ThrottlingController` 匀速睡眠 |
| 并发 | 读 `avgUsedTokens` 是滑动窗口值；`tryOccupyNext` 预支未来令牌 |
| 协议 | 策略 = 阈值类型（QPS/线程数）× 调用方（DIRECT/RELATE/CHAIN） |
| 陷阱 | prioritized 匀速会 `sleep` 业务线程；WarmUp 前几秒阈值只有 1/coldFactor |

**一句记忆钩子**：*"快速失败拒、冷启动爬、匀速睡；限流是入口闸，多条规则是与关系。"*

---

## 第四讲：DegradeSlot 熔断 —— CircuitBreaker 状态机

> **本讲问题**：熔断器的三态怎么转？半开为什么只放一个请求？慢调用比例的分子分母是什么？

### 4.1 DegradeSlot：链尾的守门人

```java
// DegradeSlot.java:37-45
@Spi(order = Constants.ORDER_DEGRADE_SLOT)
public class DegradeSlot extends AbstractLinkedProcessorSlot<DefaultNode> {
    @Override
    public void entry(Context context, ResourceWrapper resourceWrapper, DefaultNode node, int count,
                      boolean prioritized, Object... args) throws Throwable {
        performChecking(context, resourceWrapper);          // 逐个熔断器 tryPass
        fireEntry(context, resourceWrapper, node, count, prioritized, args);
    }
    void performChecking(Context context, ResourceWrapper r) throws BlockException {
        List<CircuitBreaker> circuitBreakers = DegradeRuleManager.getCircuitBreakers(r.getName());
        if (circuitBreakers == null || circuitBreakers.isEmpty()) { return; }
        for (CircuitBreaker cb : circuitBreakers) {
            if (!cb.tryPass(context)) {
                throw new DegradeException(cb.getRule().getLimitApp(), cb.getRule());
            }
        }
    }
```

`DegradeRuleManager` 在规则加载时就把 `DegradeRule` 转成 `CircuitBreaker` 对象（`DefaultCircuitBreakerRuleManager`）——**规则变、熔断器跟着重建**，热更新因此是原子的。

### 4.2 ResponseTimeCircuitBreaker：慢调用比例

```java
// ResponseTimeCircuitBreaker.java:34-115（节选）
private final long maxAllowedRt;          // 慢调用阈值 RT（ms）
private final double maxSlowRequestRatio; // 慢调用比例阈值
private final int minRequestAmount;       // 窗口内最少请求数
private final LeapArray<SlowRequestCounter> slidingCounter;   // 1 个桶 × statIntervalMs

public void onRequestComplete(Context context) {           // exit 时回调
    SlowRequestCounter counter = slidingCounter.currentWindow().value();
    long rt = completeTime - entry.getCreateTimestamp();
    if (rt > maxAllowedRt) { counter.slowCount.add(1); }    // 慢调用 +1
    counter.totalCount.add(1);                              // 总调用 +1
    handleStateChangeWhenThresholdExceeded(rt);
}

private void handleStateChangeWhenThresholdExceeded(long rt) {
    if (currentState.get() == State.OPEN) { return; }
    if (currentState.get() == State.HALF_OPEN) {
        // In detecting request
        if (rt > maxAllowedRt) { fromHalfOpenToOpen(1.0d); }   // 探测请求也慢 → 重新打开
        else { fromHalfOpenToClose(); }                        // 探测成功 → 关闭熔断
        return;
    }
    List<SlowRequestCounter> counters = slidingCounter.values();
    long slowCount = 0, totalCount = 0;
    for (SlowRequestCounter c : counters) { slowCount += c.slowCount.sum(); totalCount += c.totalCount.sum(); }
    if (totalCount < minRequestAmount) { return; }            // 样本不够不算
    double currentRatio = slowCount * 1.0d / totalCount;
    if (currentRatio > maxSlowRequestRatio) { transformToOpen(currentRatio); }
}
```

三个关键数：**`maxAllowedRt`（RT 阈值）、`maxSlowRequestRatio`（比例）、`minRequestAmount`（样本下限）**。配置 `grade=RT, count=200ms, slowRatioThreshold=0.5, minRequestAmount=10, statIntervalMs=10s` 的含义是：10 秒窗口内至少 10 个请求，其中超过一半 RT > 200ms 就熔断 10 秒（`timeWindow`）。

### 4.3 状态机与半开探测

```
CLOSED ──统计超阈值──► OPEN ──timeWindow 到──► HALF_OPEN ──探测请求──┬──成功──► CLOSED
   ▲                                                              └──失败/慢──► OPEN
   └────────────────────── fromHalfOpenToClose ──────────────────────┘
```

- `OPEN` 持续 `rule.getTimeWindow()` 秒后由 `AbstractCircuitBreaker` 的定时任务转 `HALF_OPEN`（`DefaultCircuitBreakerSlot` 注册的 observer）；
- `HALF_OPEN` 只放**一个探测请求**（源码注释 "In detecting request"），结果直接决定回 `CLOSED` 还是重新 `OPEN`；
- `fromHalfOpenToOpen(1.0d)` 传 1.0 表示"探测失败视为 100% 慢比例"。

`ExceptionCircuitBreaker`（异常比例/异常数）同构，只是统计维度换成 `exception` 与 `total`，且异常判定可以排除业务自己声明的 `exceptionsToIgnore`（`BlockException` 默认不算——**被 Sentinel 拒的请求不算异常**，否则熔断会自我激励）。

### 4.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `DegradeSlot.performChecking:41` 逐个 `CircuitBreaker.tryPass`；规则热更 = 重建熔断器对象 |
| 关键实现 | `onRequestComplete:62` 喂慢调用计数；`handleStateChangeWhenThresholdExceeded:81` 三态流转 |
| 并发 | 统计用 `LeapArray` 单桶；状态用 `AtomicReference<State>`；observer 注册在 `EventObserverRegistry` |
| 协议 | 慢调用 = RT > `maxAllowedRt`；比例 = slow/total；`minRequestAmount` 防小样本误判 |
| 陷阱 | 半开只放一个请求；`BlockException` 不计入异常熔断；异常数熔断已废弃 |

**一句记忆钩子**：*"熔断三态 CLOSED→OPEN→HALF_OPEN，半开一箭定生死；慢调用比例 = 慢数/总数，样本不够不熔。"*

---

## 第五讲：SystemSlot / AuthoritySlot / ParamFlowSlot

> **本讲问题**：机器快挂了 Sentinel 怎么自适应？按调用方放行/拒绝怎么配？热点参数限流的"热点"限在哪？

### 5.1 SystemSlot：五种系统指标 + BBR

```java
// SystemRuleManager.java:290（checkSystem）
public static void checkSystem(ResourceWrapper resourceWrapper, int count) throws BlockException {
    if (!checkSystemStatus.get()) { return; }                       // 开关
    if (resourceWrapper.getEntryType() != EntryType.IN) { return; } // 只拦入口流量
    // ① 全局 QPS
    double currentQps = Constants.ENTRY_NODE.passQps();
    if (currentQps + count > qps) { throw new SystemBlockException(resourceWrapper.getName(), "qps"); }
    // ② 全局线程数
    int currentThread = Constants.ENTRY_NODE.curThreadNum();
    if (currentThread > maxThread) { throw new SystemBlockException(resourceWrapper.getName(), "thread"); }
    // ③ 全局平均 RT
    double rt = Constants.ENTRY_NODE.avgRt();
    if (rt > maxRt) { throw new SystemBlockException(resourceWrapper.getName(), "rt"); }
    // ④ 系统负载（BBR）
    if (highestSystemLoadIsSet && getCurrentSystemAvgLoad() > highestSystemLoad) {
        if (!checkBbr(currentThread)) { throw new SystemBlockException(resourceWrapper.getName(), "load"); }
    }
    // ⑤ CPU 使用率
    if (highestCpuUsageIsSet && getCurrentCpuUsage() > highestCpuUsage) {
        throw new SystemBlockException(resourceWrapper.getName(), "cpu");
    }
}

private static boolean checkBbr(int currentThread) {
    if (currentThread > 1 &&
        currentThread > Constants.ENTRY_NODE.maxSuccessQps() * Constants.ENTRY_NODE.minRt() / 1000) {
        return false;                                                // 超过理论最优并发 → 拒
    }
    return true;
}
```

**五道系统指标**全部读 `Constants.ENTRY_NODE`（全局入口节点）的统计——所以系统规则天然是"全局视角"，与具体资源无关。`EntryType.IN` 的判断保证只拦入口流量，内部调用不受系统规则影响。

**BBR 那行是这个仓库最有含量的代码**（`:337` `checkBbr`）：`maxSuccessQps × minRt / 1000` 是**理论最优并发数**（Little's law：并发 = 吞吐 × 延迟）。当机器 load 超阈值时，只放进"最优并发"以内的请求——**等价于"让每个请求以最低 RT 跑，而不是把线程全占满"**，这正是 Google BBR 的思想。

`SystemRuleManager` 还维护 `highestSystemLoad`（`Double.MAX_VALUE`，`:68`）——启动后由 `checkSystemStatus` 控制首次探测，把当前 load 记为基线（`:194-215` 的重置逻辑），避免重启进程时把历史高水位当成本限。

### 5.2 AuthoritySlot：来源黑白名单

```java
// AuthoritySlot.java:34
@Spi(order = Constants.ORDER_AUTHORITY_SLOT)
public class AuthoritySlot extends AbstractLinkedProcessorSlot<DefaultNode> {
    // entry: 从 ContextUtil 取 origin，按 AuthorityRule 的 strategy
    //（AUTHORITY_WHITE=0 白名单 / AUTHORITY_BLACK=1 黑名单）判定
    // 不匹配 → throw new AuthorityException()
}
```

判定依据是 `ContextUtil.getContext().getOrigin()`——**origin 来自调用方注册**（`SphU.entry(name, type, count, origin)` 或适配器从请求头取），所以 AuthoritySlot 的可靠性取决于 origin 是否可信（内部服务之间可信，外部流量要防伪造）。注意**没有配 AuthorityRule 的资源完全放行**。

### 5.3 ParamFlowSlot：热点参数限流

```java
// ParamFlowSlot.java:34（sentinel-extension/sentinel-parameter-flow-control）
@Spi(order = -3000)
public class ParamFlowSlot extends AbstractLinkedProcessorSlot<DefaultNode> {
    public void entry(...) throws Throwable {
        // rule.getParamIdx()：第几个参数（0 起；负数表示"从后往前数"）
        int paramIdx = rule.getParamIdx();
        if (paramIdx < 0) {
            if (-paramIdx <= length) { ... }
        }
        // 按"参数值"分别建 ParameterMetric（key=参数值）
        // 先查单参数值阈值 paramIdx + paramValue 的计数，再查资源级总阈值
        // 任一超限 → ParamFlowException
    }
}
```

两级阈值：**单参数值阈值**（`paramFlowRule.getCount()`，某个热点值专属）+ **资源级总阈值**（`rule.getBurstCount()` 之外还有一个"所有参数值合计"的默认统计）。典型配置："接口总阈值 500 QPS，但 `userId=10086` 这个热点值单限 10 QPS"。

热点统计用 `ParameterMetric`（每个参数值一个 `ParameterMetricIdentifier` → 一组 `LeapArray`），所以热点限流的开销与"参数值基数"成正比——**高基数字段（如订单号）做热点限流会内存爆炸**，这是它只适合低基数枚举值（userId 少量热号、商品 id 少量爆款）的原因。

### 5.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | SystemSlot 读 `ENTRY_NODE` 全局统计；AuthoritySlot 读 `Context.origin`；ParamFlowSlot 按参数值分桶 |
| 关键实现 | `checkBbr:337` = 理论最优并发（`maxSuccessQps × minRt / 1000`）；参数值分别统计 |
| 并发 | `highestSystemLoad` 启动时探测基线（`:194`）；热点计数器随基数线性增长 |
| 协议 | 系统五指标（QPS/线程/RT/load/CPU）+ BBR；origin 黑白名单；paramIdx 支持负索引 |
| 陷阱 | 系统规则只拦 `EntryType.IN`；高基数参数禁做热点限流；origin 可伪造需网关层保证 |

**一句记忆钩子**：*"系统保护看全局入口节点，BBR 只放进最优并发的请求；热点限的是低基数的参数值。"*

---

## 第六讲：规则推送与集群限流 —— DataSource / Transport / TokenService

> **本讲问题**：控制台改的规则怎么秒级生效到几百个实例？集群限流为什么还要一个 TokenServer？

### 6.1 规则热更：SentinelProperty 观察者模式

所有 `RuleManager` 都遵循同一模式：

```java
// FlowRuleManager.java（同构于 SystemRuleManager.register2Property:107）
public static void register2Property(SentinelProperty<List<FlowRule>> property) {
    synchronized (listener) {
        RecordLog.info("[FlowRuleManager] Registering new property to flow rule manager");
        currentProperty.removeListener(listener);
        property.addListener(listener);
        currentProperty = property;
    }
}
```

`listener` 是 `PropertyListener<List<FlowRule>>`：`configUpdate(newValue)` 里做 `flowRules.clear() + addAll(newValue)` + 重建 `FlowRuleMap`（规则按资源名分组的 Map）。**换 property 是原子的**，更新期间规则读取不受影响。

数据源家族（`sentinel-extension/`）负责把远程配置变成 property：

| DataSource | 机制 | 文件 |
|---|---|---|
| `FileRefreshableDataSource` | 定时读本地文件（默认 30s） | core |
| `NacosDataSource` | Nacos 监听 + `readSource():137` 拉全量 | `sentinel-extension/sentinel-datasource-nacos/src/main/java/com/alibaba/csp/sentinel/datasource/nacos/NacosDataSource.java:71` |
| `ZookeeperDataSource` | ZK 节点监听 | `sentinel-datasource-zookeeper/` |
| `ApolloDataSource` | Apollo 长轮询 | `sentinel-datasource-apollo/` |
| `RedisDataSource` | Pub/Sub | `sentinel-datasource-redis/` |
| 控制台推送 | HTTP 长连接（`sentinel-transport-simple-http`） | `sentinel-transport/` |

**配置格式约定**：dataId = `应用名-规则类型.json`（如 `order-service-flow-rules`），内容是规则 JSON 数组。Nacos 的 `readSource` 拉一次全量覆盖——所以规则是**最终一致的全量替换**，不是增量。

### 6.2 集群限流：TokenService 协议

单机限流的死穴是"每台机器阈值独立，集群总量不可控"。Sentinel 的解法是**全局统计、本地预留**：

```
TokenClient（业务进程内）
  └─ ClusterTokenClient（sentinel-core/.../cluster/client/ClusterTokenClient.java）
       └─ TokenService.requestToken(ruleId, acquireCount, prioritized)   // :36
            └─ 向 TokenServer 发请求（Netty / envoy-rls）
                 ├─ 允许 → TokenResult(OK)
                 ├─ 拒绝 → TokenResult(BLOCKED)
                 └─ Server 不可用 → 降级为本地限流（Fallback）
```

`FlowRule` 配 `clusterMode = true` 时，`FlowRuleChecker.canPassCheck:67` 走 `passClusterCheck`：先问 TokenServer，Server 不在线就**退回本地 `passLocalCheck`**（fail-open 到单机限流）——**集群限流永远不应该比单机限流更严格地拖垮业务**。

`DefaultEmbeddedTokenServer`（`sentinel-cluster/sentinel-cluster-server-default/src/main/java/com/alibaba/csp/sentinel/cluster/server/DefaultEmbeddedTokenServer.java:47`）是嵌入模式：TokenServer 跑在某个业务进程里（或独立进程），用 `GlobalQuotaManager` 按 `flowId` 维护全局令牌桶，`requestToken` 返回剩余配额。两种部署：

| 模式 | 部署 | 一致性 |
|---|---|---|
| 独立 TokenServer | 单独进程集群 | 强一致（单点统计） |
| 嵌入模式 | 选一台业务机兼职 | 弱（兼职机挂则降级） |

### 6.3 Transport：规则下发 + 监控上报

`sentinel-transport` 系列负责两件事：**规则推送通道**（控制台 → 客户端，HTTP long-polling）和**监控上报**（客户端 → 控制台，`sentinel-transport-common` 的 `HeartbeatSender` 定时发心跳 + metric）。dashboard 看到的实时曲线来自客户端的秒级统计聚合上报——**所以 dashboard 断线只影响展示，不影响限流熔断本身**（规则在客户端本地生效）。

### 6.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `RuleManager.register2Property:107` 观察者模式；DataSource 家族产 property；TokenClient/Server 管集群 |
| 关键实现 | 规则更新 = 整体替换 + 重建 Map；`readSource:137` 全量覆盖；`requestToken:36` 集群裁决 |
| 并发 | 换 property 原子；TokenServer 单点统计、客户端 fail-open 回退本地 |
| 协议 | dataId 约定 `应用-规则类型.json`；集群规则按 `flowId` 全局唯一 |
| 陷阱 | 嵌入 TokenServer 是单点；dashboard 断线不限流；高 QPS 下集群 RTT 是损耗 |

**一句记忆钩子**：*"规则走 property 观察者、远程全量替换；集群限流先问 TokenServer，问不到退回本地。"*

---

## 附：学习路线与复习卡汇总

### 建议学习顺序（15 讲时）

| 阶段 | 内容 | 时长建议 | 产出 |
|---|---|---|---|
| 第 1 天 | 第一、二讲（责任链 + 统计） | 4h | 默写 slot 顺序与理由 |
| 第 3 天 | 第三讲（限流三种 controller） | 3h | 说清匀速与预热差异 |
| 第 7 天 | 第四讲（熔断状态机） | 3h | 画出三态转换与半开探测 |
| 第 14 天 | 第五、六讲（系统/热点/集群） | 4h | 给出生产规则配置清单 |

### 复习卡（遮答案自测）

1. `SphU.entry` 的三道闸是什么？各自的放行理由？
2. slot 默认顺序（写得出 order 值）？为什么 Statistic 在规则前？
3. `chainMap` 为什么用整体换 Map 而不是 CHM？
4. `LeapArray` 怎么算桶下标？并发建桶怎么解决？时钟回拨怎么办？
5. QPS 在源码里怎么算的（分子分母）？
6. 三种限流 controller 的差异与适用场景？
7. `WarmUpController` 前几秒的阈值是多少？
8. 匀速排队超限时业务线程在干什么？
9. 熔断三态转换条件？半开放几个请求？
10. 慢调用比例的分子分母？`minRequestAmount` 防什么？
11. 为什么 `BlockException` 不算异常熔断？
12. BBR 那行的公式含义？
13. 系统规则为什么只拦 `EntryType.IN`？
14. 热点参数限流为什么不能用于高基数字段？
15. 规则热更的机制？TokenServer 挂了会怎样？

### 六讲记忆钩子串烧

*"三道闸放行为王，统计先行、系统优先、熔断垫后"* → *"时间戳算下标、CAS 换桶、LongAdder 记账"* → *"快速失败拒、冷启动爬、匀速睡"* → *"熔断三态，半开一箭定生死"* → *"系统保护看全局入口，BBR 只放进最优并发"* → *"规则全量替换，集群先问 Server、问不到退回本地"*

---

## 涉及文件索引

| 模块 | 文件 | 本笔记引用次数 |
|---|---|---|
| core | `sentinel-core/src/main/java/com/alibaba/csp/sentinel/CtSph.java` | 高 |
| core | `.../Constants.java`（order 常量、MAX_CONTEXT_NAME_SIZE） | 高 |
| core | `.../slotchain/DefaultSlotChainBuilder.java`、`.../slotchain/AbstractLinkedProcessorSlot.java`、`.../slotchain/ProcessorSlotChain.java` | 高 |
| core | `.../slots/DefaultSlotChainBuilder.java`、`.../slots/nodeselector/NodeSelectorSlot.java`、`.../slots/clusterbuilder/ClusterBuilderSlot.java`、`.../slots/logger/LogSlot.java` | 高 |
| core | `.../slots/statistic/StatisticSlot.java`、`.../slots/statistic/base/LeapArray.java`、`.../slots/statistic/metric/ArrayMetric.java` | 高 |
| core | `.../node/StatisticNode.java`、`.../node/DefaultNode.java`、`.../node/ClusterNode.java`、`.../node/Node.java` | 高 |
| core | `.../slots/block/flow/FlowSlot.java`、`.../FlowRuleChecker.java`、`.../FlowRule.java` | 高 |
| core | `.../slots/block/flow/controller/{DefaultController,WarmUpController,ThrottlingController,TrafficShapingController}.java` | 高 |
| core | `.../slots/block/degrade/DegradeSlot.java`、`.../circuitbreaker/{ResponseTimeCircuitBreaker,ExceptionCircuitBreaker,AbstractCircuitBreaker}.java`、`.../DefaultCircuitBreakerRuleManager.java` | 高 |
| core | `.../slots/system/SystemSlot.java`、`.../SystemRuleManager.java` | 高 |
| core | `.../slots/block/authority/AuthoritySlot.java` | 中 |
| core | `.../cluster/TokenService.java`、`.../cluster/client/ClusterTokenClient.java`、`.../cluster/server/DefaultEmbeddedTokenServer.java` | 中 |
| extension | `sentinel-extension/sentinel-parameter-flow-control/.../ParamFlowSlot.java` | 中 |
| extension | `sentinel-extension/sentinel-extension/sentinel-datasource-nacos/src/main/java/com/alibaba/csp/sentinel/datasource/nacos/NacosDataSource.java`、`sentinel-datasource-zookeeper`/`apollo`/`redis` | 中 |
| adapter | `sentinel-adapter/`（spring-webmvc、dubbo、grpc、reactor 等） | 低 |
| cluster | `sentinel-cluster/sentinel-cluster-server-default/.../DefaultEmbeddedTokenServer.java`、`sentinel-cluster-client-default/` | 中 |
| dashboard | `sentinel-dashboard/`（规则 CRUD + 监控展示） | 低 |
