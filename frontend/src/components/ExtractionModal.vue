<template>
  <BaseModal
    :show="show"
    title="从资料提取考点"
    max-width="3xl"
    :close-on-backdrop="step !== 'processing'"
    @close="handleClose"
  >
    <!-- Step Indicator -->
    <div class="mb-6">
      <div class="flex items-center justify-between text-xs font-medium text-stone-500 max-w-md mx-auto">
        <div class="flex items-center gap-1.5" :class="{ 'text-brand-600 font-semibold': step === 'select' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'select' ? 'border-brand-600 bg-brand-50 text-brand-600' : 'border-stone-300'">1</span>
          <span>选择资料</span>
        </div>
        <div class="w-8 h-px bg-stone-200" />
        <div class="flex items-center gap-1.5" :class="{ 'text-brand-600 font-semibold': step === 'processing' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'processing' ? 'border-brand-600 bg-brand-50 text-brand-600' : 'border-stone-300'">2</span>
          <span>AI 梳理</span>
        </div>
        <div class="w-8 h-px bg-stone-200" />
        <div class="flex items-center gap-1.5" :class="{ 'text-brand-600 font-semibold': step === 'review' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'review' ? 'border-brand-600 bg-brand-50 text-brand-600' : 'border-stone-300'">3</span>
          <span>核对候选</span>
        </div>
        <div class="w-8 h-px bg-stone-200" />
        <div class="flex items-center gap-1.5" :class="{ 'text-brand-600 font-semibold': step === 'linking' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'linking' ? 'border-brand-600 bg-brand-50 text-brand-600' : 'border-stone-300'">4</span>
          <span>真题关联</span>
        </div>
      </div>
    </div>

    <!-- Step 1: Select Documents -->
    <div v-if="step === 'select'" class="space-y-4">
      <p class="text-sm text-stone-600">
        请选择用于提取考点的参考教材或补充资料。系统将分析资料切片并梳理核心概念。
      </p>

      <div v-if="loadingDocs" class="py-8 text-center text-stone-400 text-sm">
        正在加载资料列表...
      </div>
      <div v-else-if="availableDocs.length === 0" class="py-8 text-center text-stone-500 text-sm">
        暂无已解析完成的教材或补充资料，请先前往资料库上传。
      </div>
      <div v-else class="space-y-2 max-h-60 overflow-y-auto pr-1">
        <label
          v-for="doc in availableDocs"
          :key="doc.id"
          class="flex items-center justify-between p-3 rounded-xl border border-stone-200 hover:border-brand-300 bg-white hover:bg-brand-50/30 cursor-pointer transition-all"
        >
          <div class="flex items-center gap-3">
            <input
              type="checkbox"
              :value="doc.id"
              v-model="selectedDocIds"
              class="w-4 h-4 text-brand-600 rounded border-stone-300 focus:ring-brand-500"
            >
            <div>
              <p class="text-sm font-medium text-stone-800">{{ doc.filename }}</p>
              <p class="text-xs text-stone-400">{{ doc.file_type === 'textbook' ? '参考教材' : '补充资料' }} · 共 {{ doc.page_count || 0 }} 页</p>
            </div>
          </div>
          <span class="text-xs text-stone-400 font-mono">已解析</span>
        </label>
      </div>

      <p v-if="docError" class="text-xs text-rose-600 bg-rose-50 p-2.5 rounded-lg border border-rose-200">
        {{ docError }}
      </p>
    </div>

    <!-- Step 2: Processing -->
    <div v-else-if="step === 'processing'" class="py-8 text-center space-y-4">
      <div class="w-12 h-12 mx-auto rounded-full bg-brand-50 flex items-center justify-center text-brand-600">
        <svg class="w-6 h-6 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      </div>
      <div>
        <h4 class="text-base font-semibold text-stone-800">正在分析资料并梳理考点</h4>
        <p class="text-xs text-stone-500 mt-1 max-w-sm mx-auto">
          {{ taskStatusMessage || '正在按切片分批分析，提取核心概念与原文依据...' }}
        </p>
      </div>

      <!-- Progress Bar -->
      <div class="max-w-md mx-auto space-y-1">
        <div class="w-full bg-stone-100 rounded-full h-2 overflow-hidden">
          <div
            class="bg-brand-600 h-2 rounded-full transition-all duration-300"
            :style="{ width: `${taskProgress}%` }"
          />
        </div>
        <div class="flex justify-between text-xs text-stone-400">
          <span>切片校验中</span>
          <span>{{ taskProgress }}%</span>
        </div>
      </div>
    </div>

    <!-- Step 3: Review Candidates -->
    <div v-else-if="step === 'review'" class="space-y-4">
      <div class="flex items-center justify-between">
        <p class="text-xs text-stone-500">
          已从资料中提取出 <span class="font-bold text-stone-800">{{ candidates.length }}</span> 个考点候选。请逐项核对并确认：
        </p>
        <div class="flex items-center gap-2">
          <button
            type="button"
            class="text-xs text-brand-600 hover:underline cursor-pointer"
            @click="setAllAction('accept')"
          >
            全部采纳
          </button>
          <span class="text-stone-300">|</span>
          <button
            type="button"
            class="text-xs text-stone-500 hover:underline cursor-pointer"
            @click="setAllAction('ignore')"
          >
            全部忽略
          </button>
        </div>
      </div>

      <div class="space-y-3 max-h-[50vh] overflow-y-auto pr-1">
        <div
          v-for="cand in candidates"
          :key="cand.id"
          class="p-3.5 rounded-xl border transition-all text-sm"
          :class="getDecision(cand.id).action === 'ignore' ? 'border-stone-200 bg-stone-50 opacity-60' : 'border-stone-200 bg-white shadow-xs'"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1 space-y-1">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-semibold text-stone-800">{{ cand.name }}</span>
                <span
                  class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium"
                  :class="getMatchBadgeClass(cand.match_type)"
                >
                  {{ getMatchBadgeLabel(cand.match_type) }}
                </span>
                <span v-if="cand.chapter" class="text-xs text-stone-400">
                  {{ cand.chapter }}
                </span>
              </div>
              <p v-if="cand.definition" class="text-xs text-stone-600 leading-relaxed">
                {{ cand.definition }}
              </p>
            </div>

            <!-- Action Select -->
            <div class="flex items-center gap-1.5 shrink-0">
              <select
                v-model="getDecision(cand.id).action"
                class="text-xs rounded-lg border-stone-300 py-1 px-2 focus:ring-brand-500"
              >
                <option value="accept">采纳入库</option>
                <option value="merge">合并至现有</option>
                <option value="ignore">忽略</option>
              </select>
            </div>
          </div>

          <!-- Merge target dropdown if merge is selected -->
          <div v-if="getDecision(cand.id).action === 'merge'" class="mt-2.5 pt-2 border-t border-stone-100 flex items-center gap-2">
            <span class="text-xs text-stone-500">选择合并目标:</span>
            <select
              v-model="getDecision(cand.id).target_kp_id"
              class="text-xs rounded-lg border-stone-300 py-1 px-2 flex-1 focus:ring-brand-500"
            >
              <option value="">-- 请选择目标考点 --</option>
              <option v-for="kp in existingKPs" :key="kp.id" :value="kp.id">
                {{ kp.name }} ({{ kp.chapter || '未分类' }})
              </option>
            </select>
          </div>

          <!-- Citations View -->
          <div v-if="cand.citations && cand.citations.length > 0" class="mt-2.5 pt-2 border-t border-stone-100">
            <details class="text-xs text-stone-500 group">
              <summary class="cursor-pointer font-medium hover:text-brand-600 flex items-center gap-1">
                <span>查看看出处依据 ({{ cand.citations.length }} 处)</span>
              </summary>
              <div class="mt-2 space-y-1.5 pl-2 border-l-2 border-stone-200">
                <div v-for="(cit, cIdx) in cand.citations" :key="cIdx" class="text-xs">
                  <span class="font-medium text-stone-700">{{ cit.doc_name }}</span>
                  <span v-if="cit.page_number" class="text-stone-400 ml-1">P{{ cit.page_number }}</span>
                  <p class="text-stone-600 italic mt-0.5 bg-stone-50 p-1.5 rounded">
                    “{{ cit.quote }}”
                  </p>
                </div>
              </div>
            </details>
          </div>
        </div>
      </div>
    </div>

    <!-- Step 4: Suggest Question Links -->
    <div v-else-if="step === 'linking'" class="space-y-4">
      <div v-if="questionLinksData?.total_confirmed_questions === 0" class="py-6 text-center space-y-2">
        <div class="w-10 h-10 mx-auto rounded-full bg-stone-100 flex items-center justify-center text-stone-400">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        </div>
        <p class="text-sm font-semibold text-stone-700">当前题库暂无已确认真题</p>
        <p class="text-xs text-stone-500 max-w-md mx-auto leading-relaxed">
          考点已成功入库。系统严格保证数据真实性，未凭空捏造虚假真题考查记录。您可随时在资料库导入真题后再进行关联。
        </p>
      </div>

      <div v-else-if="questionLinksData && questionLinksData.links.length === 0" class="py-6 text-center space-y-2">
        <p class="text-sm font-medium text-stone-700">未发现与新入库考点直接匹配的真题</p>
        <p class="text-xs text-stone-400">新考点已入库，后续可手动在题库中关联考点。</p>
      </div>

      <div v-else-if="questionLinksData && questionLinksData.links.length > 0" class="space-y-3">
        <p class="text-xs text-stone-600">
          检测到以下已确认真题与新考点高度相关，建议建立关联：
        </p>
        <div class="space-y-2 max-h-[50vh] overflow-y-auto pr-1">
          <label
            v-for="(link, lIdx) in questionLinksData.links"
            :key="lIdx"
            class="flex items-start gap-3 p-3 rounded-xl border border-stone-200 bg-white hover:border-brand-200 cursor-pointer text-xs"
          >
            <input
              type="checkbox"
              v-model="selectedLinkIndexes"
              :value="lIdx"
              class="w-4 h-4 mt-0.5 text-brand-600 rounded border-stone-300 focus:ring-brand-500"
            >
            <div class="min-w-0 flex-1 space-y-1">
              <div class="flex items-center gap-2 font-medium text-stone-800">
                <span>{{ link.year ? `${link.year}年` : '' }} {{ link.session_name || '' }} {{ link.question_num || '' }}</span>
                <span class="inline-flex items-center gap-1 text-brand-600">关联至<ArrowRight class="h-3 w-3" />「{{ link.knowledge_point_name }}」</span>
              </div>
              <p class="text-stone-600 line-clamp-2">
                {{ link.question_stem }}
              </p>
              <p v-if="link.reason" class="text-[11px] text-stone-400">
                匹配依据: {{ link.reason }}
              </p>
            </div>
          </label>
        </div>
      </div>
    </div>

    <!-- Footer Actions -->
    <template #footer>
      <div class="flex items-center justify-between w-full">
        <button
          type="button"
          class="btn btn-secondary text-xs"
          :disabled="step === 'processing'"
          @click="handleClose"
        >
          {{ step === 'linking' ? '跳过并关闭' : '取消' }}
        </button>

        <div class="flex items-center gap-2">
          <button
            v-if="step === 'select'"
            type="button"
            class="btn btn-primary text-xs"
            :disabled="selectedDocIds.length === 0"
            @click="startExtraction"
          >
            开始梳理考点 (已选 {{ selectedDocIds.length }} 份)
          </button>

          <button
            v-else-if="step === 'review'"
            type="button"
            class="btn btn-primary text-xs"
            :disabled="isApplying"
            @click="applyCandidates"
          >
            {{ isApplying ? '正在入库...' : '确认并更新考点目录' }}
          </button>

          <button
            v-else-if="step === 'linking'"
            type="button"
            class="btn btn-primary text-xs"
            :disabled="isApplyingLinks"
            @click="confirmQuestionLinks"
          >
            {{ isApplyingLinks ? '正在保存...' : '完成考点梳理' }}
          </button>
        </div>
      </div>
    </template>
  </BaseModal>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue';
