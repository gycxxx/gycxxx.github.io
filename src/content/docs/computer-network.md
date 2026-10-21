---
title: "计算机网络源码深度学习笔记"
date: 2026-10-21
categories:
  - "计算机网络"
tags:
  - "TCP"
  - "HTTP"
  - "HTTPS"
  - "DNS"
  - "网络"
  - "源码"
---

> 基于 `src/java.base/share/classes/java/net/`（`Socket`、`ServerSocket`、`DatagramSocket`、`InetAddress`、`HttpURLConnection`、`URI`/`URL`、`URLEncoder`、`HttpCookie`、`CookieManager`）、`sun/net/www/`（`KeepAliveCache`、`ChunkedOutputStream`）、`javax/net/ssl/` 以及 `src/java.base/linux/classes/sun/nio/ch/EPollSelectorImpl.java` 的精读整理而成。
>
> 阅读顺序即学习顺序：**分层与封装 → TCP → UDP 与 Socket API → HTTP/HTTPS → DNS 与工程化（含 IO 多路复用）**。
>
> 所有结论均标注了源文件位置（格式 `src/java.base/share/classes/java/net/xxx.java:行号`），建议对照代码阅读。
>
> **版本基线**：OpenJDK 主线 commit `6c48f4ed`，commit 时间 `2025-08-12 17:15:36 +0000`。
>
> 协议本身（RFC 行为）没有"源码"可引，凡是协议规则部分均为规范描述；凡是能落到 JDK 实现上的，都挂了真实行号——这也是本文的取舍：**用 Java 侧的 API 行为去反证协议行为**。NIO/Selector 的更细内容见 `java-io.md`。

---

## 〇、全景地图

```
┌──────── 七层 OSI ────────┐   ┌──────── TCP/IP 四层 ────────┐   ┌──── PDU ────┐
│ 7 应用层                 │   │                             │   │  报文/数据   │
│   HTTP / HTTPS / DNS     │──▶│ 应用层  HttpURLConnection    │   │  Message    │
│ 6 表示层  TLS/SSL/编码    │   │        CookieManager/SSL     │   │             │
│ 5 会话层                 │   │                             │   │             │
├──────────────────────────┤   ├─────────────────────────────┤   ├─────────────┤
│ 4 传输层                 │   │ 传输层  Socket(SocketImpl)   │   │ 段 Segment  │
│   TCP / UDP              │──▶│        DatagramSocket        │   │ 数据报       │
├──────────────────────────┤   ├─────────────────────────────┤   ├─────────────┤
│ 3 网络层  IP / ICMP       │──▶│ 网际层  InetAddress / IP      │   │ 包 Packet   │
├──────────────────────────┤   ├─────────────────────────────┤   ├─────────────┤
│ 2 数据链路层 以太网/ARP   │──▶│ 链路层  （网卡驱动，JDK 不触达）│   │ 帧 Frame    │
│ 1 物理层                 │   │                             │   │ 比特         │
└──────────────────────────┘   └─────────────────────────────┘   └─────────────┘
```

**封装 = 加头**：应用数据 → 加 TCP/UDP 头 → 加 IP 头 → 加以太网头尾 → 发出去；收端反向剥头。**每一层只看自己那一段头**，这就是分层的本质。

一句话主线：**TCP 负责"可靠"，IP 负责"可达"，HTTP 负责"语义"**。面试里所有网络题基本都能归到这三件事上。

---

## 第一讲 分层与封装

### 1.1 为什么要分层

- **解耦**：换链路层（以太网→WiFi→5G）不用改 HTTP
- **复用**：IP 之上可以跑 TCP 也可以跑 UDP
- **可诊断**：抓包时按层看（arp / ip / tcp / http 过滤器）

### 1.2 三个必记的"层 ↔ 地址"对应

| 层 | 寻址依据 | 典型设备 |
|---|---|---|
| 网络层 | IP 地址 | 路由器 |
| 数据链路层 | MAC 地址 | 交换机 |
| 传输层 | 端口号（+IP 组成四元组） | — |

**四元组**（源 IP、源端口、目的 IP、目的端口）唯一确定一条 TCP 连接——这是 socket 的本质，也是"一台机器最多能开多少连接"问题的起点。

---

## 第二讲 TCP：可靠是怎么做到的

