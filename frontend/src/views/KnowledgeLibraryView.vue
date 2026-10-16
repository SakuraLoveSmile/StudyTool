<template>
  <div class="page-stack library-page">
    <header class="surface p-5 sm:p-6">
      <div class="flex flex-wrap justify-between items-start gap-3">
        <div>
          <p class="text-xs text-accent font-semibold mb-2">读懂教材 · 联系真题 · 随时复习</p>
          <h1 class="font-display text-2xl font-bold text-ink">知识库学习</h1>
          <p class="text-sm text-sub mt-2">按章整理总结、思维导图和背诵段落，带着教材依据提问。</p>
        </div>
        <button class="btn btn-secondary" :disabled="materialsLoading" @click="loadMaterials">{{ materialsLoading ? '刷新中…' : '刷新资料' }}</button>
      </div>
      <p v-if="materialsError" class="notice text-err bg-err/5 mt-4" role="alert">{{ materialsError }}</p>
      <p v-if="materialsLoading && !documents.length" class="text-sm text-sub mt-5" role="status">正在读取资料…</p>
      <div v-else-if="!documents.length && !materialsError" class="empty-panel mt-5">
        <p class="font-medium text-ink">先把教材放进知识库</p>
        <p class="text-sm text-sub mt-2">导入教材并完成解析后，就可以选择章节学习。</p>
        <RouterLink class="btn btn-primary mt-4" to="/materials">去导入资料</RouterLink>
      </div>
      <div v-else class="grid sm:grid-cols-2 gap-4 mt-5">
        <label class="min-w-0">
          <span class="field-label">学习教材</span>
          <select v-model="selectedDocId" class="field" :disabled="materialsLoading">
            <option value="">请选择已解析教材</option>
            <option v-for="doc in textbooks" :key="doc.id" :value="doc.id">{{ doc.filename }}</option>
          </select>
          <span v-if="!textbooks.length" class="block text-xs text-warn mt-2">暂无已解析教材，请在资料管理中检查文件类型和解析状态。</span>
        </label>
        <label class="min-w-0">
          <span class="field-label">章节</span>
          <select v-model="selectedChapterId" class="field" :disabled="!selectedDocId || chaptersLoading">
            <option value="">{{ chaptersLoading ? '正在读取章节…' : '请选择章节' }}</option>
            <option v-for="chapter in chapters" :key="chapter.id" :value="chapter.id">{{ chapter.title }}</option>
          </select>
          <span v-if="selectedChapter" class="block text-xs text-faint mt-2">{{ selectedChapter.chunk_count }} 段原文 · {{ selectedChapter.character_count.toLocaleString() }} 字</span>
        </label>
      </div>
      <p v-if="chaptersError" class="notice text-err bg-err/5 mt-3" role="alert">{{ chaptersError }}</p>
      <p v-else-if="selectedDocId && !chaptersLoading && !chapters.length" class="text-sm text-sub mt-3">尚未找到可用章节，请检查教材解析结果。</p>
      <a v-if="selectedDocument" :href="apiClient.documentFileUrl(selectedDocument.id)" target="_blank" rel="noreferrer" class="inline-block text-xs text-accent underline mt-3">查看教材原文件</a>
      <p v-if="selectedDocument?.filename.toLowerCase().endsWith('.docx')" class="text-xs text-faint mt-3">DOCX 按可读取文字整理，图片和公式请对照原文件核对。</p>
      <details v-if="selectedDocId" class="mt-4 border-t border-line pt-4">
        <summary class="text-sm text-sub cursor-pointer font-medium">补充资料范围（可选）</summary>
        <div class="grid sm:grid-cols-2 gap-5 mt-4">
          <fieldset class="min-w-0">
            <legend class="field-label">用于交叉关联的管理学教材</legend>
            <p class="text-xs text-faint mb-2">仅勾选管理学教材。不提供时，关联内容只作为学习建议。</p>
            <label v-for="doc in managementOptions" :key="doc.id" class="check-row">
              <input v-model="managementDocIds" type="checkbox" :value="doc.id" :disabled="generating" class="accent-accent" />
              <span>{{ doc.filename }}</span>
            </label>
            <p v-if="!managementOptions.length" class="text-xs text-faint">暂无其他已解析教材，可稍后补充。</p>
          </fieldset>
          <fieldset class="min-w-0">
            <legend class="field-label">核对已考知识点的真题</legend>
            <label v-for="doc in examOptions" :key="doc.id" class="check-row">
              <input v-model="examDocIds" type="checkbox" :value="doc.id" :disabled="generating" class="accent-accent" />
              <span>{{ doc.filename }}</span>
            </label>
            <p v-if="!examOptions.length" class="text-xs text-faint">暂无已解析真题。未解析、未确认或模拟题不会作为已考依据。</p>
          </fieldset>
        </div>
      </details>
    </header>

    <div class="library-grid">
      <main class="surface min-w-0 overflow-hidden" aria-labelledby="chapter-heading">
        <div class="p-5 sm:p-6 border-b border-line">
          <div class="flex flex-wrap justify-between gap-3 items-start">
            <div class="min-w-0">
              <h2 id="chapter-heading" class="font-display text-lg font-bold text-ink">{{ selectedChapter?.title || '章节学习' }}</h2>
              <p class="text-xs text-sub mt-2">点击生成会调用服务端配置的 AI 模型，并保存结果。</p>
            </div>
            <button class="btn btn-primary shrink-0" :disabled="!selectedChapterId || generating || !!guideTaskSyncError || guideTasksLoading || chaptersLoading || guidesLoading" @click="generateGuide">
              {{ submittingGuide ? '正在提交…' : generating ? '正在生成…' : activeGuide ? '重新生成' : '生成章节资料' }}
            </button>
          </div>
          <div v-if="generationTask" class="text-xs text-sub mt-3 space-y-2" role="status">
            <p>{{ generationTask.message || '章节生成任务' }} · {{ generationTask.progress }}%</p>
            <p v-if="generating">任务已加入后台队列，离开或刷新页面后仍会继续。</p>
            <button class="text-accent underline underline-offset-2" @click="taskManager.openTaskDetail(generationTask.id)">查看任务进度与日志</button>
          </div>
          <p v-if="guideTaskSyncError" class="notice text-warn bg-warn/10 mt-3" role="alert">
            {{ guideTaskSyncError }}
            <button class="underline underline-offset-2 ml-2" @click="loadGuideTasks">重新获取任务状态</button>
          </p>
          <p v-if="guideError" class="notice text-err bg-err/5 mt-4" role="alert">{{ guideError }}</p>
          <label v-if="guides.length" class="block mt-4">
            <span class="field-label">已保存的学习资料</span>
            <select v-model="selectedGuideId" class="field text-sm">
              <option v-for="guide in guides" :key="guide.id" :value="guide.id">{{ formatTime(guide.created_at) }}{{ guide.source_stale ? ' · 来源已变化' : '' }}</option>
            </select>
          </label>
        </div>

        <div v-if="guidesLoading" class="empty-panel" role="status">正在读取已保存的章节资料…</div>
        <div v-else-if="!activeGuide" class="empty-panel">
          <BookOpen class="h-9 w-9 mx-auto text-faint mb-3" aria-hidden="true" />
          <p class="font-medium text-ink">{{ selectedChapterId ? '这一章还没有学习资料' : '从一本教材、一章内容开始' }}</p>
          <p class="text-sm text-sub mt-2">{{ selectedChapterId ? '生成后可查看总结、导图、背诵短段和管理学关联。原文仍是核对依据。' : '选择教材和章节后，可以生成并保存学习资料，也可以直接向右侧知识库提问。' }}</p>
        </div>
        <template v-else>
          <div v-if="activeGuide.source_stale" class="notice mx-5 mt-5 text-warn bg-warn/10" role="alert">来源资料已修改或删除。这份历史结果仅供回看，引用已停用，请重新生成后使用。</div>
          <div v-else-if="!guideSourcesVerified" class="notice mx-5 mt-5 text-warn bg-warn/10" role="alert">当前章节资料的来源状态尚未核实，引用已暂停。请刷新资料后再使用。</div>
          <ul v-if="activeGuide.warnings.length" class="notice mx-5 mt-5 text-warn bg-warn/5 space-y-1">
            <li v-for="(warning, index) in activeGuide.warnings" :key="index">{{ warning }}</li>
          </ul>
          <div class="tabs-scroll border-b border-line px-5 mt-2" role="tablist" aria-label="章节内容">
            <button v-for="tab in tabs" :id="`library-tab-${tab.id}`" :key="tab.id" type="button" role="tab" :aria-selected="activeTab === tab.id" :aria-controls="`library-panel-${activeTab}`" class="tab-button" :class="activeTab === tab.id ? 'text-accent border-accent' : 'text-sub border-transparent'" @click="activeTab = tab.id">{{ tab.label }}</button>
          </div>
          <section :id="`library-panel-${activeTab}`" role="tabpanel" :aria-labelledby="`library-tab-${activeTab}`" class="p-5 sm:p-6 space-y-5">
            <template v-if="activeTab === 'summary'">
              <div class="reading-content whitespace-pre-wrap">{{ activeGuide.summary }}</div>
              <h3 class="font-semibold text-ink border-t border-line pt-5">核心概念</h3>
              <article v-for="(concept, index) in activeGuide.concepts" :key="index" class="content-card">
                <h4 class="font-semibold text-ink">{{ concept.name }} <span v-if="examConcepts.includes(concept.name)" class="chip bg-warn/10 text-warn">真题已考</span></h4>
                <p class="reading-content mt-2 whitespace-pre-wrap">{{ concept.explanation }}</p>
                <div class="flex flex-wrap gap-2 mt-3">
                  <button v-for="(citation, citationIndex) in concept.citations" :key="citationIndex" class="citation-button" :disabled="!guideSourcesVerified || activeGuide.source_stale" @click="openCitation(citation)">原文 {{ citationIndex + 1 }} · {{ citation.location }}</button>
                </div>
              </article>
              <p v-if="!activeGuide.concepts.length" class="text-sm text-faint">本次结果未整理出独立概念，请结合总结核对原文。</p>
            </template>
            <template v-else-if="activeTab === 'map'">
              <KnowledgeMindMap :key="activeGuide.id" :map="activeGuide.mind_map" :exam-concepts="examConcepts" :management-concepts="managementConcepts" :selected-title="selectedMapNode" :source-dialog-open="!!sourceCitation" @select="selectMapNode">
                <template #detail>
                  <div v-if="selectedMapNode" class="space-y-4">
                    <div class="flex items-start justify-between gap-3">
                      <h3 class="font-semibold text-ink break-words min-w-0">{{ selectedMapNode }}</h3>
                      <button type="button" class="text-xs text-sub shrink-0 hover:text-accent" @click="selectedMapNode = ''">收起详情</button>
                    </div>
                    <p class="reading-content whitespace-pre-wrap">{{ selectedMapConcept?.explanation || '此节点为章节结构主题。可展开分支查看具体概念，或在章节总结中阅读完整说明。' }}</p>
                    <template v-if="selectedMapConcept">
                      <p v-if="selectedMapConcept.keywords.length" class="text-xs text-accent">{{ selectedMapConcept.keywords.join(' · ') }}</p>
                      <details v-if="selectedMapConcept.recitation" class="border-t border-line pt-3">
                        <summary class="text-sm font-medium text-ink cursor-pointer">背诵短段</summary>
                        <p class="reading-content whitespace-pre-wrap mt-2">{{ selectedMapConcept.recitation }}</p>
                      </details>
                      <div class="flex flex-wrap gap-2">
                        <button v-for="(citation, index) in selectedMapConcept.citations" :key="index" type="button" class="citation-button" :disabled="!guideSourcesVerified || activeGuide.source_stale" @click="openCitation(citation)">查看原文 {{ index + 1 }}</button>
                      </div>
                    </template>
                    <section v-if="selectedMapExamPoints.length" class="border-t border-line pt-3 space-y-2">
                      <h4 class="text-sm font-medium text-warn">真题已考</h4>
                      <article v-for="(point, index) in selectedMapExamPoints" :key="`${point.question_id}-${index}`" class="text-sm text-sub">
                        <p class="text-xs text-faint">{{ point.year == null ? '年份未标注' : `${point.year} 年` }} · {{ point.doc_name }} · {{ point.location || '定位未标注' }}</p>
                        <p class="whitespace-pre-wrap mt-1">{{ point.stem }}</p>
                      </article>
                    </section>
                    <section v-if="selectedMapConnections.length" class="border-t border-line pt-3 space-y-3">
                      <h4 class="text-sm font-medium text-accent">管理学关联</h4>
                      <article v-for="(connection, index) in selectedMapConnections" :key="index" class="text-sm text-sub space-y-2">
                        <p class="font-medium text-ink">{{ connection.management_concept }}</p>
                        <p class="text-xs text-faint">{{ connection.evidence_level === 'suggestion' ? '关联建议 · 不是已考证据' : '有教材依据 · 不代表已考' }}</p>
                        <p class="whitespace-pre-wrap">{{ connection.explanation }}</p>
                        <p class="whitespace-pre-wrap"><span class="font-medium">答题运用：</span>{{ connection.application }}</p>
                        <div class="flex flex-wrap gap-2">
                          <button v-for="(citation, citationIndex) in connection.citations" :key="citationIndex" type="button" class="citation-button" :disabled="!guideSourcesVerified || activeGuide.source_stale" @click="openCitation(citation)">关联原文 {{ citationIndex + 1 }}</button>
                        </div>
                      </article>
                    </section>
                    <p v-if="!guideSourcesVerified || activeGuide.source_stale" class="text-xs text-warn" role="status">来源尚未核实或已变化，原文入口已停用。请刷新或重新生成资料。</p>
                  </div>
                </template>
              </KnowledgeMindMap>
            </template>
            <template v-else-if="activeTab === 'recitation'">
              <p class="text-sm text-sub">先用关键词回想，再展开核对。背诵短段由 AI 整理，请以教材为准。</p>
              <details v-for="(concept, index) in activeGuide.concepts" :key="index" class="content-card">
                <summary class="cursor-pointer font-semibold text-ink">{{ concept.name }}<span class="block text-xs font-normal text-accent mt-2">{{ concept.keywords.join(' · ') || '点击展开背诵段落' }}</span></summary>
                <p class="reading-content whitespace-pre-wrap mt-4">{{ concept.recitation }}</p>
                <div class="flex flex-wrap gap-2 mt-3">
                  <button v-for="(citation, citationIndex) in concept.citations" :key="citationIndex" class="citation-button" :disabled="!guideSourcesVerified || activeGuide.source_stale" @click="openCitation(citation)">核对原文 {{ citationIndex + 1 }}</button>
                </div>
              </details>
              <p v-if="!activeGuide.concepts.length" class="text-sm text-faint">本次结果暂无背诵段落。</p>
            </template>
            <template v-else>
              <div>
                <h3 class="font-semibold text-ink">真题已考知识点</h3>
                <p class="text-xs text-sub mt-2">仅展示服务端已确认、非模拟真题的实际关联。没有关联不等于没有考过。</p>
              </div>
              <p v-if="!activeGuide.exam_points.length" class="empty-panel text-sm">暂无已确认真题关联</p>
              <article v-for="(point, index) in activeGuide.exam_points" :key="`${point.question_id}-${index}`" class="content-card">
                <p class="text-xs text-accent mb-2">{{ point.year == null ? '年份未标注' : `${point.year} 年` }} · {{ point.concept }}</p>
                <p class="text-sm text-ink whitespace-pre-wrap">{{ point.stem }}</p>
                <p class="text-xs text-faint mt-3">{{ point.doc_name }} · {{ point.location || '定位未标注' }}</p>
              </article>
              <h3 class="font-semibold text-ink border-t border-line pt-5">与管理学的联系</h3>
              <p v-if="!activeGuide.connections.length" class="text-sm text-faint">本次结果暂无管理学关联。</p>
              <article v-for="(connection, index) in activeGuide.connections" :key="index" class="content-card">
                <span class="chip mb-3" :class="connection.evidence_level === 'suggestion' ? 'bg-warn/10 text-warn' : 'bg-ok/10 text-ok'">{{ connection.evidence_level === 'suggestion' ? '关联建议 · 不是已考证据' : '有教材依据 · 不代表已考' }}</span>
                <h4 class="font-semibold text-ink">{{ connection.accounting_concept }} → {{ connection.management_concept }}</h4>
                <p class="reading-content whitespace-pre-wrap mt-3">{{ connection.explanation }}</p>
                <p class="text-sm text-sub whitespace-pre-wrap mt-3"><span class="font-semibold">如何用于答题：</span>{{ connection.application }}</p>
                <div class="flex flex-wrap gap-2 mt-3">
                  <button v-for="(citation, citationIndex) in connection.citations" :key="citationIndex" class="citation-button" :disabled="!guideSourcesVerified || activeGuide.source_stale" @click="openCitation(citation)">教材原文 {{ citationIndex + 1 }} · {{ citation.doc_name }}</button>
                </div>
              </article>
            </template>
            <details v-if="activeGuide.citations.length" class="border-t border-line pt-4">
              <summary class="text-sm text-sub cursor-pointer">本次章节资料的原文依据（{{ activeGuide.citations.length }}）</summary>
              <div class="flex flex-wrap gap-2 mt-3">
                <button v-for="(citation, index) in activeGuide.citations" :key="index" class="citation-button" :disabled="!guideSourcesVerified || activeGuide.source_stale" @click="openCitation(citation)">{{ index + 1 }}. {{ citation.doc_name }} · {{ citation.location }}</button>
              </div>
            </details>
          </section>
        </template>
      </main>

      <aside class="space-y-5 min-w-0" aria-labelledby="ask-heading">
        <section class="surface p-5">
          <h2 id="ask-heading" class="font-display text-lg font-bold text-ink">向知识库提问</h2>
          <p class="text-xs text-sub mt-2">回答限定在所选资料内，支持基于当前回答追问。</p>
          <form class="mt-4 space-y-4" @submit.prevent="askQuestion">
            <details>
              <summary class="text-sm text-sub cursor-pointer">提问资料范围 · 已选 {{ qaDocIds.length }} 份</summary>
              <div class="mt-3 max-h-56 overflow-y-auto">
                <label v-for="doc in readyDocuments" :key="doc.id" class="check-row">
                  <input v-model="qaDocIds" type="checkbox" :value="doc.id" class="accent-accent" />
                  <span>{{ doc.filename }}</span>
                </label>
                <p v-if="!readyDocuments.length" class="text-xs text-faint">请先导入并解析资料。</p>
              </div>
            </details>
            <label>
              <span class="field-label">章节范围</span>
              <select v-model="qaChapterId" class="field text-sm" :disabled="qaDocIds.length !== 1 || !qaDocIds.includes(selectedDocId) || !chapters.length">
                <option value="">所选资料的全部章节</option>
                <option v-for="chapter in chapters" :key="chapter.id" :value="chapter.id">{{ chapter.title }}</option>
              </select>
            </label>
            <p v-if="qaChapterId" class="text-xs text-faint">限定章节时，仅检索该章节所属教材。</p>
            <p v-else-if="qaDocIds.length > 1" class="text-xs text-faint">已选多份资料，将在这些资料的全部章节中查找依据。</p>
            <div v-if="followUpAvailable && activeAnswer" class="notice bg-accent/5 text-sub flex items-start gap-2">
              <span class="flex-1">本次将追问：{{ activeAnswer.question }}</span>
              <button type="button" class="text-accent text-xs shrink-0 underline" @click="startNewQuestion">新问题</button>
            </div>
            <label class="block">
              <span class="field-label">{{ followUpAvailable ? '继续追问' : '你的问题' }}</span>
              <textarea v-model="question" class="field text-sm" rows="4" maxlength="4000" placeholder="例如：这一章的核心概念之间有什么关系？请结合原文解释。" :disabled="asking" />
            </label>
            <p class="text-xs text-faint">发送会调用服务端 AI 模型。资料不足时，应保留“不足”的结论。</p>
            <button class="btn btn-primary w-full" :disabled="asking || !question.trim() || !effectiveQaDocIds.length">{{ asking ? '正在查找依据并回答…' : '发送问题' }}</button>
          </form>
          <p v-if="askError" class="notice text-err bg-err/5 mt-4" role="alert">{{ askError }}</p>
          <article v-if="activeAnswer" class="mt-5 pt-5 border-t border-line space-y-4" aria-live="polite">
            <p class="text-xs text-faint">{{ formatTime(activeAnswer.created_at) }} · 已保存到问答历史</p>
            <h3 class="font-semibold text-ink">{{ activeAnswer.question }}</h3>
            <p v-if="activeAnswer.source_stale" class="notice text-warn bg-warn/10" role="alert">来源已修改或删除，此历史回答的引用已停用。请重新提问核对。</p>
            <p v-else-if="!answerSourcesVerified" class="notice text-warn bg-warn/10" role="alert">{{ answersLoading ? '正在核对当前回答的来源，引用和追问暂不可用。' : '当前回答的来源状态尚未核实，引用和追问已暂停。请刷新问答历史后再使用。' }}</p>
            <ul v-if="activeAnswer.warnings.length" class="notice bg-warn/5 text-warn space-y-1"><li v-for="(warning, index) in activeAnswer.warnings" :key="index">{{ warning }}</li></ul>
            <div class="reading-content whitespace-pre-wrap">{{ activeAnswer.answer }}</div>
            <div class="flex flex-wrap gap-2">
              <button v-for="(citation, index) in activeAnswer.citations" :key="index" class="citation-button" :disabled="!answerSourcesVerified || activeAnswer.source_stale" @click="openCitation(citation, 'answer')">依据 {{ index + 1 }} · {{ citation.doc_name }} · {{ citation.location }}</button>
            </div>
            <p v-if="!activeAnswer.citations.length" class="text-xs text-warn">本次回答没有可核对的原文引用，请勿作为教材结论使用。</p>
          </article>
          <p v-else class="text-sm text-faint mt-5 border-t border-line pt-4">还没有回答。选好资料，提出你的第一个问题。</p>
        </section>

        <section class="surface p-5">
          <div class="flex justify-between items-center gap-2">
            <h2 class="font-semibold text-ink">问答历史</h2>
            <button class="btn-sm btn-secondary" :disabled="answersLoading" @click="loadAnswers">刷新</button>
          </div>
          <p class="text-xs text-faint mt-2">{{ selectedDocId ? '显示包含当前教材的已保存问答' : '显示当前账号的已保存问答' }}</p>
          <p v-if="answersError" class="text-sm text-err mt-3" role="alert">{{ answersError }}</p>
          <p v-if="answersLoading" class="text-sm text-faint mt-4" role="status">正在读取历史…</p>
          <p v-else-if="!answers.length && !answersError" class="text-sm text-faint mt-4">暂无问答记录。</p>
          <ul v-else class="mt-3 space-y-2 max-h-96 overflow-y-auto">
            <li v-for="answer in answers" :key="answer.id">
              <button class="history-button" :disabled="asking || answersLoading" @click="openAnswer(answer)">
                <span class="block text-sm text-ink">{{ answer.question }}</span>
                <span class="block text-xs text-faint mt-1">{{ formatTime(answer.created_at) }}{{ answer.source_stale ? ' · 来源已变化' : '' }}</span>
              </button>
            </li>
          </ul>
        </section>
      </aside>
    </div>
    <SourceQuoteModal v-if="sourceCitation" :is-open="true" :chunk-id="sourceCitation.chunk_id" :doc-name="sourceCitation.doc_name" :location-ref="sourceCitation.location" :content="sourceCitation.quote" :file-url="apiClient.documentFileUrl(sourceCitation.doc_id)" read-only @close="sourceCitation = null" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { BookOpen } from 'lucide-vue-next';