import { ArrowRight } from 'lucide-vue-next';
import { apiClient } from '../api/client';
import type { DocumentItem, ExtractionCandidate, KnowledgePoint, QuestionLinkSuggestionResponse } from '../types';
import BaseModal from './BaseModal.vue';

const props = defineProps<{
  show: boolean;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'success'): void;
}>();

const step = ref<'select' | 'processing' | 'review' | 'linking'>('select');
const availableDocs = ref<DocumentItem[]>([]);
const loadingDocs = ref(false);
const selectedDocIds = ref<string[]>([]);
const docError = ref<string | null>(null);

const extractionId = ref<string | null>(null);
const taskId = ref<string | null>(null);
const taskProgress = ref(0);
const taskStatusMessage = ref('');
let pollInterval: any = null;

const candidates = ref<ExtractionCandidate[]>([]);
const decisions = ref<Record<string, { action: 'accept' | 'ignore' | 'merge'; target_kp_id?: string; custom_name?: string }>>({});
const existingKPs = ref<KnowledgePoint[]>([]);
const isApplying = ref(false);

const questionLinksData = ref<QuestionLinkSuggestionResponse | null>(null);
const selectedLinkIndexes = ref<number[]>([]);
const isApplyingLinks = ref(false);

const loadInitialData = async () => {
  try {
    loadingDocs.value = true;
    docError.value = null;
    const docs = await apiClient.listMaterials();
    availableDocs.value = docs.filter(d => d.status === 'completed' && ['textbook', 'supplement'].includes(d.file_type));
    if (availableDocs.value.length > 0 && selectedDocIds.value.length === 0) {
      selectedDocIds.value = [availableDocs.value[0].id];
    }
    const kps = await apiClient.listKnowledgePoints();
    existingKPs.value = kps;
  } catch (err: any) {
    docError.value = err?.message || '加载资料失败';
  } finally {
    loadingDocs.value = false;
  }
};

