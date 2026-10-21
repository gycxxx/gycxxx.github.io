---
title: "Java 基础源码深度学习笔记"
date: 2026-10-21
categories:
  - "Java"
tags:
  - "Java 基础"
  - "String"
  - "反射"
  - "序列化"
  - "源码"
---

> 基于 `src/java.base/share/classes/java/lang/`、`java/util/`、`java/io/` 下最"基础"那批类的逐文件精读整理而成：`String`、`AbstractStringBuilder`、`StringBuilder`、`StringBuffer`、`Integer`、`Object`、`Class`、`Throwable`、`Enum`、`System`、`Objects`，以及 `java/lang/reflect/Method`、`java/io/ObjectOutputStream`。
>
> 阅读顺序即学习顺序：**String 家族 → 包装类与 equals/hashCode → 反射与泛型 → 异常与序列化 → Object 契约与常用工具**。
>
> 所有结论均标注了源文件位置（格式 `src/java.base/share/classes/java/lang/xxx.java:行号`），建议对照代码阅读。
>
> **版本基线**：OpenJDK **21（jdk-21-ga）**，精确 commit `890adb6410dab4606a4f26a942aed02fb2f55387`，commit 时间 `2023-08-09 20:19:41 +0000`；第四讲序列化部分取自 OpenJDK 主线（commit `6c48f4ed`，2025-08-12），因为 `java.io` 不在 21 精简检出目录内。
>
> 本文档只覆盖"语言与基础库"层面的高频考点；集合见 `java-collections.md`、并发见 `java-concurrent.md`、IO 见 `java-io.md`、JVM 见 `jdk.md`。

---

## 〇、全景地图

Java 基础的高频考点其实集中在**五个小内核**上，每个内核背后都只有一两个真正需要看源码的地方：

```
┌───────────────────────── Java 基础五大内核 ─────────────────────────┐
│                                                                     │
│  ① String 家族        ② 包装类 / == vs equals                      │
│     String(final)         Integer.valueOf → IntegerCache[-128,127]  │
│     + byte[] value        Object.equals == 引用比较                 │
│     + coder(Latin1/UTF16) hashCode 契约：equals 同 → hash 必同       │
│     + hash 缓存                                                     │
│     StringBuilder/StringBuffer                                      │
│                                                                     │
│  ③ 反射与泛型          ④ 异常与序列化                              │
│     Class.forName(初始化开关)  Throwable: cause / suppressed        │
│     Method.invoke(包装异常)    fillInStackTrace(栈快照开销)          │
│     泛型擦除 + Signature 属性  serialVersionUID / transient         │
│                                                                     │
│  ⑤ Object 契约与工具                                                │
│     getClass/hashCode/equals/clone/finalize/wait/notify             │
│     System.arraycopy(native) / nanoTime(单调) / identityHashCode     │
└─────────────────────────────────────────────────────────────────────┘
```

一句话主线：**Java 里"相等"有两套语义（`==` 比身份、`equals` 比内容），"不可变"靠 final 类 + final 数组字段实现，"泛型"只在编译期存在，"异常"的本质是一次栈快照**。

---

## 第一讲 String 家族

### 1.1 不可变是怎么"焊死"的

`String` 的不可变由三重 final 保证（`src/java.base/share/classes/java/lang/String.java:142`）：

```java
public final class String                    // :142  ① 类 final，不能被继承改写
    ...
    private final byte[] value;              // :158  ② 存储数组 private final
    private final byte coder;                // :171  ③ 编码标记（LATIN1 / UTF16）
```

`value` 是 `private` 且没有任何 setter/写方法，外部拿不到引用；类又是 `final`，子类也塞不进来。所以 String 一诞生内容就定了，**所有"看起来在改"的方法（substring/concat/replace）都是 new 一个新 String**：

- `substring`（`:2802`）：JDK 7u6 之后不再共享底层数组，一律拷贝（老版本共享 `char[]` 会造成"大字符串泄漏"）
- `concat`（`:2891`）：拼完直接 `new String(buf, true)`

不可变带来的三个真实收益，面试答这三句就够：**① 可缓存 hash ② 天然线程安全 ③ 常量池可共享**。

### 1.2 Compact Strings：为什么是 `byte[]` 不是 `char[]`

