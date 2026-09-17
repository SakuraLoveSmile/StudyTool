<template>
  <div class="page-stack">
    <!-- Page Header -->
    <header class="flex flex-wrap items-end justify-between gap-3 px-1">
      <div>
        <h1 class="font-display text-xl sm:text-2xl font-bold tracking-tight text-stone-900">资料与真题</h1>
        <p class="mt-1 text-sm text-stone-500">导入教材与历年真题，核对识别结果后进入统计与背诵流程。</p>
      </div>
    </header>

    <!-- Top Active Tasks Banner -->
    <div
      v-if="activeCount > 0"
      class="bg-brand-50 border border-brand-200 rounded-xl p-4 flex items-center justify-between gap-4 shadow-xs"
    >
      <div class="flex items-center gap-3">
        <span class="relative flex h-3 w-3">
          <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-brand-400 opacity-75" />
          <span class="relative inline-flex rounded-full h-3 w-3 bg-brand-600" />
        </span>
        <div>
          <h4 class="text-sm font-semibold text-brand-900">
            有 {{ activeCount }} 个后台任务正在运行
          </h4>
          <p class="text-xs text-brand-700 mt-0.5">
            资料解析与考点梳理在后台持续执行，您可以继续进行其他操作或随时在右侧抽屉查看进度。
          </p>
        </div>
      </div>
      <button
        type="button"
        class="btn btn-sm btn-primary shrink-0 cursor-pointer"
        @click="openDrawer"
      >
        查看任务进度
      </button>
    </div>

    <!-- Error Alert if any -->
    <div
      v-if="pageError"
      class="bg-rose-50 border border-rose-200 rounded-xl px-4 py-3 text-sm text-rose-700 flex items-center justify-between"
    >
      <span>{{ pageError }}</span>
      <button @click="reloadPage" class="font-medium hover:underline text-xs">重试</button>
    </div>

    <!-- Upload Card -->
    <div class="surface p-5 space-y-4">
      <div class="flex items-center justify-between pb-3 border-b border-stone-100">
        <div class="flex items-center gap-2">
          <UploadCloud class="w-5 h-5 text-brand-600" />
          <h3 class="font-display text-base font-semibold text-stone-800">导入新教材与真题资料</h3>
        </div>
        <button
          type="button"
          class="text-xs text-stone-500 hover:text-stone-800"
          @click="showUploadForm = !showUploadForm"
        >
          {{ showUploadForm ? '收起上传面板' : '展开上传面板' }}
        </button>
      </div>

      <form v-if="showUploadForm" @submit.prevent="handleUpload" class="space-y-4">
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
          <!-- File Input -->
          <div>
            <label class="field-label">选择文件</label>
            <input
              type="file"
              ref="fileInput"
              accept=".pdf,.docx,.jpg,.jpeg,.png"
              required
              class="field text-xs file:mr-2 file:py-1 file:px-2.5 file:rounded file:border-0 file:text-xs file:font-semibold file:bg-brand-50 file:text-brand-700 hover:file:bg-brand-100 cursor-pointer"
            />
            <p class="text-xs text-stone-400 mt-1">支持 PDF、Word (.docx)、扫描图片</p>
          </div>

          <!-- File Type -->
          <div>
            <label class="field-label">资料类别</label>
            <select v-model="uploadForm.file_type" class="field text-sm">
              <option value="textbook">教材 / 专业参考书</option>
              <option value="exam_and_answer">真题及答案合辑 (跨多年 / 题答一体)</option>
              <option value="exam">历年真题试卷 (纯试题)</option>
              <option value="answer">历年真题参考答案 (纯答案)</option>
              <option value="supplement">讲义 / 补充资料</option>
            </select>
          </div>

          <!-- Metadata -->
          <div v-if="['exam', 'answer'].includes(uploadForm.file_type)" class="grid grid-cols-2 gap-2">
            <div>
              <label class="field-label">所属年份</label>
              <input
                type="number"
                v-model.number="uploadForm.year"
                placeholder="如: 2023"
                class="field text-sm"
              />
            </div>
            <div>
              <label class="field-label">卷别 / 批次</label>
              <input
                type="text"
                v-model="uploadForm.session_name"
                placeholder="如: A卷"
                class="field text-sm"
              />
            </div>
          </div>

          <div v-else-if="uploadForm.file_type === 'exam_and_answer'" class="grid grid-cols-2 gap-2">
            <div>
              <label class="field-label">起始年份 (选填)</label>
              <input
                type="number"
                v-model.number="uploadForm.year"
                placeholder="如: 1998"
                class="field text-sm"
              />
            </div>
            <div>
              <label class="field-label">科目名称</label>
              <input
                type="text"
                v-model="uploadForm.session_name"
                placeholder="如: 921管理学"
                class="field text-sm"
              />
            </div>
          </div>

          <div v-else class="flex items-end">
            <button
              type="submit"
              :disabled="isUploading"
              class="btn btn-primary w-full"
            >
              <Loader2 v-if="isUploading" class="w-4 h-4 mr-1.5 animate-spin" />
              <span>{{ isUploading ? '正在上传并启动解析...' : '开始导入资料' }}</span>
            </button>
          </div>
        </div>

        <div v-if="['exam', 'answer', 'exam_and_answer'].includes(uploadForm.file_type)" class="flex justify-end">
          <button
            type="submit"
            :disabled="isUploading"
            class="btn btn-primary"
          >
            <Loader2 v-if="isUploading" class="w-4 h-4 mr-1.5 animate-spin" />
            <span>{{ isUploading ? '正在上传并启动解析...' : '开始导入资料' }}</span>
          </button>
        </div>
      </form>
    </div>

    <!-- Main Tabs -->
    <div class="space-y-4">
      <!-- Tab Header Bar -->
      <div class="page-toolbar">
        <div class="flex items-center gap-2">
          <button
            type="button"
            class="btn"
            :class="activeSubTab === 'documents' ? 'btn-primary' : 'btn-secondary'"
            @click="activeSubTab = 'documents'"
          >
            资料管理 ({{ documents.length }})
          </button>
          <button
            type="button"
            class="btn"
            :class="activeSubTab === 'questions' ? 'btn-primary' : 'btn-secondary'"
            @click="activeSubTab = 'questions'"
          >
            待核对真题 ({{ unconfirmedCount }})
          </button>
          <button
            type="button"
            class="btn"
            :class="activeSubTab === 'answers' ? 'btn-primary' : 'btn-secondary'"
            @click="activeSubTab = 'answers'"
          >
            待配对答案 ({{ answerCandidates.length }})
          </button>
        </div>

        <!-- Right Side Action -->
        <div class="flex items-center gap-2">
          <button
            v-if="activeSubTab === 'questions' && unconfirmedCount > 0"
            type="button"
            class="btn btn-sm btn-primary bg-emerald-600 hover:bg-emerald-700"
            @click="handleBatchConfirm"
          >
            <CheckCheck class="w-4 h-4 mr-1" />
            一键确认全部待核题目
          </button>
          <button
            type="button"
            class="btn btn-sm btn-secondary"
            @click="reloadPage"
            title="刷新数据"
          >
            刷新
          </button>
        </div>
      </div>

      <!-- Tab 1: Documents List -->
      <div v-if="activeSubTab === 'documents'" class="space-y-3">
        <div v-if="documents.length === 0">
          <EmptyState title="资料库中暂无文件" description="请在上方上传参考教材、考研真题或笔记讲义。" />
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="doc in documents"
            :key="doc.id"
            class="surface p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-4 hover:border-stone-300 transition-colors"
          >
            <div class="flex items-start gap-3.5 min-w-0">
              <div class="w-10 h-10 rounded-xl bg-brand-50 text-brand-600 flex items-center justify-center shrink-0 mt-0.5">
                <FileText class="w-5 h-5" />
              </div>
              <div class="min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="text-sm sm:text-base font-semibold text-stone-800 truncate">
                    {{ doc.filename }}
                  </span>
                  <span class="px-2 py-0.5 rounded text-xs font-medium" :class="fileTypeBadge(doc.file_type)">
                    {{ formatFileType(doc.file_type) }}
                  </span>
                  <span v-if="doc.year" class="text-xs text-stone-500">
                    ({{ doc.year }}年 {{ doc.session_name || '' }})
                  </span>
                </div>
                <div class="text-xs text-stone-400 mt-1 flex items-center gap-4 flex-wrap">
                  <span>状态: <strong :class="doc.status === 'completed' ? 'text-emerald-600' : 'text-amber-600'">{{ doc.status === 'completed' ? '解析完成' : doc.status }}</strong></span>
                  <span>总页数/片段: {{ doc.page_count }}</span>
                  <span>上传时间: {{ formatDate(doc.created_at) }}</span>
                </div>
              </div>
            </div>

            <div class="flex items-center gap-2 self-end sm:self-center shrink-0">
              <button
                type="button"
                class="btn btn-sm btn-secondary"
                @click="viewDocChunks(doc)"
              >
                查看切片与原文
              </button>
              <button
                type="button"
                class="p-2 text-stone-400 hover:text-rose-600 hover:bg-rose-50 rounded-lg transition-colors"
                title="删除资料"
                @click="deleteDoc(doc.id)"
              >
                <Trash2 class="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- Tab 2: Questions Confirmation Workbench -->
      <div v-if="activeSubTab === 'questions'" class="space-y-4">
        <!-- Filter bar -->
        <div class="surface p-3 flex items-center justify-between text-xs sm:text-sm">
          <div class="flex items-center gap-4">
            <span class="text-stone-500 font-medium">题目状态:</span>
            <label class="flex items-center gap-1.5 cursor-pointer">
              <input type="radio" v-model="confirmedFilter" :value="null" @change="fetchQuestions" />
              <span>全部</span>
            </label>
            <label class="flex items-center gap-1.5 cursor-pointer text-amber-700 font-medium">
              <input type="radio" v-model="confirmedFilter" :value="0" @change="fetchQuestions" />
              <span>待核对 ({{ unconfirmedCount }})</span>
            </label>
            <label class="flex items-center gap-1.5 cursor-pointer text-emerald-700 font-medium">
              <input type="radio" v-model="confirmedFilter" :value="1" @change="fetchQuestions" />
              <span>已正式入库</span>
            </label>
          </div>
          <span class="text-xs text-stone-400">当前列表 {{ questions.length }} 道</span>
        </div>

        <div v-if="questions.length === 0">
          <EmptyState title="暂无符合条件的试题" description="上传历年真题后，系统将识别题目并在此处供人工核对入库。" />
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="q in questions"
            :key="q.id"
            class="surface p-4 sm:p-5 transition-all space-y-3"
            :class="q.is_confirmed ? 'border-stone-200' : 'border-amber-300 ring-1 ring-amber-200 bg-amber-50/10'"
          >
            <!-- Card Header -->
            <div class="flex items-center justify-between pb-2.5 border-b border-stone-100 text-xs sm:text-sm flex-wrap gap-2">
              <div class="flex items-center gap-2 flex-wrap">
                <span
                  class="px-2 py-0.5 rounded text-xs font-semibold"
                  :class="q.is_confirmed ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'"
                >
                  {{ q.is_confirmed ? '已确认入库' : '待人工核对' }}
                </span>
                <span class="font-semibold text-stone-800">{{ q.year ? `${q.year} 年` : '年份待补充' }}</span>
                <span v-if="q.session_name" class="text-stone-500">({{ q.session_name }})</span>
                <span class="text-stone-300">·</span>
                <span class="font-medium text-brand-700">{{ q.question_num || '试题' }}</span>
                <span v-if="q.score" class="text-stone-500">[{{ q.score }} 分]</span>
              </div>

              <div class="flex items-center gap-2">
                <button
                  type="button"
                  class="btn btn-sm btn-secondary"
                  title="编辑题目"
                  @click="openQuestionEditor(q)"
                >
                  编辑
                </button>
                <button
                  v-if="!q.is_confirmed"
                  type="button"
                  class="btn btn-sm btn-primary bg-emerald-600 hover:bg-emerald-700"
                  @click="confirmSingleQuestion(q.id)"
                >
                  <Check class="w-3.5 h-3.5 mr-1" />
                  确认入库
                </button>
                <button
                  v-if="q.is_confirmed"
                  type="button"
                  class="btn btn-sm btn-secondary text-amber-700"
                  @click="retractQuestion(q.id)"
                >
                  撤回核对
                </button>
                <button
                  type="button"
                  class="p-1.5 text-stone-400 hover:text-rose-600 rounded"
                  title="删除该题"
                  @click="deleteQuestion(q.id)"
                >
                  <Trash2 class="w-4 h-4" />
                </button>
              </div>
            </div>

            <!-- Stem -->
            <p class="reading-content font-medium text-stone-900">
              {{ q.stem }}
            </p>

            <!-- Standard Answer -->
            <div class="bg-stone-50 rounded-xl p-3.5 border border-stone-100 space-y-1.5 text-xs sm:text-sm">
              <div class="flex items-center justify-between text-stone-500 font-medium">
                <span class="flex items-center gap-1">
                  <CheckCircle2 class="w-4 h-4 text-emerald-600" />
                  参考答案：
                </span>
                <span v-if="!q.standard_answer" class="text-amber-600 font-normal text-xs">
                  [暂未配对答案，如实保留不虚构]
                </span>
              </div>
              <div v-if="q.standard_answer" class="text-stone-700 whitespace-pre-wrap leading-relaxed">
                {{ q.standard_answer }}
              </div>
            </div>

            <!-- Footer Meta & Links -->
            <div class="flex items-center justify-between text-xs text-stone-500 pt-2 border-t border-stone-100 flex-wrap gap-2">
              <div class="flex items-center gap-1.5 flex-wrap">
                <Tag class="w-3.5 h-3.5 text-stone-400" />
                <span>关联考点:</span>
                <span v-if="q.knowledge_point_names && q.knowledge_point_names.length > 0" class="flex gap-1 flex-wrap">
                  <span
                    v-for="(name, idx) in q.knowledge_point_names"
                    :key="idx"
                    class="px-2 py-0.5 rounded bg-brand-50 text-brand-700 font-medium"
                  >
                    {{ name }}
                  </span>
                </span>
                <span v-else class="text-stone-400 italic">暂无关联</span>
                <button type="button" @click="openLinkModal(q)" class="text-brand-600 hover:underline ml-1">
                  修改关联
                </button>
              </div>

              <div class="flex items-center gap-3">
                <button
                  v-if="q.source_location"
                  type="button"
                  @click="openQuestionSource(q, 'question')"
                  class="text-brand-600 hover:underline"
                >
                  题干来源: {{ q.source_location }}
                </button>
                <button
                  v-if="q.answer_source_chunk_id"
                  type="button"
                  @click="openQuestionSource(q, 'answer')"
                  class="text-emerald-600 hover:underline"
                >
                  查看答案原文出处
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Tab 3: Answer Candidates -->
      <div v-if="activeSubTab === 'answers'" class="space-y-3">
        <div v-if="answerCandidates.length === 0">
          <EmptyState title="暂无待配对答案" description="当上传独立答案册或答案合辑时，未自动配对的答案记录将在此处显示。" />
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="candidate in answerCandidates"
            :key="candidate.id"
            class="surface p-4 space-y-2.5"
          >
            <div class="flex items-center justify-between text-xs font-semibold text-stone-700">
              <span>{{ candidate.year || '年份待补充' }} · {{ candidate.question_num || '题号待补充' }}</span>
              <span class="text-stone-400">候选 ID: {{ candidate.id.slice(0, 8) }}</span>
            </div>
            <p class="text-sm text-stone-700 whitespace-pre-wrap line-clamp-4 bg-stone-50 p-3 rounded-lg border border-stone-100 leading-relaxed">
              {{ candidate.answer_text }}
            </p>
            <div class="flex items-center gap-2 pt-1">
              <select v-model="candidateSelections[candidate.id]" class="field flex-1 text-xs sm:text-sm">
                <option value="">-- 选择要配对的对应题目 --</option>
                <option v-for="question in matchingQuestions(candidate)" :key="question.id" :value="question.id">
                  {{ question.year || '待补年份' }} · {{ question.question_num || '试题' }} · {{ question.stem.slice(0, 35) }}...
                </option>
              </select>
              <button
                type="button"
                @click="attachCandidate(candidate.id)"
                :disabled="!candidateSelections[candidate.id]"
                class="btn btn-primary text-xs shrink-0"
              >
                确认配对
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Document Chunks Modal -->
    <BaseModal
      :show="selectedDocForChunks !== null"
      title="文档原文切片"
      max-width="4xl"
      @close="selectedDocForChunks = null"
    >
      <div class="space-y-3">
        <div v-if="docChunks.length === 0" class="text-center py-8 text-stone-400 text-sm">
          该文档尚未生成切片或正在解析中。
        </div>
        <div v-for="c in docChunks" :key="c.id" class="p-3.5 rounded-xl border border-stone-200 bg-stone-50/50 space-y-2 text-sm">
          <div class="flex items-center justify-between text-xs text-stone-500 font-medium">
            <span class="px-2 py-0.5 rounded bg-brand-50 text-brand-700 font-semibold">
              {{ c.page_number ? `第 ${c.page_number} 页` : (c.paragraph_ref || '段落') }}
            </span>
            <span class="text-stone-400">{{ c.heading || '正文' }}</span>
            <button type="button" @click="openProofreadModal(c)" class="text-brand-600 hover:underline">
              校对修改原文
            </button>
          </div>
          <p v-if="c.parse_error" class="text-rose-600 leading-relaxed font-mono text-xs">
            解析错误: {{ c.parse_error }}
          </p>
          <p v-else class="text-stone-700 whitespace-pre-wrap leading-relaxed">
            {{ c.content }}
          </p>
        </div>
      </div>
    </BaseModal>

    <!-- Question Edit Modal -->
    <BaseModal
      :show="questionDraft !== null"
      title="编辑题目与答案"
      max-width="2xl"
      @close="questionDraft = null"
    >
      <div v-if="questionDraft" class="space-y-4 text-sm">
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="field-label">年份</label>
            <input v-model.number="questionDraft.year" type="number" class="field" placeholder="如: 2022" />
          </div>
          <div>
            <label class="field-label">卷别 / 批次</label>
            <input v-model="questionDraft.session_name" class="field" placeholder="如: A卷" />
          </div>
          <div>
            <label class="field-label">题号</label>
            <input v-model="questionDraft.question_num" class="field" placeholder="如: 简答题 1" />
          </div>
          <div>
            <label class="field-label">分值</label>
            <input v-model.number="questionDraft.score" type="number" class="field" placeholder="如: 10" />
          </div>
        </div>
        <div>
          <label class="field-label">题干内容</label>
          <textarea v-model="questionDraft.stem" rows="4" class="field" />
        </div>
        <div>
          <label class="field-label">参考答案 (若无答案可留空)</label>
          <textarea v-model="questionDraft.standard_answer" rows="5" class="field" />
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary" @click="questionDraft = null">取消</button>
        <button type="button" class="btn btn-primary" :disabled="isSavingQuestion" @click="saveQuestionDraft">
          {{ isSavingQuestion ? '保存中...' : '保存修改' }}
        </button>
      </template>
    </BaseModal>

    <!-- Link Knowledge Point Modal -->
    <BaseModal
      :show="editingQuestion !== null"
      title="关联试题到考点"
      max-width="md"
      @close="editingQuestion = null"
    >
      <div v-if="editingQuestion" class="space-y-3">
        <p class="text-xs text-stone-500 truncate">{{ editingQuestion.stem }}</p>
        <div class="max-h-60 overflow-y-auto space-y-1.5 border border-stone-200 p-2.5 rounded-xl text-sm">
          <label
            v-for="kp in allKnowledgePoints"
            :key="kp.id"
            class="flex items-center gap-2.5 p-2 hover:bg-stone-50 rounded-lg cursor-pointer transition-colors"
          >
            <input
              type="checkbox"
              :value="kp.id"
              v-model="selectedKpIds"
              class="rounded text-brand-600 focus:ring-brand-500 w-4 h-4"
            />
            <div class="min-w-0 flex-1">
              <span class="font-medium text-stone-800">{{ kp.name }}</span>
              <span v-if="kp.chapter" class="text-xs text-stone-400 ml-1.5">({{ kp.chapter }})</span>
            </div>
          </label>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary" @click="editingQuestion = null">取消</button>
        <button type="button" class="btn btn-primary" @click="saveQuestionLinks">保存关联</button>
      </template>
    </BaseModal>

    <!-- Source Quote Modal -->
    <SourceQuoteModal
      :is-open="isProofreadOpen"
      :chunk-id="proofreadChunk?.id || ''"
      :doc-name="selectedDocForChunks?.filename || '原文档'"
      :location-ref="proofreadChunk?.page_number ? `第 ${proofreadChunk.page_number} 页` : (proofreadChunk?.paragraph_ref || '段落')"
      :heading="proofreadChunk?.heading || ''"
      :content="proofreadChunk?.content || ''"
      :file-url="selectedDocForChunks ? apiClient.documentFileUrl(selectedDocForChunks.id) : ''"
      :read-only="sourceReadOnly"
      @close="isProofreadOpen = false"
      @updated="handleChunkUpdated"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import {
  UploadCloud, CheckCheck, Check, Trash2,
  CheckCircle2, Tag, FileText, Loader2
} from 'lucide-vue-next';
import { apiClient } from '../api/client';
import type { AnswerCandidate, DocumentItem, DocumentChunk, QuestionItem, KnowledgePoint } from '../types';
import SourceQuoteModal from '../components/SourceQuoteModal.vue';
import BaseModal from '../components/BaseModal.vue';
import EmptyState from '../components/EmptyState.vue';
import { useTaskManager } from '../composables/useTaskManager';

