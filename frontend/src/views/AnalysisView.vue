<template>
  <div class="page-stack">
    <!-- Top Overall Statistics Banner -->
    <div class="surface p-5 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-4 border-b border-line pb-4">
        <div>
          <h2 class="font-display text-lg font-bold text-ink tracking-tight">历年考情看板与考点目录</h2>
          <p class="text-xs text-sub mt-1">
            严守真实性原则：仅已确认真题参与频次统计。支持从上传教材智能梳理考点，低频与未考考点完整保留。
          </p>
        </div>

        <!-- Toolbar Actions -->
        <div class="flex items-center gap-2 flex-wrap">
          <button
            type="button"
            class="btn btn-primary text-xs cursor-pointer"
            @click="isExtractionModalOpen = true"
          >
            <Sparkles class="w-3.5 h-3.5 mr-1 text-warn" />
            从资料提取考点
          </button>

          <button
            type="button"
            class="btn btn-secondary text-xs cursor-pointer"
            :disabled="isRefreshing"
            @click="handleRefresh"
          >
            <RotateCw class="w-3.5 h-3.5 mr-1" :class="{ 'animate-spin': isRefreshing }" />
            <span>{{ isRefreshing ? '刷新中...' : '刷新数据' }}</span>
          </button>

          <!-- More Options Dropdown -->
          <div class="relative">
            <button
              type="button"
              class="btn btn-secondary text-xs"
              @click="showMoreMenu = !showMoreMenu"
            >
              更多操作
              <ChevronDown class="ml-1 h-3.5 w-3.5 transition-transform" :class="{ 'rotate-180': showMoreMenu }" />
            </button>
            <div
              v-if="showMoreMenu"
              class="absolute right-0 mt-1 w-44 bg-surface rounded-xl shadow-lg border border-line py-1.5 z-20 text-xs text-ink"
              @click="showMoreMenu = false"
            >
              <button
                type="button"
                class="w-full text-left px-3.5 py-2 hover:bg-raised flex items-center gap-2 cursor-pointer"
                @click="openAddKpModal"
              >
                <Plus class="w-3.5 h-3.5 text-faint" />
                手动新增考点
              </button>
              <button
                type="button"
                class="w-full text-left px-3.5 py-2 hover:bg-raised flex items-center gap-2 cursor-pointer"
                @click="openMergeModal"
              >
                <Merge class="w-3.5 h-3.5 text-faint" />
                合并同义考点
              </button>
              <div class="h-px bg-raised my-1" />
              <button
                type="button"
                class="w-full text-left px-3.5 py-2 hover:bg-raised flex items-center gap-2 text-warn cursor-pointer"
                @click="openLegacyModal"
              >
                <HelpCircle class="w-3.5 h-3.5 text-warn" />
                管理旧考点 ({{ legacyCount }})
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- KPI Summary Cards -->
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3.5">
        <div class="bg-raised/70 p-3.5 rounded-xl border border-line">
          <span class="text-xs text-sub">已收录真题年份</span>
          <div class="text-xl font-bold text-ink mt-1">
            {{ overallStats.total_recorded_years }} <span class="text-xs font-normal text-sub">个年份</span>
          </div>
          <span class="text-xs text-faint mt-0.5 block">
            跨度: {{ overallStats.min_year || '-' }} ~ {{ overallStats.max_year || '-' }}
          </span>
        </div>

        <div class="bg-raised/70 p-3.5 rounded-xl border border-line">
          <span class="text-xs text-sub">已确认真题 / 题目总量</span>
          <div class="text-xl font-bold mt-1" :class="overallStats.confirmed_questions > 0 ? 'text-accent' : 'text-ink'">
            {{ overallStats.confirmed_questions }} <span class="text-xs font-normal text-sub">/ {{ overallStats.total_questions }}</span>
          </div>
          <span class="text-xs text-sub mt-0.5 block">
            {{ overallStats.confirmed_questions > 0 ? '已确认题目计入考情统计' : '暂无确认真题，未计考频' }}
          </span>
        </div>

        <div class="bg-raised/70 p-3.5 rounded-xl border border-line">
          <span class="text-xs text-sub">考点目录总数</span>
          <div class="text-xl font-bold text-ink mt-1">
            {{ overallStats.knowledge_points_count }} <span class="text-xs font-normal text-sub">个考点</span>
          </div>
          <span class="text-xs text-faint mt-0.5 block">
            包含已提取与录入考点
          </span>
        </div>

        <div class="bg-raised/70 p-3.5 rounded-xl border border-line">
          <span class="text-xs text-sub">近五年统计基准窗</span>
          <div class="text-xl font-bold text-warn mt-1">
            {{ overallStats.recent_5yr_window.length }} <span class="text-xs font-normal text-sub">/ 5 年已收录</span>
          </div>
          <span class="text-xs text-faint mt-0.5 block">
            以最新已确认真题为准
          </span>
        </div>
      </div>

      <!-- Year Breakdown -->
      <div class="text-xs bg-raised/80 p-3 rounded-xl border border-line space-y-1.5">
        <div class="flex items-center gap-2 text-sub">
          <Calendar class="w-3.5 h-3.5 text-accent" />
          <span class="font-medium">年份收录完整性：</span>
          <span v-if="lastRefreshTime" class="text-faint text-[11px] ml-auto">
            上次更新: {{ lastRefreshTime }}
          </span>
        </div>
        <div class="flex flex-wrap gap-1.5 items-center">
          <span
            v-for="y in overallStats.recorded_years"
            :key="y"
            class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-ok/15 text-ok font-medium"
          >
            <Check class="h-3 w-3" />{{ y }}
          </span>
          <span
            v-for="my in overallStats.missing_years"
            :key="my"
            class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-err/15 text-err font-medium"
          >
            <X class="h-3 w-3" />缺失 {{ my }}
          </span>
          <span v-if="overallStats.recorded_years.length === 0" class="text-faint">
            暂无已确认真题年份。导入真题并确认后将自动计算年份覆盖。
          </span>
        </div>
      </div>
    </div>

    <!-- Running / actionable task summary (含资料解析任务；点击直达任务详情) -->
    <RunningTasksPanel />

    <!-- Filter & Search Toolbar -->
    <div class="page-toolbar">
      <div class="flex items-center gap-3 flex-wrap">
        <!-- Search Input -->
        <div class="relative min-w-[200px]">
          <input
            v-model="searchKeyword"
            type="text"
            placeholder="搜索考点名称或章节..."
            class="field py-1.5 text-xs sm:text-sm pl-8 min-h-[36px]"
          />
          <Search class="w-4 h-4 text-faint absolute left-2.5 top-2.5" />
        </div>

        <!-- Chapter Filter -->
        <select
          v-model="selectedChapter"
          class="field py-1.5 text-xs sm:text-sm min-h-[36px] w-auto"
        >
          <option value="">全部章节</option>
          <option v-for="ch in chaptersList" :key="ch" :value="ch">
            {{ ch }}
          </option>
        </select>
      </div>

      <!-- Priority Filter Tabs -->
      <div class="flex items-center gap-1 bg-raised p-1 rounded-lg text-xs font-medium">
        <button
          v-for="tab in priorityTabs"
          :key="tab.value"
          type="button"
          class="px-2.5 py-1.5 rounded-md transition-all cursor-pointer"
          :class="selectedPriority === tab.value ? 'bg-surface text-accent shadow-xs font-semibold' : 'text-sub hover:text-ink'"
          @click="selectedPriority = tab.value"
        >
          {{ tab.label }}
        </button>
      </div>
    </div>

    <!-- Knowledge Point Stats Table -->
    <div class="surface overflow-hidden">
      <div v-if="filteredPointStats.length === 0">
        <EmptyState
          title="未找到匹配的考点"
          description="尝试更换筛选条件，或者点击上方「从资料提取考点」自动从教材梳理考点。"
        >
          <template #action>
            <button
              type="button"
              class="btn btn-sm btn-primary"
              @click="isExtractionModalOpen = true"
            >
              从资料提取考点
            </button>
          </template>
        </EmptyState>
      </div>

      <div v-else class="overflow-x-auto">
        <table class="w-full text-left border-collapse text-xs sm:text-sm">
          <thead>
            <tr class="bg-raised border-b border-line text-sub font-semibold uppercase tracking-wider text-xs">
              <th class="py-3 px-4">知识点与出处</th>
              <th class="py-3 px-3 text-center">优先级</th>
              <th class="py-3 px-3 text-center">累计考题</th>
              <th class="py-3 px-3 text-center">考查年份数</th>
              <th class="py-3 px-3 text-center">近五年频次</th>
              <th class="py-3 px-3 text-center">最近考查</th>
              <th class="py-3 px-4 text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line">
            <tr
              v-for="item in filteredPointStats"
              :key="item.id"
              class="hover:bg-raised/70 transition-colors"
            >
              <!-- Name & Chapter & Source Badge -->
              <td class="py-3.5 px-4">
                <div class="flex items-center gap-2 flex-wrap">
                  <span v-if="item.code" class="text-accent font-mono text-xs">[{{ item.code }}]</span>
                  <span class="font-semibold text-ink text-sm sm:text-base">{{ item.name }}</span>
                  <span
                    class="px-1.5 py-0.5 rounded text-[11px] font-medium"
                    :class="getSourceBadgeClass(item.source_type, item.source_status)"
                  >
                    {{ getSourceBadgeLabel(item.source_type, item.source_status) }}
                  </span>
                </div>
                <div class="text-xs text-faint mt-1 flex items-center gap-2">
                  <span>{{ item.chapter || '未分类章节' }}</span>
                  <span v-if="item.source_reference" class="text-faint font-normal">
                    · {{ item.source_reference }}
                  </span>
                </div>
              </td>

              <!-- Priority Selector -->
              <td class="py-3 px-3 text-center">
                <select
                  v-model="item.priority_tag"
                  @change="updatePriorityTag(item)"
                  class="text-xs font-medium rounded-lg px-2 py-1 border transition cursor-pointer"
                  :class="priorityTagStyle(item.priority_tag)"
                >
                  <option value="important">重点背 ★</option>
                  <option value="normal">普通</option>
                  <option value="deferred">暂缓</option>
                </select>
              </td>

              <!-- Total Questions -->
              <td class="py-3 px-3 text-center font-bold text-ink">
                <span v-if="item.questions_count > 0">{{ item.questions_count }} 道</span>
                <span v-else class="text-xs text-faint font-normal">暂无考查记录</span>
              </td>

              <!-- Years Count -->
              <td class="py-3 px-3 text-center font-semibold" :class="item.years_count > 0 ? 'text-accent' : 'text-faint font-normal'">
                {{ item.years_count > 0 ? `${item.years_count} 年` : '-' }}
              </td>

              <!-- Recent 5-Year Window -->
              <td class="py-3 px-3 text-center">
                <span
                  v-if="overallStats.recent_5yr_window.length > 0"
                  class="px-2 py-0.5 rounded text-xs font-semibold"
                  :class="recentWindowBadgeClass(item)"
                >
                  {{ item.recent_5yr_count }} / {{ overallStats.recent_5yr_window.length }} 年
                </span>
                <span v-else class="text-xs text-faint">-</span>
              </td>

              <!-- Recent Year -->
              <td class="py-3 px-3 text-center text-sub">
                <span v-if="item.recent_year" class="font-medium text-ink">{{ item.recent_year }} 年</span>
                <span v-else class="text-faint text-xs italic">未曾考查</span>
              </td>

              <!-- Actions -->
              <td class="py-3 px-4 text-right space-x-1.5 whitespace-nowrap">
                <button
                  v-if="item.questions.length > 0"
                  type="button"
                  class="btn btn-sm btn-secondary"
                  @click="viewQuestionsDetail(item)"
                >
                  原题 ({{ item.questions.length }})
                </button>
                <router-link
                  :to="`/recall?chapter_id=${encodeURIComponent(item.id)}`"
                  class="btn btn-sm btn-primary"
                >
                  回忆卡
                </router-link>
                <router-link
                  :to="`/today?chapter_id=${encodeURIComponent(item.id)}`"
                  class="btn btn-sm btn-secondary"
                >
                  今日学习
                </router-link>
                <router-link
                  :to="`/cards?kpid=${encodeURIComponent(item.id)}`"
                  class="btn btn-sm btn-secondary"
                >
                  考点讲义
                </router-link>
                <button
                  type="button"
                  class="p-1.5 text-faint hover:text-err rounded"
                  title="删除考点"
                  @click="handleDeleteKp(item.id, item.name)"
                >
                  <Trash2 class="w-3.5 h-3.5" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Extraction Wizard Modal -->
    <ExtractionModal
      :show="isExtractionModalOpen"
      @close="isExtractionModalOpen = false"
      @success="handleExtractionSuccess"
    />

    <!-- Add Knowledge Point Modal -->
    <BaseModal
      :show="isAddModalOpen"
      title="新增知识点"
      max-width="md"
      @close="isAddModalOpen = false"
    >
      <div class="space-y-3.5 text-sm">
        <div>
          <label class="field-label">知识点名称</label>
          <input v-model="newKpForm.name" type="text" placeholder="如: 目标管理理论 (MBO)" class="field" />
        </div>
        <div>
          <label class="field-label">章节序号 / 编号 (选填)</label>
          <input v-model="newKpForm.code" type="text" placeholder="如: 3.2" class="field" />
        </div>
        <div>
          <label class="field-label">所属章节</label>
          <input v-model="newKpForm.chapter" type="text" placeholder="如: 第三章 组织与设计" class="field" />
        </div>
        <div>
          <label class="field-label">背诵优先级</label>
          <select v-model="newKpForm.priority_tag" class="field">
            <option value="important">重点背 ★</option>
            <option value="normal">普通</option>
            <option value="deferred">暂缓</option>
          </select>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary" @click="isAddModalOpen = false">取消</button>
        <button type="button" class="btn btn-primary" @click="handleAddKp">保存考点</button>
      </template>
    </BaseModal>

    <!-- Merge Synonymous Modal -->
    <BaseModal
      :show="isMergeModalOpen"
      title="合并同义考点"
      max-width="lg"
      @close="isMergeModalOpen = false"
    >
      <div class="space-y-4 text-sm">
        <p class="text-xs text-sub leading-relaxed">
          选择一个保留的目标知识点，勾选要合并吸纳的同义考点。合并后历史真题关联与模拟练习将平滑转移至目标考点，原考点将被软删除以防断链。
        </p>
        <div>
          <label class="field-label">保留的目标考点 (最终名称)</label>
          <select v-model="mergeTargetId" class="field">
            <option disabled value="">-- 请选择保留的目标考点 --</option>
            <option v-for="kp in pointStats" :key="kp.id" :value="kp.id">
              {{ kp.name }} ({{ kp.chapter || '未分类' }})
            </option>
          </select>
        </div>

        <div>
          <label class="field-label">被合并吸收的同义考点 (多选)</label>
          <div class="max-h-48 overflow-y-auto space-y-1.5 border border-line p-2.5 rounded-xl bg-raised/50">
            <label
              v-for="kp in pointStats.filter(p => p.id !== mergeTargetId)"
              :key="kp.id"
              class="flex items-center gap-2 p-1.5 hover:bg-surface rounded cursor-pointer text-xs"
            >
              <input type="checkbox" :value="kp.id" v-model="mergeSourceIds" class="rounded text-accent" />
              <span class="text-ink font-medium">{{ kp.name }}</span>
              <span class="text-faint">({{ kp.questions_count }} 道题)</span>
            </label>
          </div>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary" @click="isMergeModalOpen = false">取消</button>
        <button
          type="button"
          class="btn btn-primary"
          :disabled="!mergeTargetId || mergeSourceIds.length === 0"
          @click="handleMergeKp"
        >
          确认合并
        </button>
      </template>
    </BaseModal>

    <!-- Legacy Knowledge Points Modal -->
    <BaseModal
      :show="isLegacyModalOpen"
      title="管理待核实旧考点"
      max-width="2xl"
      @close="isLegacyModalOpen = false"
    >
      <div class="space-y-3.5 text-sm">
        <p class="text-xs text-sub leading-relaxed">
          以下为系统初始化保留的历史考点（来源标记为待核实）。您可以在此逐一审核：核对无误可标记为有效，或将其删除。
        </p>
        <div v-if="legacyKPs.length === 0" class="py-8 text-center text-faint text-xs">
          没有待核实的旧考点记录。
        </div>
        <div v-else class="space-y-2 max-h-60 overflow-y-auto pr-1">
          <div
            v-for="kp in legacyKPs"
            :key="kp.id"
            class="p-3 rounded-xl border border-line bg-raised/50 flex items-center justify-between gap-3 text-xs"
          >
            <div>
              <span class="font-semibold text-ink text-sm">{{ kp.name }}</span>
              <span v-if="kp.chapter" class="text-sub ml-2">({{ kp.chapter }})</span>
            </div>
            <div class="flex items-center gap-2">
              <button
                type="button"
                class="btn btn-sm btn-primary bg-emerald-600 hover:bg-emerald-700"
                @click="verifyLegacyKp(kp.id)"
              >
                标记已核实
              </button>
              <button
                type="button"
                class="btn btn-sm btn-secondary text-err hover:bg-err/10 border-err/30"
                @click="handleDeleteKp(kp.id, kp.name)"
              >
                删除
              </button>
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary" @click="isLegacyModalOpen = false">关闭</button>
      </template>
    </BaseModal>

    <!-- Questions Detail Modal -->
    <BaseModal
      :show="selectedKpDetail !== null"
      :title="`历年真题与答案: ${selectedKpDetail?.name || ''}`"
      max-width="3xl"
      @close="selectedKpDetail = null"
    >
      <div v-if="selectedKpDetail" class="space-y-3.5 text-sm">
        <div v-if="selectedKpDetail.questions.length === 0" class="py-8 text-center text-faint text-xs">
          该知识点在已确认真题中暂无考查记录。
        </div>
        <div
          v-for="q in selectedKpDetail.questions"
          :key="q.id"
          class="surface p-4 space-y-2 text-xs sm:text-sm"
        >
          <div class="flex items-center justify-between text-xs border-b border-line pb-2">
            <span class="font-semibold text-accent">
              {{ q.year ? `${q.year}年真题` : '历年真题' }} · {{ q.session_name || '' }} {{ q.question_num || '' }}
            </span>
            <span v-if="q.score" class="text-faint">[{{ q.score }} 分]</span>
          </div>
          <p class="reading-content font-medium text-ink">
            {{ q.stem }}
          </p>
          <div class="bg-raised p-3 rounded-lg border border-line text-xs space-y-1">
            <span class="font-semibold text-sub block">参考答案：</span>
            <p class="text-ink whitespace-pre-wrap leading-relaxed">
              {{ q.standard_answer || '[暂未收录参考答案]' }}
            </p>
          </div>
        </div>
      </div>
    </BaseModal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import {
  Sparkles, RotateCw, Plus, Merge, Calendar,
  Search, Trash2, HelpCircle, ChevronDown, Check, X
} from 'lucide-vue-next';
import { apiClient } from '../api/client';
import type { KnowledgePointStats, OverallStats, KnowledgePoint } from '../types';
import ExtractionModal from '../components/ExtractionModal.vue';
import RunningTasksPanel from '../components/RunningTasksPanel.vue';
import BaseModal from '../components/BaseModal.vue';
import EmptyState from '../components/EmptyState.vue';

