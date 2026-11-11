---
title: "Kafka 实战与参数手册"
date: 2026-11-11
categories:
  - "中间件"
tags:
  - "Kafka"
  - "调优"
  - "参数"
  - "运维"
  - "实战"
---

> 配套文档：`kafka.md`（《Kafka 源码深度学习笔记》）讲的是**每个类怎么实现的**；本文讲的是**上线前该怎么选、该怎么配、出了问题该怎么查**。
> 源码仓库：`D:/work/2026/deep-dive/sources/kafka`，版本 tag `4.3.1`，commit `26b251a451ce941d3d7a55e6487bcb7f16b5ad48`。
> 所有结论标注了源文件位置（`相对仓库根目录:行号`）。
> **版本提醒**：本文所有默认值以 Kafka **4.3.1** 为准。Kafka 3.x 及以前的默认值与 4.x 差异很大——`acks` 默认从 `1` 改成 `all`、`enable.idempotence` 默认从 `false` 改成 `true`、`linger.ms` 默认从 `0` 改成 `5`。网上流传的"默认值表"大多停留在 2.x 时代，直接照抄会配错。

### 常见说法的版本修正（先看这里）

| 常见说法 | 2.x/3.x 时代 | **Kafka 4.3.1 实际** | 源码锚点 |
|---|---|---|---|
| `acks` 默认是 `1` | ✅ | ❌ 默认已是 **`all`** | `ProducerConfig.java:393` |
| 需要手动开幂等 | ✅ | ❌ `enable.idempotence` 默认已 **`true`** | `ProducerConfig.java:529` |
| `linger.ms` 默认 `0`（攒够就发） | ✅ | ❌ 默认已是 **5ms** | `ProducerConfig.java:405` |
| 消费者默认就手动提交 | ❌ | ❌ 仍是 `true`，**必须显式关** | `ConsumerConfig.java:458` |
| `min.insync.replicas=2` = 两个副本写成功 | ✅（误解） | ❌ 是"ISR 少于 2 就**拒绝写**" | `Partition.scala:1234` |

---

## 〇、全景地图

```
        业务方 A          业务方 B          业务方 C
           │ send()          │ send()          │ send()
           ▼                 ▼                 ▼
      ┌─────────────────────────────────────────────┐
      │  Producer 端（每进程一个实例）               │
      │  序列化 → 分区器(按 key 哈希) → 攒批         │
      │  RecordAccumulator(内存池) → Sender → 攒够/超时 │
      └─────────────────────────────────────────────┘
           │ Produce(acks=all)
           ▼
┌────────────────────────────────────────────────────────┐
│ Broker 集群（KRaft 模式，无 ZooKeeper）                   │
│  Leader Replica ──append──▶ 磁盘（顺序写 Segment）        │
│       │ 同步复制                                      │
│       ├──▶ Follower Replica 1（ISR）                    │
│       └──▶ Follower Replica 2（ISR）                    │
│  AR = ISR ∪ OSR；HW = ISR 中最慢副本的 LEO              │
└────────────────────────────────────────────────────────┘
           │ Fetch（长轮询）
           ▼
      ┌─────────────────────────────────────────────┐
      │  Consumer 端（Consumer Group 内并行消费）        │
      │  poll() → Fetcher → 批量拉取 → 业务处理 → 提交offset│
      └─────────────────────────────────────────────┘
```

一句话概括 Kafka 的设计哲学：**用"顺序写 + 批量 + 内存换磁盘"换吞吐，用"多副本 + ISR"换可靠，用"分区"换并行度**。所有调参都是在这三组取舍之间找平衡点。

---

## 一、为什么需要消息队列

先说结论：**引入 MQ 是用"最终一致 + 异步"换"解耦 + 削峰"，代价是数据多了一次落地、链路多了一处可能出错的地方。**

同步调用（HTTP/RPC）是"打电话"——必须等对方响应才能继续。消息队列是"发微信"——投递完就走，消费者有空再处理。三个典型收益：

| 收益 | 本质 | 代价 |
|---|---|---|
| **解耦** | 生产者只认 Topic，不认消费者是谁；新增下游不用改上游代码 | 多了一个中间环节，排障链路变长 |
| **削峰填谷** | 突发流量先进 Topic 排队，消费端按自己能力匀速消费 | 堆积期间消费延迟上升，需监控 lag |
| **异步** | 主流程只做入队，不等下游 | 失去了"下游立即失败则主流程也失败"的强一致 |

**什么时候不该上 MQ**：链路短、QPS 低、要求强一致（转账扣款必须同步成功）时，引入 MQ 只会把简单问题复杂化。Kafka 的分区顺序保证也帮不上忙——它只保证**单个分区内**有序。

