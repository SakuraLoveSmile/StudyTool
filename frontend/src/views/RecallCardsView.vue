<template>
  <div class="page-stack">
    <header class="surface p-5 sm:p-6">
      <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div class="flex flex-wrap items-center gap-2">
            <span class="inline-flex items-center rounded-full bg-accent/10 px-2.5 py-1 text-xs font-semibold text-accent">主动回忆</span>
            <span class="text-xs text-faint">{{ cards.length }} 张卡片</span>
          </div>
          <h1 class="mt-2 font-display text-xl sm:text-2xl font-bold tracking-tight text-ink">回忆卡库</h1>
          <p class="mt-1 max-w-2xl text-sm leading-relaxed text-sub">管理基于资料生成的回忆卡。修改内容会通过修订流程保留历史，便于校对和回溯。</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <router-link to="/today" class="btn btn-secondary text-xs">进入今日学习</router-link>
          <button type="button" class="btn btn-secondary text-xs" :disabled="loading" @click="openBatchGenerateModal">批量生成回忆卡</button>
          <button type="button" class="btn btn-primary" :disabled="loading" @click="startCreate">新建回忆卡</button>
        </div>
      </div>
    </header>

    <div v-if="pageError" class="rounded-xl border border-err/30 bg-err/10 px-4 py-3 text-sm text-err" role="alert">{{ pageError }}</div>

    <div class="page-toolbar">
      <div class="flex w-full flex-col gap-2 sm:flex-row sm:items-center">
        <div class="relative min-w-0 flex-1 sm:max-w-md">
          <input v-model="search" type="search" class="field pl-9" placeholder="搜索问题、答案或标签…" @keyup.enter="loadCards" />
          <svg class="absolute left-3 top-3 h-4 w-4 text-faint" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="m21 21-4.35-4.35m2.35-5.65a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z" /></svg>
        </div>
        <select v-model="chapterFilter" class="field sm:w-48" @change="loadCards">
          <option value="">全部章节</option>
          <option v-for="chapter in chapters" :key="chapter.id" :value="chapter.id">{{ chapter.name }}</option>
        </select>
        <input v-model="knowledgePointFilter" type="search" class="field sm:w-44" placeholder="知识点 ID" @keyup.enter="loadCards" />
        <label class="flex shrink-0 items-center gap-2 text-xs text-sub">
          <input v-model="includeCandidates" type="checkbox" class="rounded border-line-strong text-accent focus:ring-accent" @change="loadCards" />
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
              <span class="rounded-full px-2 py-0.5 text-[11px] font-medium" :class="statusBadgeClass(card.status)">{{ statusLabel(card.status) }}</span>
              <span v-if="card.chapter_name" class="text-xs text-faint">{{ card.chapter_name }}</span>
              <span v-if="hasPendingChanges(card)" class="rounded-full bg-warn/10 px-2 py-0.5 text-[11px] font-medium text-warn">有待审核修改</span>
            </div>
            <h2 class="mt-2 whitespace-pre-wrap break-words text-sm font-semibold leading-relaxed text-ink">{{ card.prompt }}</h2>
          </div>
          <div class="flex shrink-0 items-center gap-1">
            <button type="button" class="rounded-lg p-2 text-faint hover:bg-raised hover:text-ink" title="编辑" @click="startEdit(card)">
              <Pencil class="h-4 w-4" aria-hidden="true" /><span class="sr-only">编辑</span>
            </button>
            <button v-if="card.lifecycle_status === 'draft' || !card.is_confirmed || hasPendingChanges(card)" type="button" class="rounded-lg px-2 py-1 text-[11px] font-medium text-ok hover:bg-ok/10 disabled:opacity-50" :disabled="busyCardId === card.id" @click="approveAndPublish(card)">审核并发布</button>
            <button v-if="card.lifecycle_status === 'draft' || !card.is_confirmed || hasPendingChanges(card)" type="button" class="rounded-lg px-2 py-1 text-[11px] font-medium text-err hover:bg-err/10 disabled:opacity-50" :disabled="busyCardId === card.id" @click="rejectCandidate(card)">拒绝修改</button>
            <button v-if="hasPendingChanges(card)" type="button" class="rounded-lg px-2 py-1 text-[11px] font-medium text-accent hover:bg-accent/10" @click="startEdit(card)">预览草稿</button>
            <button v-if="card.lifecycle_status === 'published' || card.lifecycle_status === 'paused'" type="button" class="rounded-lg px-2 py-1 text-[11px] font-medium text-warn hover:bg-warn/10" @click="togglePause(card)">{{ card.is_paused ? '恢复' : '暂停' }}</button>
          </div>
        </div>
        <div class="rounded-xl bg-raised p-3 text-xs leading-relaxed text-ink">
          <span class="font-semibold text-sub">答案：</span>
          <span class="whitespace-pre-wrap">{{ card.answer }}</span>
        </div>
        <div v-if="card.tags.length > 0" class="flex flex-wrap gap-1.5">
          <span v-for="tag in card.tags" :key="tag" class="rounded-full bg-accent/10 px-2 py-0.5 text-[11px] text-accent">#{{ tag }}</span>
        </div>
        <div class="mt-auto flex items-center justify-between gap-2 border-t border-line pt-3 text-[11px] text-faint">
          <span>复习 {{ card.review_count }} 次</span>
          <span v-if="card.due_at">下次：{{ formatDate(card.due_at) }}</span>
          <button type="button" class="font-medium text-accent hover:underline" @click="openHistory(card)">查看历史</button>
        </div>
      </article>
    </div>

    <div v-if="!loading && cards.length === 0" class="surface p-10 text-center">
      <div class="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-raised text-faint">
        <Layers class="h-6 w-6" />
      </div>
      <h2 class="mt-3 font-display text-base font-semibold text-ink">还没有回忆卡</h2>
      <p class="mt-1 text-xs text-sub">可以新建卡片，或在后端生成任务完成后刷新这里。</p>
      <button type="button" class="btn btn-primary mt-4" @click="startCreate">新建回忆卡</button>
    </div>

    <div v-if="chapters.length === 0 && !loading" class="surface p-8 text-center">
      <Layers class="mx-auto h-8 w-8 text-faint" />
      <h2 class="mt-3 font-display text-base font-semibold text-ink">先创建章节</h2>
      <p class="mt-1 text-xs text-sub">章节用于组织卡片，也是今日学习的范围。</p>
      <button type="button" class="btn btn-primary mt-4" @click="showChapterEditor = true">新建章节</button>
    </div>

    <div v-if="showEditor" class="fixed inset-0 z-40 flex items-end justify-center bg-sink/40 p-0 sm:items-center sm:p-4" role="presentation" @click.self="closeEditor">
      <div class="max-h-[95vh] w-full max-w-2xl overflow-y-auto sm:max-h-[90vh]">
        <RecallCardEditor :card="editingCard" :chapters="chapters" :saving="saving" @save="saveCard" @cancel="closeEditor" />
      </div>
    </div>

    <div v-if="showChapterEditor" class="fixed inset-0 z-40 flex items-end justify-center bg-sink/40 p-0 sm:items-center sm:p-4" role="presentation" @click.self="closeChapterEditor">
      <section class="w-full max-w-lg rounded-t-2xl bg-surface p-5 sm:rounded-2xl" role="dialog" aria-modal="true" aria-labelledby="chapter-dialog-title">
        <h2 id="chapter-dialog-title" class="font-display text-lg font-semibold text-ink">新建章节</h2>
        <p class="mt-1 text-xs text-sub">创建后会自动用于新回忆卡。</p>
        <label class="mt-4 block" for="new-chapter-name"><span class="field-label">章节名称</span></label>
        <input id="new-chapter-name" v-model="chapterName" class="field mt-1" maxlength="120" placeholder="例如：管理学原理｜组织设计" @keyup.enter="createChapter" />
        <p v-if="chapterError" class="mt-2 text-xs text-err" role="alert">{{ chapterError }}</p>
        <div class="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button type="button" class="btn btn-secondary" :disabled="chapterSaving" @click="closeChapterEditor">取消</button>
          <button type="button" class="btn btn-primary" :disabled="chapterSaving || !chapterName.trim()" @click="createChapter">{{ chapterSaving ? '创建中…' : '创建章节' }}</button>
        </div>
      </section>
    </div>

    <div v-if="historyCard" class="fixed inset-0 z-40 flex items-end justify-center bg-sink/40 p-0 sm:items-center sm:p-4" @click.self="historyCard = null">
      <section class="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-t-2xl bg-surface p-5 sm:rounded-2xl">
        <div class="flex items-center justify-between gap-3 border-b border-line pb-3">
          <div>
            <h2 class="font-semibold text-ink">修订历史</h2>
            <p class="mt-1 line-clamp-2 text-xs text-sub">{{ historyCard.prompt }}</p>
          </div>
          <button type="button" class="rounded-lg px-2 py-1 text-faint hover:bg-raised" @click="historyCard = null">关闭</button>
        </div>
        <div v-if="historyLoading" class="py-8 text-center text-xs text-faint">加载历史中…</div>
        <div v-else-if="history.length === 0" class="py-8 text-center text-xs text-faint">暂无修订记录</div>
        <div v-else class="mt-4 space-y-3">
          <div v-for="item in history" :key="item.id" class="rounded-xl border border-line p-3 text-xs">
            <div class="flex items-center justify-between gap-2 text-sub"><span>{{ item.action }}</span><span>{{ formatDate(item.created_at) }}</span></div>
            <p v-if="item.snapshot" class="mt-2 whitespace-pre-wrap leading-relaxed text-ink">{{ item.snapshot.prompt + '\n\n' + item.snapshot.answer }}</p>
          </div>
        </div>
      </section>
    </div>

    <!-- Batch Generate Modal -->
    <BaseModal
      :show="showBatchModal"
      title="批量生成回忆卡"
      max-width="lg"
      @close="!batchGenerating && (showBatchModal = false)"
    >
      <div class="space-y-4 text-sm">
        <p class="text-xs text-sub leading-relaxed">
          基于所选章节的知识点与教材切片，AI 自动提取核心概念并生成结构化「问答」回忆卡，作为待审核候选存入卡库。
        </p>

        <div v-if="batchError" class="rounded-xl border border-err/30 bg-err/10 px-3 py-2 text-xs text-err">
          {{ batchError }}
        </div>
        <div v-if="batchSuccessMessage" class="rounded-xl border border-ok/30 bg-ok/10 px-3 py-2 text-xs text-ok">
          {{ batchSuccessMessage }}
        </div>

        <div>
          <label class="block text-xs font-semibold text-sub mb-1.5">目标章节</label>
          <select v-model="batchChapterId" class="field text-xs w-full" :disabled="batchGenerating">
            <option v-for="ch in chapters" :key="ch.id" :value="ch.id">{{ ch.name }}</option>
          </select>
        </div>

        <div>
          <label class="block text-xs font-semibold text-sub mb-1.5">生成要求 / 额外提示（选填）</label>
          <textarea
            v-model="batchInstructions"
            rows="3"
            class="field text-xs w-full"
            placeholder="例如：着重考察核心定义与易错概念辨析，问题表述要求简明扼要…"
            :disabled="batchGenerating"
          />
        </div>
      </div>

      <template #footer>
        <div class="flex justify-end gap-2">
          <button type="button" class="btn btn-secondary text-xs" :disabled="batchGenerating" @click="showBatchModal = false">取消</button>
          <button type="button" class="btn btn-primary text-xs" :disabled="batchGenerating" @click="handleBatchGenerate">
            {{ batchGenerating ? '正在提交生成任务…' : '开始批量生成' }}
          </button>
        </div>
      </template>
    </BaseModal>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { Layers, Pencil } from 'lucide-vue-next';
