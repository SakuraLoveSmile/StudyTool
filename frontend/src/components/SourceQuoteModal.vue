<template>
  <div v-if="isOpen" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4">
    <div class="bg-white rounded-xl shadow-2xl w-full max-w-2xl overflow-hidden border border-slate-200 animate-in fade-in zoom-in-95 duration-150">
      <!-- Modal Header -->
      <div class="px-6 py-4 border-b border-slate-100 flex items-center justify-between bg-slate-50/80">
        <div class="flex items-center space-x-2">
          <FileText class="w-5 h-5 text-indigo-600" />
          <h3 class="font-semibold text-slate-800">资料原文与定位溯源</h3>
        </div>
        <button @click="close" class="text-slate-400 hover:text-slate-600 p-1 rounded-md">
          <X class="w-5 h-5" />
        </button>
      </div>

      <!-- Modal Body -->
      <div class="p-6 space-y-4">
        <!-- Metadata pills -->
        <div class="flex flex-wrap gap-2 text-xs">
          <span class="px-2.5 py-1 rounded bg-indigo-50 text-indigo-700 font-medium">
            来源文件: {{ docName }}
          </span>
          <span class="px-2.5 py-1 rounded bg-amber-50 text-amber-700 font-medium">
            定位位置: {{ locationRef }}
          </span>
          <span v-if="heading" class="px-2.5 py-1 rounded bg-slate-100 text-slate-700 font-medium">
            章节: {{ heading }}
          </span>
          <a v-if="fileUrl" :href="fileUrl" target="_blank" rel="noreferrer" class="px-2.5 py-1 rounded bg-emerald-50 text-emerald-700 font-medium hover:underline">
            打开原文件
          </a>
        </div>

        <!-- Content Area / Editable Area -->
        <div>
          <div class="flex justify-between items-center mb-1.5">
            <label class="text-xs font-medium text-slate-600">原文切片内容{{ readOnly ? '' : '（支持人工校对与修改）' }}：</label>
            <span v-if="savedSuccess" class="text-xs text-emerald-600 font-medium flex items-center">
              <Check class="w-3.5 h-3.5 mr-0.5" /> 已保存修改并更新检索索引
            </span>
          </div>
          <textarea
            v-model="editableContent"
            rows="8"
            :readonly="readOnly"
            class="w-full text-sm p-3.5 rounded-lg border border-slate-200 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 font-mono leading-relaxed bg-slate-50/50"
          ></textarea>
        </div>
      </div>

      <!-- Modal Footer -->
      <div class="px-6 py-3 bg-slate-50 border-t border-slate-100 flex justify-end space-x-2">
        <button
          @click="close"
          class="px-4 py-2 text-sm text-slate-600 hover:bg-slate-200 rounded-lg transition"
        >
          关闭
        </button>
        <button
          v-if="!readOnly"
          @click="handleSave"
          :disabled="isSaving"
          class="px-4 py-2 text-sm bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg transition shadow-sm font-medium flex items-center"
        >
          <Loader2 v-if="isSaving" class="w-4 h-4 mr-1.5 animate-spin" />
          保存校对修改
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { FileText, X, Check, Loader2 } from 'lucide-vue-next';
import { apiClient } from '../api/client';

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
