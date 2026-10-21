---
title: "操作系统源码深度学习笔记"
date: 2026-10-21
categories:
  - "操作系统"
tags:
  - "进程"
  - "线程"
  - "内存管理"
  - "IO"
  - "源码"
---

> 基于两条真实源码线的精读整理而成：
> ① **HotSpot 虚拟机**（`src/hotspot/share/runtime/`：`javaThread.cpp`、`thread.cpp`、`os.cpp`、`osThread.hpp`、`continuationFreezeThaw.cpp`）——JVM 是"离操作系统最近"的 Java 程序，它对线程的每一次创建、挂起、恢复都直接踩在 OS 系统调用上；
> ② **JDK 类库里最贴近内核的那几个类**（`java.base/unix/classes/java/lang/ProcessImpl.java`、`java/lang/ProcessHandleImpl.java`、`sun/nio/ch/FileChannelImpl.java`、`FileDispatcher.java`、`linux/classes/sun/nio/ch/EPollSelectorImpl.java`、`java/lang/Thread.java`、`VirtualThread.java`）。
>
> 阅读顺序即学习顺序：**进程与线程 → 上下文切换与调度 → 内存管理 → IO 与零拷贝 → 同步互斥与死锁**。
>
> 所有结论均标注了源文件位置（格式 `src/hotspot/share/runtime/xxx.cpp:行号` 或 `src/java.base/.../xxx.java:行号`），建议对照代码阅读。
>
> **版本基线**：JDK **21（jdk-21-ga）** commit `890adb64`（HotSpot 与 `java.lang` 部分）；`java.io`/`java.net`/`sun.nio.ch` 与 Unix 平台类取自 OpenJDK 主线 commit `6c48f4ed`（2025-08-12）。
>
> 说明：Linux 内核自身（`fs/`、`mm/`、`kernel/`）不在本仓源码范围内，凡是内核内部机制均为规范描述；凡能落到 JVM/JDK 实现上的，都挂了真实行号——**本文的方法是用"JVM 怎么调 OS"来反证 OS 概念**。并发与锁的 Java 侧细节见 `java-concurrent.md`、`java-lock.md`，IO 多路复用见 `java-io.md`。

---

## 〇、全景地图

```
┌────────────────────────── 操作系统四大件 ──────────────────────────┐
│                                                                    │
│  ① 进程/线程管理          ② 内存管理                               │
│     fork+exec 建进程         虚拟地址空间 → 页表 → 物理页           │
│     pthread_create 建线程     页大小 vm_page_size（os.cpp:1488）     │
│     JavaThread 创建链路        TLB / 缺页中断 / 页面置换             │
│     VirtualThread（用户态调度）                                      │
│                                                                    │
│  ③ 文件系统与 IO           ④ 同步互斥                              │
│     系统调用 read/write        互斥锁 / 条件变量 / park-unpark       │
│     PageCache + mmap           死锁四条件与四种对策                  │
│     sendfile 零拷贝（FileChannelImpl:934）                          │
│     epoll 多路复用（EPollSelectorImpl:94）                          │
└────────────────────────────────────────────────────────────────────┘
```

一句话主线：**OS 做两件事——"抽象"（把 CPU 抽象成进程/线程、把内存抽象成地址空间、把设备抽象成文件）和"仲裁"（谁先用、用多少、出事了怎么收场）**。面试里所有 OS 题都能归到这两件事上。

---

## 第一讲 进程与线程

### 1.1 进程是怎么"生"出来的

Linux 的经典模型是 **fork + exec**：

- `fork()`：复制出一个几乎一模一样的子进程（写时复制 COW，不是真拷内存）
- `exec()`：把子进程的代码段换成新程序

JDK 里创建进程就是这条路的直接映射：

```java
private native int forkAndExec(int mode, byte[] helperpath, ...)   // src/java.base/unix/classes/java/lang/ProcessImpl.java:282
pid = forkAndExec(launchMechanism.ordinal() + 1, ...);              // :300  拿到子进程 pid
processHandle = ProcessHandleImpl.getInternal(pid);                 // :308
```