### 2.1 三次握手：为什么不是两次

```
客户端                                     服务端
  |---- SYN  (seq=x)               ------->|   客户端：我要连，我的序号从 x 开始
  |<------- SYN+ACK (seq=y,ack=x+1) -------|   服务端：收到，我的序号从 y 开始
  |---- ACK (ack=y+1)              ------->|   客户端：收到你的序号
  状态：ESTABLISHED                        状态：ESTABLISHED
```

**两次不够的关键原因**：两次握手只能确认"客户端→服务端"的序列号，服务端无法确认自己的初始序号被客户端收到。若失效的旧 SYN 重传到服务端，两次握手会直接建立一条**幽灵连接**并分配资源；三次握手下，旧连接请求因客户端不会回 ACK 而作废。

### 2.2 四次挥手与 TIME_WAIT

```
主动方                                     被动方
  |---- FIN (seq=u)              -------->|   我说完了
  |<------- ACK (ack=u+1)         --------|   知道了（被动方可能还有数据要发）
  |<------- FIN (seq=v)           --------|   我也说完了
  |---- ACK (ack=v+1)            -------->|   知道了
  进入 TIME_WAIT（2MSL，通常 1~4 分钟）        收到后 CLOSE
```

- **为什么四次**：TCP 是全双工，两个方向要分别关闭；被动态的 ACK 和 FIN 通常不能合并（中间还要把剩余数据发完）
- **TIME_WAIT 的两个作用**：① 保证最后一个 ACK 能到达（丢了的话对方会重发 FIN）② 让本连接的残余报文在网络中消散，避免污染新连接
- **大量 TIME_WAIT 的处理**：主动关闭方才进 TIME_WAIT。服务器侧应尽量避免主动关闭；开启 `SO_REUSEADDR`（`src/java.base/share/classes/java/net/ServerSocket.java:770`）可复用处于 TIME_WAIT 的地址，否则重启会 `BindException: Address already in use`

### 2.3 Java 侧能看到的 TCP 细节

| 场景 | API | 位置 |
|---|---|---|
| 建连（触发三次握手） | `Socket.connect(endpoint, timeout)` | `src/java.base/share/classes/java/net/Socket.java:639` |
| 无超时版建连 | `Socket.connect(endpoint)` | `:596` |
| 半连接/全连接队列长度 | `ServerSocket.bind(endpoint, backlog)` / `new ServerSocket(port, backlog)` | `src/java.base/share/classes/java/net/ServerSocket.java:310` / `:177` |
| 关闭行为（四次挥手） | `Socket.setSoLinger(on, linger)` | `src/java.base/share/classes/java/net/Socket.java:1153` |
| 读超时 | `Socket.setSoTimeout(timeout)` | `:1273` |
| 小包合并（Nagle）开关 | `Socket.setTcpNoDelay(boolean)` | `:1116` |
| 心跳探活 | `Socket.setKeepAlive(boolean)` | `:1431` |
| 地址复用 | `ServerSocket.setReuseAddress(boolean)` | `src/java.base/share/classes/java/net/ServerSocket.java:770` |

几个直接能答的追问：

- **`backlog` 是干什么的**：服务端 `listen` 的队列长度。JDK 里透传给 `bind`（`:310`）。队列满后新连接被丢弃/拒连，客户端表现为连接超时或 RST
- **`SO_LINGER` 的作用**：默认 `close()` 立即返回（数据由内核继续发）；设置 `SO_LINGER` 后 `close()` 会**阻塞直到数据发完或超时**；设为 0 则直接发 RST（异常关闭，丢弃缓冲数据）——这就是"四次挥手 vs RST"的分界点（`:1153`）
- **`TCP_NODELAY`**：关掉 Nagle 算法。Nagle 会把小包攒起来再发（省带宽、增延迟），交互型应用（RPC、游戏、终端）应 `setTcpNoDelay(true)`（`:1116`）
- **`SO_TIMEOUT` 不是连接超时**：它只作用于 `read()`，建连超时要用 `connect(endpoint, timeout)`（`:639`）

### 2.4 可靠传输的四根支柱

