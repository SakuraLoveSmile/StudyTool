<template>
  <div class="page-stack">
    <!-- Top Header -->
    <div class="page-toolbar">
      <div>
        <h2 class="font-display text-base sm:text-lg font-bold text-ink tracking-tight">
          四维考点学习卡与答题模板
        </h2>
        <p class="text-xs text-sub mt-0.5">
          精简背诵版 · 答题骨架模型 · 展开论述版 · 易混辨析与真实案例（杜绝虚构）
        </p>
      </div>

      <div class="flex items-center gap-2">
        <button
          v-if="selectedKp"
          type="button"
          class="btn btn-primary text-xs"
          :disabled="isGenerating"
          @click="triggerGenerateCard"
        >
          <Sparkles v-if="!isGenerating" class="w-3.5 h-3.5 mr-1 text-warn" />
          <Loader2 v-else class="w-3.5 h-3.5 mr-1 animate-spin" />
          <span>{{ card ? '重新生成/更新卡片' : '生成专属学习卡' }}</span>
        </button>
      </div>
    </div>

    <!-- Main Split View Layout -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
      <!-- Left Column: Knowledge Points List (4 cols on lg) -->
      <div class="lg:col-span-4 space-y-3">
        <div class="surface p-4 space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-xs font-semibold text-ink">知识点目录 ({{ filteredKPs.length }})</span>
          </div>

          <!-- Search Input -->
          <div class="relative">
            <input
              v-model="kpSearch"
              type="text"
              placeholder="搜索考点或章节..."
              class="field py-1.5 text-xs pl-8 min-h-[36px]"
            />
            <Search class="w-4 h-4 text-faint absolute left-2.5 top-2.5" />
          </div>

          <!-- KP List -->
          <div v-if="filteredKPs.length === 0" class="py-8 text-center text-faint text-xs">
            未找到匹配的考点
          </div>
          <div v-else class="space-y-1.5 max-h-[calc(100vh-20rem)] overflow-y-auto pr-1">
            <button
              v-for="kp in filteredKPs"
              :key="kp.id"
              type="button"
              class="w-full text-left p-3 rounded-xl border transition-all cursor-pointer flex flex-col gap-1"
              :class="selectedKpId === kp.id ? 'border-accent bg-accent/10 shadow-xs' : 'border-line hover:border-line bg-surface hover:bg-raised/50'"
              @click="handleSelectKp(kp.id)"
            >
              <div class="flex items-center justify-between gap-1">
                <span class="font-semibold text-xs sm:text-sm text-ink truncate">
                  {{ kp.name }}
                </span>
                <span
                  class="px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0"
                  :class="priorityBadgeClass(kp.priority_tag)"
                >
                  {{ priorityBadgeLabel(kp.priority_tag) }}
                </span>
              </div>
              <span class="text-xs text-faint truncate">
                {{ kp.chapter || '未分类章节' }}
              </span>
            </button>
          </div>
        </div>
      </div>

      <!-- Right Column: Study Card Reading View (8 cols on lg) -->
      <div class="lg:col-span-8 space-y-5">
        <div v-if="!selectedKpId" class="surface p-12 text-center">
          <EmptyState title="请选择一个知识点" description="从左侧目录中点击考点，查看或生成专属四维学习卡。" />
        </div>

        <div v-else-if="isLoadingCard" class="surface p-12 text-center">
          <div class="w-12 h-12 mx-auto rounded-full bg-accent/10 flex items-center justify-center text-accent mb-3">
            <Loader2 class="w-6 h-6 animate-spin" />
          </div>
          <p class="text-sm font-medium text-ink">正在加载学习卡...</p>
        </div>

        <div v-else-if="!card" class="surface p-12 text-center space-y-4">
          <div class="w-12 h-12 mx-auto rounded-full bg-accent/10 flex items-center justify-center text-accent">
            <Sparkles class="w-6 h-6" />
          </div>
          <div>
            <h4 class="text-base font-semibold text-ink">
              「{{ selectedKp?.name }}」尚未生成学习卡
            </h4>
            <p class="text-xs text-sub mt-1 max-w-sm mx-auto">
              基于已上传的教材切片与考情，一键生成精简背诵版、答题结构与展开论述。
            </p>
          </div>
          <button
            type="button"
            class="btn btn-primary"
            :disabled="isGenerating"
            @click="triggerGenerateCard"
          >
            <Loader2 v-if="isGenerating" class="w-4 h-4 mr-1.5 animate-spin" />
            <span>{{ isGenerating ? '正在生成中...' : '立即生成四维学习卡' }}</span>
          </button>
        </div>

        <!-- Rendered Card Content -->
        <div v-else class="space-y-5">
          <!-- Update needed alert -->
          <div
            v-if="card.needs_update"
            class="bg-warn/10 border border-warn/30 rounded-xl p-3.5 text-xs text-warn flex items-center justify-between gap-3 shadow-xs"
          >
            <div class="flex items-center gap-2">
              <AlertTriangle class="w-4 h-4 text-warn shrink-0" />
              <span>检测到该知识点关联的真题或教材切片近期已被修改，建议更新学习卡以保持同步。</span>
            </div>
            <button
              type="button"
              class="btn btn-sm btn-secondary bg-surface text-warn border-warn/40 hover:bg-warn/15 shrink-0 cursor-pointer"
              @click="triggerGenerateCard"
            >
              立即更新
            </button>
          </div>

          <!-- Section 1: 精简背诵版 -->
          <div class="surface p-5 sm:p-6 border-l-4 border-l-accent space-y-3">
            <div class="flex items-center justify-between">
              <h3 class="text-base font-bold text-ink flex items-center gap-2">
                <Bookmark class="w-4 h-4 text-accent" />
                一、精简背诵版（核心定义 · 口诀要点 · 关键词）
              </h3>
              <span class="text-xs px-2 py-0.5 rounded bg-accent/10 text-accent font-medium">
                考前速记
              </span>
            </div>
            <div class="reading-content whitespace-pre-wrap bg-raised/70 p-4 rounded-xl border border-line font-mono text-ink">
              {{ card.concise_content }}
            </div>
          </div>

          <!-- Section 2: 答题结构规范 -->
          <div class="surface p-5 sm:p-6 border-l-4 border-l-warn space-y-3">
            <div class="flex items-center justify-between">
              <h3 class="text-base font-bold text-ink flex items-center gap-2">
                <GitFork class="w-4 h-4 text-warn" />
                二、答题结构模型（概念 → 特征 → 适用条件 → 案例）
              </h3>
              <span class="text-xs px-2 py-0.5 rounded bg-warn/10 text-warn font-medium">
                答题骨架
              </span>
            </div>
            <div class="reading-content whitespace-pre-wrap bg-warn/10 p-4 rounded-xl border border-warn/20/60 text-ink">
              {{ card.answer_structure }}
            </div>
          </div>

          <!-- Section 3: 展开答题版 -->
          <div class="surface p-5 sm:p-6 border-l-4 border-l-ok space-y-3">
            <div class="flex items-center justify-between">
              <h3 class="text-base font-bold text-ink flex items-center gap-2">
                <FileText class="w-4 h-4 text-ok" />
                三、展开答题版（按教材规范组织完整论述）
              </h3>
              <span class="text-xs px-2 py-0.5 rounded bg-ok/10 text-ok font-medium">
                大题论述
              </span>
            </div>
            <div class="reading-content whitespace-pre-wrap bg-raised/50 p-4 rounded-xl border border-line text-ink">
              {{ card.detailed_content }}
            </div>
          </div>

          <!-- Section 4: 易混点与真实案例 -->
          <div class="surface p-5 sm:p-6 border-l-4 border-l-purple-500 space-y-3">
            <div class="flex items-center justify-between">
              <h3 class="text-base font-bold text-ink flex items-center gap-2">
                <Split class="w-4 h-4 text-purple-600" />
                四、易混点辨析与教材真实案例（杜绝虚构）
              </h3>
              <span class="text-xs px-2 py-0.5 rounded bg-purple-50 text-purple-700 font-medium">
                拉开差距
              </span>
            </div>
            <div class="reading-content whitespace-pre-wrap bg-purple-50/20 p-4 rounded-xl border border-purple-100/50 text-ink">
              {{ card.confusing_points_and_cases }}
            </div>
          </div>

          <!-- Citations Bar -->
          <div class="surface p-4 space-y-2.5">
            <div class="flex items-center justify-between">
              <h4 class="text-xs font-semibold text-ink flex items-center gap-1.5">
                <Quote class="w-4 h-4 text-faint" />
                学习卡依据的教材切片与原题引用 ({{ card.citations.length }} 处)
              </h4>
            </div>
            <div v-if="card.citations.length === 0" class="text-xs text-faint italic">
              暂无切片引用明细
            </div>
            <div v-else class="flex flex-wrap gap-2">
              <button
                v-for="cite in card.citations"
                :key="cite.chunk_id"
                type="button"
                class="btn btn-sm btn-secondary text-xs flex items-center gap-1.5 cursor-pointer"
                @click="openQuote(cite)"
              >
                <span class="font-medium text-ink">{{ cite.doc_name }}</span>
                <span class="text-accent">[{{ cite.page_or_ref }}]</span>
              </button>
            </div>
          </div>

          <!-- Question Answering Assistant (Collapsible / Bottom Card) -->
          <div class="surface p-5 space-y-4">
            <div class="border-b border-line pb-3">
              <h3 class="text-base font-bold text-ink flex items-center gap-2">
                <HelpCircle class="w-4 h-4 text-accent" />
                考点真题解题问答助手
              </h3>
              <p class="text-xs text-sub mt-1">
                选择已收录真题或输入自定义问题，结合教材切片与参考答案生成破题思路与规范解题。
              </p>
            </div>

            <div class="space-y-3 text-xs sm:text-sm">
              <div>
                <label class="field-label">选择该考点下的历年真题：</label>
                <select
                  v-model="selectedAssistantQid"
                  class="field text-xs sm:text-sm"
                >
                  <option value="">-- 手动输入自定义题目 --</option>
                  <option v-for="q in kpQuestions" :key="q.id" :value="q.id">
                    {{ q.year }}年: {{ q.stem.substring(0, 32) }}...
                  </option>
                </select>
              </div>

              <div v-if="!selectedAssistantQid">
                <label class="field-label">或输入自定义题目：</label>
                <textarea
                  v-model="customQuestionStem"
                  rows="3"
                  placeholder="输入需要获得思路与解答的专业课题干..."
                  class="field text-xs sm:text-sm"
                />
              </div>

              <button
                type="button"
                class="btn btn-primary w-full"
                :disabled="isAssistantRunning"
                @click="handleRunAssistant"
              >
                <Loader2 v-if="isAssistantRunning" class="w-4 h-4 mr-1.5 animate-spin" />
                <span>{{ isAssistantRunning ? '正在结合教材切片分析解题...' : '生成解题思路与规范答案' }}</span>
              </button>
            </div>

            <!-- Assistant Output -->
            <div v-if="assistantResult" class="space-y-3 pt-3 border-t border-line text-xs sm:text-sm">
              <div v-if="assistantResult.official_answer" class="surface p-4 space-y-1 bg-raised/70">
                <span class="font-bold text-ink block">已收录参考答案：</span>
                <p class="text-ink whitespace-pre-wrap leading-relaxed">{{ assistantResult.official_answer }}</p>
              </div>

              <div class="surface p-4 space-y-1 bg-accent/10 border-accent/20">
                <span class="font-bold text-accent block">答题思路与切入点：</span>
                <p class="text-accent whitespace-pre-wrap leading-relaxed">{{ assistantResult.thought_process }}</p>
              </div>

              <div class="surface p-4 space-y-1 bg-ok/10 border-ok/30">
                <span class="font-bold text-ok block">AI 规范参考解答：</span>
                <p class="text-ok whitespace-pre-wrap leading-relaxed">{{ assistantResult.ai_answer }}</p>
              </div>

              <div v-if="assistantResult.conflict_notes" class="surface p-4 space-y-1 bg-warn/10 border-warn/30 text-warn">
                <span class="font-bold block">观点依据辨析提示：</span>
                <p class="whitespace-pre-wrap leading-relaxed">{{ assistantResult.conflict_notes }}</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Source Quote Modal -->
    <SourceQuoteModal
      :is-open="isQuoteModalOpen"
      :chunk-id="activeQuote?.chunk_id || ''"
      :doc-name="activeQuote?.doc_name || ''"
      :location-ref="activeQuote?.page_or_ref || ''"
      :content="activeQuote?.snippet || ''"
      @close="isQuoteModalOpen = false"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { useRoute } from 'vue-router';