import { apiClient, sessionTransport } from '../api/client';
import { libraryApi } from '../api/library';
import SourceQuoteModal from '../components/SourceQuoteModal.vue';
import KnowledgeMindMap from '../components/KnowledgeMindMap.vue';
import { useTaskManager } from '../composables/useTaskManager';
import type { DocumentItem, TaskStatus } from '../types';
import type { LibraryAnswer, LibraryChapter, LibraryCitation, LibraryGuide } from '../types/library';

const documents = ref<DocumentItem[]>([]);
const materialsLoading = ref(false);
const materialsError = ref('');
const selectedDocId = ref('');
const selectedChapterId = ref('');
const chapters = ref<LibraryChapter[]>([]);
const chaptersLoading = ref(false);
const chaptersError = ref('');
const managementDocIds = ref<string[]>([]);
const examDocIds = ref<string[]>([]);
const guides = ref<LibraryGuide[]>([]);
const selectedGuideId = ref('');
const guidesLoading = ref(false);
const guideSourcesVerified = ref(false);
const taskManager = useTaskManager();
const generationTask = ref<TaskStatus | null>(null);
const submittingGuide = ref(false);
const guideTasksLoading = ref(false);
const guideTaskSyncError = ref('');
const activeTaskStates = new Set(['pending', 'processing', 'cancel_requested']);
const generating = computed(() => submittingGuide.value || (!!generationTask.value && activeTaskStates.has(generationTask.value.status)));
const guideError = ref('');
const activeTab = ref('summary');
const tabs = [ { id: 'summary', label: '章节总结' }, { id: 'map', label: '思维导图' }, { id: 'recitation', label: '背诵短段' }, { id: 'connections', label: '真题与管理学' } ];
const selectedMapNode = ref('');
const sourceCitation = ref<LibraryCitation | null>(null);
const sourceCitationOwner = ref<'guide' | 'answer'>('guide');
const qaDocIds = ref<string[]>([]);
const qaChapterId = ref('');
const question = ref('');
const asking = ref(false);
const askError = ref('');
const activeAnswer = ref<LibraryAnswer | null>(null);
const answers = ref<LibraryAnswer[]>([]);
const answersLoading = ref(false);
const answersError = ref('');
const answerSourcesVerified = ref(false);
const answerHistoryVerified = ref(false);
let materialsSequence = 0;
let chapterSequence = 0;
let guideSequence = 0;
let answerHistorySequence = 0;
let askSequence = 0;
let generationSequence = 0;
let generationTimer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;