选型上，Kafka 与传统 MQ 的定位不同：Kafka 面向**高吞吐日志流**（日志、埋点、流式计算、事件溯源），RabbitMQ 面向**任务路由**（复杂路由、延迟队列、事务消息）。需要复杂路由或 per-message ack 时，Kafka 并不是最合适的。

---

## 二、整体架构：四个角色

| 角色 | 职责 | 源码锚点 |
|---|---|---|
| **Producer** | 序列化 → 按 key 分区 → 攒批 → 发送 | `RecordAccumulator.java:285`（`computeIfAbsent` 按 topic 建 `TopicInfo`） |
| **Broker** | 接收、存储、复制、对外服务 | `KafkaApis.scala:171`（`ApiKeys.FETCH => handleFetchRequest`） |
| **Consumer** | 拉取、处理、提交位点 | `ConsumerConfig.java:90`（`max.poll.records`） |
| **Topic / Partition** | 逻辑队列 → 物理分片 | `Partition.scala`（分区状态机） |

关键认知：**Topic 是逻辑概念，Partition 才是物理存储单元**。一个 Topic 有 N 个 Partition，每个 Partition 在每个 Broker 上有一个目录，里面躺着若干 Segment 文件。分区数决定并行度上限——**消费线程数超过分区数，多出来的线程只能空转**。

---

## 三、Partition：并行度的来源

一个 Partition 对应 Broker 上的一个目录，数据以**顺序追加**方式写入 Segment 文件，每条消息在分区内有递增的 offset。

由此推出三条硬约束：

1. **分区内严格有序，跨分区无序**。所以"全局有序"只能靠单分区实现，而单分区又限制了吞吐——这是 Kafka 无法同时给"全局有序 + 高吞吐"的原因。
2. **一个 Partition 同时只能被一个消费者消费**（同一消费组内）。想提高消费并发，加分区，不是加线程。
3. **分区数只能增不能减**（历史数据归属不可变）。建 topic 时分区数要一次给够，或用 `kafka-topics.sh --alter --partitions` 扩。

**业务 key 决定分区归属**：相同 key 走同一个分区，这是实现"按订单顺序处理"的标准手段——生产者侧保证有序，不要指望消费者侧排序。

---

## 四、Replica：Leader / Follower 与读写分离

每个 Partition 配置多个副本，角色严格二分：

| 角色 | 职责 | 说明 |
|---|---|---|
| **Leader** | 处理**所有**读写请求 | 写请求只能发往 Leader，读请求默认也走 Leader |
| **Follower** | 异步拉取 Leader 日志，保持同步 | 不对外服务（除非配 `read.only.replicas`） |

Follower 存在的唯一理由是**数据冗余 + 故障接管**。它不参与写路径，所以复制是异步的——这个异步窗口正是"消息可能丢"的地方，靠 `acks` + ISR 收口。

---

## 五、ISR：Kafka 可靠性的核心机制

这是整份材料里最值得细读的一段，也是最容易被误解的地方。

- **AR（Assigned Replicas）**：某个分区所有副本的集合
- **ISR（In-Sync Replicas）**：与 Leader 保持同步的副本子集
- **OSR（Out-of-Sync Replicas）**：AR − ISR，跟不上的那批

ISR 是**动态**的。Broker 用"心跳没按时到达"来判定副本掉队：`replica.lag.time.max.ms`（默认 30s）内没完成同步的副本会被踢出 ISR（`Partition.scala:1099` 的 `getOutOfSyncReplicas` → `prepareIsrShrink`，`:1117`），落后太多则直接从 OSR 移除。

**ISR 的两个作用**：

1. **Leader 选举的候选人池**。Leader 宕机时只能从 ISR 里选新 Leader——因为只有 ISR 里的副本保证数据是最新的。如果从 OSR 选，可能丢数据。
2. **决定 HW（高水位）**。HW 取 ISR 中**最慢**副本的 LEO（Log End Offset）。含义是：HW 之前的数据，ISR 里每个副本都有；HW 之后的数据，只有 Leader 有。消费者**只能读到 HW**，不能读到 HW 之后——否则 Leader 挂了换人后会发现数据消失。

**`unclean.leader.election.enable=false`（默认）**：禁止从 OSR 选 Leader，宁可整个分区不可用也不冒丢数据的风险。设为 `true` 能提高可用性，但会丢数据，生产环境通常保持默认。

---

## 六、消息不丢失：三端协同

消息从生产者到消费者手里，要过三关。**任何一端单独配置都不够**。

### 6.1 生产者端

