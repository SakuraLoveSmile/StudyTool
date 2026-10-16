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
      <div v-if="isOpen" ref="dialog" role="dialog" aria-modal="true" aria-label="资料原文与定位溯源" tabindex="-1" class="fixed inset-0 z-50 flex items-center justify-center bg-sink/50 backdrop-blur-sm p-4" @click.self="close" @keydown="onDialogKeydown">
        <div class="bg-surface rounded-2xl shadow-pop w-full max-w-3xl max-h-[90dvh] flex flex-col overflow-hidden border border-line">
          <!-- Modal Header -->
          <div class="px-6 py-4 border-b border-line flex items-center justify-between bg-raised/60 shrink-0">
            <div class="flex items-center space-x-2">
              <FileText class="w-5 h-5 text-accent" />
              <h3 class="font-display font-semibold text-ink">资料原文与定位溯源</h3>
            </div>
            <button @click="close" class="text-faint hover:text-sub p-1.5 rounded-lg hover:bg-raised transition-colors" aria-label="关闭">
              <X class="w-5 h-5" />
            </button>
          </div>

          <!-- Modal Body -->
          <div class="p-4 sm:p-6 space-y-4 overflow-y-auto min-h-0">
            <!-- Metadata pills -->
            <div class="flex flex-wrap gap-2 text-xs">
              <span class="px-2.5 py-1 rounded-full bg-accent/10 text-accent font-medium break-all">
                来源文件: {{ docName }}
              </span>
              <span class="px-2.5 py-1 rounded-full bg-warn/10 text-warn font-medium">
                定位位置: {{ locationRef }}
              </span>
              <span v-if="heading" class="px-2.5 py-1 rounded-full bg-raised text-ink font-medium">
                章节: {{ heading }}
              </span>
              <a v-if="fileUrl" :href="fileUrl" target="_blank" rel="noreferrer" class="px-2.5 py-1 rounded-full bg-ok/10 text-ok font-medium hover:underline">
                打开原文件
              </a>
            </div>

            <!-- Content Area / Editable Area -->
            <div>
              <div class="flex flex-wrap justify-between items-center gap-2 mb-3">
                <label v-if="!readOnly && !readingPreview" for="source-content-editor" class="field-label mb-0">原文切片内容（支持人工校对与修改）</label>
                <span v-else class="field-label mb-0">原文切片内容</span>
                <button v-if="!readOnly" type="button" class="btn-sm btn-secondary" :aria-pressed="readingPreview" @click="readingPreview = !readingPreview">
                  {{ readingPreview ? '返回校对编辑' : '阅读排版预览' }}
                </button>
                <span v-if="savedSuccess" class="text-xs text-ok font-medium flex items-center">
                  <Check class="w-3.5 h-3.5 mr-0.5" /> 已保存修改并更新检索索引
                </span>
              </div>
              <div v-if="readOnly || readingPreview" class="rounded-xl border border-line bg-page px-4 py-5 sm:px-6">
                <SourceText :content="editableContent" />
              </div>
              <textarea
                v-else
                id="source-content-editor"
                v-model="editableContent"
                rows="12"
                class="field source-editor bg-raised/50"
              ></textarea>
            </div>
          </div>

          <!-- Modal Footer -->
          <div class="px-6 py-3.5 bg-raised/60 border-t border-line flex justify-end gap-2 shrink-0">
            <button @click="close" class="btn btn-secondary">
              关闭
            </button>
            <button
              v-if="!readOnly"
              @click="handleSave"
              :disabled="isSaving"
              class="btn btn-primary"
            >
              <Loader2 v-if="isSaving" class="w-4 h-4 mr-1.5 animate-spin" />
              保存校对修改
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { FileText, X, Check, Loader2 } from 'lucide-vue-next';
import { apiClient } from '../api/client';
import SourceText from './SourceText.vue';

const props = defineProps<{
  isOpen: boolean;
  chunkId: string;
  docName: string;
  locationRef: string;
  heading?: string;
  content: string;
  fileUrl?: string;
  readOnly?: boolean;
}>();

const readOnly = computed(() => props.readOnly ?? false);

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'updated', newContent: string): void;
}>();

const editableContent = ref(props.content);
const isSaving = ref(false);
const savedSuccess = ref(false);
const readingPreview = ref(false);
const dialog = ref<HTMLElement>();
let previousFocus: HTMLElement | null = null;

function restoreFocus() {
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
  previousFocus = null;
}

watch(() => props.isOpen, async open => {
  if (!open) { restoreFocus(); return; }
  readingPreview.value = false;
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  await nextTick();
  dialog.value?.focus({ preventScroll: true });
}, { immediate: true });
onBeforeUnmount(restoreFocus);

function onDialogKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return; }
  if (event.key !== 'Tab' || !dialog.value) return;
  const focusable = Array.from(dialog.value.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], textarea:not([disabled]), [tabindex="0"]')).filter(element => element.getClientRects().length);
  const first = focusable[0]; const last = focusable.at(-1);
  if (!first || !last) return;
  if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.value)) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

watch(() => props.content, (newVal) => {
  editableContent.value = newVal;
  savedSuccess.value = false;
});

const close = () => {
  savedSuccess.value = false;
  emit('close');
};

const handleSave = async () => {
  if (!props.chunkId) return;
  isSaving.value = true;
  try {
    await apiClient.updateChunk(props.chunkId, editableContent.value);
    savedSuccess.value = true;
    emit('updated', editableContent.value);
    setTimeout(() => {
      savedSuccess.value = false;
    }, 3000);
  } catch (err: any) {
    alert('保存校对修改失败: ' + (err.response?.data?.detail || err.message));
  } finally {
    isSaving.value = false;
  }
};
</script>

<style scoped>
.source-editor {
  font-family: inherit;
  font-size: 1rem;
  line-height: 1.9;
  overflow-wrap: anywhere;
}
</style>