const readyDocuments = computed(() => documents.value.filter(doc => doc.status === 'completed'));
const textbooks = computed(() => readyDocuments.value.filter(doc => doc.file_type === 'textbook'));
const selectedDocument = computed(() => documents.value.find(doc => doc.id === selectedDocId.value));
const managementOptions = computed(() => textbooks.value.filter(doc => doc.id !== selectedDocId.value));
const examOptions = computed(() => readyDocuments.value.filter(doc => ['exam', 'exam_and_answer'].includes(doc.file_type)));
const selectedChapter = computed(() => chapters.value.find(chapter => chapter.id === selectedChapterId.value));
const activeGuide = computed(() => guides.value.find(guide => guide.id === selectedGuideId.value));
const examConcepts = computed(() => activeGuide.value?.exam_points.map(point => point.concept) || []);
const managementConcepts = computed(() => activeGuide.value?.connections.map(connection => connection.accounting_concept) || []);
const effectiveQaDocIds = computed(() => qaChapterId.value ? qaDocIds.value.filter(id => id === selectedDocId.value) : qaDocIds.value);
const qaScope = computed(() => JSON.stringify([effectiveQaDocIds.value.slice().sort(), qaChapterId.value]));
const followUpAvailable = computed(() => answerSourcesVerified.value && !!activeAnswer.value && !activeAnswer.value.source_stale && JSON.stringify([activeAnswer.value.doc_ids.slice().sort(), activeAnswer.value.chapter_id || '']) === qaScope.value);
const selectedMapConcept = computed(() => activeGuide.value?.concepts.find(concept => concept.name === selectedMapNode.value));
const selectedMapExamPoints = computed(() => activeGuide.value?.exam_points.filter(point => point.concept === selectedMapNode.value) || []);
const selectedMapConnections = computed(() => activeGuide.value?.connections.filter(connection => connection.accounting_concept === selectedMapNode.value) || []);

