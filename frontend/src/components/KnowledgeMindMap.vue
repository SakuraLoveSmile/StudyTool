<template>
  <div class="mind-map" aria-label="章节思维导图">
    <div class="map-root">{{ map.title }}</div>
    <ul class="map-branches">
      <li v-for="(branch, index) in map.children" :key="index">
        <details open class="map-branch">
          <summary>{{ branch.title }}<span v-if="examConcepts?.includes(branch.title)" class="ml-2 text-xs text-warn">真题已考</span></summary>
          <ul v-if="branch.children.length" class="map-leaves">
            <li v-for="(leaf, leafIndex) in branch.children" :key="leafIndex">
              <button type="button" class="map-leaf" @click="emit('select', leaf.title)">
                {{ leaf.title }}<span v-if="examConcepts?.includes(leaf.title)" class="ml-2 text-xs text-warn">真题已考</span><span class="block text-xs text-faint mt-1">查看概念</span>
              </button>
            </li>
          </ul>
          <button type="button" class="map-leaf mt-2" @click="emit('select', branch.title)">查看“{{ branch.title }}”的概念说明</button>
        </details>
      </li>
    </ul>
    <p class="text-xs text-faint mt-4">点击主题可展开或收起，点击末级节点查看概念和来源。</p>
  </div>
</template>

<script setup lang="ts">
import type { LibraryMindMap } from '../types/library';
defineProps<{ map: LibraryMindMap; examConcepts?: string[] }>();
const emit = defineEmits<{ select: [title: string] }>();
</script>

<style scoped>
.mind-map { min-width: 0; overflow-wrap: anywhere; }
.map-root { border: 1px solid rgb(var(--c-accent) / .25); border-radius: .8rem; padding: 1rem; background: rgb(var(--c-accent) / .07); font-weight: 600; color: rgb(var(--c-ink)); }
.map-branches { margin: 0 0 0 .9rem; padding: 1rem 0 0 1.1rem; border-left: 2px solid rgb(var(--c-line)); display: grid; gap: 1rem; list-style: none; }
.map-branch { border: 1px solid rgb(var(--c-line)); border-radius: .8rem; padding: .8rem; background: rgb(var(--c-raised) / .45); min-width: 0; }
summary { cursor: pointer; color: rgb(var(--c-ink)); font-weight: 600; padding: .2rem 0; }
summary:focus-visible, .map-leaf:focus-visible { outline: 2px solid rgb(var(--c-accent)); outline-offset: 3px; }
.map-leaves { list-style: none; padding: .8rem 0 0 .9rem; margin-left: .3rem; border-left: 1px solid rgb(var(--c-line)); display: grid; gap: .6rem; }
.map-leaf { width: 100%; text-align: left; border-radius: .6rem; border: 1px solid rgb(var(--c-line)); background: rgb(var(--c-surface)); padding: .7rem; color: rgb(var(--c-sub)); overflow-wrap: anywhere; }
.map-leaf:hover { border-color: rgb(var(--c-accent) / .5); color: rgb(var(--c-accent)); }
</style>
