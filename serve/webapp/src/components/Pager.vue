<script setup>
// 列表分页条。视觉只用原版已有的东西：st-btn--secondary 做翻页按钮、muted small 做说明、
// tabular-nums 让页码不跳动。没有新控件。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import Icon from "./Icon.vue";

const props = defineProps({
  page: { type: Number, default: 1 },
  perPage: { type: Number, default: 8 },
  total: { type: Number, default: 0 },
});
const emit = defineEmits(["update:page"]);
const { t } = useI18n();

const pages = computed(() => Math.max(1, Math.ceil(props.total / props.perPage)));
const from = computed(() => (props.total ? (props.page - 1) * props.perPage + 1 : 0));
const to = computed(() => Math.min(props.total, props.page * props.perPage));
const go = (p) => emit("update:page", Math.max(1, Math.min(pages.value, p)));
</script>

<template>
  <div v-if="total > perPage" class="pager">
    <span class="muted small pager__range">{{ t("page.range", { from, to, total }) }}</span>
    <div class="pager__ctl">
      <button type="button" class="st-btn st-btn--secondary pager__btn" :disabled="page <= 1"
              :aria-label="t('page.prev')" @click="go(page - 1)">
        <Icon name="chevron" cls="st-icon st-icon--sm pager__chev" />
      </button>
      <span class="pager__now">{{ t("page.of", { page, pages }) }}</span>
      <button type="button" class="st-btn st-btn--secondary pager__btn" :disabled="page >= pages"
              :aria-label="t('page.next')" @click="go(page + 1)">
        <Icon name="chevron" cls="st-icon st-icon--sm" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.pager { display: flex; align-items: center; justify-content: space-between; gap: var(--st-s-3);
         padding-top: var(--st-s-3); margin-top: var(--st-s-2); border-top: 1px solid var(--st-line-soft); }
.pager__range { font-variant-numeric: tabular-nums; }
.pager__ctl { display: inline-flex; align-items: center; gap: var(--st-s-2); }
.pager__btn { width: 32px; height: 32px; padding: 0; }
.pager__chev { transform: rotate(90deg); }      /* chevron 是朝下的，转成朝左 */
.pager__now { min-width: 56px; text-align: center; font-size: var(--st-fs-sm); color: var(--st-ink-soft);
              font-variant-numeric: tabular-nums; }
</style>
