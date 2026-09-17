<template>
  <form class="surface p-4 sm:p-5 space-y-4" @submit.prevent="submitForm">
    <div class="flex items-start justify-between gap-3 border-b border-stone-100 pb-3">
      <div>
        <h3 class="text-base font-semibold text-stone-900">{{ card ? '编辑回忆卡' : '新建回忆卡' }}</h3>
        <p class="text-xs text-stone-500 mt-1">用清晰的问题触发主动回忆，答案只在揭示后显示。</p>
      </div>
      <span v-if="card" class="text-[11px] text-stone-400 font-mono">{{ card.id.slice(0, 8) }}</span>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <div class="sm:col-span-2">
        <label class="field-label" for="recall-prompt">问题 / 提示面</label>
        <textarea
          id="recall-prompt"
          v-model="form.prompt"
          class="field min-h-[96px] resize-y"
          rows="3"
          required
          maxlength="2000"
          placeholder="例如：解释组织设计中的部门化方式，并说明各自适用条件。"
        />
      </div>

      <div class="sm:col-span-2">
        <label class="field-label" for="recall-answer">参考答案 / 背面</label>
        <textarea
          id="recall-answer"
          v-model="form.answer"
          class="field min-h-[140px] resize-y"
          rows="5"
          required
          maxlength="10000"
          placeholder="写下教材依据的要点、定义和答题结构。"
        />
      </div>

      <div>
        <label class="field-label" for="recall-chapter">所属章节</label>
        <select id="recall-chapter" v-model="form.chapter_id" class="field" required>
          <option value="" disabled>请选择章节</option>
          <option v-for="chapter in chapters" :key="chapter.id" :value="chapter.id">
            {{ chapter.name }}
          </option>
        </select>
      </div>

      <div>
        <label class="field-label" for="recall-status">卡片状态</label>
        <select id="recall-status" v-model="form.status" class="field">
          <option value="new">新卡</option>
          <option value="learning">学习中</option>
          <option value="review">复习中</option>
          <option value="suspended">已暂停</option>
          <option value="archived">已归档</option>
        </select>
      </div>

      <div class="sm:col-span-2">
        <label class="field-label" for="recall-hint">提示（可选）</label>
        <input
          id="recall-hint"
          v-model="form.hint"
          class="field"
          maxlength="1000"
          placeholder="给自己一个关键词或记忆线索，不要直接泄露完整答案。"
        />
      </div>

      <div class="sm:col-span-2">
        <label class="field-label" for="recall-excerpt">来源摘录（可选）</label>
        <textarea
          id="recall-excerpt"
          v-model="form.source_excerpt"
          class="field min-h-[84px] resize-y"
          rows="3"
          maxlength="5000"
          placeholder="记录用于校对的教材原文摘录。"
        />
      </div>

      <div class="sm:col-span-2">
        <label class="field-label" for="recall-tags">标签（逗号分隔）</label>
        <input
          id="recall-tags"
          v-model="form.tags"
          class="field"
          maxlength="500"
          placeholder="定义, 易混, 高频"
        />
      </div>
    </div>

    <p v-if="errorMessage" class="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700" role="alert">
      {{ errorMessage }}
    </p>

    <div class="flex flex-col-reverse sm:flex-row sm:justify-end gap-2 pt-1">
      <button type="button" class="btn btn-secondary" @click="emit('cancel')">取消</button>
      <button type="submit" class="btn btn-primary" :disabled="saving">
        <span v-if="saving" class="mr-1.5 inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-white border-t-transparent" />
        {{ saving ? '保存中…' : '保存回忆卡' }}
      </button>
    </div>
  </form>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import type { LearningCardPayload, LearningChapter, RecallCard } from '../types/learning';

interface Props {
  card?: RecallCard | null;
  chapters?: LearningChapter[];
  saving?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  card: null,
  chapters: () => [],
  saving: false,
});

const emit = defineEmits<{
  save: [payload: LearningCardPayload];
  cancel: [];
}>();

interface EditorForm {
  chapter_id: string;
  prompt: string;
  answer: string;
  hint: string;
  source_excerpt: string;
  tags: string;
  status: 'new' | 'learning' | 'review' | 'suspended' | 'archived';
}

const emptyForm = (): EditorForm => ({
  chapter_id: '',
  prompt: '',
  answer: '',
  hint: '',
  source_excerpt: '',
  tags: '',
  status: 'new',
});

const form = ref<EditorForm>(emptyForm());
const errorMessage = ref('');

const syncForm = (card: RecallCard | null) => {
  if (!card) {
    form.value = emptyForm();
    return;
  }
  form.value = {
    chapter_id: card.chapter_id ?? '',
    prompt: card.prompt,
    answer: card.answer,
    hint: card.hint ?? '',
    source_excerpt: card.source_excerpt ?? '',
    tags: card.tags.join(', '),
    status: card.status,
  };
};

watch(() => props.card, card => syncForm(card), { immediate: true });

const submitForm = () => {
  const prompt = form.value.prompt.trim();
  const answer = form.value.answer.trim();
  if (!form.value.chapter_id.trim()) {
    errorMessage.value = '请选择所属章节。';
    return;
  }
  if (!prompt || !answer) {
    errorMessage.value = '问题和参考答案不能为空。';
    return;
  }
  errorMessage.value = '';
  const tags = form.value.tags
    .split(',')
    .map(tag => tag.trim())
    .filter(Boolean)
    .slice(0, 20);
  emit('save', {
    chapter_id: form.value.chapter_id || null,
    prompt,
    answer,
    hint: form.value.hint.trim() || null,
    source_excerpt: form.value.source_excerpt.trim() || null,
    tags,
    status: form.value.status,
  });
};
</script>