const overallStats = ref<OverallStats>({
  total_recorded_years: 0,
  min_year: null,
  max_year: null,
  recorded_years: [],
  missing_years: [],
  recent_5yr_window: [],
  total_questions: 0,
  confirmed_questions: 0,
  knowledge_points_count: 0
});

const pointStats = ref<KnowledgePointStats[]>([]);
const legacyKPs = ref<KnowledgePoint[]>([]);
const selectedPriority = ref<string>('all');
const searchKeyword = ref('');
const selectedChapter = ref('');
const selectedKpDetail = ref<KnowledgePointStats | null>(null);

const isRefreshing = ref(false);
const lastRefreshTime = ref('');
const showMoreMenu = ref(false);

const isExtractionModalOpen = ref(false);
const isAddModalOpen = ref(false);
const isMergeModalOpen = ref(false);
const isLegacyModalOpen = ref(false);
let fetchGeneration = 0;

const newKpForm = ref({
  name: '',
  code: '',
  chapter: '',
  priority_tag: 'important' as 'important' | 'normal' | 'deferred'
});

const mergeTargetId = ref('');
const mergeSourceIds = ref<string[]>([]);

const priorityTabs = [
  { label: '全部考点', value: 'all' },
  { label: '重点背 ★', value: 'important' },
  { label: '普通', value: 'normal' },
  { label: '暂缓', value: 'deferred' }
];