import { apiClient } from '../api/client';
import BaseModal from '../components/BaseModal.vue';
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
const showChapterEditor = ref(false);
const chapterName = ref('');
const chapterSaving = ref(false);
const chapterError = ref('');

const showBatchModal = ref(false);
const batchChapterId = ref('');
const batchInstructions = ref('');
const batchGenerating = ref(false);
const batchSuccessMessage = ref('');
const batchError = ref('');

const openBatchGenerateModal = () => {
  if (chapters.value.length === 0) {
    showChapterEditor.value = true;
    return;
  }
  batchChapterId.value = chapterFilter.value || chapters.value[0]?.id || '';
  batchInstructions.value = '';
  batchError.value = '';
  batchSuccessMessage.value = '';
  showBatchModal.value = true;
};

const handleBatchGenerate = async () => {
  if (!batchChapterId.value) {
    batchError.value = '请选择章节';
    return;
  }
  batchGenerating.value = true;
  batchError.value = '';
  try {
    await apiClient.createLearningGeneration({
      chapter_id: batchChapterId.value,
      instructions: batchInstructions.value.trim() || undefined,
    });
    batchSuccessMessage.value = '已成功提交批量生成任务！生成完成后可在列表中勾选「含待审核候选」查看。';
    includeCandidates.value = true;
    await loadCards();
    setTimeout(() => {
      showBatchModal.value = false;
      batchSuccessMessage.value = '';
    }, 2000);
  } catch (error) {
    batchError.value = error instanceof Error ? error.message : '创建生成任务失败，请稍后重试。';
  } finally {
    batchGenerating.value = false;
  }
};

