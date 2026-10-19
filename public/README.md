# gycxxx.github.io

个人源码深读笔记站，由 [Astro](https://astro.build) 静态生成（2026-10 起，此前为 Hexo）。

- 站源工程：本地 `deep-dive-site/`（Astro 5 + Content Collections）
- 内容事实源：`deep-dive/` 下的 Markdown 源码笔记，所有结论带 `文件:行号`
- 构建：`python scripts/sync_content.py && npx astro build`
- 发布：`python scripts/deploy.py --push`

`public/.nojekyll` 是必需的：没有它，GitHub Pages 的 Jekyll 会丢弃 `_astro/` 目录。