const legacyCount = computed(() => legacyKPs.value.length);

const chaptersList = computed(() => {
  const chapters = new Set<string>();
  for (const p of pointStats.value) {
    if (p.chapter) chapters.add(p.chapter.trim());
  }
  return Array.from(chapters);
});

const filteredPointStats = computed(() => {
  return pointStats.value.filter(p => {
    if (selectedPriority.value !== 'all' && p.priority_tag !== selectedPriority.value) {
      return false;
    }
    if (selectedChapter.value && p.chapter?.trim() !== selectedChapter.value) {
      return false;
    }
    if (searchKeyword.value.trim()) {
      const q = searchKeyword.value.trim().toLowerCase();
      const matchName = p.name.toLowerCase().includes(q);
      const matchChapter = p.chapter?.toLowerCase().includes(q);
      const matchCode = p.code?.toLowerCase().includes(q);
      if (!matchName && !matchChapter && !matchCode) return false;
    }
    return true;
  });
});

const fetchAll = async () => {
  const token = ++fetchGeneration;
  try {
    isRefreshing.value = true;
    const [overall, list, legacy] = await Promise.all([
      apiClient.getOverallStats(),
      apiClient.getKnowledgePointsStats(),
      apiClient.getLegacyKnowledgePoints()
    ]);
    if (token !== fetchGeneration) return;
    overallStats.value = overall;
    pointStats.value = list;
    legacyKPs.value = legacy;
    const now = new Date();
    lastRefreshTime.value = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
  } catch (e) {
    console.error('Failed to load stats:', e);
  } finally {
    if (token === fetchGeneration) isRefreshing.value = false;
  }
};

