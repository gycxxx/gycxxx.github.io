import type { CollectionEntry } from "astro:content";

export const CATEGORY_ORDER = [
  "中间件",
  "Spring",
  "存储与缓存",
  "Java",
  "游戏与云原生",
  "RAG",
  "RAG 与 Agent",
  "学习路线",
];

export function sortByDateDesc<T extends CollectionEntry<"docs">>(docs: T[]): T[] {
  return [...docs].sort((a, b) => b.data.date.valueOf() - a.data.date.valueOf());
}

export function href(entry: CollectionEntry<"docs">) {
  return `/${entry.id}/`;
}

export function groupByCategory(docs: CollectionEntry<"docs">[]) {
  const map = new Map<string, CollectionEntry<"docs">[]>();
  for (const d of docs) {
    for (const c of d.data.categories) {
      if (!map.has(c)) map.set(c, []);
      map.get(c)!.push(d);
    }
  }
  const known = CATEGORY_ORDER.filter((c) => map.has(c)).map((c) => [c, map.get(c)!] as const);
  const extra = [...map.keys()]
    .filter((c) => !CATEGORY_ORDER.includes(c))
    .sort()
    .map((c) => [c, map.get(c)!] as const);
  return [...known, ...extra];
}

export function allTags(docs: CollectionEntry<"docs">[]) {
  const map = new Map<string, CollectionEntry<"docs">[]>();
  for (const d of docs) {
    for (const t of d.data.tags) {
      if (!map.has(t)) map.set(t, []);
      map.get(t)!.push(d);
    }
  }
  return [...map.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
}

/** 取文章正文里第一段像样的文字做摘要 */
export function excerpt(body: string | undefined, max = 90) {
  if (!body) return "";
  const lines = body.split("\n");
  for (const ln of lines) {
    const s = ln.trim();
    if (!s) continue;
    if (/^(#|>|\||---|\*|-|\d+\.|```|<)/.test(s)) continue;
    const flat = s.replace(/[*`_[\]()]/g, "").trim();
    if (flat.length < 10) continue;
    return flat.length > max ? flat.slice(0, max) + "…" : flat;
  }
  return "";
}
