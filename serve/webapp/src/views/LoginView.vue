<script setup>
// 登录页。**只拦 AI 对话** —— 监控、可视化、关于、会话历史都不登录就能用，
// 所以这不是一道盖住整个应用的墙，而是应用内的一页（#login），页头还在、随时能切走。
//
// 两条路：
//   · 用软工集市账号登录（普通用户走这条，服务端代持引擎 key，用户永远看不到 key）；
//   · 直接在下面填 API token（开发者/压测走这条，等价于原来的用法）。
// 视觉只用原版设计系统的东西：卡片、按钮、输入框、令牌，不自创控件。
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import Icon from "../components/Icon.vue";
import { store, toast, headers } from "../api.js";
import { NAME, NAME_ZH } from "../brand.js";
import { auth, login } from "../auth.js";

const { t } = useI18n();
const emit = defineEmits(["back"]);
const token = ref("");
const busy = ref(false);
const err = ref("");

const logoSrc = "/web/logo.svg";

/** 填了 token 就存下来并验一次；验通了刷新页面，这时闸门自己就放开了。 */
async function useToken() {
  const v = token.value.trim();
  if (!v) return;
  store.set("apikey", v);
  busy.value = true;
  err.value = "";
  try {
    const r = await fetch("health", { headers: headers() });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    toast("success", t("login.token.saved"), t("login.token.saved.sub"));
    setTimeout(() => emit("back"), 400);          // 回应用，不用整页刷新
  } catch (e) {
    store.set("apikey", "");
    err.value = t("login.token.bad");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="gate">
    <div class="st-card gate__card">
      <div class="gate__brand">
        <img class="gate__logo" :src="logoSrc" alt="" width="40" height="40">
        <div>
          <div class="gate__name">{{ NAME }}<span class="gate__zh">{{ NAME_ZH }}</span></div>
          <div class="muted small">{{ t("login.tagline") }}</div>
        </div>
      </div>

      <p class="gate__lead">{{ t("login.lead") }}</p>

      <button class="st-btn st-btn--primary gate__go" :disabled="auth.app.configured === false"
              @click="login">
        <Icon name="link" cls="st-icon st-icon--sm" />{{ t("login.button") }}
      </button>
      <p v-if="auth.app.configured === false" class="muted small gate__warn">
        {{ t("login.notconfigured", { list: (auth.app.missing || []).join("、") }) }}
      </p>

      <div class="gate__sep"><span>{{ t("login.or") }}</span></div>

      <label class="st-field">
        <span class="st-label">{{ t("login.token") }} <output>{{ t("login.token.hint") }}</output></span>
        <input v-model="token" class="st-input" type="password" autocomplete="off"
               :placeholder="t('login.token.ph')" @keydown.enter="useToken">
      </label>
      <button class="st-btn st-btn--secondary gate__go" :disabled="!token.trim() || busy" @click="useToken">
        <Icon name="check" cls="st-icon st-icon--sm" />{{ t("login.token.use") }}
      </button>

      <p v-if="err" class="gate__err">{{ err }}</p>
      <p class="muted small gate__foot">{{ t("login.foot") }}</p>
      <button class="st-btn st-btn--secondary gate__back" @click="emit('back')">{{ t("login.back") }}</button>
    </div>
  </div>
</template>

<style scoped>
.gate { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center;
        padding: var(--st-s-6); overflow-y: auto; }
.gate__card { width: 100%; max-width: 430px; display: flex; flex-direction: column; gap: var(--st-s-4); }

.gate__brand { display: flex; align-items: center; gap: var(--st-s-3); }
.gate__logo { display: block; width: 40px; height: 40px; border-radius: 10px; flex: none; }
.gate__name { font-weight: var(--st-fw-black); font-size: var(--st-fs-xl); letter-spacing: -.02em; }
.gate__zh { margin-left: 8px; font-size: var(--st-fs-sm); font-weight: var(--st-fw-medium);
            color: var(--st-ink-muted); letter-spacing: .04em; }

.gate__lead { margin: 0; font-size: var(--st-fs-sm); line-height: 1.65; color: var(--st-ink-soft); }
.gate__go { width: 100%; }
.gate__warn { margin: -6px 0 0; color: var(--st-warn-text); }

/* 分隔线中间嵌一个「或者」 */
.gate__sep { display: flex; align-items: center; gap: var(--st-s-3); color: var(--st-ink-muted);
             font-size: var(--st-fs-xs); }
.gate__sep::before, .gate__sep::after { content: ""; flex: 1; height: 1px; background: var(--st-line); }

.gate__err { margin: 0; font-size: var(--st-fs-sm); color: var(--st-danger-text); }
.gate__back { align-self: center; height: 34px; font-size: var(--st-fs-sm); }
.gate__foot { margin: 0; padding-top: var(--st-s-2); border-top: 1px solid var(--st-line-soft); }
</style>
