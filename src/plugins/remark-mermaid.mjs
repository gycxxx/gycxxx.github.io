function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// 在 remark 阶段（Shiki 高亮之前）把 ```mermaid 代码块换成原始文本的
// <div class="mermaid">，避免被 Shiki 转义成 HTML 实体后 mermaid 无法解析。
export default function remarkMermaid() {
  return (tree) => {
    const walk = (node) => {
      if (!node || !Array.isArray(node.children)) return;
      for (let i = 0; i < node.children.length; i++) {
        const child = node.children[i];
        if (child.type === 'code' && child.lang === 'mermaid') {
          node.children[i] = {
            type: 'html',
            value: '<div class="mermaid">\n' + escapeHtml(child.value) + '\n</div>',
          };
          continue;
        }
        walk(child);
      }
    };
    walk(tree);
  };
}