有意思的是 JDK 还有第三种更快的机制（`launchMechanism`）：`fork` 之外还能走 **`posix_spawn`** 或 **`vfork`**，因为"先复制再 exec"在超大堆的 JVM 上代价极高——这是"Java 里 `Runtime.exec()` 为什么慢"的标准解释。

子进程结束后的收尸：

```java
ProcessHandleImpl.completion(pid, true).handle((exitcode, throwable) -> ...)  // :342 / :378
```

- 父进程不 `wait()` 回收，子进程就变**僵尸进程**（PCB 还占着，pid 不释放）
- 父进程先死，子进程被 init/systemd 收养，成**孤儿进程**（无害）

进程的三要素：**PCB（进程控制块）+ 地址空间 + 打开的文件描述符表**。

### 1.2 线程：共享什么、私有什么

| 共享（进程级） | 私有（线程级） |
|---|---|
| 代码段、堆、全局变量 | 栈、寄存器上下文（PC/SP） |
| 打开的文件描述符、信号处理 | 线程局部存储（ThreadLocal） |
| 进程凭证、地址空间 | 调度状态、errno |

**为什么线程切换比进程切换快**：线程切换不换页表（不换 CR3），因此 **TLB 不失效**；进程切换要换地址空间，TLB 全刷，代价高一个量级。

### 1.3 JVM 创建一条 Java 线程的完整链路

```
Thread.start()                       ← src/java.base/share/classes/java/lang/Thread.java
   └─ JVM_StartThread (native)
        └─ JavaThread 构造：
             pd_initialize()                       // src/hotspot/share/runtime/javaThread.cpp:513
             os::create_thread(this, thr_type, stack_sz)  // :605  真正 pthread_create
        └─ Thread::start(thread)                   // src/hotspot/share/runtime/thread.cpp:371
             os::start_thread(thread)              // :382  让它跑起来
        └─ JavaThread::run()                       // javaThread.cpp:666  初始化 TLAB、栈保护页
        └─ 结束：set_thread_status(TERMINATED)      // javaThread.cpp:742
```

三个可直接引用的结论：

1. **Java 线程 = 内核线程（1:1 模型）**：`os::create_thread`（`javaThread.cpp:605`）最终落到 `pthread_create`，所以 `new Thread()` 的代价 ≈ 一次系统调用 + 内核栈/线程结构体分配，**默认栈 1MB 级**（`-Xss` 可调）
2. **创建失败会 OOM**：源码注释明确写了"可能因活跃线程太多导致 `_osthread` 为 null，抛 OutOfMemoryError"（`javaThread.cpp:607` 附近）——这就是"unable to create new native thread"的出处
3. **线程栈大小按页对齐**：`JavaThread::set_stack_size_at_create(align_up(stack_size_in_bytes, vm_page_size()))`（`src/hotspot/share/runtime/os.cpp:2081`），所以 `-Xss` 设成非页整数倍会被向上取整

### 1.4 进程信息：Java 也能查

```java
public long pid()                                  // src/java.base/share/classes/java/lang/ProcessHandleImpl.java:269
public Stream<ProcessHandle> children()            // :391
boolean destroyProcess(boolean force)              // :332  force=true 就是 SIGKILL
```

`children()`（`:391`）在 Linux 上是读 `/proc/*/stat` 的 PPID 字段拼出进程树——**Linux 把进程信息做成文件系统的典型例子**（`destroyProcess` 则是 `kill(pid, SIGTERM/SIGKILL)`）。

### 1.5 协程与虚拟线程

虚拟线程（JDK 21）是**用户态调度**的线程：

```java
Continuation.yield(VTHREAD_SCOPE);   // src/java.base/share/classes/java/lang/VirtualThread.java:441
private void mount()                 // :351  绑定到 carrier 线程
private void unmount()               // :378  解绑
```

挂起时 JVM 要把 Java 栈帧从 carrier 线程的栈搬到堆上的 continuation chunk：

```c
static JRT_BLOCK_ENTRY(int, freeze(JavaThread* current, intptr_t* sp))  // src/hotspot/share/runtime/continuationFreezeThaw.cpp:230
static int freeze(JavaThread* thread, intptr_t* const sp)                // :271
```

