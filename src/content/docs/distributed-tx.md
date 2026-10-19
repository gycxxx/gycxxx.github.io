---
title: "分布式事务（Seata）源码深度学习笔记"
date: 2026-10-19
categories:
  - "中间件"
tags:
  - "分布式事务"
  - "Seata"
  - "AT"
  - "TCC"
  - "源码"
---

> 基于对 Apache Seata 源码（`org.apache.seata` 包名，已完成 Apache 捐赠迁移）的逐文件精读整理而成。
>
> 源码位于 `tm/`（TransactionManager）、`rm/`（ResourceManager）、`rm-datasource/`（AT 模式数据源代理）、`tcc/`（TCC 模式）、`saga/`（Saga 状态机）、`integration-tx-api/`（注解与拦截器）、`server/`（TC 事务协调器）。
>
> 阅读顺序即学习顺序：全局图景与 TM → TC 协调核心 → AT 模式 → TCC 与 fence → Saga 与 XA → 存储与集群。
>
> 所有结论均标注了源文件位置（格式 `模块/相对路径:行号`），行号均经 Read/Grep 实际核对。
>
> **版本基线**：commit `50ca2d9`（`test: isolate Redis registry background tasks between fixtures (#8307)`）。
> **注意**：server 模块的 raft 集群部分仍在 `io.seata.server.cluster.raft` 旧包名下（迁移未完成），引用时以实际文件为准。

---

## 目录

