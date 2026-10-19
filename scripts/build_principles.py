"""从 src/content/docs/*.md 抽取「高频 Q&A（先结论）」表格，生成原理速查数据。

输出 src/data/principles.json，供首页「核心原理速查」板块使用。
只认统一的四列表格：| # | 问题 | 一句话结论 | 深入 |
"""
import io
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "src", "content", "docs")
OUT = os.path.join(ROOT, "src", "data", "principles.json")

# 分类 -> 展示名（与首页 META 保持一致，未列出的用原分类名）
CAT_OF = {}


def split_row(line):
    """拆一行 markdown 表格，返回 cell 列表（去掉首尾空管道）"""
    s = line.strip()
    if not s.startswith("|"):
        return None
    s = s.strip("|")
    return [c.strip() for c in s.split("|")]


def extract(path):
    lines = io.open(path, encoding="utf-8").read().split("\n")
    rows = []
    in_qa = False
    for ln in lines:
        if re.match(r"^#{2,4}\s*.*(高频 Q&A|速通)", ln):
            in_qa = True
            continue
        if in_qa and re.match(r"^#{1,4}\s", ln):
            in_qa = False
            continue
        if not in_qa:
            continue
        if ln.strip().startswith("|:"):  # 表格对齐行
            continue
        cells = split_row(ln)
        if not cells or len(cells) < 3:
            continue
        # 跳过表头与分隔行
        joined = "".join(cells)
        if set(joined) <= set("-: ") or "面试问题" in joined or "追问方向" in joined:
            continue
        if not re.fullmatch(r"\d+", cells[0]):
            continue
        q = cells[1]
        a = cells[2]
        deep = cells[3] if len(cells) > 3 else ""
        q = re.sub(r"\*\*(.+?)\*\*", r"\1", q).strip()
        a = re.sub(r"\*\*(.+?)\*\*", r"\1", a)
        a = re.sub(r"`([^`]+)`", r"\1", a).strip()
        if len(q) < 4 or len(a) < 8:
            continue
        rows.append({"q": q, "a": a, "d": deep.strip()})
    return rows


def main():
    items = []
    per_doc = {}
    for name in sorted(os.listdir(DOCS)):
        if not name.endswith(".md"):
            continue
        slug = name[:-3]
        raw = io.open(os.path.join(DOCS, name), encoding="utf-8").read()
        fm = re.match(r"^---\n([\s\S]*?)\n---", raw)
        cats = []
        if fm:
            block = fm.group(1)
            m = re.search(r"^categories:\s*\[(.+?)\]", block, re.M)
            if m:
                cats = [c.strip().strip("\"'") for c in m.group(1).split(",")]
            else:
                m = re.search(r"^categories:\s*\n((?:\s*-\s*.+\n)+)", block, re.M)
                if m:
                    cats = [
                        c.strip().lstrip("- ").strip().strip("\"'")
                        for c in m.group(1).strip().split("\n")
                    ]
        m2 = re.search(r"^title:\s*(.+)$", raw, re.M)
        title = m2.group(1).strip().strip("\"'") if m2 else slug
        rows = extract(os.path.join(DOCS, name))
        per_doc[slug] = len(rows)
        for r in rows:
            items.append(
                {
                    "q": r["q"],
                    "a": r["a"],
                    "d": r["d"],
                    "s": slug,
                    "t": title,
                    "c": cats[0] if cats else "其他",
                }
            )

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    io.open(OUT, "w", encoding="utf-8").write(
        json.dumps(items, ensure_ascii=False, indent=1)
    )
    print("docs:", len(per_doc), "items:", len(items))
    for k, v in sorted(per_doc.items(), key=lambda x: -x[1]):
        print("  %-20s %d" % (k, v))


if __name__ == "__main__":
    main()