| 参数 | 作用 | 说明 |
|---|---|---|
| `acks=all` | 等 ISR 中**所有**副本确认才返回成功 | 4.x 已是默认值 |
| `enable.idempotence=true` | 去重 + 保证单分区顺序 | 4.x 已是默认值 |
| `retries` | 重试次数 | 配合幂等才安全 |
| `delivery.timeout.ms` | 投递总超时 | 默认 120s（`ProducerConfig.java:168`） |

`acks=all` 的精确语义在 `Partition.scala:1234`：

```scala
if (inSyncSize < minIsr && requiredAcks == -1) {
  throw new NotEnoughReplicasException(...)
}
```

即：**acks=-1 时，若当前 ISR 数量少于 `min.insync.replicas`，写入直接失败**——宁可拒绝服务也不冒丢数据风险。

### 6.2 服务端（Topic 级）

| 参数 | 推荐值 | 作用 |
|---|---|---|
| `replication.factor` | 3 | 副本数 |
| `min.insync.replicas` | 2 | ISR 少于这个数就拒绝写 |
| `unclean.leader.election.enable` | false | 禁止脏选举 |

这三个都在 `TopicConfig.java`：`MIN_IN_SYNC_REPLICAS_CONFIG`（`:174`）、`UNCLEAN_LEADER_ELECTION_ENABLE_CONFIG`（`:167`）。

> 常见误解：`min.insync.replicas=2` 不是"至少两个副本写成功才算成功"，而是"**ISR 里至少要有 2 个**才允许写入"。若副本掉队导致 ISR 缩到 1，即使 acks=all 也会写失败——这是**故意的**，用可用性换一致性。

### 6.3 消费者端

必须**关闭自动提交、改为手动提交**，且在业务处理成功后再提交位点。

Kafka 4.3.1 中 `enable.auto.commit` 默认仍是 `true`（`ConsumerConfig.java:458`），但用默认 group.id 时会被强制改为 `false`（`:756` 的 `maybeOverrideEnableAutoCommit`）。**显式指定 group.id 时默认仍是自动提交**——所以生产代码必须显式写 `enable.auto.commit=false`。

手动提交的写法：

```java
consumer.poll(Duration.ofMillis(500));  // 拿到一批
processAll(records);                      // 业务全部成功
consumer.commitSync();                    // 再提交位点
```

自动提交是在 `poll()` 时提交**上次 poll 的位置**，所以业务还没处理完，下一次 poll 就把位点推进了——崩溃后这批消息就丢了。

---

## 七、为什么快：四大机制

### 7.1 磁盘顺序写

Kafka 只在日志末尾追加，**完全不做随机写**。机械磁盘的瓶颈是寻道（seek），顺序写吞吐可以是随机写的几十倍。SSD 时代顺序写的优势缩小，但省掉的是内存拷贝而非寻道，仍值得。

### 7.2 页缓存（Page Cache）

生产者写入先落到 OS 页缓存就返回，由内核异步刷盘。Kafka 因此"看起来像在用内存"。

这也意味着：**机器重启或掉电，未刷盘的数据会丢**。所以 `acks=all` 之外，副本机制正是为了对抗这个风险——Leader 落盘前已经同步给了 Follower。

### 7.3 零拷贝

消费时若走「读文件 → 写 socket」的传统路径，数据要经过用户态两次拷贝。Kafka 直接调 `sendfile` 系统调用，让数据从页缓存直达网卡。

源码证据在 `PlaintextTransportLayer.java:213`：

```java
public long transferFrom(FileChannel fileChannel, long position, long count) throws IOException {
    return fileChannel.transferTo(position, count, socketChannel);
}
```

`transferTo` 在目标 `SocketChannel` 是 JDK 内部实现时才能走真正的零拷贝快路径（`TransferableChannel.java:41` 的注释说明了这条限制）。**注意**：SSL 通道走不了这条路——`SslTransportLayer.java:1003` 的 `transferFrom` 因为要加密，必须把数据读上来做拷贝。

### 7.4 批量与压缩

- **攒批**：`RecordAccumulator` 按分区把多条消息合并成一个 batch（`RecordAccumulator.java:329` 用 `Math.max(batchSize, ...)` 估算上限），`linger.ms` 给攒批留窗口
- **批量拉取**：消费者 `max.poll.records` 默认 500 条（`ConsumerConfig.java:94`）
- **压缩**：`compression.type` 默认可配 `lz4`/`zstd`/`snappy`，压缩后网络与磁盘都省

批量是把"每条一次系统调用 + 一次网络往返"摊薄成"一批一次"，这是吞吐提升最大的单一因素。

---

## 八、消费者组：队列模式与广播模式