1. **序列号 + 确认号**：每个字节都有序号，ACK 表示"这之前的都收到了"（累积确认）
2. **超时重传**：RTO 动态计算（基于 RTT 采样），超时未 ACK 就重发
3. **滑动窗口**：接收方通告窗口（rwnd）做**流量控制**，防止把接收方冲垮
4. **拥塞控制**：发送方自己维护 cwnd，经典四阶段——**慢启动（指数增长）→ 拥塞避免（线性增长）→ 快重传（收到 3 个重复 ACK 立刻重传）→ 快恢复（不回到慢启动，直接从新阈值线性爬）**

> 区分清楚：**流量控制是"接收方扛不扛得住"，拥塞控制是"网络扛不扛得住"**。

### 2.5 TCP 粘包/拆包

TCP 是**字节流**，没有消息边界；UDP 是**数据报**，有边界。粘包的根源是字节流 + 缓冲区合并/Nagle，解决办法固定三件套：① 定长消息 ② 分隔符 ③ **长度字段（最常用，如 HTTP 的 `Content-Length`/chunked）**。

---

## 第三讲 UDP 与 Socket API

### 3.1 UDP 是什么

无连接、不重传、不排序、无拥塞控制，**只加 8 字节头**（源端口/目的端口/长度/校验和）。所以它快、它简单、它可能丢。

适合：音视频（丢一帧无所谓，延迟敏感）、DNS 查询、广播/多播、QUIC（在 UDP 上自己实现可靠）。

### 3.2 Java 侧

```java
DatagramSocket ds = new DatagramSocket(9999);
ds.connect(addr, port);   // src/java.base/share/classes/java/net/DatagramSocket.java:414  仅"默认对端"，不建连
ds.send(packet);          // :592
ds.setBroadcast(true);    // :908  开启广播（UDP 才有）
```

注意 `UDP 的 connect` 只是绑定默认地址（过滤来源、省去每次填地址），**不产生任何握手**。

### 3.3 Socket 编程最小骨架（TCP）

```java
try (ServerSocket ss = new ServerSocket(8080, 128);   // backlog=128
     Socket s = ss.accept()) {
    s.setTcpNoDelay(true);
    s.setSoTimeout(30_000);
    InputStream in = s.getInputStream();
    ...
}
```

---

## 第四讲 HTTP 与 HTTPS

### 4.1 请求/响应报文

```
请求行：GET /api/user HTTP/1.1         状态行：HTTP/1.1 200 OK
请求头：Host / Accept / Cookie ...       响应头：Content-Type / Set-Cookie ...
空行                                    空行
请求体（GET 通常没有）                    响应体
```

### 4.2 方法与状态码

- 方法：`GET`（安全、幂等、可缓存）、`POST`（非幂等）、`PUT`（幂等）、`DELETE`（幂等）、`HEAD`、`PATCH`、`OPTIONS`
- 状态码速记：`1xx` 继续 / `2xx` 成功（200、201 Created、204 No Content）/ `3xx` 重定向（**301 永久、302 临时、304 未修改走缓存**）/ `4xx` 客户端错（**400 参数错、401 未认证、403 无权限、404 不存在、405 方法不允许**）/ `5xx` 服务端错（**500、502 网关坏、503 不可用、504 网关超时**）

### 4.3 长连接与连接池（Java 侧真凭实据）

HTTP/1.1 默认 `Connection: keep-alive`。JDK 的实现在 `KeepAliveCache`：

```java
public class KeepAliveCache ...                // src/java.base/share/classes/sun/net/www/http/KeepAliveCache.java:49
static final int MAX_CONNECTIONS = 5;          // :85  默认每目标最多 5 条空闲长连接
result = Integer.getInteger("http.maxConnections", MAX_CONNECTIONS);  // :89  可调
public void put(final URL url, Object obj, HttpClient http)  // :114  归还连接
public HttpClient get(URL url, Object obj)                   // :202  取连接
```

也就是说 `HttpURLConnection` 自带一个**每 host 上限 5** 的连接池，超出就新建/关闭。生产环境应换 Apache HttpClient / OkHttp，它们有更完善的池化与超时控制。

`disconnect()`（`src/java.base/share/classes/sun/net/www/protocol/http/HttpURLConnection.java:2698`）会真正关闭连接，**不归还连接池**——想复用连接就不要调它。

### 4.4 chunked 分块传输