const { activeCount, openDrawer } = useTaskManager();

const activeSubTab = ref<'documents' | 'questions' | 'answers'>('documents');
const confirmedFilter = ref<number | null>(0);
const showUploadForm = ref(true);

const documents = ref<DocumentItem[]>([]);
const questions = ref<QuestionItem[]>([]);
const answerCandidates = ref<AnswerCandidate[]>([]);
const candidateSelections = ref<Record<string, string>>({});
const allKnowledgePoints = ref<KnowledgePoint[]>([]);

const isUploading = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const uploadForm = ref({
  file_type: 'textbook',
  year: undefined as number | undefined,
  session_name: '',
});

// Chunks Drawer & Proofreading
const selectedDocForChunks = ref<DocumentItem | null>(null);
const docChunks = ref<DocumentChunk[]>([]);
const isProofreadOpen = ref(false);
const proofreadChunk = ref<DocumentChunk | null>(null);
const sourceReadOnly = ref(false);

// Link Knowledge Points Modal
const editingQuestion = ref<QuestionItem | null>(null);
const selectedKpIds = ref<string[]>([]);
const questionDraft = ref<QuestionItem | null>(null);
const isSavingQuestion = ref(false);
const pageError = ref('');

const unconfirmedCount = computed(() => questions.value.filter(q => !q.is_confirmed).length);

