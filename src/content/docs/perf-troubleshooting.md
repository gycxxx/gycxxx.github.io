---
title: "线上问题排查与性能瓶颈定位实战"
date: 2026-10-20
categories:
  - "Java"
tags:
  - "排查"
  - "性能"
  - "JVM"
  - "运维"
  - "源码"
---

## 〇、全景地图

线上问题排查只有三个朴素原则，本笔记的一切方法都从它们推导出来：

1. **证据优先**：第一件事永远是**快照**，不是重启。重启能恢复业务，但会销毁现场——线程栈、GC 状态、连接数这些"案发现场"只存在于故障当时。
2. **定量 + 定性**：监控指标告诉你"有多慢、慢在哪个接口"（定量），线程栈告诉你"卡在哪一行代码"（定性）。只看指标是猜，只看栈是盲，两者对照才能定位。
3. **趁"病中"采集**：故障恢复后再采的快照是无效证据。所以采集脚本必须**一键、快速、只读**——本笔记的两个脚本就是按这个标准写的。

本笔记围绕仓库里两个快照脚本展开：

```
┌────────────────────── 两个采集脚本 ──────────────────────┐
│                                                        │
│  perf-snapshot.sh（容器版）      perf-snapshot-host.sh  │
│  目标：K8s Pod                   目标：物理机/虚拟机 JVM │
│  入口：kubectl exec             入口：本机 JDK 工具     │
│                                                        │
│  预检A exec连通性(:62)           进程定位 PID/关键字/端口│
│  预检B 容器内工具(:83)           JDK 推导 :75-77        │
│       ↓                              ↓                 │
│  ┌── 容器版独有降级 ──┐          ┌── 主机版独有能力 ──┐  │
│  │ 无curl→port-forward│          │ top -H 线程级CPU  │  │
│  │ 无jstack→kill -3   │          │ nid→Java栈映射    │  │
│  │ 信号法(:156)       │          │ vmstat/iostat/ss  │  │
│  └────────────────────┘          │ jstat -gcutil     │  │
│                                  └────────────────────┘  │
└──────────────────────────────────────────────────────────┘
                          ↓ 产出
┌────────────────────── 四类证据 ──────────────────────────┐
│ ① 监控指标    metrics.txt / metrics-slow-api.txt          │
│               metrics-resource.txt                        │
│ ② 线程栈      jstack.txt（N 次采样，不是 trace）           │
│ ③ 聚合分析    analysis-thread-state / -hot-frames /       │
│               -blocked / -cpu-hot-stacks                  │
│ ④ 整机资源    host-resources.txt（仅主机版）               │
└──────────────────────────────────────────────────────────┘
                          ↓ 判读
        容器版：线程状态分布 → 热点栈帧 → 慢接口指标（:259）
        主机版：高CPU线程 → cpu-hot-stacks → 是否在等外部资源（:267）
```

一句话记住全景：**脚本负责把"案发现场"固化成文件，排查负责按"状态分布 → 热点帧 → 指标对照"的顺序读文件**。本笔记第二讲教你读文件，第三讲给你五个经典剧本，第四讲是信号速查表，第五讲解释信号背后的源码机制。

---

## 速览：四讲各自做什么

| 讲次 | 主题 | 核心问题 | 不学会怎样 |
|---|---|---|---|
| 第一讲 | 采集脚本全景 | 两个脚本各采集什么、怎么选、有哪些降级路径 | 采集方式不对，拿到假数据或空文件 |
| 第二讲 | 输出文件字典 | 每个产出文件看什么、什么信号指向什么根因 | 面对一堆 txt 不知道从哪读起 |
| 第三讲 | 五个经典剧本 | CPU 飙高 / 接口变慢 / 死锁 / OOM / 线程池打满 | 遇到故障靠玄学，排查全凭运气 |
| 第四讲 | 信号→根因速查 + 源码机制 | 一张大表 + Thread.State、阻塞原语、nid 映射的源码级解释 | 看得见信号，看不懂为什么 |

---

## 排查速通：高频问答

