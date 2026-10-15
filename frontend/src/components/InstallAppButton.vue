<template>
  <div v-if="visible" ref="rootEl" class="relative" @keydown.escape="open = false">
    <button
      type="button"
      :class="variant === 'menu'
        ? 'flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm font-medium text-sub transition-colors hover:bg-raised hover:text-ink'
        : 'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-line bg-surface px-3 py-1.5 text-xs font-medium text-sub transition-colors hover:bg-raised hover:text-ink'"
      :disabled="prompting"
      :aria-expanded="open"
      @click="handleInstall"
    >
      <MonitorDown class="h-4 w-4 shrink-0" aria-hidden="true" />
      <span>{{ prompting ? '正在添加…' : '添加到桌面' }}</span>
    </button>
    <div
      v-if="open && message"
      class="z-50 rounded-xl border border-line bg-surface p-4 text-sm leading-relaxed text-sub shadow-pop"
      :class="variant === 'menu' ? 'mt-2' : 'absolute right-0 mt-2 w-[min(20rem,calc(100vw-2rem))]'"
      role="status"
    >
      <p>{{ message }}</p>
      <button type="button" class="mt-3 text-xs font-medium text-accent hover:underline" @click="open = false">知道了</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { MonitorDown } from 'lucide-vue-next';
import { useInstallApp } from '../composables/useInstallApp';

withDefaults(defineProps<{ variant?: 'button' | 'menu' }>(), { variant: 'button' });
const { visible, prompting, message, install } = useInstallApp();
const open = ref(false);
const rootEl = ref<HTMLElement | null>(null);

async function handleInstall(): Promise<void> {
  await install();
  open.value = Boolean(message.value);
}

function closeOutside(event: MouseEvent): void {
  if (open.value && rootEl.value && !rootEl.value.contains(event.target as Node)) open.value = false;
}
onMounted(() => document.addEventListener('click', closeOutside));
onBeforeUnmount(() => document.removeEventListener('click', closeOutside));
</script>
