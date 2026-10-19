import { defineConfig } from 'astro/config';
import remarkMermaid from './src/plugins/remark-mermaid.mjs';

// 站点与 Hexo 版保持同一套 URL 形态：/slug/、/categories/xx/、/tags/xx/
export default defineConfig({
  site: 'https://gycxxx.github.io',
  trailingSlash: 'always',
  build: {
    format: 'directory',
  },
  markdown: {
    shikiConfig: {
      theme: 'github-light',
      wrap: false,
    },
    remarkPlugins: [remarkMermaid],
  },
});