**Consumer Group** 是消费端的逻辑容器，它把两种传统模式统一进了同一个系统：

| 模式 | 配置 | 效果 |
|---|---|---|
| **队列模式**（点对点） | 多个消费者**同一 group.id** | 分区在组内分配，每个分区只被一个消费者处理，**一条消息只被消费一次** |
| **广播模式**（发布订阅） | 多个消费者**不同 group.id** | 每个组各自维护位点，**每个组都收到完整一份** |

核心规则：**一个分区在同一消费者组内，同一时刻只能被一个消费者消费**。

每个消费组独立维护自己的 offset（存在 `__consumer_offsets` 内部 Topic 里），这是广播模式能成立的原因——不同组看到的是不同的位点。

分配策略（`partition.assignment.strategy`，`ConsumerConfig.java:151`）：

- `RangeAssignor` —— 按 topic 区间分配，**不跨 topic 均衡**，容易倾斜
- `RoundRobinAssignor` —— 轮询分配，分布均匀
- `StickyAssignor` —— 尽量保持原有分配，减少移动
- `CooperativeStickyAssignor` —— **增量式 rebalance**，先撤销再assign，避免"Stop The World"

---

## 九、Rebalance：Stop The World 及其代价

**触发条件**：
- 组内消费者数量变化（扩缩容、宕机、进程重启）
- Topic 分区数变化
- 订阅的 Topic 列表变化

**过程**：组内所有存活消费者暂停消费（停止拉取新数据），向 Coordinator 上报自己的状态，Coordinator 计算新分配方案，下发后恢复消费。

**代价**：Rebalance 期间**整个组停止消费**。频繁 Rebalance 是消费延迟的常见元凶——比如消费者处理一批消息耗时超过 `max.poll.interval.ms`（默认 5 分钟，`ConsumerConfig.java:97`），就被踢出组 → 触发 Rebalance → 它重新加入 → 又触发 Rebalance，形成恶性循环。

**减少 Rebalance 的手段**：
1. 用 `CooperativeStickyAssignor` 做增量分配
2. 把 `max.poll.interval.ms` 调到大于单批处理耗时
3. 用**静态成员**（`group.instance.id`，`ConsumerConfig.java:86`）避免成员变化引发 Rebalance
4. 合理设置 `session.timeout.ms`（默认 45s），让心跳超时检测不要过于灵敏

---

## 十、推还是拉：Kafka 选择 Pull

Kafka 选择 **Pull（拉）模式**——消费者主动调 `poll()` 向 Broker 请求数据。

**为什么是 Pull**：
- 把主动权交给消费者，消费者按自己的处理能力控制拉取节奏，不会被上游流量冲垮
- 消费进度天然由消费者自己持有，无需 Broker 维护每个消费者的游标
- 天然支持"只消费到 HW"的安全语义

**长轮询优化空轮询**：若消费者请求时无数据，Broker 不会立刻返回空响应，而是把请求挂起一段时间，等有新数据或超时才响应。

源码证据：
- `ConsumerConfig.java:210` — `DEFAULT_FETCH_MAX_WAIT_MS = 500`
- `ConsumerConfig.java:187` — `DEFAULT_FETCH_MIN_BYTES = 1`
- `KafkaApis.scala:744` — `fetchMinBytes = Math.min(fetchRequest.minBytes, fetchMaxBytes)`

即：**满足 `fetch.min.bytes` 就立刻返回，否则最多挂起 `fetch.max.wait.ms`（默认 500ms）**。这既降低了空轮询的 CPU 浪费，也把消费延迟控制在亚秒级。

> 消费端长轮询和生产端的 `linger.ms`（4.x 默认 5ms，即最多等 5ms 攒批）是两个方向相反的延迟/吞吐旋钮：调大 `linger.ms` 提升吞吐、增大发送延迟；调大 `fetch.max.wait.ms` 降低空转、增大消费延迟。

---

## 十一、重复消费与堆积治理

### 11.1 保证消息顺序

Kafka **只保证单分区内有序**。要保证一组消息的相对顺序，在发送端用**相同业务 key**（如订单 ID）路由到同一分区：

```java
producer.send(new ProducerRecord<>("orders", orderId, orderJson));
```

`RecordAccumulator` 按 topic-分区维度攒批，key 相同 → 分区相同 → 天然有序。

### 11.2 避免重复消费

Kafka 的投递语义是 **at-least-once**，重复消费是常态而非异常。要求业务代码**幂等**：

- **数据库层**：建唯一索引，捕获重复键异常后忽略（材料中提到的方式）
- **业务层**：用消息唯一 ID（如订单 ID）做去重表，或条件更新 `WHERE status = 'INIT'`
- **消费端**：关自动提交 + 处理成功后手动提交（见 6.3）