JDK 9 起 `String` 内部从 `char[]`（UTF-16，每字符 2 字节）改成 `byte[]` + `coder`：

```java
static final boolean COMPACT_STRINGS;   // :223
```

- 内容全是 Latin-1（ISO-8859-1，含 ASCII）→ `coder = LATIN1`，**每字符 1 字节**，内存直接减半
- 一旦出现非 Latin-1 字符 → `coder = UTF16`，退回 2 字节
- `coder` 也就是 `equals`（`:1857`）里为什么要先比 `coder` 再逐字节比较的原因

### 1.3 hashCode 缓存：为什么需要两个字段

```java
private int hash;              // :174   默认 0
private boolean hashIsZero;    // :180   默认 false
```

`hash` 缓存计算结果，但它同时用 0 表示"还没算过"。如果某个字符串的 hash 恰好就是 0，就会每次重算。于是 JDK 又加了 `hashIsZero` 来区分"未计算"与"算出来是 0"：

```java
public int hashCode() {                       // :2354
    int h = hash;
    if (h == 0 && !hashIsZero) {              // :2364  未计算 且 不是"0 的结果"
        h = isLatin1() ? StringLatin1.hashCode(value)
                       : StringUTF16.hashCode(value);
        if (h == 0) {
            hashIsZero = true;                // :2368  标记为"真的是 0"
        } else {
            hash = h;
        }
    }
    return h;
}
```

这是经典的"**用 0 做哨兵值就得再配一个布尔**"技巧，值得记住。

### 1.4 常量池与 `intern`

- 字面量（`"abc"`）在编译期进 class 常量池，类加载时进**字符串常量池**（JDK 7 起常量池从永久代搬到堆里，所以会被 GC 回收）
- 运行期拼接出来的字符串**不在池里**，`new String("abc")` 一定在堆上新建对象
- `public native String intern()`（`:4636`）：有则返池里那份，无则把当前对象**引用**放进池再返回

```java
String a = "abc";            // 池
String b = new String("abc");// 堆
a == b                // false
a == b.intern()       // true（intern 返回池里那一份）
```

### 1.5 拼接的三种姿势与代价

| 姿势 | 编译后做了什么 | 适用 |
|---|---|---|
| 字面量相加 `"a"+"b"` | 编译期常量折叠，直接是 `"ab"` | 常量 |
| 变量相加 `s1 + s2` | JDK 9 起是 `invokedynamic`，落在 `StringConcatFactory.makeConcat`（`src/java.base/share/classes/java/lang/invoke/StringConcatFactory.java:204`），策略可切换 | 单次拼接 |
| 循环里 `s += x` | **每轮 new 一个 StringBuilder 再 toString** → O(n²) | 禁止 |

```java
// 反例：n 轮 → n 个 StringBuilder + n 个 String
String s = "";
for (String p : parts) s += p;

// 正例：一个 StringBuilder 到底
StringBuilder sb = new StringBuilder(1024); // 预估容量，避免反复扩容
for (String p : parts) sb.append(p);
```

`StringBuilder` 的扩容来自父类 `AbstractStringBuilder`（`src/java.base/share/classes/java/lang/AbstractStringBuilder.java:67` 存 `byte[] value`；`:225` `ensureCapacity`；`:259` `newCapacity`，即"旧容量 × 2 + 2"，不够就用最小需要值）。

### 1.6 StringBuilder vs StringBuffer

| 维度 | StringBuilder | StringBuffer |
|---|---|---|
| 类定义 | `src/java.base/share/classes/java/lang/StringBuilder.java:91` | `src/java.base/share/classes/java/lang/StringBuffer.java:112` |
| append | `:178` 无锁 | `:311` **`synchronized`** |
| toString | `:473` 每次新建 | `:735`，带 `toStringCache`（`:121`），重复 toString 命中缓存 |
| 结论 | 单线程首选 | 多线程共享才用；局部变量一律 StringBuilder |

> **易错**：`StringBuffer` 的线程安全是"方法级 synchronized"，复合操作（`append(a).append(b)` 之间）仍可能被打断，与 `synchronizedMap` 一样的毛病。

---

## 第二讲 包装类、== 与 equals/hashCode

### 2.1 IntegerCache：-128~127 是缓存区间