import {
  Sparkles, Loader2, Bookmark, GitFork, FileText, Split, Quote,
  HelpCircle, AlertTriangle, Search
} from 'lucide-vue-next';
import { apiClient, sessionTransport } from '../api/client';
import type { KnowledgePoint, StudyCard, CitationItem, QuestionItem } from '../types';
import SourceQuoteModal from '../components/SourceQuoteModal.vue';
import EmptyState from '../components/EmptyState.vue';

const route = useRoute();
const knowledgePoints = ref<KnowledgePoint[]>([]);
const selectedKpId = ref<string>('');
const kpSearch = ref('');
const card = ref<StudyCard | null>(null);
const isLoadingCard = ref(false);
const isGenerating = ref(false);

// QA Assistant
const kpQuestions = ref<QuestionItem[]>([]);
const selectedAssistantQid = ref<string>('');
const customQuestionStem = ref<string>('');
const isAssistantRunning = ref(false);
const assistantResult = ref<any>(null);

// Quote Modal
const isQuoteModalOpen = ref(false);
const activeQuote = ref<CitationItem | null>(null);

const selectedKp = computed(() =>
  knowledgePoints.value.find(k => k.id === selectedKpId.value)
);

const filteredKPs = computed(() => {
  if (!kpSearch.value.trim()) return knowledgePoints.value;
  const q = kpSearch.value.trim().toLowerCase();
  return knowledgePoints.value.filter(k =>
    k.name.toLowerCase().includes(q) || (k.chapter && k.chapter.toLowerCase().includes(q))
  );
});