> 注意：手动提交仍可能重复。提交成功但业务事务回滚、或提交前崩溃，都会导致重投。**幂等是业务层的必答题，不是可选项**。

### 11.3 消息堆积治理

堆积的本质是**消费能力 < 生产速度**。治理顺序：

1. **先扩容消费者**——最直接。注意消费者数不应超过分区数（否则空转）。
2. **再扩分区**——`kafka-topics.sh --alter --partitions` 增加分区数后，同组内重新分配。注意：**扩分区会破坏原有 key→分区的映射**，同一 key 的新旧消息可能落到不同分区，顺序断裂。
3. **临时 Topic 分流**——新建分区数远多于原 Topic 的临时 Topic，把积压消息转发过去，起一批新消费者并行消费临时 Topic，最后再把结果合回。
4. **从源头限流**——生产端降级或限流，避免继续堆积。

监控上盯这三个指标：
- **Consumer Lag**（`kafka-consumer-groups.sh --describe`）：每个分区的消费延迟，是堆积的直接信号
- **Under-replicated partitions**：ISR 缩水的分区数
- **Isr Shrinks / Expands**：副本进出 ISR 的频率，频繁变动说明集群不稳定

---

## 十二、为什么换成 KRaft

### 12.1 旧架构的痛点

ZooKeeper 时代：集群元数据（Topic、分区、副本位置、Controller、ACL、配置）全存在 ZK 里。

| 痛点 | 说明 |
|---|---|
| 运维复杂度 | 必须额外部署运维 ZK 集群 |
| 元数据加载瓶颈 | 几万 Topic 时，ZK 写性能成为扩展天花板 |
| 一致性隐患 | 元数据在 ZK、消息在 Kafka，两套系统可能出现脑裂 |
| Controller 单点 | ZK 选出的 Controller 是单点，切换需要重新加载全部元数据 |

### 12.2 KRaft 的做法

**元数据也变成日志**：`__cluster_metadata` 是一个内部 Topic，Controller 变更（建 Topic、改配置、选举）以日志记录追加进去，用 Raft 保证副本一致。好处是复用了 Kafka 已有的日志复制机制——**元数据也享受顺序写、批量、快照**。

**Controller Quorum**：不再从外部 ZK 选主，而是由若干 Broker 组成投票集（voter），通过 Raft 协议选举出 Active Controller，其余为 Standby。选举逻辑在 `raft/src/main/java/org/apache/kafka/raft/`：`CandidateState.java:57` 校验本地副本必须在 voter 集合内，`CandidateState.java:76` 用 `epochElection` 做任期选举。

**Standby Controller 的意义**：Active 挂了，Standby 已经在本地持有全部元数据快照，接管**不需要重新加载**，秒级切换。

Kafka 4.x 已彻底移除 ZooKeeper 支持，纯 KRaft。

---

## 十三、核心参数速查表

### 13.1 Producer（`ProducerConfig.java`）

| 参数 | 默认值 | 锚点 | 调优方向 |
|---|---|---|---|
| `acks` | `all` | `:391` | 不为性能降到 `1`；`0` 会丢消息 |
| `batch.size` | 16384 | `:401` | 大批量 + 高压缩率场景调大 |
| `linger.ms` | 5 | `:405` | 4.x 默认已是 5ms；调大（如 20）换吞吐，牺牲延迟 |
| `compression.type` | `none` | `:397` | 改 `lz4`/`zstd`，网络与磁盘双省 |
| `max.in.flight.requests.per.connection` | 5 | `:475` | 幂等开启时 >1 仍保证顺序 |
| `enable.idempotence` | `true` | `:529` | **保持 true** |
| `retries` | 2147483647 | `:390` | 幂等下可无限重试 |
| `delivery.timeout.ms` | 120000 | `:406` | 投递总超时 |
| `max.block.ms` | 60000 | `:437` | 元数据拉取阻塞上限 |
| `max.request.size` | 1048576 | `:412` | 默认 1MB，大消息需调 |
| `transactional.id` | — | `:356` | 事务生产者必配 |

### 13.2 Consumer（`ConsumerConfig.java`）