- [〇、全景地图](#〇全景地图)
- [速览：六讲各自做了什么（先看这个再进细节）](#速览六讲各自做了什么先看这个再进细节)
- [面试速通：分布式事务高频问题一页纸](#面试速通分布式事务高频问题一页纸)
- [第一讲：全局图景与 TM —— 一个 @GlobalTransactional 的一生](#第一讲全局图景与-tm--一个-globaltransactional-的一生)
- [第二讲：TC 协调核心 —— DefaultCoordinator / DefaultCore / GlobalSession](#第二讲tc-协调核心--defaultcoordinator--defaultcore--globalsession)
- [第三讲：AT 模式 —— DataSourceProxy 与 undo_log 两阶段](#第三讲at-模式--datasourceproxy-与-undo_log-两阶段)
- [第四讲：TCC 模式与 fence 防悬挂](#第四讲tcc-模式与-fence-防悬挂)
- [第五讲：Saga 状态机与 XA 模式](#第五讲saga-状态机与-xa-模式)
- [第六讲：存储与集群 —— SessionManager / LockManager / Raft](#第六讲存储与集群--sessionmanager--lockmanager--raft)
- [附：学习路线与复习卡汇总](#附学习路线与复习卡汇总)
- [涉及文件索引](#涉及文件索引)

---

## 〇、全景地图

Seata 的分布式事务可以压缩成**三个角色 + 一张状态机 + 四种分支模式**：

- **TM（Transaction Manager，事务发起方）**：`tm/` 模块。解析 `@GlobalTransactional` 注解（`GlobalTransactionalInterceptorHandler`），按传播性决定"加入还是新建"（`TransactionalTemplate`），向 TC 发 `GlobalBegin`，业务结束后发 `GlobalCommit`/`GlobalRollback`。
- **RM（Resource Manager，资源管理方）**：`rm/` + `rm-datasource/`。拦截 JDBC 连接（AT）、拦截 TCC 方法、持有 XA 资源；一阶段干活时向 TC 注册分支（`BranchRegister`）拿 `branchId`，二阶段等 TC 指令做 `BranchCommit`/`BranchRollback`。
- **TC（Transaction Coordinator，事务协调者）**：`server/` 模块。持有全局事务会话（`GlobalSession`），驱动 20+ 状态的流转，管理分支注册、全局锁、超时检查、失败重试；持久化到 file/db/redis/raft。

```
┌─────────────────────────── TM（业务进程内） ───────────────────────────┐
│  @GlobalTransactional                                                 │
│    └─ GlobalTransactionalInterceptorHandler:164 (invoke)              │
│         └─ handleGlobalTransaction:188                                │
│              └─ TransactionalTemplate.execute:190                     │
│                   ├─ 传播性分派 REQUIRED/NESTED/REQUIRES_NEW...  :192  │
│                   ├─ beginTransaction → DefaultGlobalTransaction:190   │
│                   │     └─ transactionManager.begin → GlobalBeginRequest│
│                   ├─ business.execute()                               │
│                   ├─ commitTransaction:330 → tx.commit():300           │
│                   └─ completeTransactionAfterThrowing:318 → rollback   │
└────────────────────────────────────────────────────────────────────────┘
              │ RPC（Netty，BatchMessage / 单条 Request）        ▲
              ▼                                                  │ 回报
┌─────────────────────────── TC（server/ 独立进程） ─────────────────────┐
│  DefaultCoordinator（协调者门面，:759 init）                            │
│    ├─ doGlobalBegin:321  ── 建 GlobalSession（Begin）                  │
│    ├─ doBranchRegister:369 ── 挂 BranchSession + 抢全局锁              │
│    ├─ doGlobalCommit:340 / doGlobalRollback:347                        │
│    ├─ timeoutCheck:406 ───── 定时扫超时会话 → Rollbacking              │
│    ├─ handleRetryRollbacking:451 / handleRetryCommitting:487           │
│    ├─ handleAsyncCommitting:524 ── 一阶段成功即可异步提交               │
│    └─ undoLogDelete:549 ───── 通知 RM 删 undo_log（默认保留 7 天）      │
│  DefaultCore（分支模式路由，CORE_MAP:59）                               │
│    └─ begin:222 / commit:237 / rollback:401 / branchRegister:105       │
│  GlobalSession（:62）── xid + status + branchSessions + locks          │
└────────────────────────────────────────────────────────────────────────┘
              │ RPC                                              ▲
              ▼                                                  │ 上报
┌─────────────────────────── RM（业务进程内） ───────────────────────────┐
│  AT  : DataSourceProxy:50 → ExecuteTemplate:60                          │
│        → AbstractDMLBaseExecutor:97（beforeImage/afterImage/undoLog）   │
│        → ConnectionProxy.commit:185 → doCommit:227 → register:267       │
│  TCC : TccActionInterceptorHandler（try 方法拦截 + ActionContext 传递）  │
│  XA  : ConnectionProxyXA → XAResource.prepare/commit/rollback           │
│  Saga: StateMachineEngine.start:40                                      │
└────────────────────────────────────────────────────────────────────────┘
                     │                        │
                     ▼                        ▼
         storage/file（本地文件）    storage/db / storage/redis / storage/raft
         （FileSessionManager       （分布式会话；raft = RaftCoordinator 多 TC
          + FileLocker）             强一致复制）
```

一句话记住全景：**TM 说"开始/结束"，RM 说"我干了什么/听指令回滚"，TC 说"都记下了，我说了算"**。

---

## 速览：六讲各自做了什么（先看这个再进细节）

| 讲次 | 主题 | 核心类 | 要回答的问题 | 不学会怎样 |
|---|---|---|---|---|
| 第一讲 | 全局图景与 TM | `TransactionalTemplate` | `@GlobalTransactional` 怎么变成两次 RPC | 只会用注解，不懂传播性与参与者 |
| 第二讲 | TC 协调核心 | `DefaultCoordinator` | 20+ 状态怎么流转、五个定时任务各干什么 | 悬挂/空回滚/幂等说不清 |
| 第三讲 | AT 模式 | `DataSourceProxy` | 前后镜像怎么生成、undo_log 怎么回滚 | 全局锁死锁、脏写无法解释 |
| 第四讲 | TCC 与 fence | `DefaultCommonFenceHandler` | 防悬挂、防空回滚、幂等的表结构 | TCC 接入只会抄 demo |
| 第五讲 | Saga 与 XA | `StateMachineEngine` | 长事务怎么编排、XA 和 AT 差异 | 选错模式背锅 |
| 第六讲 | 存储与集群 | `SessionManager` 家族 | 会话存哪、锁怎么实现、多 TC 怎么一致 | 生产只会单机 file 模式 |

---

## 面试速通：分布式事务高频问题一页纸

### 高频 Q&A（先结论）

| # | 问题 | 一句话结论 | 出处 |
|---|---|---|---|
| 1 | Seata 三个角色？ | TM 发起/结束全局事务；RM 管理分支资源、执行二阶段；TC 持会话、驱动状态、管全局锁 | `server/src/main/java/org/apache/seata/server/coordinator/DefaultCoordinator.java` |
| 2 | XID 怎么跨服务传递？ | `RootContext` 的 ThreadLocal + RPC  Filter 把 xid 塞进请求头；下游 `RootContext.getXID()` 非空就是 Participant | `tm/src/main/java/org/apache/seata/tm/api/DefaultGlobalTransaction.java:235` |
| 3 | AT 一阶段做了什么？ | 解析 SQL → 查**前镜像** → 执行 → 查**后镜像** → 生成 undo_log → 写业务表和 undo_log **同一本地事务**提交 → 注册分支 | `rm-datasource/src/main/java/org/apache/seata/rm/datasource/exec/AbstractDMLBaseExecutor.java:97` |
| 4 | AT 二阶段回滚原理？ | TC 发 `BranchRollback` → RM 按 undo_log 的 `rollback_info` 反向生成 SQL 执行；**回滚完才删 undo_log**，保证可重入 | `rm-datasource/src/main/java/org/apache/seata/rm/datasource/undo/AbstractUndoLogManager.java:89` |
| 5 | AT 二阶段提交做什么？ | 删 undo_log（异步，`Phase two committing` 阶段不做业务操作）；一阶段成功即可认为事务完成 | `server/src/main/java/org/apache/seata/server/coordinator/DefaultCoordinator.java:549` |
| 6 | 全局锁什么时候拿？ | RM 注册分支时把 `lockKeys`（表名+主键值）报给 TC，TC 的 `LockManager` 抢先到先得；写冲突抛 `LockConflictException` 重试 | `server/src/main/java/org/apache/seata/server/storage/db/lock/DataBaseLockManager.java` |
| 7 | 脏写怎么防？ | 本地事务提交前 `checkLock` 校验全局锁——如果这行记录被别的事务的全局锁占着，本地提交直接失败 | `rm-datasource/src/main/java/org/apache/seata/rm/datasource/ConnectionProxy.java:237` |
| 8 | AT 的隔离级别？ | **读未提交（RU）**——一阶段提交后别的事务就能读到未全局提交的数据；Seata 默认只加写锁，读靠 undo_log 的 `afterImage` 做补偿查询（`@GlobalLock`） | 设计取舍 |
| 9 | 空回滚是什么？ | TC 决定回滚但 RM 根本没收到过一阶段（没注册分支）→ RM 要能识别"这个 xid+branchId 我没见过"并不报错 | `tcc/fence` |
| 10 | 悬挂是什么？ | RM 一阶段网络拥堵，TC 先回滚完（空回滚）， RM 的一阶段后到 → 不回滚就悬挂。fence 表靠 `status` 状态机挡住 | `integration-tx-api/src/main/java/org/apache/seata/integration/tx/api/fence/DefaultCommonFenceHandler.java:24` |
| 11 | TCC 的幂等靠什么？ | `confirm/cancel` 必须幂等实现；fence 表记录 `xid+branchId+action` 的状态（`PREPARED/COMMITTED/ROLLBACKED`），重复调用直接返回上次结果 | 同上 `:53` |
| 12 | Saga 和 TCC 差异？ | TCC 三个方法都要业务写；Saga 是**状态机 JSON 编排**，正向服务 + 补偿服务，长事务/遗留系统友好 | `saga/seata-saga-engine/src/main/java/org/apache/seata/saga/engine/StateMachineEngine.java:29` |
| 13 | XA 模式和 AT 差异？ | XA 是数据库标准协议（`XAResource.prepare/commit`），**业务无侵入但依赖库支持、性能差**；AT 靠 undo_log 补偿，无侵入且快 | `rm-datasource/src/main/java/org/apache/seata/rm/datasource/xa/ConnectionProxyXA.java` |
| 14 | TC 挂了正在跑的事务怎么办？ | TC 重启后从存储 reload 会话（`SessionHolder.reload`），继续驱动到终态；RM 有 `report` 重试 | `server/src/main/java/org/apache/seata/server/session/SessionHolder.java` |
| 15 | `@GlobalTransactional` 传播性有哪些？ | 七种：`REQUIRED/SUPPORTS/MANDATORY/REQUIRES_NEW/NOT_SUPPORTED/NEVER/NESTED`，和 Spring 同名同义 | `tm/src/main/java/org/apache/seata/tm/api/TransactionalTemplate.java:192` |
| 16 | 参与者（Participant）能回滚吗？ | **不能**。`rollbackTransaction:395` 开头就判断 `role != Launcher` 直接忽略——只有发起方有权结束全局事务 | `tm/src/main/java/org/apache/seata/tm/api/TransactionalTemplate.java:395` |
| 17 | 全局事务超时了会怎样？ | TC `timeoutCheck:406` 扫到超时会话 → `TimeoutRollbacking` → 逐分支回滚；RM 提交时若 TC 已判超时，`commit(xid)` 返回 `TimeoutRollbacking` 转为回滚 | `server/src/main/java/org/apache/seata/server/coordinator/DefaultCore.java:242` |
| 18 | 二阶段提交失败会无限重试吗？ | 不会。有 `MAX_COMMIT_RETRY_TIMEOUT`（`:165`）/`MAX_ROLLBACK_RETRY_TIMEOUT`（`:168`）和 `RETRY_DEAD_THRESHOLD`（`:177`），超过进人工处理（告警） | `server/src/main/java/org/apache/seata/server/coordinator/DefaultCoordinator.java:165` |
| 19 | 为什么 AT 要求业务表必须有主键？ | 前后镜像按主键定位行；`isMultiPk():114` 还会对复合主键做特殊处理 | `rm-datasource/src/main/java/org/apache/seata/rm/datasource/exec/AbstractDMLBaseExecutor.java:114` |
| 20 | 批量插入为什么特殊？ | 批量没有 beforeImage，回滚只能按主键删；`InsertExecutor`/`MySQLInsertExecutor` 专门处理自增列和 `on duplicate update` | `rm-datasource/src/main/java/org/apache/seata/rm/datasource/exec/InsertExecutor.java` |

### 易错点（最容易讲错的 8 条）

1. **AT 的 undo_log 和业务 SQL 是同一个本地事务**——所以业务成功 undo 必然存在，业务失败 undo 必然不存在，天然原子。
2. **全局锁是 TC 端的，不是数据库行锁**：TC 锁冲突只阻塞/重试写方，不阻塞读方（RU 隔离）。
3. **`GlobalCommit` 到 TC 时如果分支还没注册完**：`commit()` 会先 `close()` 会话（`DefaultCore.java:246` 注释 "Firstly, close the session, then no more branch can be registered"），后续迟到的 `BranchRegister` 被拒——**防的就是提交后还有分支进来**。
4. **回滚失败不等于事务结束**：RM 回滚失败会 `report(false)`，TC 把分支置 `RollbackRetrying`，由 `handleRetryRollbacking:451` 定时重扫继续回滚。
5. **fence 表是 TCC 的，不是 AT 的**：AT 靠 undo_log 天然幂等，TCC 靠 fence 表。
6. **Saga 没有"准备"阶段**：服务调用直接生效，失败了才跑补偿；所以补偿必须幂等、可重入。
7. **XA 模式下 prepare 后连接不能释放**：`prepare` 意味着"挂起"，连接必须保持到 TC 的二阶段指令，连接池容易被占满。
8. **`undo_log_delete` 由 TC 通知、RM 执行**（`DefaultCoordinator.java:549`）：TC 只是广播"该删了"，真正删除在 RM 本地，因为 undo_log 存在业务库。

### 数值速记

| 数值 | 含义 | 出处 |
|---|---|---|
| `7` 天 | undo_log 默认保留天数 | `server/src/main/java/org/apache/seata/server/coordinator/DefaultCoordinator.java:557` |
| `5000` | 分支异步队列大小 `DEFAULT_BRANCH_ASYNC_QUEUE_SIZE` | `DefaultCoordinator.java:158` |
| `2 × CPU` | 分支异步线程池大小 | `DefaultCoordinator.java:163` |
| `0` | `ALWAYS_RETRY_BOUNDARY`（无限重试标记） | `DefaultCoordinator.java:153` |
| `5000ms` | 定时任务关闭最大等待 | `DefaultCoordinator.java:108` |

---

## 第一讲：全局图景与 TM —— 一个 @GlobalTransactional 的一生

> **本讲问题**：业务方法上加一个 `@GlobalTransactional`，到 TC 再回来，中间到底发生了几次 RPC、状态怎么变？

### 1.1 拦截器：注解只是入口

`@GlobalTransactional` 的织入由 `GlobalTransactionalInterceptorHandler`（`integration-tx-api/`）完成，核心分派：

```java
// integration-tx-api/src/main/java/org/apache/seata/integration/tx/api/interceptor/handler/GlobalTransactionalInterceptorHandler.java:164
    return handleGlobalTransaction(invocation, transactional);
// :188
Object handleGlobalTransaction(...) {
    // 1. 读事务属性（超时、传播性、回滚规则）
    // 2. 构造 TransactionalTemplate
    // 3. template.execute(business)
}
```

注意区分两个 handler：`GlobalTransactionalInterceptorHandler`（全局事务）和 `TccActionInterceptorHandler`（TCC 的 try 方法），`CombineTransactionalInterceptorHandler` 管两者叠加的场景。

### 1.2 TransactionalTemplate：七种传播性 + 五步骨架

```java
// tm/src/main/java/org/apache/seata/tm/api/TransactionalTemplate.java:190（节选）
    // 1.2 Handle the transaction propagation.
    Propagation propagation = txInfo.getPropagation();
    switch (propagation) {
        case NOT_SUPPORTED:  tx.suspend(false); return business.execute();   // 挂起后裸跑
        case REQUIRES_NEW:   suspend + tx = GlobalTransactionContext.createNew();  // 挂旧起新
        case SUPPORTS:       notExistingTransaction(tx) ? business.execute() : 继续;
        case REQUIRED:       tx = GlobalTransactionContext.getCurrentOrCreate();   // 最常用
        case NEVER:          existingTransaction(tx) ? throw : business.execute();
        case MANDATORY:      notExistingTransaction(tx) ? throw : 继续;
        case NESTED:         ...
    }
    ...
    try {
        beginTransaction(txInfo, tx);        // 2. Launcher 才发 GlobalBegin
        Object rs = business.execute();      // 3. 业务
        commitTransaction(tx, txInfo);       // 4. 发 GlobalCommit/GlobalRollback
        return rs;
    } finally { resumeGlobalLockConfig(...); }
```

五步骨架：**传播性分派 → begin → business → commit/rollback → 清理**。Participant（XID 非空）在 `beginTransaction` 里啥也不发（`tm/src/main/java/org/apache/seata/tm/api/DefaultGlobalTransaction.java:232` 注释 "Participants don't initiate transactions"），只挂 hook。

### 1.3 DefaultGlobalTransaction：角色决定行为

```java
// tm/src/main/java/org/apache/seata/tm/api/DefaultGlobalTransaction.java:235
public void begin(int timeout, String name) throws TransactionException {
    this.createTime = System.currentTimeMillis();
    // Participants don't initiate transactions, they just join existing ones
    if (role != GlobalTransactionRole.Launcher) {
        assertXIDNotNull();
        ...
        return;                       // ★ 参与者直接返回，不发 RPC
    }
    ...
    xid = transactionManager.begin(applicationId, transactionServiceGroup, name, timeout);
    RootContext.bind(xid);             // 绑进 ThreadLocal，后续 RM 靠它取
}
```

`commit():300` 和 `rollback():387` 同样先判角色——**Participant 调 commit/rollback 是静默忽略的**（`TransactionalTemplate.rollbackTransaction:395` 开头那段），这是"只有 launcher 能结束全局事务"的源码级保证。

### 1.4 一条完整链路（带状态变迁）

以 `订单服务 → 库存服务` 为例：

```
① 订单服务 @GlobalTransactional 方法进入
   TM: TransactionalTemplate → GlobalBegin
   TC: DefaultCore.begin:222 → GlobalSession(Begin) + 分配 xid
       SessionHolder 持久化（file/db/redis/raft）
② 订单服务本地写订单（DataSourceProxy 拦截）
   RM: beforeImage → INSERT → afterImage → undo_log → 本地事务提交
   RM: ConnectionProxy.doCommit:227 → register():267 → BranchRegister(xid, lockKeys)
   TC: doBranchRegister:369 → 挂 BranchSession + LockManager 抢锁
③ 订单服务 RPC 调库存服务（Filter 透传 xid）
   库存服务 RootContext 有 xid → Participant，不 begin
   库存服务本地扣减 → register 分支
④ 订单服务业务正常返回
   TM: commitTransaction → DefaultGlobalTransaction.commit():300
       → GlobalCommit RPC
   TC: DefaultCore.commit:237 → close() → Committing
       → 逐分支 BranchCommit（异步场景 asyncCommit）
       → GlobalSession 转 Committed → 删会话 → undoLogDelete 广播
⑤ 若 ② 或 ③ 抛异常
   TM: completeTransactionAfterThrowing:318 → 匹配 RollbackRule
       → tx.rollback() → GlobalRollback
   TC: 转 Rollbacking → 逐分支 BranchRollback
       → RM 按 undo_log 反向 SQL 恢复 → report
       → Rollbacked → 删会话
```

### 1.5 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `@GlobalTransactional` → `GlobalTransactionalInterceptorHandler:164` → `TransactionalTemplate:190` → `DefaultGlobalTransaction:235` |
| 关键实现 | 五步骨架：传播性 → begin → business → commit/rollback → finally 清理；Participant 全程不发 begin |
| 并发 | XID 存 `RootContext` ThreadLocal；Participant 与 Launcher 的行为差异是源码级 if 判断 |
| 协议 | 正常 2 次 RPC（Begin + Commit），异常多 1 次（Rollback）；RM 每分支 2 次（Register + Report/Commit） |
| 陷阱 | `NESTED` 在 Seata 里是"嵌套全局事务"（部分支持），别和 Spring 的本地嵌套混淆 |

**一句记忆钩子**：*"Launcher 发号施令，Participant 只管干活；一次全局事务 = Begin + N×Register + Commit/Rollback。"*

---

---

## 第二讲：TC 协调核心 —— DefaultCoordinator / DefaultCore / GlobalSession

> **本讲问题**：TC 凭什么当"协调者"？20+ 个状态是谁在拨弄？五个定时任务在 failure 恢复里各扮演什么角色？

### 2.1 两个类的分工

`DefaultCoordinator`（`server/src/main/java/org/apache/seata/server/coordinator/DefaultCoordinator.java`，930 行）是**门面 + 定时任务**：它处理 RPC 入口（`doGlobalBegin:321`/`doBranchRegister:369`/`doGlobalCommit:340`/`doGlobalRollback:347`），并持有五个定时线程池：

```java
// DefaultCoordinator.java:182-200
private final ScheduledThreadPoolExecutor retryRollbacking = ...   // 回滚重试
private final ScheduledThreadPoolExecutor retryCommitting  = ...   // 提交重试
private final ScheduledThreadPoolExecutor asyncCommitting   = ...   // 异步提交
private final ScheduledThreadPoolExecutor timeoutCheck      = ...   // 超时检查
private final ScheduledThreadPoolExecutor undoLogDelete     = ...   // undo_log 删除
private final ScheduledThreadPoolExecutor syncProcessing    = ...   // raft 同步
```

`DefaultCore`（`DefaultCore.java`，552 行）是**分支模式路由**：`CORE_MAP`（`:59`）按 `BranchType`（AT/TCC/SAGA/XA）找到对应的 `AbstractCore` 实现，把 `branchCommit/branchRollback/branchRegister` 分发下去。这种设计让新增一种事务模式不用改 TC 主流程。

### 2.2 状态机：GlobalStatus 的一生

`GlobalSession`（`server/src/main/java/org/apache/seata/server/session/GlobalSession.java`）持有一个 `volatile GlobalStatus status`（`:97`），核心状态流转：

```
Begin ──commit──► Committing ──全部成功──► Committed（终态，删会话）
  │                  │
  │                  └─有分支失败─► CommitRetrying ──重试──► Committed
  │
  ├──rollback──► Rollbacking ──全部成功──► Rollbacked（终态，删会话）
  │                  │
  │                  └─有分支失败─► RollbackRetrying ──重试──► Rollbacked
  │
  └──timeoutCheck──► TimeoutRollbacking ──► TimeoutRollbacked
                     （超时回滚，RM 侧收到 TimeoutRollbacking 后
                       未提交的分支必须放弃提交转为回滚）

AsyncCommitting ──► Committed（一阶段成功即最终一致，批量异步删 undo_log）
```

`commit()` 的一个关键细节（`DefaultCore.java:246`）：

```java
// DefaultCore.java:246-249
if (globalSession.getStatus() == GlobalStatus.Begin) {
    // Highlight: Firstly, close the session, then no more branch can be registered.
    globalSession.close();                       // ① 先关门
    if (globalSession.canBeCommittedAsync()) {
        globalSession.asyncCommit();             // ② 一阶段都成功 → 异步提交
    } else {
        globalSession.changeGlobalStatus(GlobalStatus.Committing);  // ③ 正常二阶段
        shouldCommitNow = true;
    }
    globalSession.clean();
}
```

**先 close 再提交**：close 之后迟到的 `BranchRegister` 全部被拒——防止"都开始提交了还有新分支进来"。`canBeCommittedAsync()`（`GlobalSession.java:165`）判据是所有分支都已 `Phase two Committing`（即 RM 一阶段成功并 report 过），这种事务直接进 `AsyncCommitting` 让 RM 异步删 undo_log，**不用 TC 逐个发 BranchCommit**——这是 Seata 高性能的关键路径。

### 2.3 五个定时任务：failure 恢复的全部机关

| 任务 | 行号 | 扫什么状态 | 干什么 |
|---|---|---|---|
| `timeoutCheck` | `:406` | `Begin` | `isTimeout()` 为真 → `close()` + `TimeoutRollbacking` |
| `handleRetryRollbacking` | `:451` | `Rollbacking` + `RollbackRetrying` | 超 `MAX_ROLLBACK_RETRY_TIMEOUT` 就 `endRollbackFailed`（转人工）；否则 `core.doGlobalRollback(session, true)` 重试 |
| `handleRetryCommitting` | `:487` | `CommitRetrying` | 同上逻辑，超时转 `CommitRetryTimeout` 人工处理 |
| `handleAsyncCommitting` | `:524` | `AsyncCommitting` | 逐个分支发异步提交指令 |
| `undoLogDelete` | `:549` | — | 向所有 RM 广播"删 undo_log"（默认保留 7 天，`:557`） |

`isRetryTimeout(now, MAX_..., beginTime)`（`:177` 的 `RETRY_DEAD_THRESHOLD` 相关）是**重试死亡线**：超过就不再自动重试，把会话置终态失败并告警——**防止无限重试把 TC 拖死**。

`SessionHelper.forEach(..., lambda)`（`:461`）是带并行度的遍历（内部线程池分批处理大量会话），`setLazyLoadBranch(true)` 表示只加载会话列表、不加载分支（分支在真正要回滚时才查）——**大集群下这是内存保护**。

### 2.4 分支注册与全局锁

`doBranchRegister:369` 做两件事：`GlobalSession.add(branchSession)`（`:129`）+ `LockManager` 抢锁。`DataBaseLockManager`（`server/src/main/java/org/apache/seata/server/storage/db/lock/DataBaseLockManager.java`）把 `lockKeys`（`表名:主键值` 格式）转成 `lock_table` 行记录，靠数据库唯一索引保证互斥；file 模式用 `FileLocker`（本地文件锁）；redis 模式用 Lua 脚本（`RedisLuaLocker`）。

**注册失败的传播**：RM 的 `register():267` 拿不到 branchId 会抛 `TransactionException`，`processGlobalTransactionCommit` 里 `recognizeLockKeyConflictException` 把它翻译成 `LockConflictException` → `LockRetryPolicy`（`AbstractDMLBaseExecutor.java:185`）按配置重试，超过 `lock.retryTimes` 抛 `LockWaitTimeoutException` 让业务失败——**这就是 AT 写冲突时的完整链路**。

### 2.5 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `DefaultCoordinator` = RPC 门面 + 5 个定时线程池；`DefaultCore` = `CORE_MAP:59` 按 BranchType 路由 |
| 关键实现 | `commit()` 先 `close()` 再提交（`DefaultCore.java:246`）；`canBeCommittedAsync` 走高性能异步路径 |
| 并发 | `SessionHolder.lockAndExecute` 给单会话加执行锁；`SessionHelper.forEach` 并行处理大批会话 |
| 协议 | 分支注册 = add + 抢全局锁；锁键格式 `表名:主键值`；重试死亡线 `RETRY_DEAD_THRESHOLD` |
| 陷阱 | 超时事务走 `TimeoutRollbacking`，RM 未提交分支必须放弃提交；重试超限进人工态不会自愈 |

**一句记忆钩子**：*"TC 五个定时任务：超时扫、回滚重扫、提交重扫、异步提交扫、删日志广播——failure 恢复全靠扫存储。"*

---

## 第三讲：AT 模式 —— DataSourceProxy 与 undo_log 两阶段

> **本讲问题**：不改业务代码怎么实现事务？前后镜像长什么样？为什么 undo_log 和业务 SQL 必须同一个本地事务？

> 🎬 配套动画：[Seata AT 两阶段提交](/animations/seata-at/) —— 21 步单步播放，TM/TC/RM 三个角色与 undo_log、GlobalSession、全局锁同步变化，含异常回滚分支。

### 3.1 代理链：从 DataSource 到 Statement 的五层包装

```
DataSourceProxy（:50）                    ← 用户注入的 Bean
 └─ getConnection():211 → new ConnectionProxy(this, targetConnection)（:213）
     └─ prepareStatement(sql) → PreparedStatementProxy
         └─ ExecuteTemplate.execute:60（:78 重载收 sqlRecognizers）
             └─ SQLVisitorFactory.get(sql, dbType) 解析成 SQLRecognizer
                 └─ new UpdateExecutor/InsertExecutor/DeleteExecutor...
                     └─ AbstractDMLBaseExecutor.executeAutoCommitTrue:146 / False:97
```

`ExecuteTemplate.execute` 第一行就是**快速通道**：

```java
// ExecuteTemplate.java:82-84
if (!RootContext.requireGlobalLock() && BranchType.AT != RootContext.getBranchType()) {
    // Just work as original statement
    return statementCallback.execute(statementProxy.getTargetStatement(), args);
}
```

**没有全局事务上下文时，直接放行走原生 Statement**——Seata 对无事务业务的零损耗就是这么保证的。

### 3.2 一阶段：前镜像 → 执行 → 后镜像 → undo_log

`AbstractDMLBaseExecutor.executeAutoCommitFalse:97` 是 AT 的心脏：

```java
// AbstractDMLBaseExecutor.java:97-108
protected T executeAutoCommitFalse(Object[] args) throws Exception {
    try {
        TableRecords beforeImage = beforeImage();          // ① SELECT 前镜像
        T result = statementCallback.execute(statementProxy.getTargetStatement(), args);  // ② 真执行
        TableRecords afterImage = afterImage(beforeImage); // ③ SELECT 后镜像
        prepareUndoLog(beforeImage, afterImage);           // ④ 拼 undo_log
        return result;
    } catch (TableMetaException e) {
        // 表结构缓存过期 → 刷新重来
        statementProxy.getConnectionProxy().getDataSourceProxy().tableMetaRefreshEvent();
        throw e;
    }
}
```

以一条 `UPDATE product SET stock = stock - 1 WHERE id = 100` 为例（`UpdateExecutor.beforeImage:67` 会把 WHERE 条件拼成 `SELECT ... WHERE id = 100`）：

```json
// undo_log.rollback_info（BeforeImage/AferImage 序列化）
{
  "@class": "org.apache.seata.rm.datasource.undo.BranchUndoLog",
  "xbranch": 987654321,
  "undoItems": [{
    "tableName": "product",
    "sqlType": "UPDATE",
    "beforeImage": {"rows": [{"fields": [{"name":"id","value":100},{"name":"stock","value":10}]}]},
    "afterImage":  {"rows": [{"fields": [{"name":"id","value":100},{"name":"stock","value":9}]}]}
  }]
}
```

回滚时 `MySQLUndoLogManager`（`rm-datasource/src/main/java/org/apache/seata/rm/datasource/undo/mysql/MySQLUndoLogManager.java:47`）读 `rollback_info`，比对当前值和 `afterImage`：**一致才回滚**（防脏写），然后按 `beforeImage` 反向生成 `UPDATE product SET stock = 10 WHERE id = 100` 执行。

**四条关键设计**：

1. **自动提交关闭**：`changeAutoCommit()`（`ConnectionProxy.java:296`）把连接的 autocommit 设 false——保证"业务 SQL + undo_log INSERT"在**同一个本地事务**里，要么都成功要么都回滚；
2. **主键必需**：`isMultiPk():114` 会检查主键列数，无主键表直接抛异常——因为镜像按主键定位；
3. **insert 无前镜像**：`BaseInsertExecutor` 回滚就是按主键删；
4. **批量 SQL**：`MultiUpdateExecutor`/`MultiDeleteExecutor` 按行拆分记录镜像，`on duplicate update` 有专门的 `MySQLInsertOnDuplicateUpdateExecutor`。

### 3.3 本地提交：register + flushUndoLogs + commit

```java
// ConnectionProxy.java:227-245
private void doCommit() throws SQLException {
    if (context.inGlobalTransaction()) {
        processGlobalTransactionCommit();        // 全局事务路径
    } else if (context.isGlobalLockRequire()) {
        processLocalCommitWithGlobalLocks();     // @GlobalLock 路径：checkLock 后裸提交
    } else {
        targetConnection.commit();               // 普通路径
    }
}

private void processGlobalTransactionCommit() throws SQLException {
    try {
        register();                              // ① BranchRegister 拿 branchId + 占全局锁
    } catch (TransactionException e) {
        recognizeLockKeyConflictException(e, context.buildLockKeys());  // 锁冲突翻译
    }
    try {
        UndoLogManagerFactory.getUndoLogManager(this.getDbType()).flushUndoLogs(this);  // ② INSERT undo_log
        targetConnection.commit();               // ③ 业务+undo 同一个本地事务提交
    } catch (Throwable ex) {
        report(false);                           // 失败上报 TC：这个分支要回滚
        throw new SQLException(ex);
    }
    if (IS_REPORT_SUCCESS_ENABLE) {
        report(true);                            // ④ 成功上报：让 TC 知道一阶段完成
    }
    context.reset();
}
```

**顺序的精髓**：先 register（占全局锁）→ 再 flush undo → 最后本地 commit。为什么 register 要在本地事务提交前？因为**锁必须在数据可见之前拿到**——否则别人读到未提交数据还改了，undo_log 就失效了。`report(true)` 让 TC 把分支置 `Phase two Committing`，这是 `canBeCommittedAsync` 的前提。

### 3.4 二阶段：TC 指令 → RM 执行

TC 发 `BranchCommit`：RM 直接删 undo_log（`AbstractUndoLogManager.deleteUndoLog`，SQL 在 `:93` `DELETE_UNDO_LOG_SQL`）——**AT 的提交不做业务补偿**，因为业务已经提交，只是清理现场。

TC 发 `BranchRollback`：RM 按 `SELECT_UNDO_LOG_SQL`（`:89`）查 undo_log → 校验 `afterImage` 与当前一致 → 反向 SQL 回滚 → 删 undo_log。全部失败会 `report(false)`，TC 的 `handleRetryRollbacking:451` 接手重试。

`AsyncWorker`（`rm-datasource/src/main/java/org/apache/seata/rm/datasource/AsyncWorker.java`）把 undo_log 的删除做成异步批处理——**删日志不阻塞二阶段返回**，这是 AT 提交快的另一个原因。

### 3.5 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | 五层代理：DataSourceProxy → ConnectionProxy → StatementProxy → ExecuteTemplate → Executor |
| 关键实现 | 一阶段四步（前镜像→执行→后镜像→undo）在同一本地事务；`register` 在本地 commit 前 |
| 并发 | autocommit 被强改 false；全局锁冲突 → `LockRetryPolicy` 重试 → `LockWaitTimeoutException` |
| 协议 | undo_log 与业务同库同事务；`report(true/false)` 驱动 TC 的 async 判定 |
| 陷阱 | 无主键表不能用 AT；`afterImage` 不一致说明有脏写，回滚会失败 |

**一句记忆钩子**：*"AT = 前镜像 + 后镜像 + undo_log 三件套，注册在提交前，undo 与业务同事务。"*

---

---

## 第四讲：TCC 模式与 fence 防悬挂

> **本讲问题**：TCC 为什么需要业务写三个方法？防悬挂/防空回滚/幂等这三座大山，Seata 用什么表、什么状态机挡住？

### 4.1 TCC 的三个方法与被拦截的只有 Try

```java
// tcc/src/main/java/org/apache/seata/rm/tcc/api/TwoPhaseBusinessAction.java:47
public @interface TwoPhaseBusinessAction {
    String name();                          // 分支动作名
    Class<?>[] commitMethod() default {};   // 二阶段提交方法
    Class<?> rollbackMethod() default {};   // 二阶段回滚方法（默认约定同名 cancel）
    boolean useTCCFence() default false;    // 是否启用 fence 防悬挂
    ...
}
```

`TccActionInterceptorHandler`（`tcc/src/main/java/org/apache/seata/rm/tcc/interceptor/TccActionInterceptorHandler.java:48`）只代理 **Try** 方法（`methodsToProxy`，构造时由 `TccActionInterceptorParser` 解析注解得到）。执行链路：

```
Try 方法调用
 ├─ 解析 @TwoPhaseBusinessAction → BusinessActionContext（xid、branchId、actionName、参数快照）
 ├─ 构造 ActionContext（JSON 序列化，随二阶段 RPC 传给 confirm/cancel）
 ├─ 调 TCC RM 注册分支（BranchType.TCC）
 ├─ 执行真正的 Try 业务
 └─ 方法上 @CommitMethod 的所在 Bean 由框架托管（confirm/cancel 由 TC 二阶段 RPC 驱动调用）
```

**ActionContext 的传递是 TCC 最容易出错的地方**：Try 的入参必须能序列化进上下文，confirm/cancel 才能还原（`ActionContextUtil`）。参数里有不可序列化对象（如 `HttpServletRequest`）时，TCC 会直接失败——这是 TCC 侵入性大于 AT 的根本原因。

### 4.2 fence：一张表解决三个问题

Seata 1.5+ 的 `useTCCFence` 把"防悬挂、防空回滚、幂等"收进一张本地表 `tcc_fence_log`：

```java
// integration-tx-api/src/main/java/org/apache/seata/integration/tx/api/fence/constant/CommonFenceConstant.java:32-47
public static final int STATUS_TRIED     = 1;   // try 已执行
public static final int STATUS_COMMITTED = 2;   // confirm 已完成
public static final int STATUS_ROLLBACKED = 3;  // cancel 已完成
public static final int STATUS_SUSPENDED = 4;   // 悬挂（try 迟到，已被回滚）
```

核心 SQL（`integration-tx-api/src/main/java/org/apache/seata/integration/tx/api/fence/store/db/sql/CommonFenceStoreSqls.java`）：

```java
// :45  INSERT_LOCAL_TCC_LOG —— try 时插入一行（status=TRIED）
"insert into tcc_fence_log (xid, branch_id, action_name, status, gmt_create, gmt_modified) values (?, ?, ?, ?, ?, ?)"

// :49  QUERY_BY_BRANCH_ID_AND_XID —— confirm/cancel 先查（for update 行锁）
"select xid, branch_id, status, ... from tcc_fence_log where xid = ? and branch_id = ? for update"

// :81  UPDATE_STATUS_BY_BRANCH_ID_AND_XID —— 带旧状态的 CAS 式更新
"update tcc_fence_log set status = ?, gmt_modified = ? where xid = ? and branch_id = ? and status = ?"
```

`update ... and status = ?` 这个**带旧状态的条件**就是幂等的全部机关：confirm 只能把 `TRIED(1) → COMMITTED(2)`，重复 confirm 时旧状态已是 `2`，更新影响 0 行 → 框架直接返回"已处理过"。DAO 层对应 `CommonFenceStoreDataBaseDAO.updateCommonFenceDO:146`。

三个问题的挡法：

| 问题 | 场景 | fence 怎么挡 |
|---|---|---|
| **空回滚** | TC 回滚时 RM 没执行过 try（没这行记录） | cancel 查不到记录 → 插入一条 `SUSPENDED(4)` 记录，**告诉后面的 try"我已经回滚过了"** |
| **悬挂** | try 迟于 cancel 到达 | try 时先查：已有 `ROLLBACKED` 或 `SUSPENDED` 记录 → **拒绝执行 try 业务**，只记日志 |
| **幂等** | TC 重试导致 confirm/cancel 重复调用 | 带旧状态的 update 影响 0 行 → 视为已处理，直接返回成功 |

清理任务 `initCommonFenceCleanTask:187` 定期删终态行（`queryEndStatusXidsByDate:96` + `deleteTCCFenceDO:183`），SQL 里 `QUERY_END_STATUS_BY_DATE`（`:53`）只删 `gmt_modified` 过期的终态记录——**同事务其他分支还在非终态的行不会被误删**（源码注释专门强调了这一点，`:100-103`）。

### 4.3 和 AT 的对比（选型依据）

| 维度 | AT | TCC |
|---|---|---|
| 业务侵入 | 零（换 DataSource） | 高（三个方法 + ActionContext） |
| 隔离性 | RU（一阶段提交即可见） | 由 Try 的资源预留程度决定 |
| 性能 | 好（无预留，undo 补偿） | 中（预留资源，锁持有到二阶段） |
| 适用 | 普通关系型数据库 CRUD | 非数据库资源（消息、第三方 API、Redis） |
| 幂等 | undo_log 天然幂等 | **业务自己保证**（fence 是兜底） |

### 4.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `TwoPhaseBusinessAction:47` 声明三方法；只有 Try 被拦截（`TccActionInterceptorHandler:48`） |
| 关键实现 | fence 表四状态（TRIED/COMMITTED/ROLLBACKED/SUSPENDED）+ 带旧状态的 CAS update |
| 并发 | `QUERY_BY_BRANCH_ID_AND_XID` 用 `for update` 行锁串行化同一分支的二阶段 |
| 协议 | ActionContext 序列化随 RPC 传递；confirm/cancel 由 TC 驱动 |
| 陷阱 | 悬挂的根治靠"SUSPENDED 占位"；清理任务不能删非终态的同 xid 行 |

**一句记忆钩子**：*"fence 一张表四状态：TRIED 起步、CAS 保幂等、SUSPENDED 占位挡悬挂。"*

---

## 第五讲：Saga 状态机与 XA 模式

> **本讲问题**：几百个服务的长事务怎么编排？XA 和 AT 在源码层的分界线在哪？

### 5.1 Saga：JSON 定义的状态机

`StateMachineEngine`（`saga/seata-saga-engine/src/main/java/org/apache/seata/saga/engine/StateMachineEngine.java:29`）是接口，`ProcessCtrlStateMachineEngine`（`.../engine/impl/ProcessCtrlStateMachineEngine.java:60`）是实现：

```java
// ProcessCtrlStateMachineEngine.java:75
public StateMachineInstance start(String stateMachineName, String tenantId, Map<String, Object> startParams)
// :223
public StateMachineInstance forward(String stateMachineInstId, Map<String, Object> replaceParams)   // 继续推进
// :235
protected StateMachineInstance forwardInternal(...)   // 核心循环：找下一个状态 → 执行 → 持久化
```

一个 Saga 状态机就是一份 JSON：`States`（`ServiceTask`/`Choice`/`Succeed`/`Fail`/`SubStateMachine`）+ `StartState` + 每个状态的 `Input/Output` 表达式。执行模型：

```
start() → 建 StateMachineInstance（持久化到 state_machine_inst 表）
   → forwardInternal:256
       ├─ 找"上次成功状态的下一跳"（:271）
       ├─ 执行 ServiceTask（调本地服务或 HTTP）
       ├─ 成功 → 记 StateInstance（state_inst 表）→ 推下一跳
       └─ 失败 → 进入补偿模式：按执行栈逆序逐个调 compensator
```

**和 TCC 的本质差异**：Saga 没有"预留"，服务调用**直接生效**；失败后靠补偿服务（`compensateState`）逆序回滚。所以 Saga 的补偿必须幂等、可重入——`state_inst` 表记录的正是"哪些状态执行过、补偿到哪了"，崩溃后靠它续跑（`:335` 注释 "skip forward" 就是续跑判定）。

### 5.2 XA：走数据库标准协议

XA 模式的代理在 `rm-datasource/src/main/java/org/apache/seata/rm/datasource/xa/ConnectionProxyXA.java`，与 AT **共用 DataSourceProxy 外壳、换掉内核**：

```java
// ConnectionProxyXA.java:345（一阶段）
int prepare = xaResource.prepare(xaBranchXid);
if (prepare == XAResource.XA_RDONLY) { ... }   // 只读分支，直接结束
// prepareTime 记录（:65/:409）——TC 靠它判断分支是否处于 prepared 悬挂态
```

链路差异：

| 阶段 | AT | XA |
|---|---|---|
| 一阶段 | 前后镜像 + undo_log + 本地 commit | `XAResource.end/prepare`（**不 commit**，连接挂起） |
| 二阶段提交 | 删 undo_log | `XAResource.commit(xaBranchXid, false)` |
| 二阶段回滚 | undo 反向 SQL | `XAResource.rollback(xaBranchXid)` |
| 资源占用 | 无（提交即释放） | **prepare 后连接不能归还池**，二阶段结束才释放 |

`commit():229` 开头那几个检查（`combine`/`currentAutoCommitStatus`/`xaActive`）说明 XA 连接有严格状态机：autocommit 会话和只读事务直接跳过；非 active 会话调用 commit 抛 `SQLSTATE_XA_NOT_END`。

**XA 的坑**：`prepare` 后如果 TC 一直不发二阶段指令，连接永远挂起 → 连接池打满 → 业务雪崩。Seata 靠 `prepareTime` + TC 超时检查缓解，但生产上 XA 模式集群规模通常明显小于 AT。

### 5.3 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | Saga = `ProcessCtrlStateMachineEngine:60` + JSON 状态机 + state_inst 持久化；XA = `ConnectionProxyXA` + `XAResource` |
| 关键实现 | `forwardInternal:235` 的"找下一跳 → 执行 → 持久化"循环；XA `prepare` 在 `:345` |
| 并发 | Saga 状态实例有状态校验（`:256` `checkStatus`），防并发 forward；XA prepare 后连接独占 |
| 协议 | Saga 补偿逆序执行；XA 二阶段靠 TC 指令驱动 `commit/rollback` |
| 陷阱 | Saga 补偿必须幂等；XA prepare 后连接挂起是容量杀手 |

**一句记忆钩子**：*"Saga 没有预留、失败补；XA 有 prepare、连接挂；AT 有镜像、undo 偿。"*

---

## 第六讲：存储与集群 —— SessionManager / LockManager / Raft

> **本讲问题**：TC 单机存文件、集群存什么？多 TC 之间怎么保证同一个 xid 只有一个协调者？

### 6.1 SessionManager 家族：同一抽象四种存储

```
SessionManager（接口）
 └─ AbstractSessionManager（模板方法：add/remove/find 的公共逻辑）
     ├─ FileSessionManager（server/src/main/java/org/apache/seata/server/session/FileSessionManager.java）—— 本地文件，单机
     ├─ DataBaseSessionManager（server/src/main/java/org/apache/seata/server/storage/db/session/DataBaseSessionManager.java:43）—— MySQL
     ├─ RedisSessionManager（server/src/main/java/org/apache/seata/server/storage/redis/session/RedisSessionManager.java:41）—— Redis + Lua
     └─ RaftSessionManager（server/src/main/java/org/apache/seata/server/storage/raft/session/RaftSessionManager.java:52）
         └─ extends FileSessionManager —— 本地文件 + Raft 复制，多 TC 强一致
```

`SessionHolder`（`server/src/main/java/org/apache/seata/server/session/SessionHolder.java`）是静态门面：`reload(Collection<GlobalSession>, SessionMode)`（`:175`）在 TC 启动时把存储里的会话**全部加载回内存**，`findGlobalSession(xid):373` 走内存 Map，`distributedLockAndExecute:428` 给跨节点的操作加分布式锁。

LockManager 与之一一对应：`FileLocker`（本地）/`DataBaseLockManager`（唯一索引）/`RedisLuaLocker`（Lua）/`RaftLockManager`。

### 6.2 三种部署形态

| 形态 | 存储 | 一致性 | 适用 |
|---|---|---|---|
| 单机 file | 本地文件（`data/sessionStore`） | 无 | 开发/测试 |
| db/redis | 共享数据库/Redis | 依赖外部组件 | 中小生产（最常见） |
| raft | 本地文件 + Raft 日志复制 | **强一致（Raft 选主 + 多数派）** | 大生产、不依赖外部组件 |

### 6.3 RaftCoordinator：多 TC 的协调者

`RaftCoordinator`（`server/src/main/java/org/apache/seata/server/coordinator/RaftCoordinator.java:37`）继承 `DefaultCoordinator`，把每个事务操作（`AddGlobalSession`/`RemoveGlobalSession`/`AddBranchSession`/`BranchReleaseLock`…）封装成 Raft 日志条目（`server/cluster/raft/execute/` 下的 `*Execute` 类），走 `RaftServer`（`server/cluster/raft/RaftServer.java`）复制到多数派后才应用。

`GROUP_PREVENT`（`:39`）+ `onApplicationEvent:76` 监听 `ClusterChangeEvent`：**leader 切换时阻止旧 group 的写**，防止脑裂（两个 TC 同时认为自己是协调者）。这是 raft 模式比 db 模式强的关键——db 模式下靠数据库锁和 `distributedLockAndExecute` 弱保证。

### 6.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `SessionManager` 四实现（file/db/redis/raft）+ 对应 `LockManager`；`SessionHolder` 静态门面 |
| 关键实现 | TC 启动 `reload:175` 全量加载；`RaftSessionManager:52` 本地文件 + raft 复制 |
| 并发 | `distributedLockAndExecute:428` 跨节点串行化；raft leader 切换用 `GROUP_PREVENT` 防脑裂 |
| 协议 | 事务操作 = Raft 日志条目，多数派落盘才应用 |
| 陷阱 | file 模式换机器丢事务；db 模式性能瓶颈在 lock_table 行锁 |

**一句记忆钩子**：*"会话内存查、存储持久化；file 单机、db/redis 共享、raft 强一致还防脑裂。"*

---

## 附：学习路线与复习卡汇总

### 建议学习顺序（15 讲时）

| 阶段 | 内容 | 时长建议 | 产出 |
|---|---|---|---|
| 第 1 天 | 第一、二讲（TM + TC 协调） | 4h | 画出一次全局事务的全部 RPC 与状态变迁 |
| 第 3 天 | 第三讲（AT 模式） | 4h | 手写一条 UPDATE 的前后镜像与回滚 SQL |
| 第 7 天 | 第四讲（TCC + fence） | 3h | 说清悬挂/空回滚/幂等各自的挡法 |
| 第 14 天 | 第五、六讲（Saga/XA/存储） | 4h | 给出三种模式的选型表 |

### 复习卡（遮答案自测）

1. TM 一次成功全局事务发几次 RPC？各是什么？
2. Participant 调 `commit()` 会发生什么？源码在哪一行？
3. `commit()` 为什么先 `close()` 再改状态？
4. `canBeCommittedAsync` 的判据是什么？命中后走什么路径？
5. 五个定时任务分别扫什么状态、做什么动作？
6. AT 一阶段四个动作的顺序，为什么 register 在本地 commit 前？
7. undo_log 和业务 SQL 为什么必须同一本地事务？
8. AT 回滚前为什么要比对 `afterImage`？
9. fence 表四个状态的含义与流转？
10. "update ... where status = ?" 这行 SQL 解决了什么问题？
11. Saga 和 TCC 的三个核心差异？
12. XA prepare 之后连接处于什么状态？有什么风险？
13. `SessionHolder.reload` 在 TC 启动时做什么？
14. raft 模式怎么防脑裂？
15. AT 为什么要求主键？无主键表怎么办？

### 六讲记忆钩子串烧

*"Launcher 发号施令，Participant 只管干活"* → *"TC 五个定时任务，failure 恢复全靠扫存储"* → *"AT = 前镜像 + 后镜像 + undo_log 三件套"* → *"fence 一张表四状态，SUSPENDED 占位挡悬挂"* → *"Saga 没有预留、失败补；XA 有 prepare、连接挂"* → *"会话内存查、存储持久化；file 单机、db/redis 共享、raft 强一致"*

---

## 涉及文件索引

| 模块 | 文件 | 本笔记引用次数 |
|---|---|---|
| tm | `tm/src/main/java/org/apache/seata/tm/api/TransactionalTemplate.java`（543 行） | 高 |
| tm | `tm/src/main/java/org/apache/seata/tm/api/DefaultGlobalTransaction.java`（581 行） | 高 |
| tm | `tm/src/main/java/org/apache/seata/tm/DefaultTransactionManager.java` | 中 |
| integration-tx-api | `.../interceptor/handler/GlobalTransactionalInterceptorHandler.java` | 高 |
| integration-tx-api | `.../fence/DefaultCommonFenceHandler.java`、`.../fence/store/db/sql/CommonFenceStoreSqls.java`、`.../fence/constant/CommonFenceConstant.java` | 高 |
| server | `server/src/main/java/org/apache/seata/server/coordinator/DefaultCoordinator.java`（930 行） | 高 |
| server | `.../coordinator/DefaultCore.java`（552 行） | 高 |
| server | `.../coordinator/RaftCoordinator.java` | 中 |
| server | `.../session/GlobalSession.java`、`.../session/SessionHolder.java`、`.../session/BranchSession.java` | 高 |
| server | `.../storage/db/lock/DataBaseLockManager.java`、`.../storage/file/lock/FileLocker.java` | 中 |
| server | `.../storage/{db,redis,raft,file}/session/*SessionManager.java` | 中 |
| rm-datasource | `rm-datasource/src/main/java/org/apache/seata/rm/datasource/DataSourceProxy.java` | 高 |
| rm-datasource | `.../rm/datasource/ConnectionProxy.java` | 高 |
| rm-datasource | `.../rm/datasource/exec/ExecuteTemplate.java`、`.../exec/AbstractDMLBaseExecutor.java`、`.../exec/UpdateExecutor.java`、`.../exec/InsertExecutor.java` | 高 |
| rm-datasource | `.../rm/datasource/undo/AbstractUndoLogManager.java`、`.../undo/mysql/MySQLUndoLogManager.java` | 高 |
| rm-datasource | `.../rm/datasource/xa/ConnectionProxyXA.java`、`.../xa/AbstractConnectionProxyXA.java` | 中 |
| tcc | `tcc/src/main/java/org/apache/seata/rm/tcc/api/TwoPhaseBusinessAction.java`、`.../interceptor/TccActionInterceptorHandler.java` | 中 |
| saga | `saga/seata-saga-engine/src/main/java/org/apache/seata/saga/engine/StateMachineEngine.java`、`.../engine/impl/ProcessCtrlStateMachineEngine.java` | 中 |
