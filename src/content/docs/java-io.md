---
title: "Java IO 源码深度学习笔记"
date: 2026-10-19
categories:
  - "Java"
tags:
  - "IO"
  - "NIO"
  - "源码"
---

> 基于对 OpenJDK **25**（JDK 24 GA 后的开发线，bug 编号 `834xxxx` 前缀）`java.io` 与 `java.nio` 源码的逐文件精读整理而成。
>
> 源码位于 `src/java.base/share/classes/java/io/`（IO 流家族）、`src/java.base/share/classes/java/nio/`（Buffer 家族）、`src/java.base/share/classes/sun/nio/ch/`（Channel/Selector 实现）、`src/java.base/unix/classes/sun/nio/ch/`（Linux `poll` 版 Selector）与 `src/java.base/windows/classes/sun/nio/ch/`（Windows `IOCP`/`poll` 版实现）。
>
> 阅读顺序即学习顺序：字节流骨架 → 缓冲与数据流 → 字符流与编解码 → File/RandomAccessFile → NIO 三件套 → 序列化与管道。
>
> 所有结论均标注了源文件位置（格式 `相对路径:行号`），行号均经 Read/Grep 实际核对。
>
> **版本基线**：OpenJDK **25 开发线**，`git log -1` = `6c48f4ed707bf0b15f9b6098de30db8aae6fa40f`，commit 时间 `2025-08-12 17:15:36 +0000`。
> **注意**：JDK 源码树中 `java/io/` 与 `java/nio/` 都在 `src/java.base/share/classes/` 下；Selector 的平台实现分 `share`（抽象）/`unix`（`PollSelectorImpl`，Linux）/`windows`（`WindowsSelectorImpl`）三处，本文引用默认指 Linux 侧。

---

## 目录