| 参数 | 默认值 | 锚点 | 调优方向 |
|---|---|---|---|
| `enable.auto.commit` | `true` | `:458` | **生产必须显式设 false** |
| `max.poll.records` | 500 | `:94` | 大批量减少 poll 次数 |
| `max.poll.interval.ms` | 300000 | `:629` | 必须 > 单批处理耗时 |
| `session.timeout.ms` | 45000 | `:438` | 过高则故障发现慢 |
| `fetch.min.bytes` | 1 | `:187` | 调大可攒更多数据再返回 |
| `fetch.max.wait.ms` | 500 | `:210` | 长轮询挂起上限 |
| `auto.offset.reset` | `latest` | `:546` | 新消费者默认只读新消息 |
| `partition.assignment.strategy` | — | `:151` | 优先 `CooperativeStickyAssignor` |
| `group.instance.id` | — | `:86` | 静态成员，避免 Rebalance |
| `group.id` | — | `:80` | **不设则不能自动提交**（`:756`） |

### 13.3 Broker / Topic 级

| 参数 | 推荐值 | 锚点 | 说明 |
|---|---|---|---|
| `min.insync.replicas` | 2 | `TopicConfig.java:174` | ISR 门槛，配合 acks=all |
| `unclean.leader.election.enable` | false | `TopicConfig.java:167` | 禁脏选举 |
| `replication.factor` | 3 | — | 副本数 |
| `segment.ms` | 604800000 (7d) | `TopicConfig.java:36` | Segment 滚动周期 |
| `retention.ms` | 604800000 (7d) | `TopicConfig.java:75` | 保留时长 |
| `max.message.bytes` | 1048588 | `TopicConfig.java:109` | 单消息上限 |
| `cleanup.policy` | `delete` | `TopicConfig.java:153` | `compact` 用于状态类 Topic |
| `num.io.threads` | 8 | `ServerConfigs.java:51` | 网络线程数 |
| `background.threads` | 10 | `ServerConfigs.java:55` | 后台线程数 |
| `request.timeout.ms` | 30000 | `ServerConfigs.java:63` | broker 侧请求超时 |

### 13.4 一份能用的生产配置

```properties
# Producer
acks=all
enable.idempotence=true
max.in.flight.requests.per.connection=5
linger.ms=10
batch.size=65536
compression.type=lz4
delivery.timeout.ms=120000

# Consumer
group.id=orders-consumer
enable.auto.commit=false
max.poll.records=500
max.poll.interval.ms=300000
partition.assignment.strategy=org.apache.kafka.clients.consumer.CooperativeStickyAssignor
auto.offset.reset=earliest

# Topic
replication.factor=3
min.insync.replicas=2
unclean.leader.election.enable=false
retention.ms=604800000
```

> 调参优先级：**先保证不丢（acks + min.insync + 手动提交）→ 再保证不重（幂等 + 业务幂等）→ 最后才是吞吐（linger.ms / batch.size / max.poll.records）**。

---

## 面试高频 Q&A

