<template>
  <div class="page-stack">
    <header class="surface p-5 sm:p-6">
      <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div class="flex flex-wrap items-center gap-2">
            <span class="inline-flex items-center rounded-full bg-indigo-50 px-2.5 py-1 text-xs font-semibold text-indigo-700">主动回忆</span>
            <span class="text-xs text-slate-400">{{ cards.length }} 张卡片</span>
          </div>
          <h1 class="mt-2 text-xl sm:text-2xl font-bold tracking-tight text-slate-900">回忆卡库</h1>
          <p class="mt-1 max-w-2xl text-sm leading-relaxed text-slate-500">管理基于资料生成的回忆卡。修改内容会通过修订流程保留历史，便于校对和回溯。</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <router-link to="/today" class="btn btn-secondary text-xs">进入今日学习</router-link>
          <button type="button" class="btn btn-primary" :disabled="chapters.length === 0" @click="startCreate">新建回忆卡</button>
        </div>
      </div>
    </header>

    <div v-if="pageError" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700" role="alert">{{ pageError }}</div>

    <div class="page-toolbar">
      <div class="flex w-full flex-col gap-2 sm:flex-row sm:items-center">
        <div class="relative min-w-0 flex-1 sm:max-w-md">
          <input v-model="search" type="search" class="field pl-9" placeholder="搜索问题、答案或标签…" @keyup.enter="loadCards" />
          <svg class="absolute left-3 top-3 h-4 w-4 text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="m21 21-4.35-4.35m2.35-5.65a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z" /></svg>
        </div>
        <select v-model="chapterFilter" class="field sm:w-48" @change="loadCards">
          <option value="">全部章节</option>
          <option v-for="chapter in chapters" :key="chapter.id" :value="chapter.id">{{ chapter.name }}</option>
        </select>
        <input v-model="knowledgePointFilter" type="search" class="field sm:w-44" placeholder="知识点 ID" @keyup.enter="loadCards" />
        <label class="flex shrink-0 items-center gap-2 text-xs text-slate-600">
          <input v-model="includeCandidates" type="checkbox" class="rounded border-slate-300 text-blue-600 focus:ring-blue-500" @change="loadCards" />
          含待审核候选
        </label>
        <select v-model="statusFilter" class="field sm:w-36" @change="loadCards">
          <option value="all">全部状态</option>
          <option value="new">新卡</option>
          <option value="learning">学习中</option>
          <option value="review">复习中</option>
          <option value="suspended">已暂停</option>
          <option value="archived">已归档</option>
        </select>
        <button type="button" class="btn btn-secondary shrink-0" :disabled="loading" @click="loadCards">{{ loading ? '加载中…' : '刷新' }}</button>
      </div>
    </div>

    <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <article v-for="card in cards" :key="card.id" class="surface flex flex-col gap-4 p-4 sm:p-5">
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <span class="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">{{ statusLabel(card.status) }}</span>
              <span v-if="card.chapter_name" class="text-xs text-slate-400">{{ card.chapter_name }}</span>
            </div>
            <h2 class="mt-2 whitespace-pre-wrap break-words text-sm font-semibold leading-relaxed text-slate-900">{{ card.prompt }}</h2>
          </div>
          <div class="flex shrink-0 items-center gap-1">
            <button type="button" class="rounded-lg p-2 text-slate-400 hover:bg-slate-100 hover:text-slate-700" title="编辑" @click="startEdit(card)">
              <span aria-hidden="true">✎</span><span class="sr-only">编辑</span>
            </button>
            <button v-if="card.lifecycle_status === 'draft' || !card.is_confirmed" type="button" class="rounded-lg px-2 py-1 text-[11px] font-medium text-emerald-700 hover:bg-emerald-50 disabled:opacity-50" :disabled="busyCardId === card.id" @click="approveAndPublish(card)">审核并发布</button>
            <button v-if="card.lifecycle_status === 'draft' || !card.is_confirmed" type="button" class="rounded-lg px-2 py-1 text-[11px] font-medium text-rose-600 hover:bg-rose-50 disabled:opacity-50" :disabled="busyCardId === card.id" @click="rejectCandidate(card)">排除</button>
            <button v-if="card.lifecycle_status === 'published' || card.lifecycle_status === 'paused'" type="button" class="rounded-lg px-2 py-1 text-[11px] font-medium text-amber-700 hover:bg-amber-50" @click="togglePause(card)">{{ card.is_paused ? '恢复' : '暂停' }}</button>
          </div>
        </div>
        <div class="rounded-xl bg-slate-50 p-3 text-xs leading-relaxed text-slate-700">
          <span class="font-semibold text-slate-500">答案：</span>
          <span class="whitespace-pre-wrap">{{ card.answer }}</span>
        </div>
        <div v-if="card.tags.length > 0" class="flex flex-wrap gap-1.5">
          <span v-for="tag in card.tags" :key="tag" class="rounded-full bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700">#{{ tag }}</span>
        </div>
        <div class="mt-auto flex items-center justify-between gap-2 border-t border-slate-100 pt-3 text-[11px] text-slate-400">
          <span>复习 {{ card.review_count }} 次</span>
          <span v-if="card.due_at">下次：{{ formatDate(card.due_at) }}</span>
          <button type="button" class="font-medium text-blue-600 hover:underline" @click="openHistory(card)">查看历史</button>
        </div>
      </article>
    </div>

    <div v-if="!loading && cards.length === 0" class="surface p-10 text-center">
      <div class="text-3xl">▱</div>
      <h2 class="mt-3 text-base font-semibold text-slate-800">还没有回忆卡</h2>
      <p class="mt-1 text-xs text-slate-500">可以新建卡片，或在后端生成任务完成后刷新这里。</p>
      <button type="button" class="btn btn-primary mt-4" :disabled="chapters.length === 0" @click="startCreate">新建回忆卡</button>
    </div>

    <div v-if="showEditor" class="fixed inset-0 z-40 flex items-end justify-center bg-slate-900/40 p-0 sm:items-center sm:p-4" @click.self="closeEditor">
      <div class="max-h-[95vh] w-full max-w-2xl overflow-y-auto sm:max-h-[90vh]">
        <RecallCardEditor :card="editingCard" :chapters="chapters" :saving="saving" @save="saveCard" @cancel="closeEditor" />
      </div>
    </div>

    <div v-if="historyCard" class="fixed inset-0 z-40 flex items-end justify-center bg-slate-900/40 p-0 sm:items-center sm:p-4" @click.self="historyCard = null">
      <section class="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-t-2xl bg-white p-5 sm:rounded-2xl">
        <div class="flex items-center justify-between gap-3 border-b border-slate-100 pb-3">
          <div>
            <h2 class="font-semibold text-slate-900">修订历史</h2>
            <p class="mt-1 line-clamp-2 text-xs text-slate-500">{{ historyCard.prompt }}</p>
          </div>
          <button type="button" class="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-100" @click="historyCard = null">关闭</button>
        </div>
        <div v-if="historyLoading" class="py-8 text-center text-xs text-slate-400">加载历史中…</div>
        <div v-else-if="history.length === 0" class="py-8 text-center text-xs text-slate-400">暂无修订记录</div>
        <div v-else class="mt-4 space-y-3">
          <div v-for="item in history" :key="item.id" class="rounded-xl border border-slate-200 p-3 text-xs">
            <div class="flex items-center justify-between gap-2 text-slate-500"><span>{{ item.action }}</span><span>{{ formatDate(item.created_at) }}</span></div>
            <p v-if="item.snapshot" class="mt-2 whitespace-pre-wrap leading-relaxed text-slate-700">{{ item.snapshot.prompt }}\n\n{{ item.snapshot.answer }}</p>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { apiClient } from '../api/client';