const fetchDocuments = async () => {
  try {
    documents.value = await apiClient.listMaterials();
  } catch (e) {
    console.error(e);
    pageError.value = '资料列表加载失败，请点击刷新重试。';
  }
};

const fetchQuestions = async () => {
  try {
    const params: any = {};
    if (confirmedFilter.value !== null) {
      params.is_confirmed = confirmedFilter.value;
    }
    questions.value = await apiClient.listQuestions(params);
  } catch (e) {
    console.error(e);
    pageError.value = '题目列表加载失败，请点击刷新重试。';
  }
};

const fetchAnswerCandidates = async () => {
  try {
    answerCandidates.value = await apiClient.listAnswerCandidates();
  } catch (e) {
    console.error(e);
  }
};

const fetchKnowledgePoints = async () => {
  try {
    allKnowledgePoints.value = await apiClient.listKnowledgePoints();
  } catch (e) {
    console.error(e);
  }
};

const reloadPage = async () => {
  pageError.value = '';
  await Promise.all([fetchDocuments(), fetchQuestions(), fetchAnswerCandidates()]);
};

const handleUpload = async () => {
  if (!fileInput.value?.files?.[0]) return;
  const file = fileInput.value.files[0];
  const formData = new FormData();
  formData.append('file', file);
  formData.append('file_type', uploadForm.value.file_type);
  if (uploadForm.value.year) formData.append('year', uploadForm.value.year.toString());
  if (uploadForm.value.session_name) formData.append('session_name', uploadForm.value.session_name);

  isUploading.value = true;
  try {
    await apiClient.uploadMaterial(formData);
    if (fileInput.value) fileInput.value.value = '';
    await reloadPage();
    openDrawer();
  } catch (err: any) {
    alert('上传失败: ' + (err.response?.data?.detail || err.message));
  } finally {
    isUploading.value = false;
  }
};