**对比表**：

| | 进程 | 线程（内核） | 协程 / 虚拟线程 |
|---|---|---|---|
| 调度方 | 内核 | 内核 | **用户态（JVM）** |
| 切换代价 | 最高（换页表） | 中（内核进出） | 最低（不进内核） |
| 阻塞影响 | 只影响自己 | 只影响自己 | 会占住 carrier（**pinning**） |
| 数量级 | 百 | 千 | **百万** |

> **虚拟线程的核心告诫**：它解决的是"**并发数多但多因 IO 等待**"的场景；不要在虚拟线程里跑 CPU 密集任务，也不要在 `synchronized` 里做阻塞 IO（会把 carrier 钉住）。

---

## 第二讲 上下文切换与调度

### 2.1 一次上下文切换到底换了什么

**进程切换**：保存/恢复寄存器 → 切换页表（CR3）→ **TLB 失效** → 内核栈切换 → 调度器选下一个。
**线程切换**：同进程的线程不换页表，省掉 TLB 刷新这一大块。

**什么时候会切**：
- **自愿（voluntary）**：等 IO、等锁、sleep、yield
- **非自愿（involuntary）**：时间片用完、被更高优先级抢占

`Thread.yield()` 只是"提示调度器我可以下台"，**不保证真的让出**（且 JVM 优先级映射到 OS 优先级后可能被忽略，见 `src/java.base/share/classes/java/lang/Thread.java:363/368/373`）。自旋等待时应调用：

```java
public static void onSpinWait() {}   // src/java.base/share/classes/java/lang/Thread.java:636
```

它是给 CPU 的提示（x86 上是 `PAUSE`），能降低自旋功耗、减少对流水线的破坏。

### 2.2 线程状态：Java 说的和 OS 说的不是一回事

```java
public enum State {   // src/java.base/share/classes/java/lang/Thread.java:2664
    NEW, RUNNABLE, BLOCKED, WAITING, TIMED_WAITING, TERMINATED
}
```

HotSpot 侧另有自己的状态记录（而且只作**提示**用）：

```c
volatile ThreadState _state;   // src/hotspot/share/runtime/osThread.hpp:69  "Thread state *hint*"
```

**高频考点**：`RUNNABLE` **不等于正在 CPU 上跑**——JDK 文档明说它也包含"等待操作系统资源（如 IO）"的状态。所以 jstack 里看到一片 `RUNNABLE` 且栈顶是 `socketRead`，真实情况是**卡在网络 IO 上**（详见 `perf-troubleshooting.md`）。

### 2.3 调度算法速记

| 算法 | 规则 | 问题 |
|---|---|---|
| FCFS | 先来先服务 | 长作业拖死短作业（护航效应） |
| SJF/STCF | 最短作业优先 | 长作业饥饿 |
| RR 时间片轮转 | 每人一片 | 片太大退化 FCFS，太小切换开销大 |
| 多级反馈队列 MLFQ | 优先级 + 降级 | Linux CFS 的近亲（实际用"虚拟运行时间"红黑树） |

---

## 第三讲 内存管理

### 3.1 虚拟内存：为什么要多一层地址

- **隔离**：每个进程看到的是独立的地址空间，踩不到别人
- **简化链接**：链接器可以用固定地址
- **超订（overcommit）**：虚拟地址空间可以比物理内存大得多，靠"按需调页"兑现

JVM 侧的页大小：

```c
size_t os::page_size_for_region(...)  // src/hotspot/share/runtime/os.cpp:1473
return vm_page_size();                 // :1488  标准页（x86 通常 4KB）
```

以及前面提到的栈大小按页对齐（`os.cpp:2081`）。大页（HugePage，2MB/1GB）能显著减少 TLB miss，是数据库/JVM 大堆的常见优化项（`-XX:+UseLargePages`）。

### 3.2 分页与缺页

```
虚拟地址 = 页号 + 页内偏移
   │
   ├─ TLB 命中 → 直接拿物理页（快）
   └─ TLB 未命中 → 查页表 → 页表项 Present?
          ├─ 是 → 填 TLB，重试指令
          └─ 否 → **缺页中断**（page fault）：
                     • 合法但未分配 → 分配物理页、可能触发**页面置换**
                     • 非法 → SIGSEGV
```