响应体长度未知时无法写 `Content-Length`，改用 `Transfer-Encoding: chunked`（每块带十六进制长度，末尾 0 长度块结束）：

```java
protected int chunkLength = -1;                                    // src/java.base/share/classes/java/net/HttpURLConnection.java:70
public void setChunkedStreamingMode(int chunklen)                  // :280
```

发大文件时应显式设置 chunked 或 fixed length；否则 JDK 会把整个 body 缓冲在内存里再发（`HttpURLConnection.java:672`、`:679`、`:1180` 的 `streaming()` 分支就是干这个判断的）——**这是"上传大文件 OOM"的经典成因**。

### 4.5 Cookie

```java
private String domain;           // src/java.base/share/classes/java/net/HttpCookie.java:72
public boolean hasExpired()      // :238
public long getMaxAge()          // :421
public CookieManager(CookieStore store, CookiePolicy cookiePolicy)  // src/java.base/share/classes/java/net/CookieManager.java:152
policyCallback = (cookiePolicy == null) ? CookiePolicy.ACCEPT_ORIGINAL_SERVER  // :156
```

- Cookie 靠请求头 `Cookie` / 响应头 `Set-Cookie` 传递，属性：`Domain`/`Path`/`Max-Age`/`Expires`/`HttpOnly`/`Secure`/`SameSite`
- `HttpOnly` 防 JS 读取（防 XSS 窃取）、`Secure` 只走 HTTPS、`SameSite=Lax/Strict` 防 CSRF
- JDK 默认策略 `ACCEPT_ORIGINAL_SERVER`（`:156`）：只接受"响应来源主机"的 cookie，且默认存内存（`InMemoryCookieStore`）

### 4.6 HTTPS = HTTP over TLS

TLS 握手要点（1.2）：ClientHello（支持的套件/随机数）→ ServerHello + 证书 → 客户端验证证书链 + 域名 → 用证书公钥加密/签名交换出**对称会话密钥** → 之后全部走对称加密。

**为什么既用非对称又用对称**：非对称安全地协商密钥（但慢），对称加密大量数据（快）。

Java 侧两个关键点：

```java
public static SSLSocketFactory getDefaultSSLSocketFactory()  // src/java.base/share/classes/javax/net/ssl/HttpsURLConnection.java:315
public void setHostnameVerifier(HostnameVerifier v)          // :257
```

> **严禁**在生产里写"信任所有证书 / 关掉主机名校验"——那等于把 HTTPS 降级成明文。证书校验 + 主机名校验是 TLS 防中间人的最后一道防线。

HTTP/2：多路复用（一条连接并发多个流，解决队头阻塞）、头部压缩 HPACK、二进制分帧、服务端推送。HTTP/3：把 TCP 换成 **QUIC（基于 UDP）**，在用户态实现可靠与拥塞控制，彻底解决 TCP 队头阻塞与握手延迟。

---

## 第五讲 DNS、URL 与工程化

### 5.1 DNS 解析与 JDK 缓存

```
浏览器缓存 → 操作系统缓存/hosts → 本地 DNS 服务器（递归）→ 根 → 顶级域 → 权威 DNS（迭代）
```

JDK 侧有独立缓存，由安全属性控制（文档就在 `InetAddress` 头部）：

```
networkaddress.cache.ttl         // src/java.base/share/classes/java/net/InetAddress.java:197   正向解析缓存秒数
networkaddress.cache.negative.ttl（默认 10）  // :224  失败结果缓存 10 秒
```

`InetAddress.getAllByName0(host)`（`:779`）是真正的解析入口。**坑**：JVM 默认缓存成功的 DNS 结果**直到 JVM 退出**（除非配了 ttl），所以 DNS 切换时 Java 应用可能长时间不生效；运维场景要显式设置 `networkaddress.cache.ttl`。

### 5.2 URI vs URL（高频）

| | URI | URL |
|---|---|---|
| 定义 | 统一资源**标识符**（只要能标识） | 统一资源**定位符**（必须能定位，带访问方式） |
| 类 | `public final class URI`（`src/java.base/share/classes/java/net/URI.java:519`） | `URL` |
| 常用方法 | `normalize()`（`:1044`）路径规整、`resolve()`（`:1104`）相对转绝对 | `openConnection()` 真正发起访问 |