```java
public static Integer valueOf(int i) {                       // :1067
    if (i >= IntegerCache.low && i <= IntegerCache.high)     // low/high 见下方
        return IntegerCache.cache[i + (-IntegerCache.low)];
    return new Integer(i);
}

private static class IntegerCache {      // src/java.base/share/classes/java/lang/Integer.java:1008
    static final int low = -128;         // :1009  下界写死
    static final int high;               // :1010  上界默认 127，可用
                                         //        -XX:AutoBoxCacheMax 或
                                         //        java.lang.Integer.IntegerCache.high 调大
```

所以：

```java
Integer a = 127, b = 127;  a == b;   // true  （同一份缓存对象）
Integer c = 128, d = 128;  c == d;   // false （两次 new）
Integer e = 128; int f = 128; e == f;// true  （e 自动拆箱成 int 再比数值）
```

**规则一句话**：包装类与包装类 `==` 比引用（可能踩缓存边界）；包装类与基本类型 `==` 会拆箱比数值。永远用 `equals` 或 `Objects.equals`。

### 2.2 `==` 与 `equals` 的分工

`Object` 的默认实现：

```java
public boolean equals(Object obj) { return (this == obj); }  // :163
public native int hashCode();                                 // :103  默认与身份相关
```

- `==`：基本类型比**值**，引用类型比**地址（身份）**
- `equals`：默认就是 `==`，重写后比**内容**
- `Objects.equals(a, b)`（`src/java.base/share/classes/java/util/Objects.java:63`）：先 `a == b` 短路再做 null 安全比较，写起来最省心

### 2.3 equals / hashCode 契约（必背四条）

1. 自反：`x.equals(x)` 为 true
2. 对称：`x.equals(y)` ⇔ `y.equals(x)`
3. 传递：`x.equals(y)` 且 `y.equals(z)` ⇒ `x.equals(z)`
4. 一致 + **hash 一致**：`x.equals(y)` ⇒ `x.hashCode() == y.hashCode()`（反过来不成立，哈希冲突允许）

> **后果**：只重写 `equals` 不重写 `hashCode`，放进 `HashMap`/`HashSet` 就会"存得进去取不出来"。`Objects.hash(...)`（`:132`）内部就是 `Arrays.hashCode`，重写 hashCode 时直接用它。

### 2.4 值传递还是引用传递

**Java 只有值传递**。引用类型传的是"引用的副本"：

```java
void f(StringBuilder sb) {
    sb.append("x");      // 有效：副本引用指向同一个对象
    sb = new StringBuilder(); // 无效：只是把副本指向了新对象
}
```

---

## 第三讲 反射与泛型

### 3.1 `Class.forName` 的第二个参数很关键

```java
public static Class<?> forName(String className)                          // src/java.base/share/classes/java/lang/Class.java:409
public static Class<?> forName(String name, boolean initialize,           // :501
                               ClassLoader loader)
```

`initialize=false` 只加载不初始化（**不执行 static 块**）。JDBC 老写法 `Class.forName("com.mysql.cj.jdbc.Driver")` 依赖的就是"加载即初始化，Driver 静态块里注册自己"——SPI 时代已经不需要手写。

### 3.2 getMethod 与 getDeclaredMethod 的区别

| 方法 | 行号 | 语义 |
|---|---|---|
| `getMethod` | `Class.java:2385` | 本类 + 父类的 **public** 方法 |
| `getDeclaredMethod` | `Class.java:2838` | 本类**所有**方法（含 private/protected/包级），不含继承 |
| `getDeclaredField` | `Class.java:2772` | 同上，字段版 |
| `privateGetDeclaredMethods` | `Class.java:3570` | 真正去解析 class 文件的方法表 |

`private` 方法拿到后还要 `setAccessible(true)`；JDK 9 模块系统之后跨模块反射会抛 `InaccessibleObjectException`，需要 `--add-opens`。

### 3.3 Method.invoke 的两个坑

```java
public Object invoke(Object obj, Object... args)   // src/java.base/share/classes/java/lang/reflect/Method.java:558
```

1. **异常被包一层**：目标方法抛出的任何异常都被包成 `InvocationTargetException`，要 `getTargetException()`/`getCause()` 才能拿到原始异常——日志里满屏 `InvocationTargetException` 就是这个原因
2. **性能**：反射调用要走 `MethodAccessor`（JDK 内部有 native → 字节码生成的"膨胀"机制，调用次数超过阈值后生成专用字节码加速），但仍无法享受 JIT 内联与逃逸分析。**热路径上应缓存 `Method` 对象，或直接改成接口调用 / MethodHandle**