const hasPendingChanges = (card: RecallCard): boolean => {
  return card.has_unpublished_changes === true || Boolean(card.current_revision_id && card.current_revision_status === 'pending');
};

const pendingRevisionId = (card: RecallCard): string | undefined => card.current_revision_id || card.revision_id || undefined;

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
    const revisionId = pendingRevisionId(card);
    if (revisionId) {
      await apiClient.reviewCardRevision({
        revisions: [{ revision_id: revisionId, decision: 'approve' }],
      } as RevisionReviewPayload);
    }
    await apiClient.publishRecallCard(card.id, {
      revision_id: revisionId,
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
    const revisionId = pendingRevisionId(card);
    if (revisionId) {
      await apiClient.reviewCardRevision({
        revisions: [{ revision_id: revisionId, decision: 'reject' }],
      } as RevisionReviewPayload);
    }
    if (card.lifecycle_status === 'draft' || !card.is_confirmed) {
      await apiClient.setRecallCardLifecycle(card.id, 'rejected', card.version);
    }
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

const closeChapterEditor = () => {
  if (!chapterSaving.value) {
    showChapterEditor.value = false;
    chapterName.value = '';
    chapterError.value = '';
  }
};

const createChapter = async () => {
  const name = chapterName.value.trim();
  if (!name) return;
  chapterSaving.value = true;
  chapterError.value = '';
  try {
    const chapter = await apiClient.createLearningChapter({ name });
    chapters.value = [...chapters.value, chapter];
    chapterFilter.value = chapter.id;
    showChapterEditor.value = false;
    chapterName.value = '';
    startCreate();
  } catch (error) {
    chapterError.value = error instanceof Error ? error.message : '创建章节失败，请稍后重试。';
  } finally {
    chapterSaving.value = false;
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
  if (chapters.value.length === 0) {
    showChapterEditor.value = true;
    return;
  }
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

const statusBadgeClass = (status: RecallCard['status']): string => {
  const classes: Record<RecallCard['status'], string> = {
    new: 'bg-accent/10 text-accent',
    learning: 'bg-violet/10 text-violet',
    review: 'bg-warn/10 text-warn',
    suspended: 'bg-raised text-sub',
    archived: 'bg-raised text-faint',
  };
  return classes[status];
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