页面置换算法：OPT（理论最优，不可实现）、**FIFO（可能 Belady 异常）**、LRU（最接近 OPT）、LFU、Clock（LRU 的近似，OS 实际用得多）。

### 3.3 进程的内存布局

```
高地址
  ┌──────────────┐
  │   内核空间    │  用户态不可见（系统调用才进得去）
  ├──────────────┤
  │   栈 stack    │  向下增长，存放局部变量与调用帧
  ├──────────────┤
  │   mmap 区     │  动态库、mmap 文件映射
  ├──────────────┤
  │   堆 heap     │  向上增长（malloc / JVM 堆）
  ├──────────────┤
  │   BSS / Data  │  未初始化 / 已初始化的全局量
  ├──────────────┤
  │   代码段 text │
低地址
```

**栈溢出 vs 堆溢出**：栈是线程私有的、大小固定（`StackOverflowError`）；堆是所有线程共享（`OutOfMemoryError: Java heap space`）。JVM 还会在栈边缘放**保护页**做栈溢出检测（`javaThread.cpp:666` 的 `create_stack_guard_pages()`）。

---

## 第四讲 IO：系统调用、多路复用与零拷贝

### 4.1 用户态 / 内核态

- **用户态**：跑应用程序，不能直接访问硬件/内核内存
- **内核态**：跑内核代码，能访问一切
- **切换的三种触发**：系统调用（主动）、中断（异步）、异常（故障）

**系统调用的代价**：陷入内核（保存现场、切栈）、权限检查、可能的调度 → 单次约微秒级。所以"减少系统调用次数"是所有 IO 优化的第一原则——**缓冲（BufferedInputStream）就是这么来的**。

### 4.2 阻塞 / 非阻塞、同步 / 异步

| | 阻塞 | 非阻塞 |
|---|---|---|
| 同步 | read 一直等到数据（最常见） | read 立刻返回 EAGAIN，需轮询 |
| 异步 | — | 提交请求，完成由回调/信号通知 |

Java 里：`BIO`（同步阻塞）→ `NIO`（同步非阻塞 + 多路复用）→ `AIO`（异步，Linux 上实际由线程池模拟）。

### 4.3 多路复用：一个线程管万条连接

```java
class EPollSelectorImpl extends SelectorImpl {                     // src/java.base/linux/classes/sun/nio/ch/EPollSelectorImpl.java:49
    protected int doSelect(Consumer<SelectionKey> action, long timeout)  // :94  内部就是 epoll_wait
```

- `select`/`poll`：每次都要把整个 fd 集合拷进内核并 O(n) 扫描
- `epoll`：内核维护红黑树 + 就绪链表，`epoll_wait` 只返回就绪的 fd → O(就绪数)

**为什么 epoll 能撑 C10K**：不随连接数线性增长，且不需要为每个连接开一个线程（省掉线程栈与切换开销）。

### 4.4 零拷贝：sendfile 是怎么回事

传统"读文件再发出去"要走 **4 次拷贝 + 4 次上下文切换**：磁盘→内核缓冲区→用户缓冲区→socket 内核缓冲→网卡。

Java 的 `transferTo` 直接走内核的 `sendfile`：

```java
public long transferTo(long position, long count, WritableByteChannel target)  // src/java.base/share/classes/sun/nio/ch/FileChannelImpl.java:934
private long transferToTrustedChannel(...)     // :849  目标是可信 channel（SocketChannel）→ 走 sendfile
private long transferToArbitraryChannel(...)  // :901  不可信则退化成 mmap/缓冲拷贝
abstract long transferTo(FileDescriptor src, long position, long count, ...)   // src/java.base/share/classes/sun/nio/ch/FileDispatcher.java:86
```

`FileDispatcher.transferTo`（`:86`）是 native 方法，在 Linux 上就是 `sendfile(2)`：数据**全程不进用户态**，只剩"磁盘→内核缓冲→网卡"两次 DMA 拷贝。