| # | 问题 | 答案 | 深入 |
|---|---|---|---|
| 1 | 为什么需要消息队列？ | 解耦 + 削峰填谷 + 异步，代价是最终一致与链路变长；强一致短链路场景不该上 | 第一讲 |
| 2 | Kafka 整体架构？ | Producer/Broker/Consumer/Topic 四角色，Topic 是逻辑概念、Partition 是物理单元 | 第二讲 |
| 3 | Partition 是什么？ | 逻辑 Topic 的物理分片，对应 Broker 上一个目录，顺序追加写 Segment，分区内 offset 递增 | 第三讲 |
| 4 | 副本有哪两种角色？ | Leader 处理全部读写，Follower 异步拉取日志仅作冗余 | 第四讲 |
| 5 | ISR 是什么？有何作用？ | AR 中与 Leader 同步的副本子集；两个作用：Leader 选举的唯一候选池 + 决定 HW 取最慢副本的 LEO | 第五讲 |
| 6 | 怎么保证消息不丢失？ | 三端协同：生产者 `acks=all`+幂等、服务端 RF=3+`min.insync.replicas=2`+禁脏选举、消费者关自动提交改手动提交 | 第六讲 |
| 7 | 为什么 acks=all 还会丢消息？ | 前提是 ISR 里有足够副本。若 ISR 缩到 1 而 `min.insync.replicas=2`，写入本就被拒绝；若副本都写成功但都未刷盘，机器掉电仍可能丢 | 第六讲 |
| 8 | Kafka 为什么快？ | 磁盘顺序写 + 页缓存 + `sendfile` 零拷贝 + 攒批压缩，四者叠加 | 第三、四讲 |
| 9 | 零拷贝怎么实现的？ | `PlaintextTransportLayer.transferFrom` 调 `FileChannel.transferTo`；SSL 通道因需加密走不了快路径 | 第三讲 |
| 10 | 什么是消费者组？ | 消费端逻辑容器；同组内分区独占（队列模式），不同组各存位点各收一份（广播模式） | 第五讲 |
| 11 | 什么是 Rebalance？ | 成员或分区变化后重新协商分配，期间全组暂停消费（Stop The World），频繁触发是延迟元凶 | 第五讲 |
| 12 | 怎么减少 Rebalance？ | `CooperativeStickyAssignor` 增量分配 + 调大 `max.poll.interval.ms` + `group.instance.id` 静态成员 | 第五讲 |
| 13 | Kafka 是推还是拉？ | Pull；长轮询（`fetch.max.wait.ms` 默认 500ms）避免空轮询，激活权在消费者 | 第五讲 |
| 14 | 怎么保证消息顺序？ | 发送端用相同业务 key 定向到同一分区；Kafka 只保证单分区内有序 | 第四讲 |
| 15 | 怎么避免重复消费？ | at-least-once 语义下重复是常态，靠业务幂等：唯一索引捕获、状态机条件更新、去重表 | 第六讲 |
| 16 | 消息堆积怎么治理？ | 先扩容消费者（不超分区数）→ 再扩分区（注意顺序断裂）→ 临时 Topic 分流 → 从源头限流；盯 consumer lag 指标 | 第六讲 |
| 17 | 为什么换成 KRaft？ | 元数据变成内部 Topic 的 Raft 日志，去掉 ZK 的运维负担与扩展瓶颈；Controller Quorum 选举，Standby 持有快照秒级接管 | 第二讲 |
| 18 | `acks=all` 就一定不丢吗？ | 不一定。`acks=all` 只保证 ISR 内副本都确认；若这些副本都未 fsync 就掉电，仍可能丢。需结合副本数跨机架部署 | 第六讲 |
| 19 | `min.insync.replicas=2` 的真实语义？ | 是"ISR 少于 2 就拒绝写"，不是"两个副本写成功即可"；ISR 缩小时写入直接失败 | 第六讲 |
| 20 | 4.x 的默认值和 2.x 有何不同？ | `acks` 默认已是 `all`、`enable.idempotence` 默认已是 `true`；`enable.auto.commit` 仍默认 `true` 但用默认 group.id 时强制 false | 本篇 |
| 21 | 消费者 `poll()` 返回 0 条说明什么？ | 没有新数据（长轮询超时返回），或数据被 `auto.offset.reset` 跳过了；先看 lag 再判断 | 第五讲 |
| 22 | 分区数能减少吗？ | 不能。分区只能增不能减，历史数据归属不可变 | 第三讲 |
| 23 | 消费线程数多于分区数会怎样？ | 多出的线程分不到分区，空转拉心跳，浪费资源；并行度上限是分区数 | 第五讲 |
| 24 | 怎么监控 Kafka 健康度？ | consumer lag（堆积）、under-replicated partitions（副本缩水）、ISR shrink/expand 频率（集群稳定性）、请求队列与 P99 延迟 | 第六讲 |
| 25 | 临时 Topic 分流法的风险？ | 破坏原 key→分区映射导致顺序断裂；两套位点管理复杂；需额外保证转发不丢 | 第六讲 |

### 易错点与面试坑

1. **"min.insync.replicas=2 就是两个副本写成功"** —— 错。它是"ISR 少于 2 就拒绝写"的门槛。
2. **"acks=all 绝对不丢"** —— 错。副本全部未刷盘时掉电仍丢；`acks=all` 解决的是"Leader 挂了换人数据还在"。
3. **"enable.auto.commit 默认是 false"** —— 4.x 里默认仍是 `true`（`ConsumerConfig.java:458`），只有用默认 group.id 时才强制 false（`:756`）。生产必须显式配置。
4. **"acks 默认是 1"** —— 4.x 已改为 `all`（`ProducerConfig.java:391`）。这是从 2.x 时代抄来的配置表。
5. **"HW 是所有副本 LEO 的最大值"** —— 错。HW 取 ISR 中**最慢**副本的 LEO，消费者只能读到 HW。
6. **"ISR 是静态配置"** —— 错。副本超时会被踢出 ISR（`Partition.scala:1117`），恢复后再扩回。
7. **"消费者线程越多吞吐越高"** —— 错。同一组内分区独占，线程数超过分区数就空转。
8. **"Pull 模式所以会有延迟"** —— 错。长轮询把空转成本压到近乎为零，延迟上界是 `fetch.max.wait.ms`。
9. **"幂等开了就不用管重复消费"** —— 错。幂等只覆盖**单分区、单会话**内的 Producer 重试；跨分区或消费者侧仍需业务幂等。
10. **"零拷贝对 SSL 也生效"** —— 错。加密必须把数据读上来拷贝，`SslTransportLayer.transferFrom` 走的是普通路径。
11. **"Kafka 是数据库"** —— 错。它是**提交日志**，不支持按 key 点查、不支持多行事务、更新要靠"日志 + 物化视图"（Kafka Streams / Flink）。
12. **"扩分区是无损的"** —— 错。key→分区映射被打破，同一 key 的新旧消息可能落在不同分区。