### 3.4 泛型擦除：编译期的事，运行时没有

- 擦除发生在编译期：`List<String>` 与 `List<Integer>` 运行时是同一个 `List`，`instanceof List<String>` 非法
- 编译器自动插入 `checkcast` 保证类型安全；返回协变类型时会生成**桥接方法**（bridge method）
- **但泛型信息并没有 100% 丢**：类/方法/字段上的泛型签名写在 class 文件的 `Signature` 属性里，反射能读回来：

```java
public Type getGenericReturnType()   // src/java.base/share/classes/java/lang/reflect/Method.java:297
```

这就是为什么 Spring/MyBatis 能靠反射还原出 `List<User>` 的真实参数类型。

- 通配符遵循 **PECS**：`Producer extends`（只往外拿，用 `? extends T`）、`Consumer super`（只往里塞，用 `? super T`）

---

## 第四讲 异常与序列化

### 4.1 Throwable 的四要素

```java
private Throwable cause = this;                          // src/java.base/share/classes/java/lang/Throwable.java:200
private List<Throwable> suppressedExceptions = SUPPRESSED_SENTINEL;  // :235
public Throwable(String message) { ... fillInStackTrace(); }          // :272 / :258
public final synchronized void addSuppressed(Throwable exception)     // :1094
```

- `cause = this` 是"哨兵"：没设置过 cause 时 `cause` 指向自己，`getCause()` 靠判断 `cause != this` 决定返回 null
- 构造时立刻调 `fillInStackTrace()`（`:258`）——**异常的成本主要在这里**：遍历栈帧填 `StackTraceElement[]`。高频抛异常（如用异常做流程控制）会明显拖慢
- `suppressedExceptions`：try-with-resources 的 close 失败不会吞掉主异常，而是 `addSuppressed` 挂在主异常上（`:1094`），打印时显示 `Suppressed:`

### 4.2 受检 vs 非受检

```
Throwable
├── Error                 —— JVM 级（OOM/StackOverflow），不该捕获
└── Exception
    ├── RuntimeException  —— 非受检（NPE/IOOBE/ClassCast），可捕可不捕
    └── 其他 Exception    —— 受检（IOException/SQLException），必须 catch 或 throws
```

`RuntimeException` 及其子类属于"编程错误"，编译器不强制；受检异常强制你处理，是 Java 唯一强制错误处理的机制（也是被吐槽最多的设计）。

### 4.3 序列化的五个结论

`ObjectOutputStream.writeObject`（`src/java.base/share/classes/java/io/ObjectOutputStream.java:319`）→ `writeObject0`（`:1006`）：

1. `Serializable` 是**标记接口**，没有方法；不实现就抛 `NotSerializableException`
2. `serialVersionUID` 不一致 → `InvalidClassException`。**显式声明**它，否则类改一个字段就全废
3. `transient` 字段被跳过；**static 字段不参与序列化**（它属于类不属于对象）
4. 反序列化**不走构造器**（绕过构造逻辑，这也是反序列化漏洞的根源之一）
5. 枚举特殊：所有 enum 的 `serialVersionUID` 固定为 0L（`:144-145`），且反序列化会走 `valueOf` 保证单例

---

## 第五讲 Object 契约与常用工具

### 5.1 Object 的 11 个方法（面试常问"Object 有哪些方法"）

| 方法 | 行号 | 说明 |
|---|---|---|
| `getClass()` | `src/java.base/share/classes/java/lang/Object.java:67` | final native，返回运行时 Class |
| `hashCode()` | `:103` | native，与身份相关（可被重写） |
| `equals(Object)` | `:163` | 默认 `==` |
| `clone()` | `:229` | protected native，不实现 `Cloneable` 就抛 `CloneNotSupportedException`；**默认浅拷贝** |
| `toString()` | — | 默认 `类名@16进制hash` |
| `notify()` / `notifyAll()` | `:293` / `:318` | 必须在 `synchronized` 块内调用 |
| `wait()` / `wait(long)` | — | 释放锁并等待；**必须写在 while 循环里防虚假唤醒** |
| `finalize()` | `:592` | JDK 9 起废弃（`@Deprecated`），不确定何时执行，**不要用** |

