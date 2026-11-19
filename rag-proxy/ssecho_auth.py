#!/usr/bin/env python3
"""软工集市统一 OAuth 登录 + 本地会话 Cookie。

协议是**授权码模式**（和 iWiki、图床用的是同一套）：

    1. GET  {base}/connect?appid=…&redirect_uri=…&response_type=code&scope=read_basic&state=…
    2. 用户在集市授权 → 302 回 {redirect_uri}?code=…&state=…
    3. POST {api}/api/auth/connect/token     表单 {code, appid, app_secret} → {access_token}
    4. POST {api}/api/auth/connect/userinfo  Bearer access_token → {user_id, name, email, avatar_url, …}
    5. 本地签发一张 JWT 塞进 HttpOnly Cookie（7 天）

**为什么用 Cookie，而不是照抄参考实现把 JWT 交给前端存**：
前端给引擎的鉴权走的是 `Authorization: Bearer <引擎 key>` / `x-api-key`。会话要是也用
Authorization，同一个头就没法同时表达「我是谁」和「我带了引擎 key」——两件事会打架。
Cookie 天然分开，而且是 HttpOnly：脚本读不到，XSS 也偷不走。

只用标准库（hmac/hashlib/json/urllib），不引第三方 —— 这个代理本来就只有 numpy 一个依赖，
不该为登录再多背一个 JWT 库。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COOKIE_NAME = "ssecho_session"
SESSION_TTL_S = 7 * 86400          # 登录态有效期（和参考实现一致：7 天）
STATE_TTL_S = 900                  # state 有效期
HTTP_TIMEOUT_S = 10


# ------------------------------------------------------------------ 小工具

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _unwrap(body: bytes):
    """IdP 的响应有时是裸对象、有时包一层 {code,data}，两种都认。"""
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("data"), (dict, list)):
        return obj["data"]
    return obj


def _err_message(body: bytes, fallback: str) -> str:
    obj = _unwrap(body)
    if isinstance(obj, dict):
        for k in ("message", "msg", "error", "error_description"):
            if obj.get(k):
                return str(obj[k])
    return fallback


# ------------------------------------------------------------------ 配置

class OAuthConfig:
    """来自 config.json 的 oauth 段；环境变量 SSECHO_OAUTH_* 可覆盖。

    前缀特意用 SSECHO_：部署平台自己会注入 OAUTH_* 之类的变量，
    裸名字会和平台撞车（图床那边就在 README 里专门踩过这个坑）。
    """

    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        env = os.environ

        def pick(key: str, default: str = "") -> str:
            return env.get("SSECHO_OAUTH_" + key.upper()) or cfg.get(key) or default

        self.base_url = pick("base_url", "https://ssemarket.cn/new").rstrip("/")
        api = pick("api_base_url")
        # 授权页在 /new 下，API 不在 —— 没显式给就从 base_url 去掉 /new 推
        self.api_base = (api or self.base_url[:-4] if self.base_url.endswith("/new") else api).rstrip("/")
        self.app_id = pick("app_id")
        self.redirect_uri = pick("redirect_uri")
        self.authorize_path = pick("authorize_path", "/connect")
        self.scope = pick("scope", "read_basic")
        self.app_secret = env.get("SSECHO_OAUTH_APP_SECRET") or self._secret_from_file(pick("secret_file")) \
            or cfg.get("app_secret") or ""

    @staticmethod
    def _secret_from_file(path: str) -> str:
        """密钥单独放一个不进 git 的文件（和引擎 key 一个先例：~/logs/ 已被 .gitignore）。

        config.json 是**被 git 跟踪的**，把 app_secret 写进去就等于提交进仓库 ——
        参考包里那个图床就把密钥明文写在 config.yaml 里，它自己的 README 都在警告别这么干。
        """
        if not path:
            return ""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return str(data.get("app_secret") or "")
        except (OSError, ValueError):
            return ""

    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.redirect_uri)

    def missing(self) -> list[str]:
        lack = []
        if not self.app_id:
            lack.append("app_id")
        if not self.app_secret:
            lack.append("app_secret")
        if not self.redirect_uri:
            lack.append("redirect_uri")
        return lack


# ------------------------------------------------------------------ 会话 JWT

class Session:
    """HS256 的极简 JWT。密钥持久化到文件，重启不踢人。"""

    def __init__(self, secret_path: Path):
        self.secret_path = secret_path
        self._secret = b""
        self._load()

    def _load(self):
        try:
            data = self.secret_path.read_bytes().strip()
            if len(data) >= 32:
                self._secret = data
                return
        except OSError:
            pass
        self._secret = secrets.token_bytes(48)
        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_path.write_bytes(self._secret)
        os.chmod(self.secret_path, 0o600)

    def issue(self, claims: dict) -> str:
        head = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        body = dict(claims, iat=int(time.time()), exp=int(time.time()) + SESSION_TTL_S)
        payload = _b64e(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
        sig = _b64e(hmac.new(self._secret, f"{head}.{payload}".encode(), hashlib.sha256).digest())
        return f"{head}.{payload}.{sig}"

    def verify(self, token: str) -> dict | None:
        try:
            head, payload, sig = token.split(".")
        except (ValueError, AttributeError):
            return None
        want = _b64e(hmac.new(self._secret, f"{head}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):          # 常数时间比较，别给时序侧信道
            return None
        try:
            claims = json.loads(_b64d(payload))
        except Exception:
            return None
        if int(claims.get("exp", 0)) < time.time():
            return None
        return claims


# ------------------------------------------------------------------ OAuth

class OAuth:
    def __init__(self, cfg: OAuthConfig, state_secret: bytes, session: Session):
        self.cfg = cfg
        self.session = session
        self._state_secret = state_secret
        self._states: dict[str, float] = {}             # state -> 过期时间（进程内一次性）

    # ---- state（防 CSRF）：随机串 + HMAC 签名，不用服务端存储也能验真伪
    def _sign(self, nonce: str) -> str:
        return hmac.new(self._state_secret, nonce.encode(), hashlib.sha256).hexdigest()

    def create_state(self) -> str:
        nonce = secrets.token_hex(24)                   # 48 hex
        state = nonce + self._sign(nonce)[:16]
        self._expire_states()
        self._states[state] = time.time() + STATE_TTL_S
        return state

    def consume_state(self, state: str) -> bool:
        self._expire_states()
        if not state or self._states.pop(state, 0) < time.time():
            return False
        nonce = state[:48]
        return hmac.compare_digest(state[48:], self._sign(nonce)[:16])

    def _expire_states(self):
        now = time.time()
        for k in [k for k, v in self._states.items() if v < now]:
            self._states.pop(k, None)
        if len(self._states) > 4096:                    # 兜底：别让没人回调的 state 堆着
            self._states.clear()

    def authorize_url(self, state: str) -> str:
        q = urllib.parse.urlencode({
            "appid": self.cfg.app_id, "app_id": self.cfg.app_id,
            "redirect_uri": self.cfg.redirect_uri,
            "response_type": "code", "scope": self.cfg.scope, "state": state,
        })
        return f"{self.cfg.base_url}{self.cfg.authorize_path}?{q}"

    # ---- IdP 调用
    def _post(self, url: str, data: bytes | None, ctype: str, bearer: str = ""):
        req = urllib.request.Request(url, data=data, method="POST")
        if ctype:
            req.add_header("Content-Type", ctype)
        if bearer:
            req.add_header("Authorization", "Bearer " + bearer)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 0, json.dumps({"message": f"无法连接登录服务：{e}"}).encode()

    def exchange_code(self, code: str) -> tuple[str, str]:
        """code 换 access_token。返回 (token, 错误)。

        表单字段严格按 IdP 契约来：`code / appid / app_id / app_secret`，**不带 redirect_uri**
        （接入指南里就是这么写的；多塞参数是自找麻烦）。内容类型必须是
        application/x-www-form-urlencoded —— IdP 那边是 `c.PostForm` 取参，发 JSON 取不到。
        """
        form = urllib.parse.urlencode({
            "code": code, "appid": self.cfg.app_id, "app_id": self.cfg.app_id,
            "app_secret": self.cfg.app_secret,
        }).encode()
        status, body = self._post(self.cfg.api_base + "/api/auth/connect/token",
                                  form, "application/x-www-form-urlencoded")
        if status < 200 or status >= 300:
            return "", _err_message(body, "换取 access_token 失败")
        data = _unwrap(body) or {}
        token = data.get("access_token") or ""
        return (token, "") if token else ("", "登录服务没有返回 access_token")

    def userinfo(self, access_token: str) -> tuple[dict, str]:
        status, body = self._post(self.cfg.api_base + "/api/auth/connect/userinfo",
                                  None, "application/json", bearer=access_token)
        if status < 200 or status >= 300:
            return {}, _err_message(body, "获取用户信息失败")
        data = _unwrap(body) or {}
        if not data.get("name"):
            return {}, "用户信息不完整"
        return data, ""

    def check_app(self) -> tuple[dict, str]:
        payload = json.dumps({"appid": self.cfg.app_id, "app_id": self.cfg.app_id}).encode()
        status, body = self._post(self.cfg.api_base + "/api/auth/connect/check", payload, "application/json")
        if status < 200 or status >= 300:
            return {}, _err_message(body, "应用校验失败")
        data = _unwrap(body) or {}
        return {"app_name": data.get("app_name") or "", "app_icon": data.get("app_icon") or ""}, ""

    # ---- 会话
    def claims_of(self, profile: dict) -> dict:
        uid = profile.get("user_id")
        return {
            "sub": str(uid) if uid is not None else "",
            "name": profile.get("name") or "",
            "email": profile.get("email") or "",
            "avatar": profile.get("avatar_url") or "",
            "intro": profile.get("intro") or "",
        }

    def cookie(self, token: str, max_age: int | None = None) -> str:
        age = SESSION_TTL_S if max_age is None else max_age
        # 不设 Secure：容器里是明文 HTTP，TLS 在中继那侧终结（见容器规范 §1），
        # 设了 Secure 浏览器就不会回传 Cookie，登录会「成功但立刻又变未登录」。
        parts = [f"{COOKIE_NAME}={token}", "Path=/", f"Max-Age={age}", "HttpOnly", "SameSite=Lax"]
        return "; ".join(parts)

    def clear_cookie(self) -> str:
        return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


# ------------------------------------------------------------------ 工厂

def build(cfg: dict, secret_dir: Path) -> OAuth:
    ocfg = OAuthConfig(cfg.get("oauth") if isinstance(cfg, dict) else None)
    state_secret = (ocfg.app_secret or "ssecho-state").encode()
    return OAuth(ocfg, state_secret, Session(secret_dir / ".session-secret"))


def session_cookie_value(cookie_header: str | None) -> str:
    """从 Cookie 头里取出会话 JWT。"""
    if not cookie_header:
        return ""
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME:
            return v
    return ""
