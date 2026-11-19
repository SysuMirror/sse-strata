<script setup>
// 图片查看器：点开看大图 + 下载。聊天里生成的图、上传的图、首页画廊、工作台预览都用它。
// 下载走 fetch→blob，因为签名直链 / dataURL / 跨域图用 <a download> 不一定生效。
import { onMounted, onBeforeUnmount, ref } from "vue";
import Icon from "./Icon.vue";

const props = defineProps({
  src: { type: String, required: true },
  name: { type: String, default: "" },
});
const emit = defineEmits(["close"]);
const busy = ref(false);

function close() { emit("close"); }
function onKey(e) { if (e.key === "Escape") close(); }

function guessName() {
  if (props.name) return props.name;
  const ext = (props.src.match(/\.(png|jpe?g|webp|gif)(\?|$)/i) || [])[1] || "png";
  return `image-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.${ext.toLowerCase()}`;
}

async function download() {
  if (busy.value) return;
  busy.value = true;
  const name = guessName();
  try {
    const r = await fetch(props.src);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (e) {
    // 拿不到 blob（跨域/网络）就退回新窗口打开，至少能另存
    const a = document.createElement("a");
    a.href = props.src; a.download = name; a.target = "_blank"; a.rel = "noopener";
    document.body.appendChild(a); a.click(); a.remove();
  } finally {
    busy.value = false;
  }
}

onMounted(() => document.addEventListener("keydown", onKey));
onBeforeUnmount(() => document.removeEventListener("keydown", onKey));
</script>

<template>
  <div class="imgview" role="dialog" aria-modal="true" @click.self="close">
    <header class="imgview__bar">
      <span class="imgview__name">{{ name || "image" }}</span>
      <button class="st-btn st-btn--secondary imgview__btn" :disabled="busy" @click="download">
        <Icon name="download" cls="st-icon st-icon--sm" />{{ busy ? "下载中…" : "下载" }}</button>
      <button class="st-btn st-btn--icon imgview__btn" title="关闭（Esc）" aria-label="关闭" @click="close">✕</button>
    </header>
    <img class="imgview__img" :src="src" :alt="name || 'image'" @click.stop>
  </div>
</template>

<style scoped>
.imgview { position: fixed; inset: 0; z-index: 90; background: rgba(8, 12, 22, .86); display: grid;
           grid-template-rows: auto minmax(0, 1fr); padding: 14px 16px calc(14px + env(safe-area-inset-bottom)); }
.imgview__bar { display: flex; align-items: center; gap: 10px; color: #e5e7eb; padding-bottom: 10px; }
.imgview__name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                 font-size: var(--st-fs-sm); opacity: .85; }
.imgview__btn { flex: none; }
.imgview__img { max-width: 100%; max-height: 100%; width: auto; height: auto; margin: 0 auto; object-fit: contain;
                border-radius: 10px; box-shadow: 0 24px 80px rgba(0,0,0,.5); cursor: default; }
@media (max-width: 720px) {
  .imgview { padding: 10px 10px calc(10px + env(safe-area-inset-bottom)); }
  .imgview__name { display: none; }
  .imgview__bar { justify-content: flex-end; }
}
</style>