### 5.2 System 三件套

```java
public static native long currentTimeMillis();   // src/java.base/share/classes/java/lang/System.java:527  挂钟时间，会被 NTP 回拨
public static native long nanoTime();            // :571  单调时间，只适合算耗时差
public static native void arraycopy(...);        // :666  本地实现，比手写循环快
public static native int identityHashCode(Object x); // :684  拿"未被重写"的 hashCode
```

**记时一律用 `nanoTime()`**：`currentTimeMillis` 可能因校时被回拨，算出负数耗时。

### 5.3 深拷贝 vs 浅拷贝

- 浅拷贝：只复制对象本身，内部引用字段仍指向同一对象（`Object.clone` 默认行为）
- 深拷贝：连引用链一起复制（手写递归 / 序列化再反序列化 / 拷贝构造器）
- 数组 `clone()` 也是浅拷贝

### 5.4 final / finally / finalize

| 关键字 | 层面 | 语义 |
|---|---|---|
| `final` | 类/方法/变量 | 类不可继承、方法不可重写、变量引用不可再指（**对象内容可变**） |
| `finally` | 语句块 | 一定会执行（除非 `System.exit` / 线程被 kill / JVM 崩溃） |
| `finalize` | 方法 | 已废弃，JDK 9+ 标记 `@Deprecated`（`Object.java:592`） |

`final` 字段还有 **JMM 语义**：构造器中正确初始化的 final 字段，无需同步即可被其他线程看见（配合"构造器不逸出 this"）。

### 5.5 枚举：天生单例

```java
public abstract class Enum<E extends Enum<E>>      // src/java.base/share/classes/java/lang/Enum.java:80
public final String name()                          // :101
public final int ordinal()                          // :127
protected final Object clone()                      // :202  直接抛异常，禁止克隆
public static <T extends Enum<T>> T valueOf(...)    // :285  底层是 enum 的静态 map 查找
```

`clone` 被 final 化并抛异常 + 反序列化走 `valueOf` ⇒ **enum 是实现单例最稳的方式**。

---

## 六、面试高频 Q&A

27 道最高频题的「一句话答案」。先报结论占住阵地，面试官真追问时，按「深入」列展开到对应讲次。