| # | 问题 | 一句话结论 | 深入 |
|---|---|---|---|
| 1 | 线上出问题，第一件事做什么？ | **先快照再重启**。重启恢复业务但销毁现场（线程栈/GC/连接数只存在于故障当时）；用 perf-snapshot 脚本一键固化证据，再考虑重启 | `perf-snapshot.sh:26`、`perf-snapshot-host.sh:24` |
| 2 | 容器里和物理机上排查有什么不同？ | 容器版靠 `kubectl exec` 进 Pod，可能遇到无 curl/无 jstack 的精简镜像（有三级降级）；主机版能用 `top -H` 拿线程级 CPU 并把 nid 映射回 Java 栈，这是容器版拿不到的 | `perf-snapshot.sh:93-98`、`perf-snapshot-host.sh:135-145` |
| 3 | jstack 采样多少次合适？ | 默认 10 次、间隔 1 秒（容器版默认参数）。**jstack 是采样不是 trace**：采样间隔内的短任务会漏，慢任务才会反复出现——热点帧出现次数越多越可能是瓶颈 | `perf-snapshot.sh:31`、`perf-snapshot.sh:146-153` |
| 4 | 线程状态分布怎么读？ | 看占比。`BLOCKED` 多 = 锁竞争；`socketRead0` 帧多 = 在等外部（DB/Redis/下游）；`RUNNABLE` 多但 CPU 不高 = 大量线程卡在 native 系统调用 | `perf-snapshot.sh:196-198`、`perf-snapshot.sh:236-241` |
| 5 | RUNNABLE 一定是在跑 CPU 吗？ | **不是**。线程阻塞在 native `read(2)`/`write(2)` 时 Java 侧同样显示 RUNNABLE——`Thread.State` 的 RUNNABLE 注释明说"可能正在等操作系统的处理器等资源" | `java/lang/Thread.java:2343` |
| 6 | 怎么找到真正烧 CPU 的线程？ | 主机版：`top -b -n1 -H -p PID` 抓 CPU>10% 的线程，把十进制 tid 格式化成十六进制 nid，再去 jstack 里 `grep nid=0x...` 找对应栈（脚本已自动做好 `analysis-cpu-hot-stacks.txt`） | `perf-snapshot-host.sh:140`、`perf-snapshot-host.sh:216-219` |
| 7 | 容器里没有 jstack 怎么办？ | 信号法：`kill -3 <java_pid>` 让 JVM 把线程栈打到 stdout，再从 `kubectl logs --tail=1500` 里捞（输出混有业务日志，按线程段头搜索） | `perf-snapshot.sh:156-173` |
| 8 | 没有 curl 的容器怎么采指标？ | 本地 `kubectl port-forward -n <ns> pod/<pod> <port>:<port>`，再 `curl http://127.0.0.1:<port>/actuator/prometheus`。脚本会把这行命令直接打印出来 | `perf-snapshot.sh:118-122` |
| 9 | `socketRead0` 在栈里出现说明什么？ | 线程阻塞在 socket 读上——在等下游响应。脚本的 SUMMARY 已按 `druid/jedis/okhttp` 三类归好命中数，直接看是哪类外部依赖在拖后腿 | `perf-snapshot.sh:238-241`、`sun/nio/ch/SocketChannelImpl.java:1373` |
| 10 | 接口慢但 CPU  idle，去哪找证据？ | `metrics-slow-api.txt`（P99/Max 排行）定位是哪个接口，再对照多次 jstack 的热点帧：如果帧大量停在数据库/Redis/HTTP 客户端的调用处，就是下游慢不是自己慢 | `perf-snapshot.sh:185-188` |
| 11 | 怎么判断线程池打满了？ | 对比 `tomcat_threads_busy_threads` 与 `tomcat_threads_config_max_threads`：busy 贴着 max 且持续不降，就是打满；此时新请求在 accept 队列排队，表现为 RT 整体抬升 | `perf-snapshot.sh:245-246`、`TomcatMetricsBinder.java:64` |
| 12 | OOM 之后还能采到什么？ | 进程已死，jstack/jstat 都采不到。所以 OOM 要靠**事前**配置 `-XX:+HeapDumpOnOutOfMemoryError`；事后只能看 Pod 的 restart 记录和上一次 OOM 前是否有人采过快照 | `perf-snapshot-host.sh:166-171` |
| 13 | `waiting to lock` 和 `parking to wait for` 有什么区别？ | 前者是 `synchronized` 监视器锁（线程状态 `BLOCKED`），后者是 JUC/AQS 锁如 `ReentrantLock`（线程状态 `WAITING`）。竞争激烈程度和修复方式不同 | `java/lang/Thread.java:2352`、`java/util/concurrent/locks/ReentrantLock.java:322` |
| 14 | 死锁怎么确认？ | jstack 输出里直接搜 `Found one Java-level deadlock`。注意容器版 `analysis-blocked.txt` 的 grep 模式**不含** deadlock 关键词，要自己搜 `jstack.txt` | `perf-snapshot.sh:207-208`、`perf-snapshot-host.sh:225-226` |
| 15 | `/actuator/prometheus` 返回 404 怎么办？ | actuator 没暴露该端点。开启 `management.endpoints.web.exposure.include`（含 prometheus）；脚本探测失败时会把这行配置建议直接打出来 | `perf-snapshot.sh:134-135`、`MetricsProperties.java:89` |
| 16 | 高 CPU 但热点帧全是自家业务代码，说明什么？ | 两种可能：真在做重计算（算法/正则/序列化），或 GC 线程在烧 CPU（Young GC 风暴）。看 `jstat -gcutil` 的 YGC/FGC 增速和 `jvm_gc_pause_seconds_max` 区分 | `perf-snapshot-host.sh:175-180`、`perf-snapshot.sh:249` |
| 17 | 整机负载高但进程 CPU 不高，查什么？ | 主机版的 `vmstat` 看 `r`（运行队列）是否超过核数、`wa`（IO 等待）是否高，`iostat -x` 看磁盘 `%util`；再用 `ss -ant` 看连接堆积。这是"邻居吵闹"或磁盘瓶颈的典型信号 | `perf-snapshot-host.sh:111-113`、`perf-snapshot-host.sh:116-124` |
| 18 | 为什么采集脚本都要做预检？ | 避免产出**假数据**：exec 不通时后续所有文件都是空的，会误导排查方向。容器版预检 A 失败立即退出并打印排查清单（namespace 错/Pod 已重建/多容器未指定） | `perf-snapshot.sh:61-75` |

### 易错点（现场最容易踩的 8 条）

1. **重启前不快照**：故障窗口一过，同样的栈再也采不到。快照只要几十秒，代价极低。
2. **采样间隔太长**：默认 1 秒是经验值；秒级抖动（如定时任务尖刺）用 1 秒间隔可能整段错过，应缩短间隔加大次数。
3. **只看线程状态分布下结论**：RUNNABLE 里混着大量"阻塞在 native IO"的线程（见 Q5），必须结合热点帧和 CPU 数据。
4. **热点帧排行忽略 JDK 前缀过滤**：脚本已排除 `java.base/jdk.internal/sun./java.lang/java.util` 前缀（`perf-snapshot.sh:203`），但这些帧恰是锁/IO 阻塞的**现场**（如 `LockSupport.park`），排查阻塞问题时要回原 `jstack.txt` 看完整栈。
5. **把 P99 高当成事故**：先看调用量。QPS 极低的接口 P99 天然波动大，要结合 `_count` 类和 Max 一起看。
6. **容器版直接搜 deadlock**：容器版 `analysis-blocked.txt` 不含 deadlock 模式，死锁证据要搜原始 `jstack.txt` 的 `Found one Java-level deadlock`。
7. **主机版用错 JDK 工具**：jstack/jcmd 版本必须和目标 JVM 匹配，否则栈采不全。脚本用 `/proc/PID/exe` 反推进程自身的 JDK（`perf-snapshot-host.sh:75-77`），不要用 PATH 里的另一个 JDK。
8. **忽略变更时间线**：快照只给"现在为什么坏"，发布记录/配置变更才给"什么时候开始坏"。两者对照才能定责。

---

## 第一讲：采集脚本全景 —— 两个快照工具的分工与降级设计

> **本讲问题**：两个脚本各自采集什么？容器和主机的采集能力差在哪？为什么脚本里有这么多"预检"和"降级"？

### 1.1 perf-snapshot.sh：容器版，kubectl exec 进 Pod

```bash
bash scripts/perf-snapshot.sh <pod名> [容器名] [端口] [采样次数] [间隔秒]
# 示例
bash scripts/perf-snapshot.sh zyaimp-card-bizcard-test-8479869bdf-g2sv6 "" 18006
bash scripts/perf-snapshot.sh zyaimp-card-gateway-test-546dd68f65-vqh55 zyaimp-card-gateway-test 18008 10 1
```

参数默认值（`perf-snapshot.sh:30-32`）：端口 `18006`、采样 `10` 次、间隔 `1` 秒。namespace 优先取环境变量 `NS`，未指定时取当前 kubeconfig context 的 namespace（`kubectl config view --minify -o jsonpath='{..namespace}'`，`:36`），再兜底 `default`——**多集群/多命名空间环境一定要显式传 NS**，否则容易采错环境。

执行分四个阶段，外加两道预检：