const confirmSingleQuestion = async (id: string) => {
  try {
    await apiClient.confirmQuestion(id);
    await fetchQuestions();
  } catch (err: any) {
    alert('确认失败: ' + err.message);
  }
};

const matchingQuestions = (candidate: AnswerCandidate) => questions.value.filter((question) => {
  if (candidate.year !== null && question.year !== candidate.year) return false;
  if (candidate.session_name && question.session_name && candidate.session_name !== question.session_name) return false;
  if (candidate.question_num && question.question_num && candidate.question_num !== question.question_num) return false;
  return true;
});

const attachCandidate = async (candidateId: string) => {
  const questionId = candidateSelections.value[candidateId];
  if (!questionId) return;
  try {
    await apiClient.attachAnswerCandidate(questionId, candidateId);
    delete candidateSelections.value[candidateId];
    await Promise.all([fetchAnswerCandidates(), fetchQuestions()]);
  } catch (err: any) {
    alert('答案配对失败: ' + (err.response?.data?.detail || err.message));
  }
};

const openQuestionEditor = (question: QuestionItem) => {
  questionDraft.value = { ...question };
};

const saveQuestionDraft = async () => {
  if (!questionDraft.value) return;
  const draft = questionDraft.value;
  if (!draft.stem.trim()) {
    alert('题干不能为空');
    return;
  }
  if (draft.year !== null && draft.year !== undefined && !Number.isInteger(draft.year)) {
    alert('年份必须为空或有效整数');
    return;
  }
  isSavingQuestion.value = true;
  try {
    await apiClient.updateQuestion(draft.id, {
      year: draft.year ?? null,
      session_name: draft.session_name,
      question_num: draft.question_num,
      score: draft.score,
      stem: draft.stem,
      standard_answer: draft.standard_answer,
    });
    questionDraft.value = null;
    await fetchQuestions();
  } catch (err: any) {
    alert('保存题目失败: ' + (err.response?.data?.detail || err.message));
  } finally {
    isSavingQuestion.value = false;
  }
};