| # | 问题 | 答案（先给结论） | 深入 |
| --- | --- | --- | --- |
| 1 | String 为什么不可变？ | 类 `final`（`String.java:142`）+ 存储数组 `private final byte[] value`（`:158`）+ 无任何写方法；所有"修改"方法都 new 新对象 | 第一讲 |
| 2 | String 内部为什么是 `byte[]` 不是 `char[]`？ | JDK 9 Compact Strings：Latin-1 内容按 1 字节存，内存减半；`coder`（`:171`）标记编码，`COMPACT_STRINGS`（`:223`）总开关 | 第一讲 |
| 3 | `String s = new String("abc")` 创建几个对象？ | 常量池已有 `"abc"` 时**只创建 1 个堆对象**；没有则 2 个（池里一份 + 堆上一份） | 第一讲 |
| 4 | `intern()` 做了什么？ | 池里有就返回池中引用，没有就把当前对象引用放进去再返回；JDK 7 起常量池在堆中，可被 GC（`String.java:4636`） | 第一讲 |
| 5 | String 的 hashCode 为什么要缓存？ | 字符串作 HashMap key 极频繁；缓存靠 `hash`（`:174`）+ `hashIsZero`（`:180`）两个字段区分"未算"与"算出 0" | 第一讲 |
| 6 | 循环里用 `+` 拼接字符串有什么问题？ | 每轮 new 一个 StringBuilder 再 toString，O(n²)；应显式持有一个 `StringBuilder` 并预设容量 | 第一讲 |
| 7 | StringBuilder 与 StringBuffer 区别？ | 后者方法全部 `synchronized`（`StringBuffer.java:311`）且带 `toStringCache`（`:121`）；单线程一律用 StringBuilder | 第一讲 |
| 8 | `Integer a=127, b=127; a==b` 为什么是 true？ | `valueOf`（`Integer.java:1067`）走 `IntegerCache`，默认缓存 `-128~127`（`:1009`）；128 就越界 new 了 | 第二讲 |
| 9 | `==` 和 `equals` 的区别？ | `==` 比身份（引用类型比地址）；`equals` 默认就是 `==`（`Object.java:163`），重写后比内容 | 第二讲 |
| 10 | 为什么重写 equals 必须重写 hashCode？ | 契约要求 equals 相等则 hash 必相等；否则 HashMap 里存得进、取不出 | 第二讲 |
| 11 | Java 是值传递还是引用传递？ | **只有值传递**：引用类型传的是"引用的副本"，改副本指向不影响实参，改副本指向对象的内容会影响 | 第二讲 |
| 12 | `Class.forName` 有什么讲究？ | 两参版（`:501`）可传 `initialize=false` 只加载不初始化（不执行 static 块）；单参版（`:409`）会初始化 | 第三讲 |
| 13 | getMethod 与 getDeclaredMethod 区别？ | `getMethod`（`:2385`）取本类+父类 public；`getDeclaredMethod`（`:2838`）取本类全部（含 private），不含继承 | 第三讲 |
| 14 | 反射慢在哪？怎么做才不慢？ | 走 `MethodAccessor`、无法内联与逃逸分析；缓存 Class/Method 对象，热路径改接口或 MethodHandle | 第三讲 |
| 15 | 反射调用为啥总看到 `InvocationTargetException`？ | `Method.invoke`（`reflect/Method.java:558`）把目标异常包了一层，要 `getCause()` 才拿到原始异常 | 第三讲 |
| 16 | 什么是泛型擦除？ | 编译期把 `T` 替换为边界（无边界为 Object）并插 `checkcast`；运行时 `List<String>` 与 `List<Integer>` 同类 | 第三讲 |
| 17 | 泛型信息真的一点都不剩吗？ | 类/方法/字段的泛型签名写在 class 文件 `Signature` 属性里，反射能读回（`reflect/Method.java:297`） | 第三讲 |
| 18 | 受检异常与非受检异常的区别？ | `RuntimeException`/`Error` 是非受检（编译器不强制）；其余 `Exception` 是受检，必须 catch 或 throws | 第四讲 |
| 19 | finally 一定会执行吗？ | 除非 `System.exit`、线程被 kill、JVM 崩溃；**且 finally 里的 return 会覆盖 try 的返回值**（别这么写） | 第四讲 |
| 20 | try-with-resources 的异常去哪了？ | close 失败不吞主异常，而是 `addSuppressed`（`Throwable.java:1094`）挂上去，打印时显示 `Suppressed:` | 第四讲 |
| 21 | 为什么不建议用异常控制流程？ | 构造异常要 `fillInStackTrace`（`Throwable.java:258`）遍历整个栈，成本远高于一次判断 | 第四讲 |
| 22 | serialVersionUID 有什么用？ | 版本校验：不一致直接 `InvalidClassException`；不显式声明则由编译器按类结构生成，改字段就失效 | 第四讲 |
| 23 | `transient` 关键字的作用？ | 标记字段不参与序列化；反序列化后为基本类型默认值。注意 **static 字段本来就不参与** | 第四讲 |
| 24 | Object 有哪些方法？ | `getClass`/`hashCode`/`equals`/`clone`/`toString`/`notify`/`notifyAll`/`wait`×3/`finalize`（`Object.java:67~592`） | 第五讲 |
| 25 | 深拷贝与浅拷贝？ | 浅拷贝只复制一层（`Object.clone` 默认，`:229`），内部引用共享；深拷贝要递归或序列化 | 第五讲 |
| 26 | 记时该用 `currentTimeMillis` 还是 `nanoTime`？ | 算耗时一律 `nanoTime`（`System.java:571`）——单调不回拨；`currentTimeMillis`（`:527`）是挂钟时间 | 第五讲 |
| 27 | 为什么推荐用枚举实现单例？ | `clone` 被 final 化抛异常（`Enum.java:202`）、反序列化走 `valueOf`（`:285`）保证同一实例、天然防反射攻击 | 第五讲 |

---

## 七、易错点清单