watch(() => props.show, (val) => {
  if (val) {
    step.value = 'select';
    loadInitialData();
  } else {
    stopPolling();
  }
});

const startExtraction = async () => {
  if (selectedDocIds.value.length === 0) return;
  try {
    docError.value = null;
    step.value = 'processing';
    taskProgress.value = 5;
    taskStatusMessage.value = '正在创建提取任务...';

    const batch = await apiClient.createExtraction(selectedDocIds.value);
    extractionId.value = batch.id;
    taskId.value = batch.task_id;

    startPolling();
  } catch (err: any) {
    step.value = 'select';
    docError.value = err?.response?.data?.detail || err.message || '启动考点提取失败';
  }
};

const startPolling = () => {
  stopPolling();
  pollInterval = setInterval(async () => {
    if (!taskId.value || !extractionId.value) return;
    try {
      const task = await apiClient.getTaskStatus(taskId.value);
      taskProgress.value = task.progress;
      taskStatusMessage.value = task.message || '';

      if (task.status === 'completed') {
        stopPolling();
        await loadCandidates();
      } else if (task.status === 'failed' || task.status === 'cancelled') {
        stopPolling();
        step.value = 'select';
        docError.value = task.error_msg || task.message || '任务失败';
      }
    } catch (err) {
      console.error('Error polling extraction task:', err);
    }
  }, 1500);
};