import RecallCardEditor from '../components/RecallCardEditor.vue';
import type { LearningCardHistoryItem, LearningChapter, LearningCardPayload, RecallCard, LearningCardStatus, RevisionReviewPayload } from '../types/learning';

const cards = ref<RecallCard[]>([]);
const chapters = ref<LearningChapter[]>([]);
const search = ref('');
const chapterFilter = ref('');
const knowledgePointFilter = ref('');
const includeCandidates = ref(true);
const busyCardId = ref('');
const statusFilter = ref<LearningCardStatus | 'all'>('all');
const loading = ref(false);
const saving = ref(false);
const pageError = ref('');
const showEditor = ref(false);
const editingCard = ref<RecallCard | null>(null);
const historyCard = ref<RecallCard | null>(null);
const history = ref<LearningCardHistoryItem[]>([]);
const historyLoading = ref(false);

const loadCards = async () => {
  loading.value = true;
  pageError.value = '';
  try {
    cards.value = await apiClient.listRecallCards({
      search: search.value.trim() || undefined,
      chapter_id: chapterFilter.value || undefined,
      knowledge_point_id: knowledgePointFilter.value.trim() || undefined,
      status: statusFilter.value,
      include_candidates: includeCandidates.value,
    });
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '加载回忆卡失败，请稍后重试。';
  } finally {
    loading.value = false;
  }
};

