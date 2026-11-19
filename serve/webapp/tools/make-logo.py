#!/usr/bin/env python3
"""把品牌位图转成引擎能服务的形式：一个内嵌 base64 位图的 logo.svg。

为什么必须包成 SVG：Strata 引擎对 /web/<名字> 只服务**扁平**的 .css/.js/.svg
（serve/server.py:2420-2435），favicon.ico / logo.png 放进去一律 404。把位图嵌进
SVG 的 <image> 里就能走同一条路由，且任何浏览器都认。

用法：/home/cloud/Strata/.venv/bin/python tools/make-logo.py [源图] [输出] [边长=128]
"""
import base64
import io
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "brand" / "image.png"
out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent / "public" / "logo.svg"
size = int(sys.argv[3]) if len(sys.argv) > 3 else 128

im = Image.open(src).convert("RGBA").resize((size, size), Image.LANCZOS)
buf = io.BytesIO()
im.save(buf, "PNG", optimize=True)
b64 = base64.b64encode(buf.getvalue()).decode("ascii")

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
       f'width="{size}" height="{size}" role="img" aria-label="Strata">\n'
       f'  <image width="{size}" height="{size}" href="data:image/png;base64,{b64}"/>\n'
       f'</svg>\n')
out.write_text(svg, encoding="utf-8")
print(f"{src} -> {out}  ({size}px, 位图 {len(buf.getvalue()) / 1024:.1f} KB, SVG {len(svg) / 1024:.1f} KB)")
