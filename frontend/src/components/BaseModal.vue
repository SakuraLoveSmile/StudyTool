<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="show"
        class="fixed inset-0 z-50 overflow-y-auto bg-sink/50 backdrop-blur-sm flex items-center justify-center p-4 sm:p-6"
        @click.self="handleBackdropClick"
      >
        <div
          class="bg-surface rounded-2xl shadow-pop w-full flex flex-col overflow-hidden border border-line transform transition-all"
          :class="maxWidthClass"
          role="dialog"
          aria-modal="true"
        >
          <!-- Header -->
          <div class="px-6 py-4 border-b border-line flex items-center justify-between bg-raised/50">
            <div class="flex items-center gap-2">
              <slot name="header-icon" />
              <h3 class="font-display text-base sm:text-lg font-semibold text-ink">
                {{ title }}
              </h3>
            </div>
            <button
              type="button"
              class="text-faint hover:text-sub p-1.5 rounded-lg hover:bg-raised transition-colors"
              @click="close"
              aria-label="关闭"
            >
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <!-- Body (scrollable) -->
          <div class="px-6 py-5 overflow-y-auto max-h-[75vh]">
            <slot />
          </div>

          <!-- Footer (optional) -->
          <div v-if="$slots.footer" class="px-6 py-4 border-t border-line bg-raised/60 flex items-center justify-end gap-3">
            <slot name="footer" />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue';

const props = withDefaults(
  defineProps<{
    show: boolean;
    title: string;
    maxWidth?: 'sm' | 'md' | 'lg' | 'xl' | '2xl' | '3xl' | '4xl';
    closeOnBackdrop?: boolean;
  }>(),
  {
    maxWidth: '2xl',
    closeOnBackdrop: true
  }
);

const emit = defineEmits<{
  (e: 'close'): void;
}>();

const maxWidthClass = computed(() => {
  switch (props.maxWidth) {
    case 'sm': return 'max-w-sm';
    case 'md': return 'max-w-md';
    case 'lg': return 'max-w-lg';
    case 'xl': return 'max-w-xl';
    case '2xl': return 'max-w-2xl';
    case '3xl': return 'max-w-3xl';
    case '4xl': return 'max-w-4xl';
    default: return 'max-w-2xl';
  }
});

const close = () => {
  emit('close');
};

const handleBackdropClick = () => {
  if (props.closeOnBackdrop) {
    close();
  }
};

const handleKeyDown = (e: KeyboardEvent) => {
  if (e.key === 'Escape' && props.show) {
    close();
  }
};

onMounted(() => {
  window.addEventListener('keydown', handleKeyDown);
});

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeyDown);
});
</script>