```
预检 A：kubectl exec 连通性（:62）
  失败 → 立即退出 + 打印排查清单（namespace 错 / Pod 名已变 / 多容器 / NS 未指定）
  目的：采不到数据时明确报错，绝不出空文件误导后续排查
预检 B：容器内工具探测（:83）
  探测 curl/wget/jstack/jcmd/jps → 决定后面走哪条降级路径
```

**降级路径一：容器内没有 curl/wget**（`HAS_CURL=0`）。指标不丢——脚本把 port-forward 命令直接打印给你（`:118-122`），本地另开终端执行即可拿到同样的 `metrics.txt`：

```bash
kubectl port-forward -n <ns> pod/<pod> 18006:18006
curl -s http://127.0.0.1:18006/actuator/prometheus -o perf-xxx/metrics.txt
curl -s http://127.0.0.1:18006/actuator/threaddump -o perf-xxx/threaddump.json
```

**降级路径二：容器内没有 jstack/jcmd**（常见于 distroless 镜像）。改用**信号法**（`:156-173`）：先找 Java 进程 PID（先 `ps -ef | grep [j]ava`，不行再扫 `/proc/*/cmdline`），然后 `kill -3` 让 JVM 自己把栈打到 stdout，最后 `kubectl logs --tail=1500` 把栈捞回来。代价是输出混有业务日志，要按线程段头（`="main"` 这类）切分。

**阶段 0 端点探测**（`:104-116`）：依次试 `/actuator/prometheus`、`/metrics`、`/actuator/metrics`，取第一个 200 的；另外探测 `/druid/index.html`，开启时会顺带拉 `/druid/basic.json` 并提示你 port-forward 后访问 `/druid/sql.html` 看慢 SQL。

**阶段 2 的 jstack 采样**（`:145-153`）有个细节：优先用 `jcmd <pid> Thread.print` 而不是 `jstack`——`jcmd` 输出更稳定（不依赖 `jsadebugd` 那条老链路），且和 `jstack` 信息等价。

### 1.2 perf-snapshot-host.sh：主机版，整机视角 + 线程级 CPU

```bash
bash scripts/perf-snapshot-host.sh <PID|进程关键字> [端口] [采样次数] [间隔秒]
bash scripts/perf-snapshot-host.sh 12345                    # 按 PID
bash scripts/perf-snapshot-host.sh card-bizcard 18006       # 按关键字猜进程
bash scripts/perf-snapshot-host.sh "" 18006 30 2            # 按端口猜进程
```

**进程定位三通道**（`perf-snapshot-host.sh:41-64`）：数字且 `/proc/<pid>` 存在 → 直接当 PID；否则按关键字匹配 `jps -l`（无 jps 退化为 `ps -eo pid,args | grep [j]ava`），匹配多个取第一个并提示；也可以按监听端口反查（`ss -lntp` 抓 `pid=`，无 ss 用 `lsof -ti tcp:<port>`）。

**JDK 工具推导**（`:75-77`）：`readlink -f /proc/PID/exe` 拿到 java 可执行文件真实路径，上翻两级就是 `JDK_HOME`，之后 `pick_tool` 优先用**进程自身的** `jstack/jcmd/jstat`。这一步很重要：用 PATH 里另一个版本的 JDK 工具去 attach 目标 JVM，可能采出残缺栈或直接失败。

**主机版的独有能力**（容器版拿不到的三样）：

1. **线程级 CPU**（`:135-145`）：`top -b -n1 -H -p <pid>` 逐线程抓 CPU，筛出 >10% 的线程，把十进制 tid 用 `printf '%x'` 换成十六进制 nid——jstack 里每个线程头都带 `nid=0x...`，两者一对就能定位"哪个 Java 线程在烧 CPU"。
2. **整机资源**（`:106-127`）：`uptime`/`free`/`vmstat 1 5`/`iostat -x 1 3`/`df`/`ss -s` + 按状态统计连接数 + 到本机端口的连接数 + 进程级 `ps`（RSS/线程数/状态）。
3. **GC 实时曲线**（`:175-180`）：`jstat -gcutil <pid> 1000 <n>`，看到 FGC 每几次采样就 +1 就是 Full GC 风暴。

**空栈保护**（`:166-171`）：如果 jstack 采出来不到 10 行，脚本直接给出三种最常见原因——运行用户与 Java 进程不同用户（需 same user 或 root）、JRE 精简版没有 jstack/jcmd、以及"你在容器里，应该用另一个脚本"。

### 1.3 两个脚本怎么选

| 维度 | perf-snapshot.sh（容器版） | perf-snapshot-host.sh（主机版） |
|---|---|---|
| 目标环境 | K8s Pod | 物理机/虚拟机上的裸 JVM |
| 入口 | `kubectl exec` | 本机 shell + 进程自身 JDK 工具 |
| 线程级 CPU | ❌ 拿不到 | ✅ `top -H` + nid→栈映射 |
| 整机资源 | ❌ 拿不到（容器视角被 cgroup 隔离） | ✅ vmstat/iostat/ss/free |
| GC 曲线 | 指标里的 `jvm_gc_pause_seconds` | ✅ `jstat -gcutil` 实时 |
| 主要降级 | 无 curl → port-forward；无 jstack → kill -3 | 无 jstack → 跳过并告警 |
| 典型场景 | 测试环境联调排查、容器内问题定性 | 生产物理机 CPU/内存/磁盘问题 |

**选择规则**：服务跑在 K8s 里用容器版；跑在虚拟机/物理机上用主机版；**主机版能回答"是不是机器/磁盘/邻居的问题"，容器版只能回答"是不是这个进程的问题"**——两者结论完全不同，选错脚本会漏掉半个真相。

### 1.4 本讲小结

| 维度 | 结论 |
|---|---|
| 设计哲学 | 只读、一键、快速；预检失败立即退出，绝不出假数据 |
| 容器版核心 | exec 连通性预检 + 工具探测 → 三级降级（port-forward / kill -3 信号法 / 跳过） |
| 主机版核心 | 进程定位三通道 + `/proc/PID/exe` 反推 JDK + `top -H` 线程级 CPU + nid 映射 |
| 产出 | 指标 / jstack×N / 聚合分析 / SUMMARY.txt 四类证据 |
| 陷阱 | namespace 靠 context 推断可能采错环境；采样是采样不是 trace；jstack 版本必须匹配 |

**一句记忆钩子**：*"容器版问进程，主机版问机器；采不到就降级，采到假数据不如不采。"*

---

## 第二讲：输出文件字典 —— 每个文件看什么、信号指向什么

> **本讲问题**：`SUMMARY.txt` 之外的细节文件怎么读？每类信号对应哪类根因？

### 2.1 指标类文件

