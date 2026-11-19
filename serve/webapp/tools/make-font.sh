#!/usr/bin/env bash
# 腾讯体 TTF → 引擎能服务的 WOFF2。
#
# 为什么必须转：引擎的 /fonts/ 路由只放行 `.woff2`（serve/server.py，`name.endswith(".woff2")`），
# 直接塞 TTF 进去会 404；而且 /web/ 路由只认 .css/.js/.svg，字体也走不了那边。
#
# 用法：bash tools/make-font.sh <TencentSans.ttf> [输出名]
#   默认输出 public/fonts/tencent-sans-w7.woff2（构建时随 public/ 一起进产物，由 /fonts/ 服务）。
#
# 实测：8.1MB TTF（29002 字形 / 20910 汉字）→ 3.3MB WOFF2，约 42%。
# 去 hinting 没有收益（这个字体的 glyf 里本来就没有指令），所以不做子集化 ——
# 模型输出什么字都可能出现，砍字集等于给自己埋雷。
set -euo pipefail

SRC="${1:?用法: bash tools/make-font.sh <字体.ttf> [输出文件名]}"
OUT_NAME="${2:-tencent-sans-w7.woff2}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/public/fonts/${OUT_NAME}"

python3 - "$SRC" "$DEST" <<'PY'
import os, sys
from fontTools.ttLib import TTFont

src, dest = sys.argv[1], sys.argv[2]
f = TTFont(src)

name = f["name"]
def family(nid):
    for r in name.names:
        if r.nameID == nid and r.platformID == 3:
            try: return r.toUnicode()
            except Exception: pass
    return "?"
print(f"字体: {family(1)} / {family(2)}  字重 {f['OS/2'].usWeightClass}  字形 {len(f.getBestCmap())}")

f.flavor = "woff2"          # 需要 brotli：pip install --break-system-packages brotli
os.makedirs(os.path.dirname(dest), exist_ok=True)
f.save(dest)
print(f"→ {dest}  {os.path.getsize(dest):,} bytes ({os.path.getsize(dest)/1048576:.2f} MB)")
PY

echo
echo "记得在 public/tokens.css 的 @font-face 里声明它 —— 字重一定要写成区间（如 100 900），"
echo "否则浏览器会对本来就粗的 W7 再合成一次加粗，字会糊。"