const approveAndPublish = async (card: RecallCard) => {
  busyCardId.value = card.id;
  pageError.value = '';
  try {
    if (card.revision_id) {
      await apiClient.reviewCardRevision({
        revisions: [{ revision_id: card.revision_id, decision: 'approve' }],
      } as RevisionReviewPayload);
    }
    await apiClient.publishRecallCard(card.id, {
      revision_id: card.revision_id || undefined,
      expected_version: card.version,
    });
    await loadCards();
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '审核发布回忆卡失败，请稍后重试。';
  } finally {
    busyCardId.value = '';
  }
};

const rejectCandidate = async (card: RecallCard) => {
  busyCardId.value = card.id;
  pageError.value = '';
  try {
    if (card.revision_id) {
      await apiClient.reviewCardRevision({
        revisions: [{ revision_id: card.revision_id, decision: 'reject' }],
      } as RevisionReviewPayload);
    }
    await apiClient.setRecallCardLifecycle(card.id, 'rejected', card.version);
    await loadCards();
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '排除候选失败，请稍后重试。';
  } finally {
    busyCardId.value = '';
  }
};

const loadChapters = async () => {
  try {
    chapters.value = await apiClient.listLearningChapters();
  } catch (error) {
    console.error('Failed to load learning chapters:', error);
  }
};

const togglePause = async (card: RecallCard) => {
  try {
    await apiClient.setRecallCardLifecycle(card.id, card.is_paused ? 'published' : 'paused', card.version);
    await loadCards();
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '更新卡片状态失败，请稍后重试。';
  }
};

const startCreate = () => {
  editingCard.value = null;
  showEditor.value = true;
};

const startEdit = (card: RecallCard) => {
  editingCard.value = card;
  showEditor.value = true;
};

const closeEditor = () => {
  if (!saving.value) showEditor.value = false;
};

const saveCard = async (payload: LearningCardPayload) => {
  saving.value = true;
  pageError.value = '';
  try {
    if (editingCard.value) {
      await apiClient.updateRecallCard(editingCard.value.id, payload);
    } else {
      await apiClient.createRecallCard(payload);
    }
    showEditor.value = false;
    await loadCards();
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '保存回忆卡失败，请稍后重试。';
  } finally {
    saving.value = false;
  }
};

const openHistory = async (card: RecallCard) => {
  historyCard.value = card;
  historyLoading.value = true;
  history.value = [];
  try {
    history.value = (await apiClient.getRecallCardHistory(card.id)).map((item, index) => ({
      ...item,
      id: item.id || `history-${index + 1}`,
      card_id: item.card_id || card.id,
      action: item.action || (item.revision_id ? 'revision' : 'history'),
      snapshot: item.snapshot || (item.prompt || item.answer ? { ...card, prompt: item.prompt || card.prompt, answer: item.answer || card.answer } : null),
    }));
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '加载修订历史失败，请稍后重试。';
  } finally {
    historyLoading.value = false;
  }
};

const statusLabel = (status: RecallCard['status']): string => {
  const labels: Record<RecallCard['status'], string> = {
    new: '新卡',
    learning: '学习中',
    review: '复习中',
    suspended: '已暂停',
    archived: '已归档',
  };
  return labels[status];
};

const formatDate = (value?: string | null): string => {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' });
};

onMounted(async () => {
  await Promise.all([loadCards(), loadChapters()]);
});
</script>