| 文件 | 内容 | 看什么 |
|---|---|---|
| `metrics.txt` | actuator/prometheus 全量指标（原始） | 其他指标文件的母本，别直接读 |
| `metrics-slow-api.txt` | `http_server_requests_seconds` 的 quantile 与 max 按值排序 Top30（`perf-snapshot.sh:185-188`） | **P99/Max 排行**定位慢接口；对照 `_count` 看调用量，低QPS 接口的 P99 参考价值有限 |
| `metrics-resource.txt` | 线程池/GC/CPU/熔断/连接池指标族（`perf-snapshot.sh:191-192`） | `tomcat_threads`（busy vs max）、`jvm_gc_pause_seconds_max`、`process_cpu_usage`、`system_load_average`、`resilience4j`（熔断器状态）、`hikaricp`（连接池 pending/active）、`jvm_memory_used_bytes`、`logback`（日志事件堆积） |

`http_server_requests_seconds` 这个指标名不是随手起的：Spring Boot 的 Observation 默认名就是 `http.server.requests`（`ObservationProperties.java:122`），Micrometer 转 Prometheus 格式时把点换成下划线、Timer 加 `_seconds` 后缀——所以排查时看到 `http_server_requests_seconds_count` 是调用量、`_sum` 是总耗时、`quantile="0.99"` 是 P99。

### 2.2 线程栈类文件

| 文件 | 内容 | 看什么 |
|---|---|---|
| `jstack.txt` | N 次完整线程栈（每次带时间戳头） | 死锁证据（`Found one Java-level deadlock`）、某个具体线程的完整调用链 |
| `analysis-thread-state.txt` | `java.lang.Thread.State` 分布统计（`perf-snapshot.sh:196-198`） | **第一眼文件**：BLOCKED 多 = 锁竞争；TIMED_WAITING 扎堆在某处 = 在 sleep/超时等待 |
| `analysis-hot-frames.txt` | 去掉行号后按"类名+方法"聚合的 Top30，已过滤 JDK 前缀（`perf-snapshot.sh:201-204`） | **第二眼文件**：反复出现的业务帧就是热点；命中次数 ÷ 采样次数 ≈ 该帧驻留时间占比 |
| `analysis-blocked.txt` | 阻塞/等待外部资源的线程命中（`BLOCKED`/`waiting to lock`/`socketRead0`/`druid`/`jedis`/`okhttp`/`feign`/`resilience4j`/`Locked ownable`） | 阻塞**归因**：等锁还是等外部资源，等的是哪类外部资源 |
| `analysis-cpu-hot-stacks.txt` | 仅主机版：高 CPU 线程（nid）对应的 Java 栈（`perf-snapshot-host.sh:212-223`） | **CPU 问题第一现场**：烧 CPU 的线程到底在执行什么 |
| `top-hot-threads.txt` | 仅主机版：CPU>10% 线程的 tid → nid 换算表（`perf-snapshot-host.sh:138-141`） | 从"哪个 OS 线程烧 CPU"到"哪段 Java 代码"的桥梁 |

### 2.3 整机与 GC 类文件（仅主机版）

| 文件 | 内容 | 看什么 |
|---|---|---|
| `host-resources.txt` | uptime/nproc/free/vmstat/iostat/df/ss/进程 ps（`perf-snapshot-host.sh:106-127`） | `vmstat` 的 `r` vs 核数（CPU 饱和）、`wa`（IO 等待）、`bi/bo`（磁盘读写）、`ss` 的 ESTAB/CLOSE_WAIT 堆积（连接泄漏）、进程 RSS 与线程数 |
| `jstat-gcutil.txt` | `jstat -gcutil` 序列 | `O` 列（老年代占比）逼近 100 = 内存压力；`FGC` 增速 = Full GC 频率；`YGC` 暴涨 = 分配速率异常 |

### 2.4 SUMMARY.txt：脚本已经帮你做好了归因

`SUMMARY.txt`（`perf-snapshot.sh:221-254`、`perf-snapshot-host.sh:229-261`）不是简单拼接，它把最关键的判断预先算好了：

```
1. 线程状态分布                     → 全局健康度
2. 热点栈帧 Top10                   → 代码级热点
3. 阻塞计数及分类（仅容器版/主机版都有）：
   BLOCKED            : N
   waiting to lock    : N
   socketRead0        : N
     ├─ druid/DB      : N      ← 在等数据库
     ├─ jedis/Redis   : N      ← 在等 Redis
     └─ okhttp/Feign  : N      ← 在等下游 HTTP
4. 关键资源指标                     → tomcat 线程数 / CPU / load
5. GC 停顿                          → jvm_gc_pause_seconds_max/sum
6. 慢接口 P99 Top10                 → 定量证据
```

第三块是精华：`socketRead0` 的命中数被按 `druid/jedis/okhttp` 拆开统计（`perf-snapshot.sh:238-241`），你不需要自己 grep 就能回答"到底是谁在拖慢接口"。

### 2.5 两个脚本的官方判读顺序

脚本作者把判读顺序写进了最后两行日志，这不是随口说的：

- 容器版（`perf-snapshot.sh:259`）：**线程状态分布 → 热点栈帧 → 慢接口指标**。先看"卡住的线程长什么样"，再看"栈里反复出现什么"，最后用指标验证"确实慢且慢在这个接口"。
- 主机版（`perf-snapshot-host.sh:266`）：**高 CPU 线程 → analysis-cpu-hot-stacks → 是否在等外部资源**。CPU 问题优先看谁在烧；如果不是 CPU 问题，转去判断阻塞归因。

顺序背后的逻辑：**先二分问题域（CPU 型 vs 等待型），再深入具体帧，最后用指标闭环验证**。跳过前两步直接看指标，很容易把"下游慢"误判成"自己慢"。

### 2.6 本讲小结

| 维度 | 结论 |
|---|---|
| 读文件顺序 | SUMMARY.txt 是入口；细节按"状态分布 → 热点帧 → 指标对照"展开 |
| 指标三件套 | slow-api 定位"哪个接口慢"，resource 定位"哪种资源紧"，gc 定位"是不是 GC 问题" |
| 栈三件套 | thread-state 看全局，hot-frames 看热点，blocked/cpu-hot-stacks 看归因 |
| 归因已预置 | SUMMARY 已把 socketRead0 按 druid/jedis/okhttp 分类，阻塞归因不用手 grep |
| 陷阱 | 热点帧排行滤掉了 JDK 帧，但锁/IO 阻塞的现场恰恰在 JDK 帧里，要回原栈看 |

**一句记忆钩子**：*"SUMMARY 先定性，分布看全局，热点帧定位，指标来闭环；等 DB 还是等 Redis，脚本已经数好了。"*