const handleRefresh = async () => {
  await fetchAll();
};

const handleExtractionSuccess = async () => {
  await fetchAll();
};

const getSourceBadgeLabel = (sourceType?: string, sourceStatus?: string) => {
  if (sourceStatus === 'supplement_needed') return '依据待补';
  switch (sourceType) {
    case 'extracted': return '资料提取';
    case 'pending_verification': return '待核实来源';
    default: return '手动录入';
  }
};

const getSourceBadgeClass = (sourceType?: string, sourceStatus?: string) => {
  if (sourceStatus === 'supplement_needed') return 'bg-err/15 text-err';
  switch (sourceType) {
    case 'extracted': return 'bg-accent/15 text-accent';
    case 'pending_verification': return 'bg-warn/15 text-warn';
    default: return 'bg-raised text-ink';
  }
};

const priorityTagStyle = (tag: string) => {
  if (tag === 'important') return 'bg-err/10 text-err border-err/30';
  if (tag === 'deferred') return 'bg-raised text-sub border-line';
  return 'bg-accent/10 text-accent border-accent/30';
};

const recentWindowBadgeClass = (item: KnowledgePointStats) => {
  const windowLen = overallStats.value.recent_5yr_window.length;
  if (windowLen === 0) return 'bg-raised text-faint';
  const ratio = item.recent_5yr_count / windowLen;
  if (item.recent_5yr_count >= 2 || ratio >= 0.4) {
    return 'bg-err/10 text-err font-bold';
  }
  if (item.recent_5yr_count >= 1) {
    return 'bg-warn/10 text-warn';
  }
  return 'bg-raised text-faint';
};

