"""把 deep-dive 仓库的源码笔记同步进 Astro 内容集合。

与 Hexo 版 convert_posts.py 使用同一份 DOCS 清单，保证两边 URL / 分类一致。
输出：
  src/content/docs/<slug>.md   —— 带 title/date/categories/tags frontmatter
  public/<静态页 slug>/index.html —— 原样拷贝，不套模板
"""
import io
import os
import re
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = "D:/work/2026/deep-dive"
DOCS_DIR = os.path.join(ROOT, "src", "content", "docs")
PUBLIC_DIR = os.path.join(ROOT, "public")

os.makedirs(DOCS_DIR, exist_ok=True)

STATIC_PAGES = [
    ("CHM协作扩容_分段认领动画.html", "chm-resize-animation"),
]

# (relpath, slug, date, categories, tags)
DOCS = [
    ("kafka.md", "kafka", "2026-10-15", ["中间件"], ["Kafka", "源码"]),
    ("nacos.md", "nacos", "2026-10-15", ["中间件"], ["Nacos", "源码"]),
    ("springboot.md", "springboot", "2026-10-15", ["Spring"], ["Spring Boot", "源码"]),
    ("springmvc.md", "springmvc", "2026-10-15", ["Spring"], ["Spring MVC", "源码"]),
    ("springcloud.md", "springcloud", "2026-10-15", ["Spring"], ["Spring Cloud", "源码"]),
    ("spring-core.md", "spring-core", "2026-10-15", ["Spring"], ["Spring", "IoC", "AOP", "事务"]),
    ("redis.md", "redis", "2026-10-15", ["存储与缓存"], ["Redis", "源码"]),
    ("mysql.md", "mysql", "2026-10-15", ["存储与缓存"], ["MySQL", "源码"]),
    ("elasticsearch.md", "elasticsearch", "2026-10-15", ["存储与缓存"], ["Elasticsearch", "源码"]),
    ("java-collections.md", "java-collections", "2026-10-15", ["Java"], ["集合", "源码"]),
    ("java-concurrent.md", "java-concurrent", "2026-10-15", ["Java"], ["并发", "JUC", "源码"]),
    ("java-lock.md", "java-lock", "2026-10-15", ["Java"], ["锁", "源码"]),
    ("java-thread.md", "java-thread", "2026-10-15", ["Java"], ["线程", "源码"]),
    ("jdk.md", "jdk", "2026-10-15", ["Java"], ["JVM", "源码"]),
    ("mybatis.md", "mybatis", "2026-10-16", ["Spring"], ["MyBatis", "持久层", "源码"]),
    ("agones.md", "agones", "2026-10-17", ["游戏与云原生"], ["Agones", "Kubernetes", "游戏服务器", "源码"]),
    ("open-match.md", "open-match", "2026-10-17", ["游戏与云原生"], ["Open Match", "匹配", "源码"]),
    ("agent/opencoze-agent.md", "opencoze-agent", "2026-10-17", ["RAG 与 Agent"], ["OpenCoze", "Agent", "Eino", "源码"]),
    ("agent/ingestion-deep-dive.md", "ragflow-ingestion", "2026-09-30", ["RAG"], ["RAGFlow", "RAG", "源码"]),
    ("agent/rag.md", "rag", "2026-09-30", ["RAG"], ["RAG", "OpenCoze"]),
    ("agent/rrf.md", "rrf", "2026-09-30", ["RAG"], ["RRF", "检索"]),
    # 已下架：learning-path.md（学习路线）与 learning-map.html（思维导图）
    # 源文件仍在 D:/work/2026/deep-dive/，需要时把这两行加回来即可重新发布。
    # ("learning-path.md", "learning-path", "2026-10-17", ["学习路线"], ["思维导图", "学习路径", "记忆方法"]),
]


def strip_first_h1(text):
    lines = text.split("\n")
    out = []
    removed = False
    started = False
    for ln in lines:
        if not started:
            if ln.strip() == "":
                out.append(ln)
                continue
            started = True
        if not removed and re.match(r"^#\s+", ln):
            removed = True
            continue
        out.append(ln)
    return "\n".join(out).strip() + "\n"


def yaml_list(items, key):
    if not items:
        return ""
    body = "\n".join('  - "%s"' % i.replace('"', '\\"') for i in items)
    return "%s:\n%s\n" % (key, body)


def main():
    total = 0
    for rel, slug, date, cats, tags in DOCS:
        path = os.path.join(SRC_ROOT, rel)
        if not os.path.exists(path):
            print("SKIP missing:", path)
            continue
        with io.open(path, encoding="utf-8") as f:
            raw = f.read()
        m = re.search(r"^#\s+(.+)$", raw, re.M)
        title = (m.group(1).strip() if m else slug).replace('"', '\\"')
        body = strip_first_h1(raw)
        fm = '---\ntitle: "%s"\ndate: %s\n%s%s---\n\n' % (
            title,
            date,
            yaml_list(cats, "categories"),
            yaml_list(tags, "tags"),
        )
        out_path = os.path.join(DOCS_DIR, slug + ".md")
        with io.open(out_path, "w", encoding="utf-8") as f:
            f.write(fm + body)
        total += 1
        print("OK  %-20s <- %s (%d bytes)" % (slug, rel, len(fm) + len(body)))
    print("TOTAL docs:", total)

    for rel, slug in STATIC_PAGES:
        src = os.path.join(SRC_ROOT, rel)
        if not os.path.exists(src):
            print("SKIP missing static page:", src)
            continue
        dst_dir = os.path.join(PUBLIC_DIR, slug)
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, os.path.join(dst_dir, "index.html"))
        print("OK  /%-19s <- %s" % (slug + "/", rel))


if __name__ == "__main__":
    main()