---

## 第三讲：五个经典剧本 —— 从现象到根因的完整路径

> **本讲问题**：CPU 飙高、接口变慢、死锁、OOM、线程池打满，各自怎么采、怎么读、怎么修？

### 3.1 剧本 A：CPU 飙高（报警 process_cpu_usage > 80%）

**采集**：主机版优先（线程级 CPU 是核心证据），容器版备用。

```bash
bash scripts/perf-snapshot-host.sh <pid> 18006 10 1
```

**判读路径**：

1. `SUMMARY.txt` 第 5 节高 CPU 线程列表 → 拿到 tid/nid；
2. `analysis-cpu-hot-stacks.txt` → 烧 CPU 的 Java 栈顶是什么；
3. 对照 `jstat-gcutil.txt`：如果烧 CPU 的线程名是 `GC task thread`/`G1 Main` 之类，问题不是业务代码而是 **GC 风暴**——看 YGC 增速（分配速率异常：大对象/频繁 new）或 FGC 增速（老年代涨：内存泄漏/缓存过大）；
4. 如果栈顶是业务代码：经典嫌疑人排行——**复杂正则回溯**（`Pattern.matcher` 嵌套量化）、**大对象序列化**（JSON/Protobuf 拷来拷去）、**死循环**（边界条件错）、**日志渲染**（字符串拼接打 INFO）。

**修复方向**：算法优化（正则改确定性自动机/限制回溯）；缓存中间结果；降级开关（CPU 高时关闭非核心计算）；GC 问题转内存剧本。

**易错点**：容器里 CPU 报警可能不是自己的进程——用主机版 `top` 确认进程级 CPU（`ps -o pcpu`，`perf-snapshot-host.sh:126`）再采栈，避免给别人的问题打工。

### 3.2 剧本 B：接口 RT 抬升 / P99 劣化（CPU 可能并不高）

**采集**：容器版为主（要慢接口排行）。

```bash
NS=<ns> bash scripts/perf-snapshot.sh <pod> <container> 18006 10 1
```

**判读路径**：

1. `metrics-slow-api.txt` → 哪些接口 P99/Max 高，结合 `_count` 排除低 QPS 噪声；
2. `analysis-thread-state.txt` → 线程卡在哪一类状态；
3. `SUMMARY.txt` 第 3 节 → `socketRead0` 命中及分类：
   - `druid` 多 → **数据库慢**：去 Druid 监控页（`/druid/sql.html`）看慢 SQL；典型根因是缺索引、深分页、大事务、行锁等待（后者在栈里表现为 `druid` 帧 + BLOCKED）；
   - `jedis` 多 → **Redis 慢**：大 key、`KEYS`/`SMEMBERS` 大集合、热 key 排队；Redis 单线程模型下一个慢命令堵全部；
   - `okhttp/feign` 多 → **下游服务慢**：看下游的 CPU/RT，必要时熔断（`resilience4j` 指标族能看到熔断器状态迁移）；
4. 如果三类都不高、`BLOCKED` 高 → 转剧本 C（锁竞争）；如果都不高但 RT 高 → 看 `analysis-hot-frames.txt` 找自家代码里的计算热点。

**修复方向**：SQL 加索引/改写；连接池参数调优（`hikaricp` 指标看 pending 是否堆积）；下游加超时+熔断+降级；自家热代码优化。

**关键认知**：**RT 高 ≠ 自己有问题**。本剧本一半以上的案子根因在下游，而"下游慢"的证据就是 `socketRead0` 帧——这正是脚本把三类外部依赖分开计数的原因。

### 3.3 剧本 C：死锁 / 锁竞争（接口偶发超时、线程数持续上涨）

**采集**：两个脚本都行；死锁是瞬时状态，**多次采样提高命中率**（建议采样次数调到 20+）。

```bash
bash scripts/perf-snapshot.sh <pod> "" 18006 20 1
```

**判读路径**：

1. 直接搜 `jstack.txt` 里的 `Found one Java-level deadlock`（注意：容器版 `analysis-blocked.txt` 的 grep 模式不含 deadlock 关键词，`perf-snapshot.sh:207-208` vs 主机版 `:225-226` 含）；
2. 没有死锁但 `waiting to lock` 命中高 → 锁竞争。区分两类：
   - 栈里 `- waiting to lock <0x..> (a java.lang.Object)` 且状态 `BLOCKED` → `synchronized` 监视器；
   - 栈里 `- parking to wait for <0x..> (a java.util.concurrent.locks.ReentrantLock$...)` 且状态 `WAITING` → JUC 锁；
3. `analysis-blocked.txt` 里找持锁线程（`- locked <0x..>`）在干什么——**持锁者不放手的原因才是根因**（常见：持锁做 IO、持锁调下游、嵌套加锁顺序不一致）；
4. 修复：缩小临界区（IO 移出锁）、统一锁顺序（防死锁）、换 `ReentrantLock.tryLock(timeout)`、无锁结构（`ConcurrentHashMap` 分段思维）。

**持锁者视角**：jstack 里线程的 `Locked ownable synchronizers` 段列出它持有的 AQS 锁，是判断"谁捂着锁"的关键信息（脚本把它列进了 grep 模式，`perf-snapshot.sh:207`）。

### 3.4 剧本 D：OOM / Full GC 风暴（Pod 反复重启、进程 RSS 持续上涨）

**采集**：主机版（有 jstat 实时曲线）。

```bash
bash scripts/perf-snapshot-host.sh <pid> 18006 30 2
```

**判读路径**：

1. `jstat-gcutil.txt`：`O` 列（老年代使用率）是否贴着 100%；`FGC` 列增速——每几次采样 +1 = Full GC 风暴；`GCT`（总停顿）累计速度；
2. `SUMMARY.txt` 的 GC 停顿（容器版看 `jvm_gc_pause_seconds_max/sum`，`perf-snapshot.sh:249`）：单次停顿几百 ms 起，RT 会同步劣化；
3. `host-resources.txt`：进程 RSS 趋势（多次采样对比）、`si/so`（swap 使用，swap 一开 GC 停顿放大十倍）；
4. 根因三分法：**内存泄漏**（RSS 只涨不降，FGC 后 O 不回落——典型嫌疑人：静态 Map 缓存无上限、ThreadLocal 未 remove、监听器未注销）、**分配过快**（YGC 暴涨但 O 稳——大对象/频繁临时对象）、**堆太小**（以上都排除，才是容量问题）。

**事中与事后**：事中（FGC 频繁但还没 OOM）是采栈的黄金窗口；已经 OOM 的进程采不到任何东西——所以生产必须配 `-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/logs/dump.hprof`，事后用 MAT 分析 dump。脚本在采不到栈时会明确告诉你"进程可能已死"（`perf-snapshot-host.sh:166-171`）。