function errorText(error: unknown, fallback: string): string {
  const response = error as { response?: { data?: { detail?: unknown }; status?: number }; code?: string };
  if (typeof response.response?.data?.detail === 'string') return response.response.data.detail;
  if (response.response?.status === 401) return '登录已失效，请重新登录后重试。';
  if (response.code === 'ECONNABORTED') return '等待服务端超时。结果可能仍在处理中，请先刷新已保存记录，再决定是否重试。';
  return fallback;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

async function loadMaterials() {
  const sequence = ++materialsSequence;
  guideSourcesVerified.value = false;
  answerSourcesVerified.value = false;
  answerHistoryVerified.value = false;
  sourceCitation.value = null;
  materialsLoading.value = true;
  materialsError.value = '';
  try {
    const result = await apiClient.listMaterials();
    if (disposed || sequence !== materialsSequence) return;
    documents.value = result;
    const readyIds = new Set(readyDocuments.value.map(doc => doc.id));
    managementDocIds.value = managementDocIds.value.filter(id => readyIds.has(id));
    examDocIds.value = examDocIds.value.filter(id => readyIds.has(id));
    qaDocIds.value = qaDocIds.value.filter(id => readyIds.has(id));
    if (!textbooks.value.some(doc => doc.id === selectedDocId.value)) selectedDocId.value = textbooks.value[0]?.id || '';
    else {
      await Promise.all([loadChapters(), loadGuides(), loadAnswers()]);
    }
  } catch (error) {
    if (sequence === materialsSequence && !sessionTransport.isCancelled(error)) materialsError.value = errorText(error, '资料列表读取失败，请刷新重试。');
  } finally {
    if (sequence === materialsSequence) materialsLoading.value = false;
  }
}

async function loadChapters() {
  const sequence = ++chapterSequence;
  const docId = selectedDocId.value;
  chaptersError.value = '';
  chaptersLoading.value = !!docId;
  if (!docId) { chapters.value = []; return; }
  try {
    const result = await libraryApi.chapters(docId);
    if (disposed || sequence !== chapterSequence) return;
    chapters.value = result;
    if (!result.some(chapter => chapter.id === selectedChapterId.value)) selectedChapterId.value = result[0]?.id || '';
  } catch (error) {
    if (sequence === chapterSequence && !sessionTransport.isCancelled(error)) chaptersError.value = errorText(error, '章节读取失败，请刷新资料重试。');
  } finally {
    if (sequence === chapterSequence) chaptersLoading.value = false;
  }
}

async function loadGuides() {
  const sequence = ++guideSequence;
  const docId = selectedDocId.value;
  const chapterId = selectedChapterId.value;
  guideSourcesVerified.value = false;
  if (sourceCitationOwner.value === 'guide') sourceCitation.value = null;
  guides.value = [];
  selectedGuideId.value = '';
  guideError.value = '';
  guidesLoading.value = !!docId && !!chapterId;
  if (!docId || !chapterId) return;
  try {
    const result = await libraryApi.guides(docId, chapterId);
    if (disposed || sequence !== guideSequence) return;
    guides.value = result;
    guideSourcesVerified.value = true;
    selectedGuideId.value = result[0]?.id || '';
  } catch (error) {
    if (sequence === guideSequence && !sessionTransport.isCancelled(error)) guideError.value = errorText(error, '已保存资料读取失败，请刷新后重试。');
  } finally {
    if (sequence === guideSequence) guidesLoading.value = false;
  }
}

async function generateGuide() {
  if (generating.value || !selectedChapter.value) return;
  const payload = { doc_id: selectedDocId.value, chapter_id: selectedChapterId.value, management_doc_ids: managementDocIds.value.slice(), exam_doc_ids: examDocIds.value.slice() };
  const epoch = sessionTransport.epoch;
  const sequence = ++generationSequence;
  submittingGuide.value = true;
  guideError.value = '';
  guideTaskSyncError.value = '';
  try {
    const result = await libraryApi.submitGuide(payload);
    if (disposed || sequence !== generationSequence || epoch !== sessionTransport.epoch) return;
    await applyGenerationTask(result, sequence);
    void taskManager.fetchTasks();
  } catch (error) {
    if (!disposed && sequence === generationSequence && epoch === sessionTransport.epoch && !sessionTransport.isCancelled(error)) {
      guideError.value = errorText(error, '章节任务提交失败，请先重新获取任务状态后再试。');
      // A lost submission response does not mean that the server failed to enqueue.
      await loadGuideTasks();
    }
  } finally {
    if (sequence === generationSequence) submittingGuide.value = false;
  }
}

async function applyGenerationTask(task: TaskStatus, sequence: number) {
  if (disposed || sequence !== generationSequence) return;
  const previous = generationTask.value;
  generationTask.value = task;
  guideTaskSyncError.value = '';
  if (activeTaskStates.has(task.status)) {
    guideError.value = '';
    scheduleGenerationPoll(sequence);
  } else if (task.status === 'completed') {
    if (previous?.id !== task.id || previous.status !== 'completed') await loadGuides();
  } else if (task.status === 'failed' || task.status === 'partial') {
    guideError.value = task.error_msg || task.message || '章节生成失败，请查看任务日志。';
  }
}

function scheduleGenerationPoll(sequence: number) {
  clearTimeout(generationTimer);
  const epoch = sessionTransport.epoch;
  generationTimer = setTimeout(async () => {
    const task = generationTask.value;
    if (disposed || sequence !== generationSequence || epoch !== sessionTransport.epoch || !task) return;
    try {
      const result = await apiClient.getTaskStatus(task.id);
      if (epoch !== sessionTransport.epoch) return;
      await applyGenerationTask(result, sequence);
    } catch (error) {
      if (disposed || sequence !== generationSequence || epoch !== sessionTransport.epoch || sessionTransport.isCancelled(error)) return;
      guideTaskSyncError.value = '任务状态更新中断，后台可能仍在处理，请勿重复提交。';
      scheduleGenerationPoll(sequence);
    }
  }, 2000);
}

async function loadGuideTasks() {
  const sequence = ++generationSequence;
  const epoch = sessionTransport.epoch;
  const docId = selectedDocId.value;
  const chapterId = selectedChapterId.value;
  clearTimeout(generationTimer);
  generationTask.value = null;
  submittingGuide.value = false;
  guideTaskSyncError.value = '';
  guideTasksLoading.value = !!docId && !!chapterId;
  if (!docId || !chapterId) return;
  try {
    const result = await libraryApi.guideTasks(docId, chapterId);
    if (disposed || sequence !== generationSequence || epoch !== sessionTransport.epoch) return;
    const latest = result.find(task => activeTaskStates.has(task.status)) || result[0];
    if (latest) await applyGenerationTask(latest, sequence);
  } catch (error) {
    if (!disposed && sequence === generationSequence && epoch === sessionTransport.epoch && !sessionTransport.isCancelled(error)) {
      guideTaskSyncError.value = '暂时无法读取章节任务，请重新获取状态后再生成。';
    }
  } finally {
    if (sequence === generationSequence) guideTasksLoading.value = false;
  }
}

async function loadAnswers() {
  const sequence = ++answerHistorySequence;
  answerSourcesVerified.value = false;
  answerHistoryVerified.value = false;
  if (sourceCitationOwner.value === 'answer') sourceCitation.value = null;
  answersLoading.value = true;
  answersError.value = '';
  try {
    const result = await libraryApi.answers(selectedDocId.value);
    if (disposed || sequence !== answerHistorySequence) return;
    answers.value = result;
    answerHistoryVerified.value = true;
    if (activeAnswer.value) {
      const refreshedAnswer = result.find(answer => answer.id === activeAnswer.value!.id);
      if (refreshedAnswer) {
        activeAnswer.value = refreshedAnswer;
        answerSourcesVerified.value = true;
      }
    }
  } catch (error) {
    if (sequence === answerHistorySequence && !sessionTransport.isCancelled(error)) answersError.value = errorText(error, '问答历史读取失败，请刷新重试。');
  } finally {
    if (sequence === answerHistorySequence) answersLoading.value = false;
  }
}

async function askQuestion() {
  if (asking.value || !question.value.trim() || !effectiveQaDocIds.value.length) return;
  const sequence = ++askSequence;
  const scope = qaScope.value;
  const submittedQuestion = question.value.trim();
  const payload = { question: submittedQuestion, doc_ids: effectiveQaDocIds.value.slice(), chapter_id: qaChapterId.value || null, previous_answer_id: followUpAvailable.value ? activeAnswer.value!.id : null };
  asking.value = true;
  askError.value = '';
  try {
    const result = await libraryApi.ask(payload);
    if (disposed || sequence !== askSequence || scope !== qaScope.value) return;
    activeAnswer.value = result;
    answerSourcesVerified.value = true;
    if (question.value.trim() === submittedQuestion) question.value = '';
    answers.value = [result, ...answers.value.filter(answer => answer.id !== result.id)];
    ++answerHistorySequence;
    answersLoading.value = false;
  } catch (error) {
    if (!disposed && sequence === askSequence && scope === qaScope.value && !sessionTransport.isCancelled(error)) askError.value = errorText(error, '问答失败，请检查服务端模型配置和所选资料后重试。');
  } finally {
    asking.value = false;
  }
}

function startNewQuestion() {
  activeAnswer.value = null;
  answerSourcesVerified.value = false;
  if (sourceCitationOwner.value === 'answer') sourceCitation.value = null;
  askError.value = '';
}

function openAnswer(answer: LibraryAnswer) {
  qaDocIds.value = answer.doc_ids.filter(id => readyDocuments.value.some(doc => doc.id === id));
  qaChapterId.value = chapters.value.some(chapter => chapter.id === answer.chapter_id) ? answer.chapter_id || '' : '';
  activeAnswer.value = answer;
  answerSourcesVerified.value = answerHistoryVerified.value && !answersLoading.value;
  if (sourceCitationOwner.value === 'answer') sourceCitation.value = null;
  question.value = '';
}

function selectMapNode(title: string) { selectedMapNode.value = title; }
function openCitation(citation: LibraryCitation, owner: 'guide' | 'answer' = 'guide') {
  if (owner === 'answer' ? !answerSourcesVerified.value || activeAnswer.value?.source_stale : !guideSourcesVerified.value || activeGuide.value?.source_stale) return;
  sourceCitationOwner.value = owner;
  sourceCitation.value = citation;
}

watch(selectedDocId, () => {
  ++chapterSequence;
  chapters.value = [];
  selectedChapterId.value = '';
  managementDocIds.value = managementDocIds.value.filter(id => id !== selectedDocId.value);
  qaDocIds.value = selectedDocId.value ? [selectedDocId.value] : [];
  qaChapterId.value = '';
  answers.value = [];
  void loadChapters();
  void loadAnswers();
}, { flush: 'sync' });
watch(selectedChapterId, () => {
  qaChapterId.value = qaDocIds.value.length === 1 && qaDocIds.value[0] === selectedDocId.value ? selectedChapterId.value : '';
  void loadGuides();
  void loadGuideTasks();
}, { flush: 'sync' });
// Retrying from the shared task drawer must also resume this chapter's status.
watch(taskManager.tasks, tasks => {
  const current = generationTask.value;
  if (!current || submittingGuide.value) return;
  const currentIndex = tasks.findIndex(task => task.id === current.id);
  // History is newest first. A fast retry can already be terminal by the first
  // refresh, and must still replace the previous attempt and reload its result.
  const retry = tasks.find((task, index) => task.task_type === 'generate_library_guide'
    && task.target_id === current.target_id && task.id !== current.id
    && (task.retry_of === current.id || (currentIndex >= 0 && index < currentIndex)));
  if (retry) {
    const sequence = ++generationSequence;
    clearTimeout(generationTimer);
    void applyGenerationTask(retry, sequence);
  }
});
watch(selectedGuideId, () => { selectedMapNode.value = ''; sourceCitation.value = null; });
watch(qaScope, () => { ++askSequence; startNewQuestion(); }, { flush: 'sync' });
watch(qaDocIds, () => { if (qaDocIds.value.length !== 1 || !qaDocIds.value.includes(selectedDocId.value)) qaChapterId.value = ''; }, { deep: true, flush: 'sync' });
onMounted(() => { void loadMaterials(); void loadAnswers(); });
onBeforeUnmount(() => { disposed = true; clearTimeout(generationTimer); ++generationSequence; ++materialsSequence; ++chapterSequence; ++guideSequence; ++answerHistorySequence; ++askSequence; });
</script>

<style scoped>
.library-page, .library-page * { min-width: 0; }
.library-page { overflow-wrap: anywhere; }
.library-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 1.5rem; align-items: start; }
.notice { border-radius: .75rem; padding: .85rem 1rem; font-size: .8rem; line-height: 1.7; overflow-wrap: anywhere; }
.empty-panel { padding: 2rem 1.25rem; text-align: center; color: rgb(var(--c-sub)); line-height: 1.7; }
.content-card { padding: 1rem; border: 1px solid rgb(var(--c-line)); border-radius: .85rem; background: rgb(var(--c-raised) / .4); }
.check-row { display: flex; align-items: start; gap: .6rem; padding: .5rem 0; color: rgb(var(--c-sub)); font-size: .8rem; line-height: 1.6; cursor: pointer; }
.check-row input { flex-shrink: 0; margin-top: .25rem; }
.tabs-scroll { display: flex; gap: 1.2rem; overflow-x: auto; max-width: 100%; }
.tab-button { flex-shrink: 0; min-width: max-content; padding: .9rem 0; font-weight: 600; font-size: .85rem; border-bottom-width: 2px; }
.tab-button:focus-visible, .citation-button:focus-visible, .history-button:focus-visible { outline: 2px solid rgb(var(--c-accent)); outline-offset: -2px; }
.citation-button { text-align: left; font-size: .75rem; line-height: 1.6; color: rgb(var(--c-accent)); background: rgb(var(--c-accent) / .06); border-radius: .5rem; padding: .45rem .6rem; overflow-wrap: anywhere; max-width: 100%; }
.citation-button:hover { text-decoration: underline; }
.citation-button:disabled { color: rgb(var(--c-faint)); cursor: not-allowed; text-decoration: none; }
.history-button { display: block; width: 100%; padding: .7rem; border: 1px solid rgb(var(--c-line)); border-radius: .7rem; text-align: left; overflow-wrap: anywhere; }
.history-button:hover { background: rgb(var(--c-raised)); }
.history-button:disabled { opacity: .5; cursor: not-allowed; }
@media (min-width: 1100px) { .library-grid { grid-template-columns: minmax(0, 1.65fr) minmax(0, 1fr); } }
</style>
