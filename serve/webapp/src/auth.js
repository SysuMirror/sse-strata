// 登录态。
//
// 会话本身是**服务端签发的 HttpOnly Cookie** —— 前端读不到它，也不用管它，
// 浏览器每次请求自动带上。这里只缓存 `GET /auth/me` 的结果，供界面决定
// 「显示登录页」还是「显示应用」，以及页头要不要显示头像。
//
// **登录只拦 AI 对话这一件事**：监控、可视化、关于、会话历史都不登录就能用。
// 想对话时：要么登录，要么在设置里填 API token；两样都没有就跳到登录页（#login）。
import { reactive } from "vue";

export const auth = reactive({
  checked: false,          // 有没有问过服务端（没问过之前不要闪登录页）
  supported: false,        // 服务端**有没有**登录这套东西（见下）
  user: null,              // { id, name, email, avatar }
  app: { configured: null },   // GET /auth/config：登录服务配没配好
  loggingOut: false,
  showLogin: location.hash === "#login",   // 登录页是应用内的一页，不是标签页
});

// supported 这个标志是为了能「先发前端、后重启后端」：
// 后端还没启用登录时 /auth/me 会 404（被当成普通路径转给引擎），
// 这时**绝不能**把人拦在登录页 —— 否则旧代理 + 新前端 = 谁都进不去。
// 只有服务端明确回 401（「这个端点存在，但你没登录」）才算支持登录。
export async function fetchMe() {
  try {
    const r = await fetch("auth/me");
    if (r.status === 401) {
      auth.supported = true;
      auth.user = null;
    } else if (r.ok) {
      auth.supported = true;
      auth.user = (await r.json()).user;
    } else {
      auth.supported = false;   // 404 等：服务端还没这套东西
      auth.user = null;
    }
  } catch (e) {
    auth.supported = false;     // 连不上同理，不拦
    auth.user = null;
  } finally {
    auth.checked = true;
  }
}

export async function fetchAuthConfig() {
  try {
    const r = await fetch("auth/config");
    if (r.ok) auth.app = await r.json();
  } catch (e) {
    auth.app = { configured: null };   // 不说「没配」，只说「问不到」
  }
}

/** 跳到应用内的登录页（发对话时没登录就走这里） */
export function openLogin() {
  auth.showLogin = true;
  if (location.hash !== "#login") location.hash = "login";
}

/** 从登录页回到应用 */
export function backToApp() {
  auth.showLogin = false;
  if (location.hash === "#login") history.replaceState(null, "", location.pathname);
}

/** 真正去集市授权：服务端 302 到授权页，回到 / 时 Cookie 已经带好了 */
export function login() {
  location.href = "/auth/login";
}

export async function logout() {
  auth.loggingOut = true;
  try {
    await fetch("auth/logout", { method: "POST" });
  } catch (e) { /* 清不掉也就清不掉了，重新加载后 /auth/me 会说实话 */ }
  auth.user = null;
  location.reload();
}