> **`URL.equals()` 有个著名的坑**：它会做**主机解析**来判断相等（`src/java.base/share/classes/java/net/URL.java:1090` 的 `equals` 内部走 `getHostAddress()`，见 `:936`），因此 `equals`/`hashCode` 可能**阻塞在网络 IO 上**，绝不能放进 HashSet/HashMap 的 key。要判等请用 `URI` 或 `toExternalForm()` 字符串比较。

### 5.3 URL 编码

```java
public static String encode(String s, String enc)   // src/java.base/share/classes/java/net/URLEncoder.java:181
```

按 `application/x-www-form-urlencoded` 规则编码（空格变 `+`，非 ASCII 变 `%XX`）。注意它**不是**通用 URL 编码器——编路径段时空格应为 `%20`，需用 `URI` 构造代替。

### 5.4 IO 多路复用：select / poll / epoll

| | select | poll | epoll |
|---|---|---|---|
| 数据结构 | 位图 fd_set | 数组 pollfd | 内核红黑树 + 就绪链表 |
| fd 上限 | `FD_SETSIZE`（1024） | 无 | 无 |
| 复杂度 | 每次 O(n) 遍历 + 拷贝 | O(n) | **O(就绪数)**，回调式 |
| 触发 | 水平 | 水平 | 水平 + 边缘（ET） |

JDK 的 `Selector` 在 Linux 上就是 epoll（`EPollSelectorImpl`）：

```java
class EPollSelectorImpl extends SelectorImpl {     // src/java.base/linux/classes/sun/nio/ch/EPollSelectorImpl.java:49
    protected int doSelect(Consumer<SelectionKey> action, long timeout)  // :94  内部 epoll_wait
```

所以 **Java NIO 的高性能不是"Java 自己快"，而是把 epoll 暴露给了 Java**。这也是"为什么 NIO 能撑住 C10K"的标准答案：一个线程 + epoll 就能管住上万个连接。

---

## 六、面试高频 Q&A

26 道最高频题的「一句话答案」。先报结论占住阵地，面试官真追问时，按「深入」列展开到对应讲次。

