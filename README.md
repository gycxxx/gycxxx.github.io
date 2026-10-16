# 源码深读笔记 · GitHub Pages

本仓库即 [https://gycxxx.github.io](https://gycxxx.github.io) 的静态站点，由 **Hexo + NexT** 生成，内容来自 `D:/work/2026/deep-dive` 下的 17 篇源码深读笔记。

> 本站为纯静态产物，不含 Hexo 源工程。源工程在 `D:/work/2026/deep-dive-blog`。

## 本仓库结构

- `index.html` / `archives/` / `categories/` / `tags/` —— 首页、归档、分类、标签
- `kafka/` `mysql/` `redis/` `elasticsearch/` `spring-*` `java-*` `jdk/` `rag*` 等 —— 各篇文章
- `css/` `js/` `images/` `fonts/` —— 主题资源（NexT）
- `.nojekyll` —— 禁止 GitHub 用 Jekyll 再处理（NexT 已是纯静态）

## 如何更新并发布

源笔记改动后，在 **源工程目录** `D:/work/2026/deep-dive-blog` 重跑：

```bash
# 1) 把 deep-dive/*.md 重新转换为 Hexo 文章（生成 front-matter）
python convert_posts.py
# 2) 构建静态站点
hexo clean && hexo generate
# 3) 把 public/ 同步到本仓库（gycxxx.github.io）
xcopy /E /Y public\* ..\gycxxx.github.io\
#    （或用 cp -r public/. ../gycxxx.github.io/）
```

然后在 **本仓库目录** `D:/work/2026/gycxxx.github.io` 提交并推送：

```bash
git add -A
git commit -m "Site updated: <日期>"
git push origin master
```

推送成功后，GitHub Pages 通常 1 分钟内生效（强制刷新清缓存）。

## 已知说明

- **Mermaid 图**：文档里的 150+ 张 mermaid 流程图由 NexT 内置 `mermaid.js` + CDN `mermaid.min.js` 在浏览器端渲染；构建时通过 `highlight.exclude_languages: [mermaid]` 让 mermaid 围栏绕过语法高亮，输出为 `<pre><code class="highlight mermaid">` 供客户端渲染。
- **老站备份**：部署前 `D:/work/2026/gycxxx.github.io-legacy-backup` 保留了 2017 年老站的完整文件（未含 .git），如需取回可从此处复制。
- **内部目录锚点**：各文档自带的中文「目录」锚点已与 Hexo 生成的标题 id 对齐（CJK 保留、标点处理一致），点击可正常跳转。

## 本地预览

```bash
cd D:/work/2026/gycxxx.github.io
python -m http.server 4000
# 浏览器打开 http://localhost:4000
```