> **注意**：`transferTo` 是否真的零拷贝取决于目标 channel 类型（`:849` vs `:901` 两条分支），而且需要在支持的系统上；不要无条件宣称"用了 transferTo 就一定零拷贝"。

---

## 第五讲 同步、互斥与死锁

### 5.1 为什么需要同步

**竞态条件（race condition）**：多个线程对共享数据的读写序列不确定。解决靠**互斥**（一次只准一个进临界区）+ **可见性/顺序**（内存屏障）。

实现手段：关中断（单核内核）、**自旋锁**（临界区极短）、**互斥锁**（会睡眠，临界区较长）、信号量、条件变量、读写锁。

### 5.2 死锁四条件（缺一不可）

1. **互斥**：资源一次只能被一个持有
2. **占有且等待**：持有一个还等着别的
3. **不可抢占**：不能强行抢走
4. **循环等待**：形成环形等待链

对应四种对策：**预防**（破坏任一条件，如统一加锁顺序破循环等待）、**避免**（银行家算法，运行时判断）、**检测 + 恢复**（画等待图，检测到就回滚/杀线程）、**鸵鸟**（不管，重启——多数数据库和 JVM 的实用选择）。

JVM 自带的死锁检测：`jstack` 能直接报 `Found one Java-level deadlock`（基于线程等待图），数据库则靠超时或等待图检测后回滚代价最小的事务。

### 5.3 进程间通信（IPC）

| 方式 | 特点 |
|---|---|
| 管道 pipe | 半双工、字节流、父子进程 |
| 命名管道 FIFO | 无亲缘关系也能用 |
| 消息队列 | 有边界的消息，内核维护 |
| 共享内存 + 信号量 | **最快**（不拷数据，靠映射同一块物理页），但要自己同步 |
| 信号 signal | 异步通知（`kill -9` 就是 SIGKILL，见 `ProcessHandleImpl.destroyProcess`） |
| socket | 跨主机也能用 |

**最快的是共享内存**，因为它唯一地"绕开了内核拷贝"。

---

## 六、面试高频 Q&A

26 道最高频题的「一句话答案」。先报结论占住阵地，面试官真追问时，按「深入」列展开到对应讲次。