const retractQuestion = async (id: string) => {
  try {
    await apiClient.updateQuestion(id, { is_confirmed: 0 });
    await fetchQuestions();
  } catch (err: any) {
    alert('撤回核对失败: ' + (err.response?.data?.detail || err.message));
  }
};

const handleBatchConfirm = async () => {
  const ids = questions.value.filter(q => !q.is_confirmed).map(q => q.id);
  if (ids.length === 0) return;
  try {
    await apiClient.batchConfirmQuestions(ids);
    await fetchQuestions();
  } catch (err: any) {
    alert('批量确认失败: ' + err.message);
  }
};

const deleteQuestion = async (id: string) => {
  if (!confirm('确定删除该题目吗？')) return;
  try {
    await apiClient.deleteQuestion(id);
    await fetchQuestions();
  } catch (err: any) {
    alert('删除失败: ' + err.message);
  }
};

const deleteDoc = async (id: string) => {
  if (!confirm('确定删除该资料吗？关联切片将同步移除，已有考点将标记为待补充依据。')) return;
  try {
    await apiClient.deleteDocument(id);
    await fetchDocuments();
    await fetchQuestions();
  } catch (err: any) {
    alert('删除失败: ' + err.message);
  }
};

const viewDocChunks = async (doc: DocumentItem) => {
  selectedDocForChunks.value = doc;
  try {
    docChunks.value = await apiClient.getChunks(doc.id);
  } catch (e) {
    console.error(e);
  }
};

