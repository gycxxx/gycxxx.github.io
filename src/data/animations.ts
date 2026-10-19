export interface AnimItem {
  href: string;
  name: string;
  cat: string;
  steps: string;
  desc: string;
  color: string;
}

/** 站点里的流程动画清单（首页板块与 /animations/ 索引页共用） */
export const ANIMATIONS: AnimItem[] = [
  {
    href: "/chm-resize-animation/",
    name: "ConcurrentHashMap 协作扩容",
    cat: "Java",
    steps: "15 步",
    desc: "64 个桶、stride=16、三个线程 CAS 抢段。看清新线程来的时候到底抢哪一段、抢不到会怎样。",
    color: "#e8a33d",
  },
  {
    href: "/animations/aqs-lock/",
    name: "AQS 独占锁取号排队",
    cat: "Java",
    steps: "20 步",
    desc: "T2 抢锁失败 → 造 Node → CAS 入队 → park → 被 unpark 后再抢一次，直到成为新的 dummy head。",
    color: "#e8a33d",
  },
  {
    href: "/animations/sync-upgrade/",
    name: "synchronized 锁升级",
    cat: "Java",
    steps: "11 步",
    desc: "mark word 从 neutral 到 stack-locked 再到 inflated，_cxq 与 _EntryList 双队列怎么接力。",
    color: "#e8a33d",
  },
  {
    href: "/animations/spring-circular/",
    name: "Spring 三级缓存救循环依赖",
    cat: "Spring",
    steps: "12 步",
    desc: "A↔B 互相依赖，看二级工厂怎么给出早期引用、二→三级搬家那行 remove 为什么是关键。",
    color: "#6aa84f",
  },
  {
    href: "/animations/mysql-2pc/",
    name: "MySQL 两阶段提交与崩溃恢复",
    cat: "存储与缓存",
    steps: "25 步",
    desc: "prepare 造中间态、binlog fsync 当仲裁者；崩在第 12 步之后怎么靠 XID 决定提交还是回滚。",
    color: "#3d7ea6",
  },
  {
    href: "/animations/kafka-produce/",
    name: "Kafka 一条 produce 的旅程",
    cat: "中间件",
    steps: "10 步",
    desc: "Acceptor → Processor → requestQueue → Handler → Purgatory，LEO 与 HW 为什么是两条线。",
    color: "#e07a5f",
  },
  {
    href: "/animations/seata-at/",
    name: "Seata AT 两阶段提交",
    cat: "中间件",
    steps: "21 步",
    desc: "前镜像→后镜像→undo_log 三件套，register 先于本地 commit；异常分支走反向 SQL 回滚。",
    color: "#e07a5f",
  },
  {
    href: "/animations/sentinel-entry/",
    name: "Sentinel 责任链过闸",
    cat: "中间件",
    steps: "18 步",
    desc: "三道闸 fail-open，十个 slot 按 @Spi(order) 依次点亮，FlowSlot 判定放行还是 FlowException。",
    color: "#e07a5f",
  },
  {
    href: "/animations/leap-array/",
    name: "LeapArray 滑动窗口",
    cat: "中间件",
    steps: "11 步",
    desc: "时间戳算下标、CAS 换桶、LongAdder 记账；2×500ms 双桶与时钟回拨各为什么这么设计。",
    color: "#e07a5f",
  },
  {
    href: "/animations/stream-decoder/",
    name: "StreamDecoder 三态机",
    cat: "Java",
    steps: "12 步",
    desc: "残字节 bb + 残字符 leftoverChar：UNDERFLOW 去读、OVERFLOW 返回、MALFORMED 抛异常。",
    color: "#e8a33d",
  },
];