| # | 问题 | 答案（先给结论） | 深入 |
| --- | --- | --- | --- |
| 1 | OSI 七层与 TCP/IP 四层怎么对应？ | 应用/表示/会话 → 应用层；传输 → 传输层；网络 → 网际层；链路+物理 → 链路层 | 第一讲 |
| 2 | 为什么要三次握手？ | 两次只能确认客户端序号，服务端无法确认自己的初始序号被收到；旧 SYN 重传会建立幽灵连接 | 第二讲 |
| 3 | 为什么要四次挥手？ | 全双工，两个方向分别关闭；被动方的 ACK 与 FIN 之间还要发完剩余数据 | 第二讲 |
| 4 | TIME_WAIT 有什么用？为什么要 2MSL？ | ① 保证最后 ACK 能到达（丢了对方重发 FIN）② 让残余报文消散；2MSL 刚好覆盖"一去一回" | 第二讲 |
| 5 | 服务器大量 TIME_WAIT 怎么办？ | 只有主动关闭方才进 TIME_WAIT；让客户端主动关、开 `SO_REUSEADDR`（`ServerSocket.java:770`）、调短内核回收 | 第二讲 |
| 6 | TCP 怎么保证可靠？ | 序列号+累积确认、超时重传、滑动窗口（流量控制）、拥塞控制四阶段 | 第二讲 |
| 7 | 流量控制和拥塞控制的区别？ | 流量控制看接收方 rwnd（别冲垮对端）；拥塞控制看网络 cwnd（别冲垮网络） | 第二讲 |
| 8 | TCP 粘包怎么解决？ | 协议层定边界：定长 / 分隔符 / **长度字段**（HTTP 的 Content-Length 与 chunked 就是后者） | 第二讲 |
| 9 | `SO_LINGER` 是什么？ | 关闭行为开关：默认 close 立即返回（后台发完）；设 0 直接发 RST 丢弃数据（`Socket.java:1153`） | 第二讲 |
| 10 | `TCP_NODELAY` 是干什么的？ | 关 Nagle 小包合并，降低交互延迟；RPC/游戏/终端应打开（`Socket.java:1116`） | 第二讲 |
| 11 | `SO_TIMEOUT` 是连接超时吗？ | 不是，它只作用于 `read()`；连接超时用 `connect(addr, timeout)`（`Socket.java:639`） | 第二讲 |
| 12 | `backlog` 是什么？ | `listen` 的队列长度，JDK 由 `bind`/构造器透传（`ServerSocket.java:310`/`:177`）；满了就拒连 | 第二讲 |
| 13 | TCP 与 UDP 的区别？ | TCP：面向连接、可靠、有序、有流量/拥塞控制、字节流；UDP：无连接、尽最大努力、有报文边界、8 字节头 | 第三讲 |
| 14 | UDP 的 `connect` 会握手吗？ | 不会，只绑定默认对端地址用于过滤和省参数（`DatagramSocket.java:414`） | 第三讲 |
| 15 | GET 与 POST 的区别？ | 语义上 GET 安全且幂等、可缓存、无 body；POST 非幂等有 body。工程上：GET 参数在 URL（长度受限、会留日志）、POST 在 body | 第四讲 |
| 16 | HTTP 状态码 301/302/304 分别是什么？ | 301 永久重定向（浏览器会缓存）；302 临时；304 未修改，用本地缓存 | 第四讲 |
| 17 | 502 与 504 有什么区别？ | 502 网关收到上游**无效响应**；504 网关**等上游超时** | 第四讲 |
| 18 | HTTP 长连接是什么？JDK 怎么池化？ | 一个 TCP 上跑多次请求响应；JDK 用 `KeepAliveCache`，**每 host 默认 5 条**（`:85`），可调 `http.maxConnections`（`:89`） | 第四讲 |
| 19 | 上传大文件为什么会 OOM？ | 未设 `Content-Length` 也未开 chunked，JDK 会把整个 body 缓冲在内存（`HttpURLConnection.java:70`/`:280`） | 第四讲 |
| 20 | Cookie 与 Session 的区别？ | Cookie 存客户端（有大小与数量限制、不安全），Session 存服务端、只给客户端一个 sessionId（通常放 Cookie） | 第四讲 |
| 21 | `HttpOnly`/`Secure`/`SameSite` 防什么？ | HttpOnly 防 XSS 读取；Secure 只走 HTTPS；SameSite 防 CSRF | 第四讲 |
| 22 | HTTPS 握手为什么既用非对称又用对称？ | 非对称安全协商会话密钥（慢），对称加密大量数据（快）；证书+主机名校验防中间人 | 第四讲 |
| 23 | HTTP/2 相比 1.1 改进了什么？ | 二进制分帧 + 多路复用（解决队头阻塞）+ HPACK 头压缩 + 服务端推送 | 第四讲 |
| 24 | HTTP/3 为什么换成 UDP？ | 底层是 QUIC，在用户态实现可靠与拥塞控制，避免 TCP 队头阻塞并降低握手延迟 | 第四讲 |
| 25 | DNS 解析过程？Java 有什么坑？ | 浏览器缓存→hosts→本地 DNS 递归→根/顶级/权威迭代；JVM 默认缓存成功结果直到退出，需设 `networkaddress.cache.ttl`（`InetAddress.java:197`） | 第五讲 |
| 26 | select / poll / epoll 的区别？ | select 位图且 1024 上限、poll 数组无上限、epoll 红黑树+就绪链表 O(就绪数)；JDK Selector 在 Linux 上就是 epoll（`EPollSelectorImpl.java:49`/`:94`） | 第五讲 |

---

## 七、易错点清单

1. **把 `SO_TIMEOUT` 当连接超时**：建连超时必须走 `connect(addr, timeout)`（`Socket.java:639`）
2. **`URL.equals()` 会做 DNS 解析**：可能阻塞、可能随网络变化，别做 HashMap key（`URL.java:1090`、`:936`）
3. **调了 `disconnect()` 还想复用长连接**：它直接关连接不归还池（`sun/net/www/protocol/http/HttpURLConnection.java:2698`）
4. **`URLEncoder` 不是通用 URL 编码器**：它是 form 编码（空格→`+`），编路径请用 `URI`
5. **以为 UDP 的 `connect` 会建连接**：不会，只是绑定默认对端（`DatagramSocket.java:414`）
6. **服务端主动关闭导致 TIME_WAIT 堆积**：尽量让客户端先关；开 `SO_REUSEADDR`
7. **关掉主机名校验/信任所有证书**：HTTPS 直接失效
8. **以为 HTTP/2 就没有队头阻塞**：TCP 层的队头阻塞仍在（丢包会阻塞整条连接），这也是 HTTP/3 换 QUIC 的原因
9. **JVM DNS 缓存导致灰度/切流不生效**：显式配 `networkaddress.cache.ttl`
10. **长连接不做心跳**：NAT/防火墙会静默断流，需要 TCP keepalive（`Socket.java:1431`）或应用层心跳

