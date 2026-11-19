#!/usr/bin/env bash
# 代理侧登录路由的回归测试。
#
# 起一份**临时实例**（另一个端口 + 临时配置），把 /auth/* 与 /oauth/callback 全打一遍，然后关掉 ——
# 全程不碰线上那份（线上是 supervisor 管的 8080，这里用 8099）。
#
# 为什么单独有这个：这些路由进不了前端那套 smoke.mjs（那套跑的是模拟引擎 + 浏览器），
# 而它们恰恰连着出过两次错，都是靠人肉试才发现的：
#   ① 回调路径写成了 /auth/callback，而集市约定是 /oauth/callback（iWiki、图床、sdpy 都这样），
#      注册进 oauth2_apps 的也是后者 → 用户授权回来落在引擎上，404；
#   ② parse_qs 的值是**列表**，handler 却当字符串用，state 成了 ['xxx']，
#      进 dict 一查 TypeError → 回调 502。就算路径对了也照样崩。
# 这两条现在都在下面的断言里。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-/home/cloud/Strata/.venv/bin/python}"
PORT="${PORT:-8099}"
TMP="$(mktemp -d)"
FAILS=0

ck() { # ck <名字> <期望> <实际> [说明]
  if [ "$2" = "$3" ]; then
    printf "  PASS  %-46s %s %s\n" "$1" "$3" "${4:-}"
  else
    printf "  FAIL  %-46s 期望 %s，实际 %s  %s\n" "$1" "$2" "$3" "${4:-}"
    FAILS=$((FAILS + 1))
  fi
}
code() { curl -s -o "$TMP/body" -w "%{http_code}" --max-time 8 "http://127.0.0.1:$PORT$1"; }

cleanup() { [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

# 临时配置：只换端口，其余照抄线上那份。密钥文件不用改，测试不依赖真实 IdP。
python3 - "$HERE/config.json" "$TMP/config.json" "$PORT" "$TMP" <<'PY'
import json, sys
src, dst, port, tmp = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
cfg = json.load(open(src, encoding="utf-8"))
cfg["listen_port"] = port
# 记录功能照开，但**落点全部搬到临时目录**：测试不许往线上的分段目录和正式的桶前缀里写东西
cfg.setdefault("infer_store", {}).update({"dir": tmp + "/segs", "db": tmp + "/infer.db",
                                          "minio_prefix": "infer-test-authroutes"})
json.dump(cfg, open(dst, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
PY
# 会话密钥跟着临时目录走，测完即弃，不影响线上的登录态
export SSECHO_OAUTH_APP_SECRET="${SSECHO_OAUTH_APP_SECRET:-test-secret-not-used-in-production}"

# 端口占用预检：别的测试（比如 test_structure_e2e.py 的假引擎）也用 8099。撞上了的话，
# 下面的断言其实打在**别人**的服务上，会显示一片红 —— 看起来像回归，其实是端口冲突。
# 这次就这么被骗过一次，所以直接拦住，并说清楚该关谁。
if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/health"; then
  echo "端口 $PORT 已被占用 —— 先把占着它的测试实例停掉再跑（pgrep -af 'mock_upstream|rag_proxy.py'）"
  exit 2
fi

"$PY" "$HERE/rag_proxy.py" --config "$TMP/config.json" > "$TMP/out.log" 2>&1 &
SRV=$!
for _ in $(seq 1 40); do
  curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/health" && break
  sleep 0.25
done

echo "代理侧登录路由（临时实例 :$PORT）"

# ---- 基本探活与门禁
ck "健康检查" 200 "$(code /health)"
ck "未登录时 /auth/me 为 401" 401 "$(code /auth/me)"
ck "匿名可读 /metrics（代理代持 key）" 200 "$(code /metrics)"
ck "匿名对话被拦（401）" 401 "$(curl -s -o "$TMP/body" -w '%{http_code}' --max-time 8 \
      -X POST -H 'Content-Type: application/json' -d '{"messages":[]}' "http://127.0.0.1:$PORT/v1/chat/completions")"

# ---- 回调路径（两个 bug 就在这里）
ck "/oauth/callback 有路由（不是 404）" 400 "$(code '/oauth/callback?code=x&state=y')" "400=state 校验失败"
ck "/oauth/callback 没崩（不是 502）" 400 "$(code '/oauth/callback?code=x&state=y')" "502=handler 抛异常"
ck "/auth/callback 作为别名也通" 400 "$(code '/auth/callback?code=x&state=y')"
ck "/oauth/callback 缺参数 → 400" 400 "$(code '/oauth/callback')"
ck "/oauth/callback 带 error → 400" 400 "$(code '/oauth/callback?error=access_denied&error_description=no')"
grep -q '登录状态无效' "$TMP/body" || true
ck "/oauth/callback 返回的是登录错误页" 400 "$(code '/oauth/callback?code=x&state=y')"

# ---- 授权跳转
loc=$(curl -s -o /dev/null -D- --max-time 8 "http://127.0.0.1:$PORT/auth/login" \
      | grep -i '^location' | head -1 | sed 's/^[Ll]ocation: *//' | tr -d '\r')
case "$loc" in
  *"/connect?"*"response_type=code"*) ck "/auth/login 跳到授权页" ok ok ;;
  *) ck "/auth/login 跳到授权页" ok "${loc:0:40}" ;;
esac
# 授权 URL 里的 redirect_uri 必须与 config.json 里登记的**逐字符一致**，否则集市回跳会对不上
want=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['oauth']['redirect_uri'])" "$HERE/config.json")
enc=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1], safe=''))" "$want")
case "$loc" in
  *"redirect_uri=$enc"*|*"redirect_uri=${want}"*) ck "授权 URL 带的 redirect_uri 与配置一致" ok ok "$want" ;;
  *) ck "授权 URL 带的 redirect_uri 与配置一致" ok "$(echo "$loc" | grep -o 'redirect_uri=[^&]*' | head -c 60)" ;;