const openProofreadModal = (chunk: DocumentChunk) => {
  proofreadChunk.value = chunk;
  sourceReadOnly.value = false;
  isProofreadOpen.value = true;
};

const openQuestionSource = async (question: QuestionItem, kind: 'question' | 'answer' = 'question') => {
  const sourceDocId = kind === 'answer' ? question.answer_source_doc_id : question.doc_id;
  if (!sourceDocId) {
    alert('该题目没有关联原始资料');
    return;
  }
  const doc = documents.value.find(item => item.id === sourceDocId);
  if (!doc) {
    alert('原始资料已不在资料库中');
    return;
  }
  try {
    const chunks = await apiClient.getChunks(doc.id);
    const sourceChunkId = kind === 'answer' ? question.answer_source_chunk_id : question.source_chunk_id;
    const page = kind === 'answer' ? (question.answer_source_page ?? null) : (question.source_page ?? null);
    proofreadChunk.value = (sourceChunkId
      ? chunks.find(chunk => chunk.id === sourceChunkId)
      : null)
      || (chunks.find(chunk => page !== null && chunk.page_number === page) || chunks[0] || null);
    if (proofreadChunk.value) {
      selectedDocForChunks.value = doc;
      sourceReadOnly.value = true;
      isProofreadOpen.value = true;
    } else {
      alert('该资料尚未生成可查看的原文切片');
    }
  } catch (err: any) {
    alert('加载原文失败: ' + (err.response?.data?.detail || err.message));
  }
};