### 关键数值与默认值速记

| 数值 | 含义 | 锚点 |
|---|---|---|
| `all` | `acks` 在 4.x 的默认值 | `ProducerConfig.java:393` |
| `true` | `enable.idempotence` 在 4.x 的默认值 | `ProducerConfig.java:529` |
| `true` | `enable.auto.commit` 在 4.x 的默认值 | `ConsumerConfig.java:459` |
| `500` | `fetch.max.wait.ms` 默认（长轮询挂起上限） | `ConsumerConfig.java:210` |
| `1` | `fetch.min.bytes` 默认 | `ConsumerConfig.java:187` |
| `500` | `max.poll.records` 默认 | `ConsumerConfig.java:94` |
| `300000` | `max.poll.interval.ms` 默认（5 分钟） | `ConsumerConfig.java:629` |
| `45000` | `session.timeout.ms` 默认（45 秒） | `ConsumerConfig.java:438` |
| `16384` | `batch.size` 默认（16KB） | `ProducerConfig.java:401` |
| `5` | `linger.ms` 默认（4.x 改为 5ms） | `ProducerConfig.java:405` |
| `8` | `num.io.threads` 默认 | `ServerConfigs.java:51` |
| `10` | `background.threads` 默认 | `ServerConfigs.java:55` |
| `30000` | broker `request.timeout.ms` 默认 | `ServerConfigs.java:63` |

---

## 附：涉及文件索引

| 文件 | 行数 | 本文用到的内容 |
|---|---|---|
| `clients/src/main/java/org/apache/kafka/clients/producer/ProducerConfig.java` | 500+ | `acks`/`batch.size`/`linger.ms`/`compression.type`/`max.in.flight`/幂等/事务/超时默认值 |
| `clients/src/main/java/org/apache/kafka/clients/consumer/ConsumerConfig.java` | 800+ | `max.poll.*`/`fetch.*`/`session.timeout`/`auto.offset.reset`/分配策略/静态成员/自动提交覆盖 |
| `clients/src/main/java/org/apache/kafka/common/config/TopicConfig.java` | 400+ | `min.insync.replicas`/`unclean.leader.election.enable`/`retention.ms`/`segment.ms`/`cleanup.policy` |
| `server-common/src/main/java/org/apache/kafka/server/config/ServerConfigs.java` | 300+ | `num.io.threads`/`background.threads`/`request.timeout.ms` |
| `core/src/main/scala/kafka/server/KafkaApis.scala` | 3500+ | `handleFetchRequest`（`:563`）、`fetchMinBytes` 计算（`:744`） |
| `core/src/main/scala/kafka/cluster/Partition.scala` | 1700+ | ISR 收缩（`:1099`/`:1117`）、`min.insync.replicas` 判定（`:1234`） |
| `clients/src/main/java/org/apache/kafka/clients/producer/internals/RecordAccumulator.java` | 1000+ | 按 topic 建 `TopicInfo`（`:285`）、批次大小估算（`:329`）、`ready`/`drain`（`:768`/`:852`） |
| `clients/src/main/java/org/apache/kafka/common/network/PlaintextTransportLayer.java` | 300+ | 零拷贝 `transferFrom`（`:213`） |
| `clients/src/main/java/org/apache/kafka/common/network/SslTransportLayer.java` | 1100+ | SSL 下无法零拷贝（`:1003`） |
| `clients/src/main/java/org/apache/kafka/common/network/TransferableChannel.java` | 60+ | `transferTo` 快路径的前提条件（`:41`） |
| `raft/src/main/java/org/apache/kafka/raft/CandidateState.java` | 200+ | KRaft 选举：voter 校验（`:57`）、`epochElection`（`:76`） |
| `clients/src/main/java/org/apache/kafka/clients/consumer/CooperativeStickyAssignor.java` | 600+ | 增量式分配（避免 Stop The World） |
| `clients/src/main/java/org/apache/kafka/clients/consumer/RangeAssignor.java` | 100+ | 区间分配，不跨 topic 均衡 |
| `clients/src/main/java/org/apache/kafka/clients/consumer/RoundRobinAssignor.java` | 100+ | 轮询分配 |

> 完整的源码逐文件精读（网络层、KRaft、存储引擎、Producer、Consumer、Group Coordinator）见 `kafka.md`。