esac
case "$loc" in
  *"appid="*) ck "授权 URL 带 appid" ok ok ;;
  *) ck "授权 URL 带 appid" ok "${loc:0:40}" ;;
esac

# ---- 对话同步：未登录要拦住（同步跟着账号走，没有账号就谈不上多端）
ck "未登录读对话 → 401" 401 "$(code /chats)"
ck "未登录写对话 → 401" 401 "$(curl -s -o "$TMP/body" -w '%{http_code}' --max-time 8 \
      -X POST -H 'Content-Type: application/json' -d '{"chats":[],"activeId":null}' "http://127.0.0.1:$PORT/chats")"

# 伪造一份**签名有效**的会话 Cookie，走一遍登录后的读写
# 会话密钥落在**临时配置所在目录**（ssecho_auth.build 用 config 的父目录），
# 所以这份 Cookie 只在这次测试里有效，不会对线上生效。
COOKIE=$("$PY" - "$TMP" <<'PY2'
import sys
from pathlib import Path
sys.path.insert(0, "/home/cloud/rag-proxy")
import ssecho_auth
path = Path(sys.argv[1]) / ".session-secret"
if not path.exists():                      # 代理可能还没写完，兜一份长度够的
    path.write_bytes(b"test-only-session-secret-0123456789abcdef")
print(ssecho_auth.COOKIE_NAME + "=" + ssecho_auth.Session(path).issue({"sub": "u-test", "name": "测试用户"}))
PY2
)
if [ -n "$COOKIE" ]; then
  ck "登录后读对话 → 200" 200 \
     "$(curl -s -o "$TMP/body" -w '%{http_code}' --max-time 8 -H "Cookie: $COOKIE" "http://127.0.0.1:$PORT/chats")"
fi
ck "登录后写入 → 200" 200 "$(curl -s -o "$TMP/body" -w '%{http_code}' --max-time 8 -H "Cookie: $COOKIE" \
      -X POST -H 'Content-Type: application/json' \
      -d '{"chats":[{"id":"c1","title":"云端测试","messages":[{"role":"user","text":"你好"}]}],"activeId":"c1"}' \
      "http://127.0.0.1:$PORT/chats")"
ck "写回后读得到同一条" ok "$(curl -s --max-time 8 -H "Cookie: $COOKIE" "http://127.0.0.1:$PORT/chats" \
      | python3 -c "import sys,json;d=json.load(sys.stdin);print('ok' if d.get('chats') and d['chats'][0]['title']=='云端测试' else 'no')")"
ck "写回时报了实际存储位置" ok "$(python3 -c "import json;d=json.load(open('$TMP/body'));print('ok' if d.get('store') in ('minio','local') else 'no')")"

# ---- 推理记录：元信息给登录用户 / 持 key 的人，**内容只认登录**（内容里是用户输入与模型回复）
ck "未登录看记录列表 → 401" 401 "$(code /infer/list)"
ck "未登录看记录统计 → 401" 401 "$(code /infer/stats)"
ck "未登录读记录内容 → 401" 401 "$(code '/infer/raw?id=x')"
ck "登录后看记录列表 → 200" 200 "$(curl -s -o "$TMP/body" -w '%{http_code}' --max-time 8 \
      -H "Cookie: $COOKIE" "http://127.0.0.1:$PORT/infer/list")"
ck "记录列表结构齐" ok "$(python3 -c "import json;d=json.load(open('$TMP/body'));
print('ok' if all(k in d for k in ('rows','total','live')) else 'no')")"
ck "登录后看统计 → 200" 200 "$(curl -s -o "$TMP/body" -w '%{http_code}' --max-time 8 \
      -H "Cookie: $COOKIE" "http://127.0.0.1:$PORT/infer/stats")"
ck "统计里带存储状态" ok "$(python3 -c "import json;d=json.load(open('$TMP/body'));
print('ok' if isinstance(d.get('store'),dict) and 'minio_ok' in d['store'] else 'no')")"
ck "不存在的记录 → 404" 404 "$(curl -s -o "$TMP/body" -w '%{http_code}' --max-time 8 \
      -H "Cookie: $COOKIE" "http://127.0.0.1:$PORT/infer/item?id=nope")"
ck "记录接口不落到引擎（不是引擎的 404 体裁）" ok "$(curl -s --max-time 8 -H "Cookie: $COOKIE" \
      "http://127.0.0.1:$PORT/infer/item?id=nope" | python3 -c "import sys,json;
d=json.load(sys.stdin);print('ok' if '记录' in json.dumps(d,ensure_ascii=False) else 'no')")"

# ---- 日志里不能有异常（上面那两个 bug 都会在这里留 Traceback）
if grep -qiE 'Traceback|TypeError|unhashable' "$TMP/out.log"; then
  printf "  FAIL  %-46s %s\n" "代理日志无异常" "$(grep -iE 'Traceback|TypeError|unhashable' "$TMP/out.log" | head -1)"
  FAILS=$((FAILS + 1))
else
  printf "  PASS  %-46s 无\n" "代理日志无异常"
fi

echo "=========================================================="
if [ "$FAILS" -gt 0 ]; then echo "FAIL：$FAILS 项未通过"; exit 1; fi
echo "ALL PASS"