const stopPolling = () => {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
};

const loadCandidates = async () => {
  if (!extractionId.value) return;
  try {
    const cands = await apiClient.getExtractionCandidates(extractionId.value);
    candidates.value = cands;

    // Initialize decisions
    const dMap: Record<string, any> = {};
    for (const c of cands) {
      dMap[c.id] = {
        candidate_id: c.id,
        action: 'accept',
        target_kp_id: c.matched_kp_id || '',
        custom_name: c.name
      };
    }
    decisions.value = dMap;
    step.value = 'review';
  } catch (err: any) {
    step.value = 'select';
    docError.value = '加载考点候选失败';
  }
};

const getDecision = (candId: string) => {
  if (!decisions.value[candId]) {
    decisions.value[candId] = { action: 'accept', candidate_id: candId } as any;
  }
  return decisions.value[candId];
};

const setAllAction = (action: 'accept' | 'ignore') => {
  for (const c of candidates.value) {
    if (decisions.value[c.id]) {
      decisions.value[c.id].action = action;
    }
  }
};

const getMatchBadgeLabel = (type: string) => {
  switch (type) {
    case 'existing': return '现有有效考点';
    case 'deleted_or_ignored': return '曾删除/待核对';
    case 'source_changed': return '依据变更';
    default: return '新发现考点';
  }
};

const getMatchBadgeClass = (type: string) => {
  switch (type) {
    case 'existing': return 'bg-emerald-100 text-emerald-800';
    case 'deleted_or_ignored': return 'bg-amber-100 text-amber-800';
    default: return 'bg-brand-100 text-brand-800';
  }
};

const applyCandidates = async () => {
  if (!extractionId.value) return;
  try {
    isApplying.value = true;
    const payload = Object.values(decisions.value).map(d => ({
      candidate_id: (d as any).candidate_id,
      action: d.action,
      target_kp_id: d.target_kp_id || undefined,
      custom_name: d.custom_name || undefined
    }));
    await apiClient.applyCandidates(extractionId.value, payload);

    // Proceed to question linking step
    await loadQuestionLinks();
  } catch (err: any) {
    alert(err?.response?.data?.detail || '保存考点决策失败');
  } finally {
    isApplying.value = false;
  }
};

const loadQuestionLinks = async () => {
  if (!extractionId.value) return;
  try {
    const data = await apiClient.getQuestionLinks(extractionId.value);
    questionLinksData.value = data;
    selectedLinkIndexes.value = data.links.map((_: any, i: number) => i);
    step.value = 'linking';

  } catch (err) {
    console.error('Failed to load question links:', err);
    emit('success');
    handleClose();
  }
};

const confirmQuestionLinks = async () => {
  if (!extractionId.value || !questionLinksData.value) {
    emit('success');
    handleClose();
    return;
  }

  if (questionLinksData.value.links.length > 0 && selectedLinkIndexes.value.length > 0) {
    try {
      isApplyingLinks.value = true;
      const linksPayload = selectedLinkIndexes.value.map(idx => {
        const item = questionLinksData.value!.links[idx];
        return {
          question_id: item.question_id,
          knowledge_point_id: item.knowledge_point_id,
          action: 'accept'
        };
      });
      await apiClient.applyQuestionLinks(extractionId.value, linksPayload);
    } catch (err) {
      console.error('Failed to apply question links:', err);
    } finally {
      isApplyingLinks.value = false;
    }
  }

  emit('success');
  handleClose();
};

const handleClose = () => {
  stopPolling();
  emit('close');
};

onMounted(() => {
  if (props.show) {
    loadInitialData();
  }
});
</script>
