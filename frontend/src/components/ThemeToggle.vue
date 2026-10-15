<template>
  <!-- 图标按钮 + 下拉三选（导航栏/登录页用） -->
  <div v-if="variant === 'icon'" ref="rootEl" class="relative">
    <button
      type="button"
      class="rounded-lg p-2 text-sub transition-colors hover:bg-raised hover:text-ink"
      :aria-label="`外观设置：当前${currentLabel}`"
      :aria-expanded="open"
      aria-haspopup="menu"
      @click="open = !open"
    >
      <Sun v-if="effective === 'light'" class="h-5 w-5" />
      <Moon v-else class="h-5 w-5" />
    </button>
    <Transition
      enter-active-class="transition duration-100 ease-out"
      enter-from-class="opacity-0 -translate-y-1 scale-95"
      leave-active-class="transition duration-75 ease-in"
      leave-to-class="opacity-0 -translate-y-1 scale-95"
    >
      <div
        v-if="open"
        role="menu"
        aria-label="外观"
        class="absolute right-0 z-50 mt-1 w-40 origin-top-right rounded-xl border border-line bg-surface p-1 shadow-pop"
        @keydown.escape="open = false"
      >
        <button
          v-for="opt in options"
          :key="opt.value"
          type="button"
          role="menuitemradio"
          :aria-checked="preference === opt.value"
          class="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-sm transition-colors"
          :class="preference === opt.value ? 'bg-accent/10 font-medium text-accent' : 'text-sub hover:bg-raised hover:text-ink'"
          @click="choose(opt.value)"
        >
          <component :is="opt.icon" class="h-4 w-4" />
          <span class="flex-1 text-left">{{ opt.label }}</span>
          <Check v-if="preference === opt.value" class="h-4 w-4" />
        </button>
      </div>
    </Transition>
  </div>

  <!-- 行内三选（移动端展开菜单用） -->
  <div v-else class="space-y-1" role="radiogroup" aria-label="外观">
    <p class="px-3 pb-1 text-xs font-semibold uppercase tracking-wider text-faint">外观</p>
    <button
      v-for="opt in options"
      :key="opt.value"
      type="button"
      role="radio"
      :aria-checked="preference === opt.value"
      class="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition-colors"
      :class="preference === opt.value ? 'bg-accent/10 text-accent' : 'text-sub hover:bg-raised hover:text-ink'"
      @click="choose(opt.value)"
    >
      <component :is="opt.icon" class="h-4 w-4" />
      <span class="flex-1 text-left">{{ opt.label }}</span>
      <Check v-if="preference === opt.value" class="h-4 w-4" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, type Component } from 'vue';
import { Check, Monitor, Moon, Sun } from 'lucide-vue-next';
import { useTheme, type ThemePreference } from '../composables/useTheme';

withDefaults(defineProps<{ variant?: 'icon' | 'menu' }>(), { variant: 'icon' });

const { preference, effective, setPreference } = useTheme();

const options: Array<{ value: ThemePreference; label: string; icon: Component }> = [
  { value: 'system', label: '跟随系统', icon: Monitor },
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: Moon },
];

const currentLabel = computed(() => options.find((o) => o.value === preference.value)?.label ?? '跟随系统');

const open = ref(false);
const rootEl = ref<HTMLElement | null>(null);

const choose = (value: ThemePreference) => {
  open.value = false;
  void setPreference(value);
};

const onClickOutside = (e: MouseEvent) => {
  if (open.value && rootEl.value && !rootEl.value.contains(e.target as Node)) open.value = false;
};
onMounted(() => document.addEventListener('click', onClickOutside));
onBeforeUnmount(() => document.removeEventListener('click', onClickOutside));
</script>