1. **`Integer` 用 `==` 比较**：缓存边界（-128~127）内外行为不一致，一律 `equals`
2. **`substring` 老版本共享数组**：JDK 7u6 前截取大字符串会拖住整个 char[]；现已拷贝（`String.java:2802`），但老代码迁移仍要留意
3. **`finally` 里写 `return`**：会吞掉 try/catch 的返回值与异常
4. **`final` 修饰引用**：限制的是"引用不可再指"，对象内部照样能改
5. **catch 顺序**：子类异常必须写在父类之前，否则编译不过（`Exception` 不能写在 `IOException` 前）
6. **`wait` 不用 while**：存在虚假唤醒，必须 `while (条件) wait()`
7. **反射拿到的异常**：不 `getCause()` 就永远看不到真实错误
8. **重写 `equals` 忘了 `hashCode`**：HashSet/HashMap 行为异常
9. **`StringBuffer` 的"线程安全"是方法级**：复合操作仍不安全
10. **`Objects.requireNonNull`（`util/Objects.java:231`）比手写 if 抛出更简洁**：别再写 `if (x == null) throw new NPE()`

---

## 八、数值速记

| 数值 | 含义 | 出处 |
| --- | --- | --- |
| `-128 ~ 127` | Integer 缓存区间（上界可调） | `Integer.java:1009-1010` |
| `0` | `String.hash` 的"未计算"哨兵值 | `String.java:174` |
| `LATIN1 / UTF16` | `String.coder` 两个取值 | `String.java:171` |
| `旧容量 × 2 + 2` | `AbstractStringBuilder` 扩容公式 | `AbstractStringBuilder.java:259` |
| `0L` | 所有枚举的固定 `serialVersionUID` | `ObjectOutputStream.java:144-145` |
| `1 / 5 / 10` | 线程优先级 MIN/NORM/MAX（OS 可能忽略） | `Thread.java:363/368/373` |

> 优先级这条顺带提醒：Java 线程优先级靠不住——它映射到 OS 优先级，而 OS 可能直接忽略（`Thread.java:636` 的 `onSpinWait()` 反而是更靠谱的自旋提示）。

---

## 九、复习卡

- [ ] 能不看源码说出 String 不可变的三重保证，并解释 `byte[] + coder` 的意义
- [ ] 能画出 `s += x` 在循环里的对象创建过程，并说出正确写法
- [ ] 能背出 equals/hashCode 四条契约，说出违反后 HashMap 的具体表现
- [ ] 能说出 IntegerCache 上下界与"包装类 vs 基本类型 == 比较"的拆箱规则
- [ ] 能解释"泛型擦除"与"Signature 属性"这对看似矛盾的事实
- [ ] 能说出 `Method.invoke` 的两个坑（异常包装、性能）
- [ ] 能说出 Throwable 的 cause/suppressed 两个机制各自的触发场景
- [ ] 能说出序列化的 5 条结论（含 static 不参与、反序列化不走构造器）
- [ ] 能列出 Object 的 11 个方法，并说出 finalize 已废弃
- [ ] 能解释 `nanoTime` 与 `currentTimeMillis` 的差别

---

## 十、文件索引

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `src/java.base/share/classes/java/lang/String.java` | 4913 | 不可变、Compact Strings、hash 缓存、intern |
| `src/java.base/share/classes/java/lang/AbstractStringBuilder.java` | 1972 | `byte[] value` 与扩容 `newCapacity` |
| `src/java.base/share/classes/java/lang/StringBuilder.java` | 525 | 单线程拼接 |
| `src/java.base/share/classes/java/lang/StringBuffer.java` | 810 | synchronized + toStringCache |
| `src/java.base/share/classes/java/lang/Integer.java` | 2091 | IntegerCache、valueOf、parseInt |
| `src/java.base/share/classes/java/lang/Object.java` | 593 | 11 个方法之家 |
| `src/java.base/share/classes/java/lang/Class.java` | 4782 | forName、getMethod、getDeclaredField |
| `src/java.base/share/classes/java/lang/reflect/Method.java` | 827 | invoke 与泛型签名 |
| `src/java.base/share/classes/java/lang/Throwable.java` | 1133 | cause / suppressed / fillInStackTrace |
| `src/java.base/share/classes/java/lang/Enum.java` | 372 | 天然单例 |
| `src/java.base/share/classes/java/lang/System.java` | 2673 | arraycopy / nanoTime / identityHashCode |
| `src/java.base/share/classes/java/util/Objects.java` | 515 | equals/hash/requireNonNull |
| `src/java.base/share/classes/java/io/ObjectOutputStream.java` | 2296 | 序列化主链路 |