- [〇、全景地图](#〇全景地图)
- [速览：六讲各自做了什么（先看这个再进细节）](#速览六讲各自做了什么先看这个再进细节)
- [面试速通：Java IO 高频问题一页纸](#面试速通java-io-高频问题一页纸)
- [第一讲：字节流骨架 —— InputStream / OutputStream 与 File 流](#第一讲字节流骨架--inputstream--outputstream-与-file-流)
- [第二讲：缓冲与数据流 —— BufferedInputStream / DataInputStream](#第二讲缓冲与数据流--bufferedinputstream--datainputstream)
- [第三讲：字符流与编解码 —— StreamDecoder 与 BufferedReader](#第三讲字符流与编解码--streamdecoder-与-bufferedreader)
- [第四讲：File、RandomAccessFile 与 FileSystem 抽象](#第四讲filerandomaccessfile-与-filesystem-抽象)
- [第五讲：NIO 三件套 —— Buffer / FileChannel / Selector](#第五讲nio-三件套--buffer--filechannel--selector)
- [第六讲：序列化与管道 —— ObjectInputStream / PipedInputStream](#第六讲序列化与管道--objectinputstream--pipedinputstream)
- [附：学习路线与复习卡汇总](#附学习路线与复习卡汇总)
- [涉及文件索引](#涉及文件索引)

---

## 〇、全景地图

Java IO 在源码层面可以压缩成**两套并行的 API + 一个公共编解码内核 + 一个共享的文件系统抽象**：

- **IO 流家族（`java.io`）**：`InputStream`/`OutputStream`（字节）+ `Reader`/`Writer`（字符）两个平行继承树，中间用 `InputStreamReader`/`OutputStreamWriter` 搭桥，桥里坐着 `sun.nio.cs.StreamDecoder`/`StreamEncoder` 这一对编解码内核（它们复用 NIO 的 `CharsetDecoder`）。
- **NIO 家族（`java.nio`）**：`Buffer`（容器抽象）+ `Channel`（双向 IO 端点）+ `Selector`（多路复用）。`FileChannel` 底层是 `sun.nio.ch.FileChannelImpl`，`Selector` 抽象在 `sun.nio.ch.SelectorImpl`，Linux 具体实现在 `sun.nio.ch.PollSelectorImpl`（`poll(2)`）。
- **横切两族的公共设施**：`File`/`FileSystem`（路径与文件属性，Unix 侧 `UnixFileSystem`、Windows 侧 `WinNTFileSystem`）、`FileDescriptor`（native fd 的 Java 句柄）、`ObjectInputStream`/`ObjectOutputStream`（序列化协议）。

```
┌────────────────────── java.io（流家族，同步阻塞） ───────────────────────┐
│                                                                          │
│  InputStream ──┬─ FileInputStream   → native open0/read0/readBytes0     │
│                ├─ ByteArrayInputStream / StringBufferInputStream         │
│                ├─ BufferedInputStream  → buf[] + fill()（:256）           │
│                ├─ DataInputStream     → readInt/readUTF（:389/:554）      │
│                ├─ PushbackInputStream → pos 回推（unread :229）           │
│                └─ PipedInputStream    → 1024 循环缓冲 + receive/awaitSpace│
│                                                                          │
│  Reader ───────┬─ InputStreamReader → StreamDecoder（解码内核）           │
│                ├─ BufferedReader    → readLine()（:320 找 \n/\r）         │
│                └─ PipedReader / CharArrayReader / StringReader            │
│                                                                          │
│  ObjectInputStream / ObjectOutputStream（序列化协议，第六讲）              │
└──────────────────────────────────────────────────────────────────────────┘
                                    │ 共用
┌────────────────────── java.nio（通道 + 缓冲 + 多路复用） ─────────────────┐
│                                                                          │
│  Buffer（java/nio/Buffer.java:217  mark <= position <= limit <= capacity）│
│     HeapByteBuffer（堆内，byte[]）/ DirectByteBuffer（堆外，unsafe 内存）  │
│     MappedByteBuffer（FileChannel.map，DirectByteBuffer 子类 :1412）      │
│                                                                          │
│  FileChannelImpl（sun/nio/ch/FileChannelImpl.java，1774 行）              │
│     read/write（:227/:331） map（:1412） lock（:1669） transferTo（:934）   │
│     → FileDispatcher 抽象 → UnixFileDispatcherImpl / FileDispatcherImpl   │
│                                                                          │
│  SelectorImpl（抽象 :52）── doSelect 钩子 ──→ PollSelectorImpl（Linux）    │
│     pollArray 本地内存 struct pollfd（:53）                              │
│     updateKeys 延迟队列 + processUpdateQueue（:143）                      │
│     wakeup() 靠 pipe 写 1 字节（PipeImpl，:240）                          │
└──────────────────────────────────────────────────────────────────────────┘
                                    │ 共用
┌────────────────────── 横切设施 ──────────────────────────────────────────┐
│  File（java/io/File.java:160 FS = DefaultFileSystem.getFileSystem()）    │
│     ├─ UnixFileSystem（unix/classes/sun/nio/fs/UnixFileSystem.java）      │
│     └─ WinNTFileSystem（windows/classes/sun/nio/fs/WinNTFileSystem.java） │
│  FileDescriptor（java/io/FileDescriptor.java:49，in/out/err 静态三件套）  │
│  RandomAccessFile（java/io/RandomAccessFile.java，seek 自由光标）          │
└──────────────────────────────────────────────────────────────────────────┘
```

一句话记住全景：**IO 流是"一个方向一条管子"，NIO 是"一个双向端点 + 一块缓冲 + 一个多路复用器"**；两者的字符/字节互转都走同一个 `StreamDecoder`，两者的文件操作都最终落到同一个 native fd。

---

## 速览：六讲各自做了什么（先看这个再进细节）

| 讲次 | 主题 | 核心类 | 要回答的问题 | 不学会怎样 |
|---|---|---|---|---|
| 第一讲 | 字节流骨架 | `InputStream`/`FileInputStream` | `read()` 一次读多少、`close()` 为什么有 `closeLock` | 读文件漏字节、重复 close 踩竞态 |
| 第二讲 | 缓冲与数据流 | `BufferedInputStream`/`DataInputStream` | 缓冲为什么省系统调用、`readUTF` 的改长前缀 | 逐字节读慢 100 倍、协议解析错位 |
| 第三讲 | 字符流与编解码 | `StreamDecoder`/`BufferedReader` | 半字节缓冲、`MalformedInputException` 何时抛 | 中文乱码、跨块字符被吞 |
| 第四讲 | File 与 RandomAccessFile | `File`/`RandomAccessFile` | 路径的 OS 差异、`seek` 与文件锁 | 路径拼接错误、随机读写性能差 |
| 第五讲 | NIO 三件套 | `Buffer`/`FileChannel`/`Selector` | `flip` 三部曲、`map` 零拷贝、Selector 的 pollfd | 直接内存 OOM、Selector 空转烧 CPU |
| 第六讲 | 序列化与管道 | `ObjectInputStream`/`PipedInputStream` | 句柄表去重、TC_* 协议分派、管道死锁 | 序列化安全漏洞、Piped 死锁 |

---

## 面试速通：Java IO 高频问题一页纸

### 高频 Q&A（先结论）

| # | 问题 | 一句话结论 | 出处 |
|---|---|---|---|
| 1 | BIO 和 NIO 的区别？ | BIO 是**面向流的阻塞**（一个连接一个线程，`InputStream.read` 阻塞到有数据）；NIO 是**面向缓冲的**（数据先到 `Buffer`）+ **非阻塞**（`Channel` 配 `Selector`，一个线程管多个连接）+ **选择器** | `java/nio/Buffer.java:217`、`sun/nio/ch/SelectorImpl.java:52` |
| 2 | `read()` 为什么返回 int 不返回 byte？ | 需要用 `-1` 表示 EOF，byte 的取值域不够。`InputStream.read():182` 注释明说 | `java/io/InputStream.java:182` |
| 3 | 一次 `read(byte[])` 一定读满数组吗？ | **不一定**。`FileInputStream.read(byte[],off,len):277` 直接透传 native `readBytes0`，底层 `read(2)` 对普通文件通常读满，但管道/网络随时可能短读；必须循环或用 `readNBytes` | `java/io/FileInputStream.java:277` |
| 4 | `BufferedInputStream` 为什么快？ | 把多次小 `read` 合并成一次 `fill()` 批量读（默认 8192 字节缓冲，`:62`），减少 `read(2)` 系统调用次数；`mark/reset` 也靠同一块 `buf` | `java/io/BufferedInputStream.java:256` |
| 5 | `mark` 之后读超多少会失效？ | `marklimit`（构造第二参数或 `mark(readlimit)`）。`fill()` 里若 `pos - markpos > marklimit` 就丢弃 mark（`markpos = -1`） | `java/io/BufferedInputStream.java:467` |
| 6 | `DataInputStream.readUTF` 和 `Writer.write("…")` 一样吗？ | **不一样**。`readUTF:632` 是**改长的 UTF-8**（2 字节长度前缀 +  supplemental 字符拆成代理对按 3 字节编码、`\0` 编码成 2 字节），和标准 UTF-8 不兼容，只能和 `DataOutputStream.writeUTF:337` 配对 | `java/io/DataInputStream.java:554` |
| 7 | `InputStreamReader` 是字节流还是字符流？ | **字符流**（`extends Reader`，`java/io/InputStreamReader.java:69`），内部持有 `StreamDecoder`；构造时**立刻读前几个字节探测 BOM/编码**（`sd = StreamDecoder.forInputStreamReader(in, this)`，`:81-89`） | `java/io/InputStreamReader.java:81` |
| 8 | 跨 `read` 调用中文字为什么会乱？ | 多字节字符可能被系统调用**切断在两次 read 之间**，`StreamDecoder` 用 `bb` 残字节缓冲解决：半字符留在 `ByteBuffer` 里下次继续解 | `sun/nio/cs/StreamDecoder.java:123` |
| 9 | `available()` 返回值可信吗？ | **不可信**。注释原文"an estimate"（`:457`），普通文件返回剩余字节，网络流返回 0 不代表没数据；不要用它决定"是否读完" | `java/io/FileInputStream.java:457` |
| 10 | `File` 和 `Path` 什么关系？ | JDK 7 起 `Path` 是主力，`File` 是遗留 API（`java.io.File` 仍在维护）。`File` 内部所有操作委托 `FileSystem FS`（`File.java:160`），`File.toPath():727` 两者互转 | `java/io/File.java:160` |
| 11 | `RandomAccessFile` 和 `FileInputStream` 本质差异？ | 它是**同一个 native fd 上既可读又可写、光标可 `seek` 自由定位**；`"rws"`/`"rwd"` 模式控制每次写的元数据/内容同步强度（构造 `:130`） | `java/io/RandomAccessFile.java:130` |
| 12 | NIO 的 `Buffer` 三个指针？ | `mark <= position <= limit <= capacity`（不变量在 `Buffer.java:217` 注释）；写模式 `limit==capacity`，读完 `flip()` 切读模式（`limit=position, position=0`，`:1009`），读完再 `clear()` | `java/nio/Buffer.java:217` |
| 13 | `HeapByteBuffer` 和 `DirectByteBuffer`？ | 前者快在分配（数组）、后者快在 IO（无内核态拷贝）；`DirectByteBuffer` 受 `-XX:MaxDirectMemorySize` 限制，靠 `Cleaner`/`Unsafe` 释放（`Bits.java` 管理 reserveMemory） | `java/nio/Direct-X-Buffer.java.template` |
| 14 | `FileChannel.map` 是什么原理？ | `mmap(2)`：把文件映射进进程地址空间，返回 `MappedByteBuffer`（`FileChannelImpl.map:1412`）；读是缺页中断按需加载，写直接落 page cache | `sun/nio/ch/FileChannelImpl.java:1412` |
| 15 | `MappedByteBuffer` 什么时候被释放？ | **不确定**。`map` 时注册 `DefaultUnmapper` 到 `Cleaner`（`:1341`），GC 触发才 `unmap`；大文件要用 `cleaner().clean()` 或 `Arena` 主动释放，否则 fd 占满 | `sun/nio/ch/FileChannelImpl.java:1341` |
| 16 | `FileChannel.force()` 是 fsync 吗？ | 是。`force(false)` → `fsync(2)`（数据+元数据）、`force(true)` → `fdatasync` 风格只刷数据（`:1074`） | `sun/nio/ch/FileChannelImpl.java:1074` |
| 17 | `transferTo`/`transferFrom` 零拷贝？ | 对普通文件会走 `FileDispatcher.transferTo` → Linux `sendfile`（一次系统调用、数据不过用户态）；不支持时退化为循环 read/write | `sun/nio/ch/FileChannelImpl.java:934` |
| 18 | Selector 怎么做到一个线程管多个连接？ | 注册时把 fd 和关注事件写进本地内存 `pollArray`（struct pollfd 数组，`PollSelectorImpl.java:53`），`doSelect` 一次 `poll(2)` 拿所有就绪 fd | `sun/nio/ch/PollSelectorImpl.java:97` |
| 19 | `Selector.wakeup()` 不阻塞 select 的原理？ | Linux 实现是一根**自建管道**（`PipeImpl`，fd0/fd1 两个字段 `:56-57`）：注册读端到 pollArray，`wakeup()` 往写端写 1 字节，`poll` 立刻返回 | `sun/nio/ch/PollSelectorImpl.java:240` |
| 20 | `select()` 返回 0 就绪也可能白跑一趟？ | 是。`wakeup` 写的字节、`updateKeys` 延迟注册都会让 `poll` 提前返回但**没有就绪事件**，所以 `selectNow` 后要复查 cancelledKeys | `sun/nio/ch/SelectorImpl.java:120` |
| 21 | 序列化怎么写同一个对象两次？ | 第一次 `TC_OBJECT` 全文，第二次只写 `TC_REFERENCE` + 句柄号（`writeObject0:…` 的 `handles.assign(obj)` 查表）；句柄表 `HandleTable` 从 0xff71 开始 | `java/io/ObjectOutputStream.java:180` |
| 22 | `serialVersionUID` 不写会怎样？ | 运行时按类结构**自动计算**哈希（`ObjectStreamClass.computeDefaultSUID`），加个字段就变，反序列化直接 `InvalidClassException`。必须显式声明 | `java/io/ObjectStreamClass.java` |
| 23 | `readObject` 里能强转吗？ | **不能直接信**。反序列化是"从不可信字节造对象"的入口，JDK 9+ 提供 `ObjectInputFilter`（`ObjectInputStream.java:1281` `setObjectInputFilter`）做白名单过滤 | `java/io/ObjectInputStream.java:1281` |
| 24 | `PipedInputStream` 死锁怎么发生的？ | 写端写满 1024 字节会 `awaitSpace`（`:273`）等读端读；读端不读、双方都阻塞就死锁。且读写两端**不是线程安全的同一把锁**，需两个线程 | `java/io/PipedInputStream.java:273` |
| 25 | IO 流用完为什么必须 close？ | `FileInputStream` 的 native fd 是**有限资源**，`close():487` 用双检 `closed` + `closeLock` 保证幂等，同时会关掉关联的 `FileChannel`（`:498`）；忘了关靠 `Cleaner` 兜底但时机不定 | `java/io/FileInputStream.java:487` |

### 易错点（最容易讲错的 8 条）

1. **`read(byte[])` 返回 0 ≠ EOF**：0 只可能出现在 `len==0` 的合法调用；EOF 永远是 `-1`。循环读必须判 `-1`。
2. **`skip(n)` 对普通文件是 `lseek`、对其他流是读出来扔掉**：`FileInputStream.skip:431` 走 `skip0`（native lseek），管道退化为 `super.skip`（读进 2048 缓冲丢弃，`:56` `MAX_SKIP_BUFFER_SIZE`）。
3. **`BufferedInputStream.close()` 不会关底层流**——它关自己再把底层流**也关掉**（`FilterInputStream.close:169` → `in.close()`），套两层时注意顺序。
4. **`InputStreamReader` 构造就发起 IO**：探测编码需要读流的前几个字节（BOM），所以"包一层 reader 再往流里写"的场景会卡住。
5. **`flip()` 不是清空**：`flip` 后 `position=0, limit=原position`，数据还在；`clear()` 才是回到写模式（但同样不清数据，只是复位指针）。
6. **`DirectByteBuffer` 的 `array()` 抛 `UnsupportedOperationException`**：堆外内存没有 Java 数组视图，`hasArray()` 永远是 false（`Direct-X-Buffer.java.template`）。
7. **`Selector` 的 `selectedKeys()` 用完必须 `remove()`**：`SelectorImpl` 的 `selectedKeys` 不会自动清（`select` 里 `processEvents` 按 key 的 `nioInterestOps` 重新计算，`Iterator.remove` 是**你的**责任，`sun/nio/ch/PollSelectorImpl.java:172`）。
8. **序列化不保存 `static` 和 `transient` 字段**：`ObjectStreamClass` 只收集非 static 非 transient 字段（`getDefaultSerialFields`），`transient` 想参与得自己实现 `writeObject/readObject`（`java/io/ObjectOutputStream.java:404`）。

### 数值速记

| 数值 | 含义 | 出处 |
|---|---|---|
| `-1` | `read()` 的 EOF 信号 | `java/io/InputStream.java:182` |
| `8192` | `FileInputStream`/`BufferedInputStream` 默认缓冲 | `java/io/FileInputStream.java:62`、`BufferedInputStream.java:62` |
| `16384` | `InputStream.readAllBytes` 首缓冲 | `java/io/InputStream.java:58` |
| `2048` | `InputStream.skip` 的丢弃缓冲 | `java/io/InputStream.java:56` |
| `1024` | `PipedInputStream` 循环缓冲大小 | `java/io/PipedInputStream.java` |
| `2` | `readUTF` 的长度前缀字节数 | `java/io/DataInputStream.java:554` |
| `0xff71` | 序列化句柄表起始 handle | `java/io/ObjectOutputStream.java`（`HandleTable`） |
| `2147483647` | `Integer.MAX_VALUE`，Buffer position/limit 上限 | `java/nio/Buffer.java:217` |

---

## 第一讲：字节流骨架 —— InputStream / OutputStream 与 File 流

> **本讲问题**：`read()` 到底把数据从哪搬到哪？`FileInputStream` 的 native 边界在哪？为什么 `close()` 写得那么啰嗦（双检 + 锁）？

### 1.1 抽象基类：所有字节流的合同

`InputStream`（`java/io/InputStream.java`）是**抽象类不是接口**——因为它给了一堆带实现的公共方法（`skip:539`、`readAllBytes:347`、`transferTo:790`、`mark/reset`），子类只需实现一个抽象方法：

```java
// java/io/InputStream.java:182
public abstract int read() throws IOException;
```

返回值三态：`0~255`（一个字节）/ `-1`（EOF）/ 抛异常。**为什么返回 int 不返回 byte**——byte 表示不了 -1，这个设计从 JDK 1.0 定型至今。

`read(byte[] b, int off, int len):283` 的默认实现是循环调用单字节 `read()`（每字节一次 native 调用，极慢），注释明确要求子类覆盖：

```java
// java/io/InputStream.java:283-296
public int read(byte[] b, int off, int len) throws IOException {
    ...
    int c = read();          // 子类通常整体覆盖这个方法
    if (c == -1) return -1;
    b[off] = (byte) c;
    int i = 1;
    ...
}
```

`InputStream` 还提供了三个"整个进内存"的方便方法：`readAllBytes():347`（读到 EOF，内部首缓冲 16384 翻倍增长）、`readNBytes(len):396`（精确读 len 个，不够就返回已有）、`transferTo(out):790`（读到 EOF 并写入 out，返回字节数）。

`OutputStream` 是对称设计：抽象方法 `write(int b):56`，其余 `write(byte[])`、`flush`、`close` 都有默认实现。

### 1.2 FileInputStream：从 Java 到 native 的那一步

`FileInputStream`（`java/io/FileInputStream.java`，584 行）是最常用的字节流，字段先看清：

```java
// java/io/FileInputStream.java:71-87
private final FileDescriptor fd;        // native fd 句柄
private final String path;              // 打开时的路径（异常信息用）
private volatile FileChannel channel;   // 懒加载：getChannel() 才建
private final Object closeLock = new Object();  // 专门保护 close
private volatile boolean closed;        // 关闭标志（volatile 快路径）
private @Stable Boolean isRegularFile;  // 是否普通文件（决定 skip 走 lseek）
```

打开路径 `open(String):184` 做两件事：`open0(name)` native 调用（`libjava` 里 `Java_java_io_FileInputStream_open0` → `handleOpen` → `os::open` + `fdval`），失败时把 errno 翻译成 `FileNotFoundException` 并带上 `path`：

```java
// java/io/FileInputStream.java:184-190
private void open(String name) throws FileNotFoundException {
    try {
        open0(name);
    } catch (IOException e) {
        ...
        throw new FileNotFoundException(... + path ...);
    }
}
```

读的原语分三档（都声明为 `private native`，实现在 `src/java.base/share/native/libjava/FileInputStream.c`）：

| Java 方法 | native 方法 | 用途 |
|---|---|---|
| `read():197` | `read0():204` | 单字节（内部还是 read(2) 一次读 1 字节） |
| `read(b,off,len):277` | `readBytes0():230` | 批量（`read(2)` 一次最多 len） |
| `readAllBytes():285` | 同上循环 | 读到 EOF |

`readAllBytes():285` 值得单独看——它**先试 `transferTo` 到 `ByteArrayOutputStream`**，失败了才退回循环 `read`，是一个小优化。

**JFR 追踪**：`traceRead0():206` / `traceReadBytes():232` 在 `jfrTracing` 打开时给每次 IO 打点（`:68` 静态开关，`Class.forName("jdk.jfr.FlightRecorder")` 探测），生产排查 IO 慢时打开 JFR 能看到每条 FileInputStream 读事件。

### 1.3 close() 为什么写得这么"啰嗦"

这是面试高频点。看完整实现：

```java
// java/io/FileInputStream.java:487-510
public void close() throws IOException {
    if (closed) {                 // ① 无锁快路径：绝大多数重复 close 直接返回
        return;
    }
    synchronized (closeLock) {    // ② 只有并发竞争时才进锁
        if (closed) {             // ③ 双检：两个线程同时通过①时只有一个能置位
            return;
        }
        closed = true;
    }
    FileChannel fc = channel;
    if (fc != null) {
        fc.close();               // ④ 关流连带关 channel（channel.close 会回头关流）
    }
    ...
    close0();                     // ⑤ 真正 close(fd)
}
```

为什么不能只用一个 `synchronized close()`？因为 **`close()` 可能和 `read()` 并发**：线程 A 在读、线程 B 在关，`fd` 被 `close0` 释放后 A 的 `read0` 行为未定义（在 Linux 上 fd 号可能已被复用，读到别的文件！）。`closeLock` 的真正用途在 native 层：`close0` 前持有锁，`read0` 内部也持有同一把锁才能发起系统调用，保证"读"和"关"互斥。

注意 ④ 的顺序细节：**先关 channel 再关 fd**。因为 `FileChannelImpl.close` 会设置 fd 为 -1 并回调 `implCloseChannel`，反过来再关流就会重入——所以注释特意提醒"subclasses should be prepared to handle possible reentrant invocation"（`:482`）。

### 1.4 FileOutputStream：写侧与 append

`FileOutputStream` 与输入流对称，差异点：

- 构造多一个 `boolean append` 参数（`FileOutputStream(String,boolean):145`），append 时 native `open` 传 `O_APPEND`；
- `write(int):286` → native `write`；`write(byte[],off,len):346` → native `writeBytes`；
- **没有 `flush()` 语义问题**：文件流 `flush` 是空操作（`FileOutputStream.flush` 不 override，基类默认啥也不做，`OutputStream.java:190`），数据在 `write(2)` 调用时已交给内核。

### 1.5 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `InputStream` 抽象类提供 15+ 个公共方法，子类只需实现单字节 `read():182` |
| 关键实现 | `FileInputStream` 三档 native 读（read0/readBytes0/skip0），JFR 可插拔打点 |
| 并发 | `close()` 双检 + `closeLock`，保证与 `read()` 互斥、重复 close 幂等 |
| 关联 | 流与 `FileChannel` 双向关联，先关 channel 再关 fd，防重入 |
| 性能 | 单字节 `read()` 每字节一次系统调用，务必包 `BufferedInputStream` |

**一句记忆钩子**：*"read 返回 int 是为了给 -1 留位，close 上锁是为了给并发留命。"*

---

## 第二讲：缓冲与数据流 —— BufferedInputStream / DataInputStream

> **本讲问题**：加一层缓冲为什么能快一个数量级？`mark/reset` 在缓冲里怎么实现？`readUTF` 的"改长 UTF-8"长什么样？

### 2.1 BufferedInputStream：一块 buf 同时干三件事

```java
// java/io/BufferedInputStream.java:90-161
protected volatile byte[] buf;    // 缓冲数组（volatile：多线程可见）
protected int count;              // buf 中有效数据末尾（exclusive）
protected int pos;                // 下一个要读的下标
protected int markpos = -1;       // mark 位置，-1 表示无 mark
protected int marklimit;          // mark 后最多可读字节数
```

`fill():256` 是唯一的"下沉"动作——一次 `in.read(buf, 0, count)` 尽量填满：

```java
// java/io/BufferedInputStream.java:256-262
private void fill() throws IOException {
    byte[] buffer = getBufIfOpen();     // 流关闭则抛 IOException
    if (markpos < 0)
        pos = 0;                       // 无 mark：从头覆盖
    else if (pos >= buffer.length)     // 有 mark 且已到数组末尾
        if (markpos > 0) {             // 且有回退空间
            int sz = pos - markpos;
            System.arraycopy(buffer, markpos, buffer, 0, sz);  // 数据搬到头部
            pos = sz; markpos = 0;
        } else if (...) { /* 扩大数组或丢弃 mark */ }
    count = pos;
    int n = in.read(buffer, pos, buffer.length - pos);   // 一次批量读
    if (n > 0) count = n + pos;
}
```

**一次 `fill()` 把 N 次 `read(2)` 变成 1 次**——这就是"快"的全部秘密。代价是 8192 字节内存和"读多了"的语义（读文件时会把文件数据提前搬进用户态）。

`mark/reset`（`:467/:488`）完全在这块 buf 里玩指针游戏：`mark` 记录 `markpos = pos`；`reset` 把 `pos` 拨回 `markpos`；超限判定在 `read1():319` 里——`pos - markpos > marklimit` 时置 `markpos = -1`，之后 `reset` 抛 `IOException("Resetting to invalid mark")`。

**缓冲数组还能长大**：`getBufIfOpen(boolean allocateIfEmpty):179` 在需要时扩容（JDK 17+ 行为），避免"mark 后大数据把 mark 挤掉"。

### 2.2 DataInputStream：把基本类型从流里抠出来

`DataInputStream`（`java/io/DataInputStream.java`）的核心是一族 `readXxx`：

| 方法 | 行为 | 行号 |
|---|---|---|
| `readBoolean():158` | 读 1 字节，非 0 为 true | |
| `readInt():389` | 高字节在前（big-endian），四次 `read()` 拼 int | |
| `readLong():411` | 八次拼 long | |
| `readFloat():434` | `readInt` + `intBitsToFloat` | |
| `readUTF():554` | 改长 UTF-8（见下） | |
| `readFully(b):183` | 精确读满数组，中途 EOF 抛 `EOFException` | |

**`readUTF` 是面试重灾区**。它不是标准 UTF-8：

```java
// java/io/DataInputStream.java:554-674（节选）
public final String readUTF() throws IOException {
    int utflen = readUnsignedShort();     // 2 字节长度前缀（0~65535）
    ...
    // 每个字符 1~3 字节；\0 编成 0xC0 0x80（两字节！），
    // supplementary 字符按代理对拆成两个 3 字节序列
}
```

三个"和标准 UTF-8 不同"的点：**2 字节长度前缀**、`\u0000` 用两字节表示（所以能表达嵌 `\0` 的字符串）、**增补平面字符被拆成代理对**按 6 字节存。所以它只能和 `DataOutputStream.writeUTF:337` 配对使用，跨语言协议（JSON/Protobuf）不要碰它。

`readFully(b):183` 和 `read(b)` 的差异是面试常考：前者**要么读满要么抛 `EOFException`**（循环调 `read` 直到 `b.length`），后者可能短读返回。协议解析必须用 `readFully` 系列。

### 2.3 PushbackInputStream：流的"后悔药"

`PushbackInputStream`（`java/io/PushbackInputStream.java`）内部一个 `byte[] buf` + `pos`，`unread(b,off,len):229` 把字节塞回流头部，下次 `read` 先吐这些。典型用途：**解析时需要多看一个字节再决定**——如判断文件头是不是 GIF，读完 6 字节发现不是，`unread` 回去让上层按别的格式处理。

### 2.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `BufferedInputStream` = `InputStream` + `byte[] buf` + 三指针（pos/count/markpos） |
| 关键实现 | `fill():256` 一次批量读填满 buf，`mark/reset` 是 buf 内指针回退 |
| 并发 | `read/skip/available/mark/reset` 全部 `synchronized`（`:306` 起），多线程读同一缓冲流会串行化 |
| 协议 | `DataInputStream.readUTF:632` 是改长 UTF-8（2 字节前缀 + `\0` 两字节 + 代理对），非标准 UTF-8 |
| 陷阱 | `readFully` 才保证读满；`read` 会短读；`fill` 会提前消费上游数据 |

**一句记忆钩子**：*"缓冲流一块数组三指针：pos 走到 count 就 fill，mark 只是记个 markpos，超限就作废。"*

---

---

## 第三讲：字符流与编解码 —— StreamDecoder 与 BufferedReader

> **本讲问题**：字节怎么变成字符？为什么一次 `read` 能把中文"切成两半"而 `InputStreamReader` 不会乱码？`BufferedReader.readLine` 比 `Scanner` 快在哪？

### 3.1 InputStreamReader：只是个壳，内核是 StreamDecoder

```java
// java/io/InputStreamReader.java:69-70
public class InputStreamReader extends Reader {
    private final StreamDecoder sd;
}
```

**所有方法原样转发给 `sd`**（`read():174` → `sd.read()`，`close():197` → `sd.close()`）。真正的逻辑在 `sun.nio.cs.StreamDecoder`——它和 `java.nio` 共用一套编解码内核，这是"IO 流"和"NIO"在源码层最早的交汇点。

构造时立刻做一件**有副作用**的事：读流的前几个字节探测编码（BOM）。`InputStreamReader(InputStream):81` 直接调 `StreamDecoder.forInputStreamReader(in, this)`（`StreamDecoder.java:69`），内部按 charset 创建 `CharsetDecoder` 并**首次 `readBytes()`** 把字节灌进 `bb`。所以"先包 reader 再写流"会卡死——写端在等 reader 消费，reader 在等写端给字节。

### 3.2 双缓冲：字节有残、字符也有残

`StreamDecoder` 维护**两套残量**，这是跨 read 调用不乱码的关键：

```java
// sun/nio/cs/StreamDecoder.java:227-230
private final Charset cs;
private final CharsetDecoder decoder;
private final ByteBuffer bb;                 // 未解码完的字节（残字节）
// :63-64
private boolean haveLeftoverChar = false;    // 未消费的字符（残字符）
private char leftoverChar;
```

- **残字节 `bb`**：一次 `read(2)` 拿到的字节可能以半个中文字符结尾（UTF-8 一个汉字 3 字节，系统调用可能在 1 字节处切断），剩下的 1~2 字节留在 `bb`，下次 `readBytes():265` 续读后一起解；
- **残字符 `leftoverChar`**：`read()` 单字符 API 一次解码 2 个字符（因为 UTF-16 代理对必须成对处理），第二个存起来下次返回（`read0():123-148`）。

### 3.3 implRead：CoderResult 三态机

```java
// sun/nio/cs/StreamDecoder.java:298-336（节选）
int implRead(char[] cbuf, int off, int end) throws IOException {
    assert (end - off > 1);            // 至少要求能放 2 个字符（代理对）
    CharBuffer cb = CharBuffer.wrap(cbuf, off, end - off);
    boolean eof = false;
    for (;;) {
        CoderResult cr = decoder.decode(bb, cb, eof);   // :314 核心解码调用
        if (cr.isUnderflow()) {          // 字节不够解一个字符
            if (eof) break;
            if (!cb.hasRemaining()) break;
            if ((cb.position() > 0) && !inReady()) break;   // 不阻塞：有点收获就走
            int n = readBytes();          // :265 再从底层流捞字节
            if (n < 0) { eof = true; ... }
            continue;
        }
        if (cr.isOverflow()) { break; }   // 字符缓冲满了
        cr.throwException();              // MALFORMED / UNMAPPABLE → 抛异常
    }
    if (eof) { decoder.reset(); }         // flush：丢弃半字符并复位
    ...
}
```

三个 `CoderResult` 状态对应三种"暂停"：**UNDERFLOW = 缺字节**（去读）、**OVERFLOW = 缺空间**（返回）、**MALFORMED = 字节序列非法**（抛 `MalformedInputException`，如 GBK 字节按 UTF-8 解）。注意 `inReady()` 的判断（`:357`）：**已经解出一些字符且底层没就绪时立刻返回**，不干等——这是 `InputStreamReader` 不无故阻塞的原因。

`readBytes():265` 还有一个安全设计：`fillZeroToPosition():218` 在 close 路径上把 `bb` 里"已读但未解码"的字节清零——防止敏感数据（如密码）残留在堆内存里被 dump 出来。

### 3.4 BufferedReader：字符流的缓冲层

`BufferedReader`（`java/io/BufferedReader.java`）和 `BufferedInputStream` 同思路：一块 `char[] cb`（默认 8192 字符），`fill()` 从底层 Reader 批量读，`readLine():399` 在缓冲里扫 `\n`/`\r`。

```java
// java/io/BufferedReader.java:320
String readLine(boolean ignoreLF, boolean[] term) throws IOException {
    ...
    // 三种行终止符：\n、\r、\r\n（:337-395 内联扫描，不走正则）
}
```

`readLine` 的结束符处理比 `Scanner.nextLine` 精细：`\r\n` 被当成**一个**换行而不是两个；`term` 输出参数告诉调用方看到的是哪种。这也是它比 `Scanner` 快的原因——`Scanner` 每次 `next` 都走正则匹配和 `Pattern` 编译，`BufferedReader` 是纯字符比较。

### 3.5 写侧：OutputStreamWriter 与 StreamEncoder

写侧完全对称：`OutputStreamWriter` 持有 `StreamEncoder`（`sun/nio/cs/StreamEncoder.java`），`write(String,off,len)` → `implWrite` → `encoder.encode(cb, bb, eof)` → `writeBytes()` 批量 `write(2)`。`flush()` 时 `encoder.flush(bb)` 把 `writeBytes` 写完再 `out.flush()`——所以**字符流的 flush 比字节流多一层语义**（还有未编码完的字符缓冲要冲下去）。

### 3.6 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `InputStreamReader` 是壳，`StreamDecoder`（`sun/nio/cs`）是内核，与 NIO 共用 `CharsetDecoder` |
| 关键实现 | `implRead():298` 的 `CoderResult` 三态机：UNDERFLOW 去读、OVERFLOW 返回、MALFORMED 抛异常 |
| 并发 | `StreamDecoder` 全部方法 `synchronized (lock)`（`:123` 起），字符流本身线程安全但性能差 |
| 协议 | 跨 read 不乱码靠双残量：残字节 `bb` + 残字符 `leftoverChar` |
| 陷阱 | 构造 `InputStreamReader` 会立刻读流探测编码；`close` 路径 `fillZeroToPosition():218` 清零防泄漏 |

**一句记忆钩子**：*"字符流 = 字节缓冲 bb + 字符残量 leftover，解码三态：缺字节去读、缺空间返回、不合法就扔异常。"*

---

## 第四讲：File、RandomAccessFile 与 FileSystem 抽象

> **本讲问题**：`new File("a/b.txt")` 到底做了什么 native 调用？为什么 Windows 和 Linux 的路径分隔符能统一处理？`RandomAccessFile` 凭什么能"既读又写还能跳"？

### 4.1 File：一个跨平台路径的 Java 包装

`File`（`java/io/File.java`）**不持有 fd，也不是 IO 端点**——它只是"一个路径字符串 + 跨平台语义"，所有真实操作最终委托 native：

```java
// java/io/File.java:160
private static final FileSystem FS = DefaultFileSystem.getFileSystem();
```

`DefaultFileSystem`（`unix/classes/java/io/DefaultFileSystem.java:37`）在类加载时按 OS 返回 `new UnixFileSystem()` 或 `new WinNTFileSystem()`——**一次判断，全局复用**。`File` 的所有方法（`exists`、`length`、`list`、`mkdir`、`delete`…）都是 `FS.xxx(this)` 的一层转发，再进 native：

| File 方法 | UnixFileSystem 转发 | native |
|---|---|---|
| `getCanonicalPath():615` | `canonicalize0`（`UnixFileSystem.java:166`） | `realpath(3)` |
| `createNewFile():945` | `createFileExclusively` | `open(O_CREAT|O_EXCL)` |
| `delete():966` | `delete0` | `unlink(2)`/`rmdir(2)` |
| `list():1031` | `list0` | `opendir/readdir` |
| `lastModified():892` | `getLastModifiedTime0`（`UnixFileSystem.java:201`） | `stat(2)` |

**路径规范化**（`UnixFileSystem.normalize:93`）处理 `//`、`/./`、`..` 等，是纯字符串操作，不碰文件系统；`resolve(String,String):118` 把父子路径拼起来（注意 Windows 侧会加盘符判断，`WinNTFileSystem.java:130`）。这就是跨平台路径统一的原因：**差异全部封在 `FileSystem` 实现类里**。

一个常见误解：`File.exists()` 不是"问内核"，而是每次调用都是 `stat(2)`（`exists():786` → `FS.exists` → native）——**高频循环里调 `exists` 是性能杀手**，且存在 TOCTOU 竞态（查到存在、用时已被删）。

### 4.2 RandomAccessFile：一个 fd，两种方向，自由光标

`RandomAccessFile`（`java/io/RandomAccessFile.java`）的独特之处在**它不继承 `InputStream`/`OutputStream`**，自成一体：

```java
// java/io/RandomAccessFile.java:130
public RandomAccessFile(String pathname, String mode) throws FileNotFoundException {
    ...
    // mode: "r" / "rw" / "rws" / "rwd"
    //  rws = 每次写都同步内容+元数据（O_SYNC 风格）
    //  rwd = 每次写只同步内容（fdatasync 风格）
}
```

四个 native 原语（`RandomAccessFile.java`）：

| Java | native | 作用 |
|---|---|---|
| `open0(name,mode):322` | `open(2)` | 按 mode 组合 `O_RDONLY/O_RDWR/O_SYNC/O_DSYNC` |
| `seek0(pos):678` | `lseek(2)` | 移动文件光标 |
| `readBytes0(b,off,len):398` | `pread/read` | 从当前光标读 |
| `writeBytes0(b,off,len):619` | `pwrite/write` | 从当前光标写 |

它还有 `getFilePointer():655`（`lseek(0,SEEK_CUR)`）、`setLength(newLength):718`（`ftruncate`）、`getFD():269`。`readFully(b):476` 循环读到满（和 `DataInputStream.readFully` 同样语义）。

**典型用法**：读大文件的某一段（`seek(offset)` + `read(buf)` 只读需要的位置，不用把整个文件读进内存）、追加写日志、实现简易数据库的页读取。

### 4.3 FileDescriptor：native fd 的 Java 句柄

`FileDescriptor`（`java/io/FileDescriptor.java`）本身几乎没有逻辑，就是 fd 号的持有者：

```java
// java/io/FileDescriptor.java:151-168
public static final FileDescriptor in  = new FileDescriptor(0);   // stdin
public static final FileDescriptor out = new FileDescriptor(1);   // stdout
public static final FileDescriptor err = new FileDescriptor(2);   // stderr
```

三个静态常量在 `<clinit>` 里初始化，这就是 `System.in/out/err` 的底层来源。`sync():209` 对应 `fsync(2)`（对 fd 调用）。类里的 `set`/`get` 静态内部类（`:69-111`）是 JDK 内部偷改 fd 的后门（`FileInputStream.setFD` 给 `System.in` 换流用），业务代码用不到。

### 4.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `File` = 路径字符串 + `FileSystem` 策略（Unix/WinNT 两套实现），无 fd |
| 关键实现 | `File` 每个方法转 native 系统调用（canonicalize→realpath、list→readdir、exists→stat） |
| 并发 | `File.exists` 每次 stat，高频调用有 TOCTOU 竞态；`RandomAccessFile` 的 `seek+read` 不是原子的 |
| 协议 | `RandomAccessFile` 四模式 `r/rw/rws/rwd`，同步强度递增；不继承流家族 |
| 陷阱 | `File` 的 `getCanonicalPath` 会解软链、`getAbsolutePath` 不会；`exists()` ≠ `canOpen()` |

**一句记忆钩子**：*"File 只是路径，fd 在流里；RandomAccessFile 是一个 fd 既读又写还能 lseek。"*

---

---

## 第五讲：NIO 三件套 —— Buffer / FileChannel / Selector

> **本讲问题**：`flip` 到底翻转了什么？`FileChannel.map` 为什么叫"零拷贝"？Selector 用一次 `poll(2)` 管一千个连接的内部结构长什么样？

### 5.1 Buffer：三个指针的不变式

```java
// java/nio/Buffer.java:217（注释）
// Invariants: mark <= position <= limit <= capacity
```

所有 Buffer 子类（`ByteBuffer`/`CharBuffer`/`IntBuffer`…`MappedByteBuffer`）都遵守这个不变量，四个方法的语义必须闭上眼睛都能背：

| 方法 | 行号 | 效果 |
|---|---|---|
| `clear():455` | | `position=0, limit=capacity, mark=-1` → **切回写模式** |
| `flip():484` | | `limit=position, position=0, mark=-1` → **写完切读模式** |
| `rewind():507` | | `position=0, mark=-1`（不动 limit）→ 重读 |
| `compact()` | | 未读数据搬到开头，`position=剩余长度, limit=capacity` → 半写半读时用 |

`position()`/`limit()` 都是 `final` 方法（`:307` 起），子类**不能改语义**。读越界检查走 `checkIndex(i, nb):777` → `Preconditions.checkIndex`，`BufferUnderflowException` 从这里抛。

**堆内 vs 堆外**：`HeapByteBuffer` 底层 `byte[]`（分配快、GC 管）；`DirectByteBuffer` 底层 `unsafe.allocateMemory` 的堆外内存（IO 快，无内核态拷贝，但要手工/靠 Cleaner 释放，受 `-XX:MaxDirectMemorySize` 控制）。`hasArray()` 对 Direct 永远 false，`array()` 直接抛异常——这是 NIO 新手最常踩的坑。

### 5.2 FileChannel：六个原语覆盖全部文件 IO

`FileChannelImpl`（`sun/nio/ch/FileChannelImpl.java`，1774 行）是文件通道的唯一实现，核心方法：

| 方法 | 行号 | 系统调用 | 说明 |
|---|---|---|---|
| `read(ByteBuffer):227` | | `read(2)`/`pread` | 从光标读，返回读到的字节数 |
| `write(ByteBuffer):331` | | `write(2)`/`pwrite` | 从光标写，推进光标 |
| `read(dst,position):1148` | | `pread` | **绝对定位读**，不动共享光标 |
| `write(src,position):1212` | | `pwrite` | **绝对定位写** |
| `map(mode,pos,size):1412` | | `mmap(2)` | 内存映射，返回 `MappedByteBuffer` |
| `force(metaData):633` | | `fsync(2)` | 刷盘；`false`=只数据 |
| `lock(pos,size,shared):1669` | | `fcntl(F_SETLK)` | 文件锁 |
| `transferTo(pos,cnt,target):934` | | `sendfile(2)` | 通道间搬运，尽量零拷贝 |

**`map` 的内部动作**（`:1412` 起）：`map0` native 调 `mmap` 拿地址 → 包成 `DirectByteBuffer` 子类 `MappedByteBuffer` → **注册 `DefaultUnmapper` 到 `Cleaner`**（`:1341`）：GC 回收这个 Buffer 时才 `munmap`。这就是"MMap 大文件可能 OOM/fd 泄漏"的根源——**映射不主动释放，靠 GC 不确定**。JDK 里正确的用法是 `map` 完立刻 `buffer.cleaner().clean()`（hack）或用 JDK 19+ 的 `Arena`/`MemorySegment` 作用域 API。

**`transferTo` 的两级策略**（`:806` `transferToDirect` → `:789` `transferToDirectInternal`）：目标也是 `FileChannel` 时走 `sendfile`（数据直接在内核态从源 fd 到目标 fd，**一次系统调用不过用户态**，Kafka/Netty 零拷贝下载就是靠它）；否则退化成"读进 DirectBuffer 再写出去"。

### 5.3 Selector：一根管道唤醒的一千个连接

Selector 家族三层：

```
SelectorImpl（抽象，sun/nio/ch/SelectorImpl.java:52）
   │  ├─ keys / selectedKeys / publicSelectedKeys / cancelledKeys（:56-66）
   │  ├─ select(timeout):139 / selectNow():151 / select(action):156
   │  ├─ lockAndDoSelect(action, timeout):120  ← 加 publicSelectedKeys 锁再调 doSelect
   │  └─ abstract doSelect(action, timeout):117  ← 平台差异全在这里
   ▼
PollSelectorImpl（Linux，unix/classes/sun/nio/ch/PollSelectorImpl.java）
   │  ├─ pollArray: AllocatedNativeObject ← 本地内存 struct pollfd 数组（:53）
   │  ├─ fd0 / fd1: 自建管道两端（:56-57）
   │  ├─ pollKeys: pollfd 槽位 → SelectionKeyImpl（:60）
   │  ├─ updateKeys: 待生效的兴趣集合（:64）
   │  └─ doSelect(action, timeout):97
```

`doSelect`（`:97-141`）四步：

1. `processUpdateQueue():143` —— 把 `updateKeys` 里排队的注册/兴趣变更刷进 `pollArray`（为什么需要延迟？`register` 时 poll 可能正持有锁，刷进去会破坏数组一致性）；
2. `poll(pollArray.address(), pollArraySize, to)`（`:384` native `poll(2)`）—— **一次系统调用等所有 fd**，`Blocker.begin/end` 包住以便被中断时能退出；
3. 被信号打断（`INTERRUPTED`）且是定时 poll → 扣掉已耗时间重试（`:121-131`）；
4. `processEvents(action):172` —— 扫 pollArray 的 revent 字段，算出就绪的 `SelectionKey`，加进 `selectedKeys`。

**`wakeup()` 的机关**（`:240`）：往 fd1 管道写 1 字节。读端 fd0 也注册在 pollArray 里，于是阻塞的 `poll` 立刻返回 → `select` 返回 → 调用方复查。这根管道是每个 Selector 一条（`INITIAL_CAPACITY = 16` 个 pollfd 起步，`:48`）。

**三个高频坑**（源码里都能看到）：

- `selectedKeys` 集合用完后**必须 `iterator.remove()`**，否则下一轮 `select` 还带着旧 key；
- `register` 之后不会立刻生效——兴趣变更排在 `updateKeys`，下次 `select` 才进 pollArray；
- `select()` 返回 0 不一定超时：可能是 `wakeup` 的字节或纯注册变更，所以业务循环不能假设"返回>0 一定有 IO"。

### 5.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | `Buffer`（`mark<=position<=limit<=capacity`，`java/nio/Buffer.java:217`）+ `FileChannelImpl` + `SelectorImpl/PollSelectorImpl` |
| 关键实现 | `map:1412` = mmap + Cleaner 兜底；`doSelect:97` = processUpdateQueue → `poll(2)` → processEvents |
| 并发 | `lockAndDoSelect:120` 持 `publicSelectedKeys` 锁；`wakeup` 靠自建管道 fd0/fd1（`:56-57`） |
| 协议 | 绝对定位 read/write 用 `pread/pwrite`，不碰共享光标——多线程同通道安全的前提 |
| 陷阱 | `selectedKeys` 要手动 remove；DirectBuffer 无 `array()`；`map` 不主动 unmap 会泄漏 |

**一句记忆钩子**：*"flip 切读、clear 切写、compact 半读半写；Selector 是一条自建管道把阻塞的 poll 叫醒。"*

---

## 第六讲：序列化与管道 —— ObjectInputStream / PipedInputStream

> **本讲问题**：同一个对象写两次为什么第二次只有几字节？`readObject` 靠什么把字节还原成对象图？管道流为什么会死锁？

### 6.1 序列化协议：TC_* 字节码流

`ObjectOutputStream`（`java/io/ObjectOutputStream.java`）写的不是"对象"，是一条带类型标签的字节流。核心分派在 `writeObject0`：

```java
// java/io/ObjectOutputStream.java:1006-1015（节选）
private void writeObject0(Object obj, boolean unshared) throws IOException {
    boolean oldMode = bout.setBlockDataMode(false);
    depth++;                                        // 递归深度保护
    try {
        if ((obj = subs.lookup(obj)) == null) { writeNull(); return; }
        else if (!unshared && (h = handles.lookup(obj)) != -1) {
            writeHandle(h);                         // ★ 同一个对象第二次出现：只写句柄
            return;
        }
        ...
        // 剩余情况按类型分派：
        if (obj instanceof String)        writeString(...);        // :1072
        else if (cl.isArray())            writeArray(...);         // :1075
        else if (obj instanceof Enum)     writeEnum(...);          // :1078
        else if (obj instanceof Serializable) writeOrdinaryObject(...);  // :1081
        else throw new NotSerializableException(cl.getName());
    } finally { depth--; bout.setBlockDataMode(oldMode); }
}
```

`writeHandle`:1105 只写两个东西：`TC_REFERENCE` 标签 + `baseWireHandle + handle`（一个 int）。**这就是"循环引用也能序列化、同一个对象只写一次"的全部机制**。

句柄表在 `HandleTable`（`:2067`）：

```java
// java/io/ObjectOutputStream.java:2098
int assign(Object obj) { ... }   // 给对象分配 handle（从 0xff71 起，:190 附近常量）
// :2222
Object lookup(Object obj) { ... } // 查对象是否已分配
```

写侧顺序：`writeOrdinaryObject:1295` → `writeClassDesc:1121`（类元数据，也走 handles.assign `:1143`）→ `writeNonProxyDesc:1164`（类名/SUID/字段数/字段描述）→ `defaultWriteObject`（非 transient 字段值）。

### 6.2 读侧：readObject0 的分派表

`readObject0`（`java/io/ObjectInputStream.java:1552`）是镜像：按 `bin.peekByte()` 拿到的 TC 标签分派——`TC_NULL`/`TC_REFERENCE`（`readHandle`，`:1738` `handles.lookupObject`）/`TC_CLASS`/`TC_CLASSDESC`/`TC_STRING`/`TC_ARRAY`（`readArray:1983`）/`TC_ENUM`/`TC_OBJECT`（`readOrdinaryObject:2094`）。

`readOrdinaryObject:2094` 的核心动作：查 `ObjectStreamClass` 缓存（本地 `Caches`，`:257`）→ 反射创建实例（`getSerializableConstructor:1318`，无参构造）→ 按字段描述逐个读值写字段。`readObject` 开头的块数据检查（`:1553-1568`）处理"自定义 `writeObject` 里写的块数据"边界（`TC_ENDBLOCKDATA`），这也是 `OptionalDataException` 的来源。

**安全**：反序列化本质是"执行字节码指定的对象构造"。JDK 9+ 的 `ObjectInputFilter`（`setObjectInputFilter:1281`）可按类名/深度/引用数白名单过滤，生产环境**必须配**。

### 6.3 PipedInputStream：1024 字节的循环缓冲

`PipedInputStream`（`java/io/PipedInputStream.java`）不是线程安全容器，而是**跨线程信箱**：

```java
// java/io/PipedInputStream.java:61-64
Thread readSide;                          // 当前读线程
Thread writeSide;                         // 当前写线程
private static final int DEFAULT_PIPE_SIZE = 1024;   // 循环缓冲大小
```

`receive(int b):207`（写端调）：`writeSide = Thread.currentThread()`，缓冲满则 `awaitSpace():273` —— `notifyAll()` 后 `wait(1000)`，**带超时**地等读端腾地方（超时是为了防"读端死了没人 notify"永久挂起）。读端 `read():310` 反过来 `notifyAll()` 叫醒写端。

**死锁的经典场景**：单线程里"先写 2000 字节再读"——写满 1024 后 `awaitSpace` 阻塞，而读的代码在同一线程后面，永远执行不到。**管道必须两个线程**，这是 `PipedInputStream` 文档开头就警告的。

### 6.4 本讲小结

| 维度 | 结论 |
|---|---|
| 结构 | 序列化 = TC_* 标签流 + `HandleTable` 句柄表（`ObjectOutputStream.java:2067`） |
| 关键实现 | `writeObject0:1006` 按类型分派；第二次遇到同一对象只写 `TC_REFERENCE`+handle（`writeHandle:1105`） |
| 并发 | `PipedInputStream` 用 `readSide/writeSide` 记录对端线程，`awaitSpace:273` 带 1s 超时 wait |
| 协议 | 只序列化非 static 非 transient 字段；`serialVersionUID` 必须显式声明 |
| 陷阱 | 反序列化是安全重灾区（配 `ObjectInputFilter`）；管道单线程使用必死锁 |

**一句记忆钩子**：*"句柄表让循环引用不死、同对象只写一次；管道是 1024 字节信箱，写满就等、读空就 notify，单线程用必锁死。"*

---

## 附：学习路线与复习卡汇总

### 建议学习顺序（20 讲时）

| 阶段 | 内容 | 时长建议 | 产出 |
|---|---|---|---|
| 第 1 天 | 第一、二讲（字节流 + 缓冲/数据流） | 3h | 能手写一个"按行读文件 + 统计"不调 read() 单字节 |
| 第 3 天 | 第三讲（字符流/编解码） | 2h | 解释一次 UTF-8 跨 read 乱码故障 |
| 第 7 天 | 第四讲（File/RAF） | 2h | 用 RAF 实现大文件分页读 |
| 第 14 天 | 第五讲（NIO 三件套） | 4h | 写出 Buffer flip 三部曲 + Selector echo server |
| 第 30 天 | 第六讲（序列化/管道） | 3h | 说清句柄表与反序列化过滤方案 |

### 复习卡（遮答案自测）

1. `read()` 为什么返回 `int`？`-1` 表示什么？
2. `close()` 的双检锁 + `closeLock` 各自防什么？
3. `fill()` 里 mark 相关的三种分支（`markpos<0` / `pos>=buf.length` 且 `markpos>0` / 其他）分别做什么？
4. `readUTF` 和标准 UTF-8 的三个差异？
5. `InputStreamReader` 构造时为什么会读流？
6. `CoderResult` 三态各触发什么动作？
7. `File.exists()` 的底层调用和竞态？
8. `"rws"` 和 `"rwd"` 的差异？
9. Buffer 的 `flip/clear/compact` 各把指针拨成什么样？
10. `map` 出来的 `MappedByteBuffer` 什么时候释放？怎么主动释放？
11. Selector 的 `wakeup` 靠什么？`register` 后为什么下次 select 才生效？
12. 同一个对象序列化两次，第二次写什么？
13. `serialVersionUID` 不声明会怎样？
14. `PipedInputStream` 单线程使用为什么死锁？
15. `readFully` 和 `read(byte[])` 的语义差异？

### 六讲记忆钩子串烧

*"read 返回 int 是为了给 -1 留位，close 上锁是为了给并发留命"* → *"缓冲流一块数组三指针"* → *"字符流 = bb 残字节 + leftover 残字符，解码三态"* → *"File 只是路径，fd 在流里"* → *"flip 切读、clear 切写；一条自建管道叫醒 poll"* → *"句柄表让循环引用不死；管道是 1024 字节信箱，单线程用必锁死"*

---

## 涉及文件索引

| 文件 | 行数 | 本笔记引用次数 |
|---|---|---|
| `java/io/InputStream.java` | 900+ | 高（read 合同、skip/transferTo/readAllBytes） |
| `java/io/FileInputStream.java` | 584 | 高（native 三档、close 双检） |
| `java/io/FileOutputStream.java` | 400+ | 中 |
| `java/io/BufferedInputStream.java` | 520+ | 高（fill/mark/reset） |
| `java/io/DataInputStream.java` | 674 | 高（readInt/readUTF/readFully） |
| `java/io/DataOutputStream.java` | 200+ | 中（writeUTF） |
| `java/io/PushbackInputStream.java` | 300+ | 低 |
| `java/io/InputStreamReader.java` | 210 | 中 |
| `java/io/BufferedReader.java` | 700+ | 中（readLine） |
| `sun/nio/cs/StreamDecoder.java` | 400+ | 高（implRead 三态机） |
| `java/io/File.java` | 2500+ | 中 |
| `java/io/RandomAccessFile.java` | 1000+ | 高（open0/seek0） |
| `java/io/FileDescriptor.java` | 230 | 低 |
| `unix/classes/java/io/UnixFileSystem.java` | 500+ | 中 |
| `unix/classes/java/io/DefaultFileSystem.java` | 40 | 低 |
| `java/nio/Buffer.java` | 800+ | 高（flip/clear/compact） |
| `sun/nio/ch/FileChannelImpl.java` | 1774 | 高（map/force/transferTo） |
| `sun/nio/ch/SelectorImpl.java` | 324 | 中 |
| `unix/classes/sun/nio/ch/PollSelectorImpl.java` | 389 | 高（doSelect/wakeup） |
| `java/io/ObjectOutputStream.java` | 2300+ | 高（writeObject0/HandleTable） |
| `java/io/ObjectInputStream.java` | 2800+ | 高（readObject0 分派） |
| `java/io/PipedInputStream.java` | 454 | 中（receive/awaitSpace） |