---

## 八、数值速记

| 数值 | 含义 | 出处 |
| --- | --- | --- |
| `5` | JDK HTTP 长连接池默认每 host 上限 | `KeepAliveCache.java:85` |
| `10` | DNS 失败结果缓存秒数（negative ttl 默认） | `InetAddress.java:224` |
| `2MSL` | TIME_WAIT 时长（一去一回，通常 1~4 分钟） | 协议（RFC 793） |
| `8 字节` | UDP 头部长度 | 协议（RFC 768） |
| `-1` | `HttpURLConnection.chunkLength` 初值（未开 chunked） | `HttpURLConnection.java:70` |
| `1024` | select 的 `FD_SETSIZE` 经典上限 | 协议/实现 |

---

## 九、复习卡

- [ ] 能画出三次握手与四次挥手的报文与状态迁移，说出 TIME_WAIT 的两个作用
- [ ] 能区分流量控制与拥塞控制，说出拥塞控制四阶段
- [ ] 能说出 TCP 粘包的三种解法，并指认 HTTP 用的是哪种
- [ ] 能说出 Java 侧 6 个 socket 选项分别管什么（`backlog`/`SO_LINGER`/`TCP_NODELAY`/`SO_TIMEOUT`/`SO_REUSEADDR`/`SO_KEEPALIVE`）
- [ ] 能背出常见状态码（301/302/304/401/403/404/502/504）
- [ ] 能说出 JDK 长连接池的默认上限与调优参数
- [ ] 能说出 HTTPS 握手为什么非对称+对称混用，以及 hostname 校验的意义
- [ ] 能说出 Cookie 三个安全属性分别防什么
- [ ] 能说出 DNS 解析链路与 JVM 缓存坑
- [ ] 能对比 select/poll/epoll，并说出 JDK Selector 在 Linux 上的实现

---

## 十、文件索引

| 文件 | 说明 |
| --- | --- |
| `src/java.base/share/classes/java/net/Socket.java` | connect / setTcpNoDelay / setSoLinger / setSoTimeout / setKeepAlive |
| `src/java.base/share/classes/java/net/ServerSocket.java` | backlog、bind、setReuseAddress |
| `src/java.base/share/classes/java/net/DatagramSocket.java` | UDP send/receive/connect/setBroadcast |
| `src/java.base/share/classes/java/net/InetAddress.java` | DNS 解析入口与缓存策略说明 |
| `src/java.base/share/classes/java/net/HttpURLConnection.java` | chunkLength / setChunkedStreamingMode |
| `src/java.base/share/classes/sun/net/www/protocol/http/HttpURLConnection.java` | 真实 HTTP 客户端、streaming 判断、disconnect |
| `src/java.base/share/classes/sun/net/www/http/KeepAliveCache.java` | 长连接池（MAX_CONNECTIONS=5） |
| `src/java.base/share/classes/java/net/HttpCookie.java` | Cookie 属性与过期判断 |
| `src/java.base/share/classes/java/net/CookieManager.java` | Cookie 策略（默认 ACCEPT_ORIGINAL_SERVER） |
| `src/java.base/share/classes/java/net/URL.java` | equals 触发主机解析的坑 |
| `src/java.base/share/classes/java/net/URI.java` | normalize / resolve |
| `src/java.base/share/classes/java/net/URLEncoder.java` | form 编码 |
| `src/java.base/share/classes/javax/net/ssl/HttpsURLConnection.java` | SSLSocketFactory / HostnameVerifier |
| `src/java.base/linux/classes/sun/nio/ch/EPollSelectorImpl.java` | epoll 选择器实现 |
