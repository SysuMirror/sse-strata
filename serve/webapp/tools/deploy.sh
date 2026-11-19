#!/usr/bin/env bash
# 构建并发布前端到引擎的静态目录（../web）。
# 先构建到暂存目录，再逐文件拷贝、**index.html 最后写** —— 引擎按请求读盘，这样切换窗口最小：
# 新 index.html 引用的哈希文件都已就位，旧页面引用的旧文件在被删前也仍可用。
set -euo pipefail
cd "$(dirname "$0")/.."
STAGE="${1:-/tmp/strata-web-build}"
DEST="$(cd .. && pwd)/web"

echo "== 构建到 $STAGE =="
VITE="./node_modules/.bin/vite"
[ -x "$VITE" ] || VITE="npx vite"
"$VITE" build --outDir "$STAGE" --emptyOutDir

echo "== 发布到 $DEST =="
cp "$STAGE"/*.js "$STAGE"/*.css "$STAGE"/*.svg "$DEST"/
cp -r "$STAGE"/fonts/. "$DEST"/fonts/
cp "$STAGE"/index.html "$DEST"/          # 切换点：写完这一份，线上就是新版

# 清掉本次产物里已经不在的旧哈希文件。**只删新产物里没有的**，别按通配符无差别删 ——
# 先删后拷的写法在拷贝期间会让旧页面引用到已删除的 JS（几十毫秒的空档）。
# 放到 index.html 之后，切换窗口就只剩「一个文件的写入」。
for f in "$DEST"/app-*.js "$DEST"/style-*.css "$DEST"/chunk-*.js "$DEST"/*.worker-*.js; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"
  [ -e "$STAGE/$base" ] || rm -f "$f"
done

echo "== 完成 =="
ls -la "$DEST"
