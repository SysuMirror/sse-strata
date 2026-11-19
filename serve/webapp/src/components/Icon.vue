<script setup>
import { SPRITE } from "../icons.js";
defineProps({ name: { type: String, required: true }, cls: { type: String, default: "st-icon" } });
</script>

<template>
  <svg :class="cls" aria-hidden="true"><use :href="`${SPRITE}#i-${name}`" /></svg>
</template>