const priorityBadgeClass = (priority?: string) => {
  switch (priority) {
    case 'important': return 'bg-err/15 text-err';
    case 'deferred': return 'bg-raised text-sub';
    default: return 'bg-accent/15 text-accent';
  }
};

const priorityBadgeLabel = (priority?: string) => {
  switch (priority) {
    case 'important': return '重点';
    case 'deferred': return '暂缓';
    default: return '普通';
  }
};

const fetchKnowledgePoints = async () => {
  try {
    knowledgePoints.value = await apiClient.listKnowledgePoints();
    if (route.query.kpid) {
      selectedKpId.value = String(route.query.kpid);
      await loadCard(selectedKpId.value);
    } else if (knowledgePoints.value.length > 0) {
      selectedKpId.value = knowledgePoints.value[0].id;
      await loadCard(selectedKpId.value);
    }
  } catch (e) {
    console.error(e);
  }
};

const handleSelectKp = async (id: string) => {
  selectedKpId.value = id;
  assistantResult.value = null;
  selectedAssistantQid.value = '';
  await loadCard(id);
};

const loadCard = async (kpid: string) => {
  isLoadingCard.value = true;
  card.value = null;
  try {
    kpQuestions.value = await apiClient.listQuestions({ knowledge_point_id: kpid, is_confirmed: 1 });
    card.value = await apiClient.getStudyCard(kpid);
  } catch (e: any) {
    card.value = null;
  } finally {
    isLoadingCard.value = false;
  }
};

