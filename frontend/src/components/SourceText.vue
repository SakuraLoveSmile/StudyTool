<template>
  <div class="source-text">
    <p v-for="(paragraph, index) in paragraphs" :key="index">{{ paragraph }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{ content: string }>();

// Only separate existing paragraphs for display; keep source text and line breaks intact.
const paragraphs = computed(() => props.content.split(/\r?\n(?:[\t ]*\r?\n)+/));
</script>

<style scoped>
.source-text {
  max-width: 42rem;
  margin-inline: auto;
  color: rgb(var(--c-ink));
  font-size: 1rem;
  line-height: 1.9;
  overflow-wrap: anywhere;
  tab-size: 4;
}

.source-text p {
  white-space: pre-wrap;
}

.source-text p + p {
  margin-top: 1em;
}
</style>
