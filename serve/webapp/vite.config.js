import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";

// 产物直接落到引擎的静态目录 serve/web/：
//  * 引擎只服务 **扁平** 的 /web/<name>（名字里不能有 "/"，见 serve/server.py:2420-2435），
//    所以 assetsDir 必须为空、资源平铺在 outDir 根下；
//  * 只认 .css/.js/.svg 三种后缀 → public/ 里只放这三类（外加字体走 /fonts/ 路由）；
//  * 字体引用是 CSS 里的 url("../fonts/…")：CSS 落在 /web/x.css 时正好解析到 /fonts/…，
//    与引擎的 /fonts/ 路由对齐，因此 tokens.css 可以原样保留、不做任何改写。
export default defineConfig({
  base: "/web/",
  plugins: [vue()],
  publicDir: "public",
  build: {
    outDir: fileURLToPath(new URL("../web", import.meta.url)),
    emptyOutDir: true,
    assetsDir: "",
    sourcemap: false,
    cssCodeSplit: false,
    // Monaco 的 codicon 图标字体是独立 .ttf/.woff —— 引擎静态服务只发 .css/.js/.svg，
    // 独立字体请求会 404（编辑器本身没事，但折叠/查找图标会变成方块）。调高内联阈值，
    // 让 Vite 把字体以 data URI 打进 CSS。
    assetsInlineLimit: 1024 * 1024,
    rollupOptions: {
      output: {
        entryFileNames: "app-[hash].js",
        chunkFileNames: "chunk-[hash].js",
        assetFileNames: "[name]-[hash][extname]",
      },
    },
  },
});