const triggerGenerateCard = async () => {
  if (!selectedKpId.value) return;
  isGenerating.value = true;
  try {
    const task = await apiClient.generateStudyCard(selectedKpId.value);
    const pollInterval = setInterval(async () => {
      try {
        const t = await apiClient.getTaskStatus(task.id);
        if (t.status === 'completed') {
          clearInterval(pollInterval);
          isGenerating.value = false;
          await loadCard(selectedKpId.value);
        } else if (t.status === 'failed') {
          clearInterval(pollInterval);
          isGenerating.value = false;
          alert('生成学习卡失败: ' + t.error_msg);
        }
      } catch (e) {
        clearInterval(pollInterval);
        isGenerating.value = false;
      }
    }, 1500);
  } catch (err: any) {
    isGenerating.value = false;
    alert('请求失败: ' + err.message);
  }
};

const handleRunAssistant = async () => {
  if (!selectedAssistantQid.value && !customQuestionStem.value.trim()) {
    alert('请选择真题或输入题目');
    return;
  }
  isAssistantRunning.value = true;
  const payload = { question_id: selectedAssistantQid.value || undefined, custom_stem: customQuestionStem.value.trim() || undefined };
  const assistantKey = sessionTransport.idempotencyKey('cards-assistant', payload);
  try {
    assistantResult.value = await apiClient.answerAssistant(payload, assistantKey);
    sessionTransport.clearIdempotencyKey('cards-assistant', payload);
  } catch (err: any) {
    alert('解题助手生成失败: ' + err.message);
  } finally {
    isAssistantRunning.value = false;
  }
};

const openQuote = (cite: CitationItem) => {
  activeQuote.value = cite;
  isQuoteModalOpen.value = true;
};

onMounted(() => {
  fetchKnowledgePoints();
});
</script>