### 3.5 剧本 E：线程池打满（雪崩前兆：RT 整体抬升、新请求拒绝）

**采集**：容器版（tomcat 指标 + 线程状态分布）。

**判读路径**：

1. `SUMMARY.txt` 第 4 节：`tomcat_threads_busy_threads` vs `tomcat_threads_config_max_threads`——busy 贴 max 且不降 = 打满；这些指标由 Spring Boot 在应用启动时把 Tomcat 的 Manager 注册进 MeterRegistry（`TomcatMetricsBinder.java:64-65`）；
2. `analysis-thread-state.txt`：如果线程总数接近 max 且大量处于 `socketRead0`/`WAITING`，说明**线程都堵在等下游**——线程池不是不够大，是被慢下游占满；
3. 根因通常是**下游变慢引发的级联占满**：先按剧本 B 定位下游；同时检查有没有加超时（没超时的 HTTP 调用会永久占线程）、有没有熔断（`resilience4j` 指标看熔断状态，Sentinel 同理）；
4. 修复三板斧：**超时**（所有外部调用必须有）、**隔离**（不同下游用独立线程池，防一处慢拖垮全局）、**熔断降级**（下游持续失败时快速失败，把线程还给线程池）。

**关联机制**：`ThreadPoolExecutor.execute()`（`ThreadPoolExecutor.java:1292`）在队列满且线程数达 max 时走拒绝策略——业务层看到的就是拒绝异常；而**线程被慢下游长期占用**会让队列越排越长，这就是雪崩的标准路径：下游慢 → 线程占满 → 队列堆积 → 上游超时 → 故障扩散。

### 3.6 本讲小结

| 剧本 | 首选脚本 | 第一证据 | 经典根因 |
|---|---|---|---|
| A CPU 飙高 | 主机版 | 高 CPU 线程栈 | 正则回溯/大对象序列化/GC 风暴 |
| B 接口变慢 | 容器版 | socketRead0 分类 | DB 慢/Redis 慢/下游慢 |
| C 死锁/锁竞争 | 均可（多次采样） | waiting to lock + 持锁者栈 | 持锁做 IO/锁顺序不一致 |
| D OOM/GC | 主机版 | jstat O+FGC | 泄漏/分配过快/堆小 |
| E 线程池打满 | 容器版 | busy vs max + 状态分布 | 下游慢级联占满 |

**一句记忆钩子**：*"CPU 看谁在烧，RT 看在等谁，死锁看谁捂着锁，OOM 看 O 和 FGC，打满先找下游。"*

---

## 第四讲：信号 → 根因速查表与源码机制

> **本讲问题**：看到每个信号该想到什么？这些信号为什么长这样？

### 4.1 信号速查总表

| 信号（在哪看到） | 最可能根因 | 下一步动作 |
|---|---|---|
| `process_cpu_usage` 高 + `system_load_average` 高（metrics-resource） | 计算型热点或 GC 风暴 | 主机版采高 CPU 线程栈；对照 jstat |
| `process_cpu_usage` 高 + load 不高 | 单线程死循环/热点计算 | analysis-cpu-hot-stacks 直接定位 |
| load 高 + 进程 CPU 不高（host-resources） | 邻居进程抢占 / run queue 积压 | `vmstat` 的 `r`、`top` 全局视图 |
| `wa` 高 + `%util` 高（vmstat/iostat） | 磁盘 IO 瓶颈 | 查谁在写盘（日志刷盘、大文件、swap） |
| `si/so` 非零（vmstat） | 开了 swap，GC 停顿会被放大 | 关闭 swap 或加内存 |
| `O` 接近 100 + FGC 稳增（jstat-gcutil） | 内存泄漏或老年代过大 | heapdump + MAT；查静态缓存 |
| YGC 暴涨 + O 平稳（jstat-gcutil） | 分配速率异常 | 查大对象、临时对象、数组拷贝 |
| `jvm_gc_pause_seconds_max` 大（metrics-resource） | 长停顿直接打花 RT | 与慢接口时间线对齐验证 |
| `tomcat_threads_busy` ≈ `max`（metrics-resource） | 线程池打满 | 状态分布看线程堵在哪；剧本 E |
| `BLOCKED` 占比高（analysis-thread-state） | synchronized 监视器竞争 | analysis-blocked 找持锁者 |
| `waiting to lock` 命中多（analysis-blocked） | 同上，看持锁者在做什么 | 缩小临界区/锁顺序 |
| `parking to wait for` 命中多（analysis-blocked） | ReentrantLock/JUC 竞争 | 检查锁粒度与公平性需求 |
| `socketRead0` 多且 druid 命中多（SUMMARY 第 3 节） | 数据库慢 | Druid 慢 SQL 页；查索引/大事务/行锁 |
| `socketRead0` 多且 jedis 命中多 | Redis 慢/大 key | Redis 慢日志；拆大 key |
| `socketRead0` 多且 okhttp 命中多 | 下游 HTTP 慢 | 查下游 RT；加超时熔断 |
| `TIMED_WAITING` 扎堆在 sleep/parkNanos | 主动等待（重试间隔、超时逻辑） | 结合业务帧判断是否合理退避 |
| `Found one Java-level deadlock`（jstack.txt） | 循环等锁 | 按提示的线程对修复锁顺序 |
| `CLOSE_WAIT` 堆积（ss） | 连接泄漏（对端关了本地没关） | 查连接池/HTTP 客户端的 close 路径 |
| `ESTAB` 到本端口数过高（ss） | 并发连接超预期/连接泄漏 | 对比历史基线 |
| `resilience4j` 状态 OPEN（metrics-resource） | 熔断已触发，请求在快速失败 | 查下游健康；恢复期关注 half-open |
| `hikaricp_connections_pending` 高（metrics-resource） | 连接池耗尽，线程在等连接 | 加大池/查慢 SQL 占连接不还 |
| `logback_events` 堆积（metrics-resource） | 日志阻塞（同步 appender 写盘慢） | 改异步 appender；查磁盘 |
| 热点帧反复出现同一业务方法（analysis-hot-frames） | 该方法是真热点 | 命中次数÷采样次数 ≈ 驻留占比 |

### 4.2 源码机制一：Thread.State 六态，每个都是排查语言

jstack 的线程状态来自 `java.lang.Thread.State` 枚举（`java/lang/Thread.java:2331`），六态与触发条件：

