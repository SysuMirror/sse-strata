import { createApp } from "vue";
import i18n, { setLang } from "./i18n.js";
import { store } from "./api.js";
import App from "./App.vue";

setLang(store.get("lang", "zh"));      // 主语言：中文
createApp(App).use(i18n).mount("#app");