| # | 问题 | 答案（先给结论） | 深入 |
| --- | --- | --- | --- |
| 1 | 进程和线程的区别？ | 进程是**资源分配**的基本单位（独立地址空间），线程是**CPU 调度**的基本单位（共享地址空间，私有栈与寄存器） | 第一讲 |
| 2 | 为什么线程切换比进程切换快？ | 线程不换页表，**TLB 不失效**；进程切换要换地址空间并刷 TLB | 第一讲 |
| 3 | 线程共享什么、私有什么？ | 共享堆/代码段/文件描述符；私有栈、寄存器上下文、ThreadLocal | 第一讲 |
| 4 | fork 和 exec 分别做什么？ | fork 复制进程（COW），exec 换成新程序；JDK 的 `forkAndExec`（`ProcessImpl.java:282`）就是这对组合 | 第一讲 |
| 5 | 什么是僵尸进程？怎么产生？ | 子进程退出但父进程没 `wait()` 回收，PCB 与 pid 仍占着 | 第一讲 |
| 6 | 孤儿进程有害吗？ | 无害，被 init/systemd 收养并回收 | 第一讲 |
| 7 | Java 线程是内核线程吗？ | 是 1:1 模型：`os::create_thread`（`javaThread.cpp:605`）最终是 `pthread_create` | 第一讲 |
| 8 | "unable to create new native thread" 是什么原因？ | 线程数触到系统/ulimit 上限或虚拟内存不足，HotSpot 在 `create_thread` 拿不到 `_osthread` 时抛 OOM | 第一讲 |
| 9 | 协程与线程的区别？ | 协程在**用户态**调度，切换不进内核，可开百万级；线程由内核调度，千级 | 第一讲 |
| 10 | 虚拟线程为什么能支撑高并发？ | 阻塞时 `Continuation.yield`（`VirtualThread.java:441`）把栈帧 freeze 到堆（`continuationFreezeThaw.cpp:230`）并释放 carrier | 第一讲 |
| 11 | 虚拟线程的 pinning 是什么？ | 阻塞操作发生在 `synchronized` 内时无法 unmount，carrier 被钉住，吞吐退化 | 第一讲 |
| 12 | 什么是上下文切换？什么时候发生？ | 保存/恢复执行现场；自愿（等 IO/锁/sleep）与非自愿（时间片到/被抢占） | 第二讲 |
| 13 | `Thread.yield()` 一定让出 CPU 吗？ | 不保证，只是提示；且 Java 优先级（1/5/10）映射到 OS 后可能被忽略 | 第二讲 |
| 14 | `onSpinWait()` 有什么用？ | 给 CPU 的自旋提示（x86 `PAUSE`），降低功耗与流水线惩罚（`Thread.java:636`） | 第二讲 |
| 15 | 线程状态 RUNNABLE 就是在跑吗？ | **不是**，它还包含"等待 OS 资源"（如阻塞 IO）——jstack 里 `RUNNABLE` + `socketRead` 其实是卡住了 | 第二讲 |
| 16 | 为什么要有虚拟内存？ | 隔离（进程之间互相踩不到）、简化链接、支持超订与按需调页 | 第三讲 |
| 17 | 什么是缺页中断？ | 访问的页不在内存（页表项未 Present）时触发，内核分配物理页/换入后重试；非法访问则 SIGSEGV | 第三讲 |
| 18 | 页面置换算法有哪些？ | OPT（理论）、FIFO（有 Belady 异常）、LRU、LFU、Clock（LRU 近似，OS 实际常用） | 第三讲 |
| 19 | 栈溢出与堆溢出的区别？ | 栈私有且固定 → `StackOverflowError`；堆共享 → `OutOfMemoryError: Java heap space` | 第三讲 |
| 20 | 用户态和内核态怎么切换？ | 系统调用（主动）、中断（异步）、异常（故障）；每次切换要保存现场+切栈，约微秒级 | 第四讲 |
| 21 | 同步/异步与阻塞/非阻塞的区别？ | 同步/异步看"结果怎么拿到"（自己等 vs 回调通知）；阻塞/非阻塞看"等的时候线程挂不挂" | 第四讲 |
| 22 | select / poll / epoll 为什么 epoll 快？ | epoll 用红黑树+就绪链表，返回就绪集合 O(就绪数)；前两者每次 O(n) 扫描+拷贝 | 第四讲 |
| 23 | 什么是零拷贝？Java 怎么做？ | 数据不经过用户态；`FileChannel.transferTo`（`FileChannelImpl.java:934`）→ `FileDispatcher.transferTo`（`:86`）→ `sendfile` | 第四讲 |
| 24 | 用了 `transferTo` 就一定零拷贝吗？ | 不一定：只有目标是可信 channel 才走 sendfile（`:849`），否则退化（`:901`） | 第四讲 |
| 25 | 死锁的四个必要条件？ | 互斥、占有且等待、不可抢占、循环等待；破任一条即可预防（最实用：统一加锁顺序） | 第五讲 |
| 26 | 进程间通信最快的是哪种？ | 共享内存（映射同一块物理页，不拷数据），但要自己配信号量同步 | 第五讲 |

---

## 七、易错点清单

1. **把 `RUNNABLE` 当"正在运行"**：它包含阻塞在 OS IO 上的线程，排查时要看栈顶帧
2. **以为 `Thread.yield()` 能控制调度**：只是提示，行为依赖 OS
3. **虚拟线程里跑 CPU 密集任务**：占住 carrier 不释放，比线程池还差
4. **虚拟线程 + `synchronized` + 阻塞 IO**：触发 pinning，吞吐骤降（应改 `ReentrantLock`）
5. **以为线程栈可以任意设**：HotSpot 会按页大小向上取整（`os.cpp:2081`）
6. **以为 `transferTo` 一律零拷贝**：分可信/不可信两条分支（`:849` vs `:901`）
7. **混淆同步异步与阻塞非阻塞**：两组正交概念，别混答
8. **忘记父进程要回收子进程**：不 `wait()` 就堆僵尸进程（`ProcessImpl.java:342` 的 completion 回调就是干这个的）
9. **把"减少系统调用"理解成"减少 IO 次数"**：真正该做的是**加大每次 IO 的粒度**（缓冲/批量）
10. **死锁只说"加锁顺序"**：四条件里任意一条都能破，检测/恢复也是正经策略