| 状态 | 行号 | 触发条件（源码注释归纳） | 排查含义 |
|---|---|---|---|
| `NEW` | `:2335` | 线程创建未 start | 通常无排查价值 |
| `RUNNABLE` | `:2343` | JVM 中执行中——**注释明说"may be waiting for other resources from the operating system such as processor"** | ⚠️ 不等于在烧 CPU：阻塞在 native IO 的线程也是 RUNNABLE |
| `BLOCKED` | `:2352` | 等待监视器锁（`synchronized`） | 锁竞争现场 |
| `WAITING` | `:2373` | `Object.wait()` 无超时 / `Thread.join()` 无超时 / `LockSupport.park()` | 在等某个事件或锁许可 |
| `TIMED_WAITING` | `:2387` | `sleep` / `wait(long)` / `join(long)` / `parkNanos` / `parkUntil` | 有超时的等待：重试退避、池获取超时 |
| `TERMINATED` | `:2393` | 已结束 | 无价值 |

**RUNNABLE 的陷阱是本表最重要的一行**。线程执行 `read(2)` 阻塞在内核时，Java 线程状态仍是 RUNNABLE——所以"RUNNABLE 线程多"既可能是计算热点，也可能是全员在等网络/磁盘。区分方法就是主机版的 `top -H`：RUNNABLE 但 OS 级 CPU 趋近 0，就是在 native 里睡着。这也是为什么容器版判读顺序把"热点帧"放在"状态分布"之后——**帧比状态更接近真相**。

### 4.3 源码机制二：socketRead0 是什么，现代 JDK 上它长什么样

老栈里阻塞的 socket 读顶帧是 `java.net.SocketInputStream.read` → native `socketRead0`。在 JDK 25 的源码里，`java.net.Socket` 的输入流早已换成 NIO 实现：`sun.nio.ch.SocketInputStream.read(byte[],off,len)`（`sun/nio/ch/SocketInputStream.java:75`）→ `SocketChannelImpl.blockingRead`（`sun/nio/ch/SocketChannelImpl.java:1373`）→ 无超时路径走 `tryRead` 拿不到就绪数据时 `park(Net.POLLIN)` 再重试，数据最终由 native `read(2)` 返回。

**对排查的直接影响**：脚本 grep 的 `socketRead0` 是老帧名。在现代 JDK 上，同样的"在等网络"现场，栈顶更可能是 `SocketChannelImpl.blockingRead` 或 `SocketInputStream.read`。脚本作者显然考虑到了这点——容器版的 grep 模式里 `socketRead0` 和 `SocketInputStream` 是**并列**的两个候选（`perf-snapshot.sh:207`），两个帧名总有一个能命中。如果都没命中但 SUMMARY 里三类外部依赖数字都很大，回 `jstack.txt` 直接搜 `blockingRead`。

### 4.4 源码机制三：waiting to lock 与 parking，两种锁两种栈

`- waiting to lock <0x...>` 出现在 `BLOCKED` 线程栈顶，对应 `synchronized` 的监视器竞争：线程在 `ObjectMonitor` 的 entry list 上排队，JVM 把这种状态直接标为 BLOCKED（`Thread.java:2352`）。

`- parking to wait for <0x...> (a java.util.concurrent.locks.ReentrantLock$NonfairSync)` 出现在 `WAITING` 线程栈顶，对应 JUC 锁：`ReentrantLock.lock()`（`java/util/concurrent/locks/ReentrantLock.java:322`）→ AQS `acquire` → `LockSupport.park()`（`java/util/concurrent/locks/LockSupport.java:365`）把自己挂起。

**为什么排查时要区分**：监视器竞争没有超时机制（`synchronized` 进不去就死等），而 AQS 锁有 `tryLock`/`lockInterruptibly` 可选语义；两者的修复手段不同（前者缩小临界区，后者还可以改锁策略与公平性）。`ReentrantLock` 的公平/非公平差异就写在 `tryAcquire` 的两个实现里（`ReentrantLock.java:242` 非公平 / `:280` 公平——公平版排队前会先问一声"前面有人排吗"）。

### 4.5 源码机制四：nid 怎么和 jstack 对上，kill -3 为什么能打栈

**tid → nid**：`top -H` 输出里线程 id 是十进制，jstack 线程头里的 `nid` 是十六进制，脚本用 `printf '%x'` 换算（`perf-snapshot-host.sh:140`、`:216`）。对应关系是 JVM 内部约定：`nid` 就是 OS 线程的 LWP id 的十六进制形式。对上之后，`grep -A25 "nid=0x..."` 就能把"烧 CPU 的 OS 线程"翻译成"执行到哪行 Java 代码"。

**kill -3**：`SIGQUIT` 是 JVM 预留的信号——信号处理函数会把所有线程的栈写到**进程 stdout**，然后继续运行（不停进程、不丢业务）。容器里 stdout 就是容器日志，所以 `kubectl logs --tail=1500` 能捞回来（`perf-snapshot.sh:156-173`）。这也是"信号法采的栈混着业务日志"的原因：两者写的是同一个流。

### 4.6 源码机制五：指标是从哪来的

排查时看到的每个指标名都能追到注册点，追不上就会在配置里抓瞎：

- `http_server_requests_seconds`：Spring Boot Observation 默认名 `http.server.requests`（`ObservationProperties.java:122`）经 Micrometer 转写为 Prometheus 命名；
- `/actuator/prometheus`：actuator 的 Web 端点，id 就是 `prometheus`（`PrometheusScrapeEndpoint.java:45`），拉取时执行 `scrape()`（`:70-71`）把 MeterRegistry 序列化成 Prometheus 文本格式；
- 端点 404：actuator 没暴露它。`management.endpoints.web.exposure.include` 控制暴露集合，对应 `MetricsProperties` 的 `include` 字段（`MetricsProperties.java:87-89`），脚本在探测失败时给出的开启建议正是这个配置（`perf-snapshot.sh:134-135`）；
- `tomcat_threads_*`：Spring Boot 在应用启动事件里把 Tomcat 的 Manager 接入 MeterRegistry（`TomcatMetricsBinder.java:64-65`），所以容器里没装 micrometer/actuator 的启动包，这类指标一个都没有——**没有指标不是没故障，是没装眼睛**。

### 4.7 本讲小结

| 维度 | 结论 |
|---|---|
| 判据顺序 | 先二分（CPU 型 vs 等待型）→ 再定位帧 → 最后指标闭环 |
| 六态语义 | RUNNABLE 含 native 阻塞；BLOCKED=监视器；WAITING=park/wait；TIMED_WAITING=带超时等待 |
| 阻塞归因 | waiting to lock→synchronized；parking→JUC；socketRead0/blockingRead→等网络 |
| 证据链 | tid→nid 打通 OS 与 Java；kill -3 借 stdout 打栈；指标名可追到注册点 |
| 陷阱 | 现代 JDK 帧名变化（socketRead0→blockingRead）；热点帧滤掉了 JDK 帧但现场在 JDK 帧里 |