const handleChunkUpdated = (newContent: string) => {
  if (proofreadChunk.value) {
    proofreadChunk.value.content = newContent;
  }
};

const openLinkModal = (q: QuestionItem) => {
  editingQuestion.value = q;
  selectedKpIds.value = [...(q.knowledge_point_ids || [])];
};

const saveQuestionLinks = async () => {
  if (!editingQuestion.value) return;
  try {
    await apiClient.updateQuestion(editingQuestion.value.id, {
      knowledge_point_ids: selectedKpIds.value
    });
    editingQuestion.value = null;
    await fetchQuestions();
  } catch (err: any) {
    alert('保存关联失败: ' + err.message);
  }
};

const formatFileType = (t: string) => {
  const map: Record<string, string> = {
    textbook: '教材参考书',
    exam_and_answer: '真题答案合辑',
    exam: '历年真题',
    answer: '参考答案',
    supplement: '补充讲义'
  };
  return map[t] || t;
};

const fileTypeBadge = (t: string) => {
  const map: Record<string, string> = {
    textbook: 'bg-brand-50 text-brand-700',
    exam_and_answer: 'bg-brand-50 text-brand-700 border border-brand-200 font-semibold',
    exam: 'bg-purple-50 text-purple-700',
    answer: 'bg-emerald-50 text-emerald-700',
    supplement: 'bg-stone-100 text-stone-700'
  };
  return map[t] || 'bg-stone-100 text-stone-700';
};

const formatDate = (iso: string) => {
  if (!iso) return '';
  return iso.replace('T', ' ').substring(0, 16);
};

onMounted(() => {
  fetchDocuments();
  fetchQuestions();
  fetchAnswerCandidates();
  fetchKnowledgePoints();
});
</script>