---

## 八、数值速记

| 数值 | 含义 | 出处 |
| --- | --- | --- |
| `4KB` | x86 标准页大小（HugePage 通常 2MB/1GB） | `os.cpp:1488` `vm_page_size()` |
| `1 / 5 / 10` | Java 线程优先级 MIN/NORM/MAX（OS 可忽略） | `Thread.java:363/368/373` |
| `6` | `Thread.State` 枚举值个数 | `Thread.java:2664` |
| `4 → 2` | 零拷贝把"数据拷贝次数"从 4 降到 2（DMA 直传） | `FileChannelImpl.java:849` |
| `SIGTERM / SIGKILL` | `destroyProcess(false)` / `destroyProcess(true)` | `ProcessHandleImpl.java:332` |
| `1:1` | Java 线程模型（一个 Java 线程一个内核线程） | `javaThread.cpp:605` |

---

## 九、复习卡

- [ ] 能说出进程与线程的三点区别，并解释"线程切换更快"的真实原因（TLB）
- [ ] 能画出 JVM 创建 Java 线程的调用链（JavaThread → os::create_thread → pthread_create）
- [ ] 能解释 fork/exec、僵尸/孤儿进程，并说出 JDK 里对应的行号
- [ ] 能说清虚拟线程 mount/unmount + freeze 的机制，以及 pinning 的触发条件
- [ ] 能区分自愿/非自愿上下文切换，并说出 RUNNABLE 的真实语义
- [ ] 能说出虚拟内存三大作用与缺页中断的处理流程
- [ ] 能说出页面置换算法并指出 OS 实际常用的那个
- [ ] 能解释用户态/内核态切换的三种触发方式与代价
- [ ] 能对比 select/poll/epoll，并说出 JDK Selector 在 Linux 的实现
- [ ] 能说出零拷贝的原理、Java API 与"并非总是零拷贝"的边界
- [ ] 能背出死锁四条件与四种对策

---

## 十、文件索引

| 文件 | 说明 |
| --- | --- |
| `src/hotspot/share/runtime/javaThread.cpp` | Java 线程创建（`:605` create_thread）、`run()`（`:666`）、TERMINATED（`:742`） |
| `src/hotspot/share/runtime/thread.cpp` | `Thread::start`（`:371`）→ `os::start_thread`（`:382`） |
| `src/hotspot/share/runtime/os.cpp` | `os::start_thread`（`:842`）、页大小（`:1488`）、栈按页对齐（`:2081`） |
| `src/hotspot/share/runtime/osThread.hpp` | OS 线程状态"提示"（`:69`） |
| `src/hotspot/share/runtime/continuationFreezeThaw.cpp` | 协程栈帧 freeze（`:230`/`:271`）、栈布局（`:144`） |
| `src/java.base/unix/classes/java/lang/ProcessImpl.java` | forkAndExec（`:282`）、pid（`:300`）、子进程回收（`:342`） |
| `src/java.base/share/classes/java/lang/ProcessHandleImpl.java` | pid（`:269`）、destroyProcess（`:332`）、children（`:391`） |
| `src/java.base/share/classes/java/lang/Thread.java` | State（`:2664`）、优先级、onSpinWait（`:636`） |
| `src/java.base/share/classes/java/lang/VirtualThread.java` | mount/unmount（`:351`/`:378`）、`Continuation.yield`（`:441`） |
| `src/java.base/share/classes/sun/nio/ch/FileChannelImpl.java` | transferTo 与零拷贝两条分支（`:934`/`:849`/`:901`） |
| `src/java.base/share/classes/sun/nio/ch/FileDispatcher.java` | `transferTo` native 声明（`:86`） |
| `src/java.base/linux/classes/sun/nio/ch/EPollSelectorImpl.java` | epoll 选择器（`:49`/`:94`） |