**一句记忆钩子**：*"RUNNABLE 会在 native 里睡着，BLOCKED 等的是监视器，parking 等的是 AQS；tid 十六进制一换，OS 线程就现形。"*

---

## 第五讲：排查纪律 —— 现场保护的 Do / Don't

> **本讲问题**：同样的工具，为什么有人十分钟定位、有人三天定位？

### 5.1 Do

1. **先快照，再操作**：看到报警第一件事跑采集脚本（只读、几十秒），拿到 `perf-<时间戳>/` 目录后再考虑重启/扩容/回滚。
2. **趁病中采集**：问题正在发生时采；恢复后同样的栈采不到第二遍。
3. **多次采样**：jstack 是采样不是 trace，间隔 1 秒会漏短任务；死锁等瞬时状态更要 20+ 次采样。
4. **保留目录**：`perf-<时间戳>/` 按时间归档，事后复盘、容量规划、对比基线都靠它。
5. **对照时间线**：把快照时间与发布记录、配置变更、流量波动对齐，一半的问题在"什么时候开始的"这一步就答案自现。
6. **用对脚本**：K8s 里用容器版，机器上用主机版；要回答"是不是机器的问题"必须主机版。

### 5.2 Don't

1. **不要重启了再采**：重启是最后手段，不是第一响应。
2. **不要只看一个指标下结论**：P99 高要看调用量；CPU 高要看是不是 GC；load 高要看是不是邻居。
3. **不要用错版本的 JDK 工具**：attach 版本不匹配会采出残缺栈（脚本用 `/proc/PID/exe` 反推 JDK 就是在防这个，`perf-snapshot-host.sh:75-77`）。
4. **不要盲信热点帧排行**：它过滤了 JDK 前缀帧，而锁/IO 现场恰好在那些帧里；判阻塞必须回原栈。
5. **不要在容器里搜 deadlock 于 analysis-blocked.txt**：容器版没把这个词纳入 grep 模式（`perf-snapshot.sh:207-208`），要搜原始 `jstack.txt`。
6. **不要采完不读**：SUMMARY.txt 已经做好归因，不读它等于白采。

### 5.3 一套标准的排查动作（SOP）

```
1. 确认现象与影响面（哪个接口/哪些用户/什么时间开始）
2. 看历史曲线定基线（是一直高还是突变）
3. 跑采集脚本（容器版/主机版按环境选），保留 perf-* 目录
4. 读 SUMMARY.txt，按判读顺序过三个文件
5. 二分：CPU 型 → 高 CPU 线程栈；等待型 → 阻塞归因（DB/Redis/下游/锁）
6. 定位到具体代码/配置/SQL，验证（改配置观察/复现/explain）
7. 修复 + 回归 + 补监控（让同类问题下次直接报警而非人工发现）
```

**一句记忆钩子**：*"先快照再重启，趁病中多次采；指标定性、栈定位、时间线定责。"*

---

## 附：命令速查卡

### 容器版

```bash
# 标准采集（显式指定 namespace，防采错环境）
NS=<ns> bash scripts/perf-snapshot.sh <pod> <container> 18006 10 1

# 多容器 pod 必须传容器名；端口按服务实际改
# 无 curl 的容器：脚本会打印 port-forward 命令，本地另开终端执行
kubectl port-forward -n <ns> pod/<pod> 18006:18006

# 无 jstack 的容器：信号法自动启用；或手工验证
kubectl exec -n <ns> <pod> -c <container> -- kill -3 <pid>
kubectl logs -n <ns> <pod> -c <container> --tail=1500 | less
```

### 主机版

```bash
# 三种定位方式
bash scripts/perf-snapshot-host.sh 12345 18006 10 1     # PID
bash scripts/perf-snapshot-host.sh card-bizcard 18006   # 关键字
bash scripts/perf-snapshot-host.sh "" 18006 30 2        # 按端口，加大采样

# 手工三板斧（脚本自动做完，这里是对照原理）
top -b -n1 -H -p <pid>                  # 线程级 CPU
printf '%x\n' <tid>                     # tid → nid
jstack <pid> | grep -A25 "nid=0x..."    # 找对应 Java 栈
jstat -gcutil <pid> 1000 10             # GC 曲线
```

### 判读入口

```bash
cd perf-<时间戳>       # 或 perfhost-<pid>-<时间戳>
cat SUMMARY.txt        # 先读它：归因已预置
head analysis-thread-state.txt    # 全局健康度
head analysis-hot-frames.txt      # 代码热点
grep -c BLOCKED jstack.txt         # 锁竞争规模
grep "Found one Java-level deadlock" jstack.txt   # 死锁实锤
head analysis-cpu-hot-stacks.txt  # 仅主机版：CPU 第一现场
```

---

## 涉及文件索引

| 文件 | 行数 | 本笔记引用次数 |
|---|---|---|
| `scripts/perf-snapshot.sh` | 259 | 高（容器版采集逻辑全量） |
| `scripts/perf-snapshot-host.sh` | 267 | 高（主机版采集逻辑全量） |
| `java/lang/Thread.java` | 2800+ | 高（State 六态 `:2331-2393`） |
| `java/lang/Object.java` | 900+ | 低（wait 合同） |
| `java/util/concurrent/locks/LockSupport.java` | 500+ | 中（park 家族） |
| `java/util/concurrent/locks/ReentrantLock.java` | 700+ | 中（lock/tryAcquire 公平与非公平） |
| `java/util/concurrent/ThreadPoolExecutor.java` | 1400+ | 中（execute 拒绝路径） |
| `sun/nio/ch/SocketInputStream.java` | 95 | 中（现代 JDK 的 socket 读栈顶） |
| `sun/nio/ch/SocketChannelImpl.java` | 1613 | 中（blockingRead 阻塞循环） |
| `module/spring-boot-micrometer-metrics/src/main/java/org/springframework/boot/micrometer/metrics/autoconfigure/MetricsProperties.java` | 200+ | 中（exposure.include） |
| `module/spring-boot-micrometer-metrics/src/main/java/org/springframework/boot/micrometer/metrics/autoconfigure/export/prometheus/PrometheusScrapeEndpoint.java` | 86 | 中（prometheus 端点） |
| `module/spring-boot-micrometer-observation/src/main/java/org/springframework/boot/micrometer/observation/autoconfigure/ObservationProperties.java` | 150+ | 低（http.server.requests 默认名） |
| `module/spring-boot-tomcat/src/main/java/org/springframework/boot/tomcat/metrics/TomcatMetricsBinder.java` | 99 | 中（tomcat 线程指标注册） |