const updatePriorityTag = async (item: KnowledgePointStats) => {
  try {
    await apiClient.updateKnowledgePoint(item.id, { priority_tag: item.priority_tag });
  } catch (err: any) {
    alert('更新优先级失败: ' + err.message);
  }
};

const viewQuestionsDetail = (item: KnowledgePointStats) => {
  selectedKpDetail.value = item;
};

const openAddKpModal = () => {
  newKpForm.value = { name: '', code: '', chapter: '', priority_tag: 'important' };
  isAddModalOpen.value = true;
};

const handleAddKp = async () => {
  if (!newKpForm.value.name.trim()) {
    alert('请输入知识点名称');
    return;
  }
  try {
    await apiClient.createKnowledgePoint({
      name: newKpForm.value.name.trim(),
      code: newKpForm.value.code.trim() || undefined,
      chapter: newKpForm.value.chapter.trim() || undefined,
      priority_tag: newKpForm.value.priority_tag,
      sort_order: 0
    });
    isAddModalOpen.value = false;
    await fetchAll();
  } catch (err: any) {
    alert('创建失败: ' + err.message);
  }
};

const openMergeModal = () => {
  mergeTargetId.value = '';
  mergeSourceIds.value = [];
  isMergeModalOpen.value = true;
};

const handleMergeKp = async () => {
  if (!mergeTargetId.value || mergeSourceIds.value.length === 0) return;
  try {
    await apiClient.mergeKnowledgePoints(mergeTargetId.value, mergeSourceIds.value);
    isMergeModalOpen.value = false;
    await fetchAll();
  } catch (err: any) {
    alert('合并失败: ' + err.message);
  }
};

const openLegacyModal = () => {
  isLegacyModalOpen.value = true;
};

const verifyLegacyKp = async (id: string) => {
  try {
    await apiClient.updateKnowledgePoint(id, { source_type: 'manual', source_status: 'valid' });
    await fetchAll();
  } catch (err: any) {
    alert('更新失败: ' + err.message);
  }
};

const handleDeleteKp = async (id: string, name: string) => {
  if (!confirm(`确定要删除考点「${name}」吗？历史练习记录将被安全保留，不会断链。`)) return;
  try {
    await apiClient.deleteKnowledgePoint(id);
    await fetchAll();
  } catch (err: any) {
    alert('删除失败: ' + err.message);
  }
};

onMounted(() => {
  fetchAll();
});
</script>
