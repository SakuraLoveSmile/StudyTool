<template>
  <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
    <!-- Header -->
    <div class="mb-8">
      <h1 class="text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">模型与系统设置</h1>
      <p class="mt-2 text-sm text-slate-600">
        配置大语言模型、向量服务与百度 OCR 凭据。凭据通过系统本地安全存储加密保存，备份与安装包不会包含您的密钥。
      </p>
    </div>

    <!-- First-run Onboarding Wizard Banner -->
    <div
      v-if="showWizard"
      class="mb-8 rounded-xl border border-blue-200 bg-blue-50/80 p-5 shadow-xs"
    >
      <div class="flex items-start justify-between gap-4">
        <div>
          <h2 class="text-base font-semibold text-blue-900">首次使用配置向导</h2>
          <p class="mt-1 text-sm text-blue-700">
            配置模型凭据后即可开启专业课扫描真题文字识别、考点切片分析与智能练习批改。您也可以直接跳过此向导，本地阅读已导入的资料与已有背诵卡。
          </p>
        </div>
        <button
          type="button"
          class="shrink-0 rounded-lg px-3 py-1.5 text-xs font-medium text-blue-700 hover:bg-blue-100"
          @click="dismissWizard"
        >
          暂不配置，跳过向导
        </button>
      </div>
    </div>

    <!-- Error Banner -->
    <div v-if="errorMessage" class="mb-6 rounded-lg bg-rose-50 p-4 text-sm text-rose-700">
      {{ errorMessage }}
    </div>

    <!-- Software Updates -->
    <section v-if="isDesktop" class="mb-8 rounded-xl border border-slate-200 bg-white p-5 shadow-xs">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 class="text-base font-semibold text-slate-900">软件更新</h2>
          <p class="mt-1 text-xs text-slate-500">当前版本 {{ updateStatus.currentVersion }}，只从正式发行版获取更新。</p>
          <p v-if="updateStatus.state === 'available'" class="mt-2 text-sm text-blue-700">
            发现新版本 {{ updateStatus.version }}，确认后下载。
          </p>
          <p v-else-if="updateStatus.state === 'downloaded'" class="mt-2 text-sm text-emerald-700">
            新版本 {{ updateStatus.version }} 已下载完成，可在空闲时安装。
          </p>
          <p v-else-if="updateStatus.state === 'downloading'" class="mt-2 text-sm text-blue-700">
            正在下载更新 {{ Math.round(updateStatus.percent || 0) }}%
          </p>
          <p v-else-if="updateStatus.state === 'error'" class="mt-2 text-sm text-rose-700">{{ updateStatus.error }}</p>
          <p v-else-if="updateStatus.state === 'not-available'" class="mt-2 text-xs text-slate-500">当前已是最新版本。</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <button
            v-if="updateStatus.state !== 'downloading'"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            :disabled="updateStatus.state === 'checking'"
            @click="handleCheckUpdates"
          >
            <RefreshCw class="h-3.5 w-3.5" />
            {{ updateStatus.state === 'checking' ? '检查中...' : '检查更新' }}
          </button>
          <button
            v-if="updateStatus.state === 'available'"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700"
            @click="handleDownloadUpdate"
          >
            <Download class="h-3.5 w-3.5" />下载更新
          </button>
          <button
            v-if="updateStatus.state === 'downloading'"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            @click="handleCancelUpdate"
          >
            取消下载
          </button>
          <button
            v-if="updateStatus.state === 'downloaded'"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700"
            @click="handleInstallUpdate"
          >
            <RefreshCw class="h-3.5 w-3.5" />重启安装
          </button>
        </div>
      </div>
    </section>

    <!-- Success Message -->
    <div v-if="successMessage" class="mb-6 rounded-lg bg-emerald-50 p-4 text-sm text-emerald-700">
      {{ successMessage }}
    </div>

    <form @submit.prevent="handleSave" class="space-y-8">
      <!-- 1. LLM Settings -->
      <section class="rounded-xl border border-slate-200 bg-white p-6 shadow-xs">
        <div class="flex items-center justify-between border-b border-slate-100 pb-4">
          <div class="flex items-center space-x-2">
            <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
              <Bot class="h-4 w-4" />
            </div>
            <div>
              <h2 class="text-base font-semibold text-slate-900">大语言模型 (LLM)</h2>
              <p class="text-xs text-slate-500">用于考点提炼、真题解析、背诵卡生成与主观题批改</p>
            </div>
          </div>
          <span
            class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
            :class="credentialsStatus?.llm.configured ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'"
          >
            {{ credentialsStatus?.llm.configured ? '已配置' : '待配置' }}
          </span>
        </div>

        <div class="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label class="block text-xs font-medium text-slate-700">API 服务地址 (Base URL)</label>
            <input
              v-model="form.llm_base_url"
              type="text"
              placeholder="https://api.openai.com/v1"
              class="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none"
            />
          </div>

          <div>
            <label class="block text-xs font-medium text-slate-700">模型名称 (Model)</label>
            <input
              v-model="form.llm_model"
              type="text"
              placeholder="gpt-4o-mini"
              class="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none"
            />
          </div>

          <!-- Sensitive Input with Feedback Masking -->
          <div class="sm:col-span-2" data-feedback-capture-mask>
            <div class="flex items-center justify-between">
              <label class="block text-xs font-medium text-slate-700">API 密钥 (API Key)</label>
              <button
                v-if="credentialsStatus?.llm.configured"
                type="button"
                class="text-[11px] text-rose-600 hover:underline"
                @click="toggleClearLlmKey"
              >
                {{ clearLlmKey ? '撤销清除' : '清除已存密钥' }}
              </button>
            </div>
            <div class="relative mt-1">
              <input
                v-if="!clearLlmKey"
                v-model="form.llm_api_key"
                type="password"
                autocomplete="off"
                :placeholder="credentialsStatus?.llm.configured ? '已保存密钥 (留空表示保持当前密钥不变)' : '请输入 API 密钥'"
                class="block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none"
              />
              <div v-else class="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                保存后将清除此模型的已存密钥。
              </div>
            </div>
          </div>
        </div>

        <div class="mt-4 flex items-center justify-between">
          <button
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            :disabled="testingLlm"
            @click="testLlm"
          >
            <Loader2 v-if="testingLlm" class="h-3.5 w-3.5 animate-spin" />
            <Sparkles v-else class="h-3.5 w-3.5 text-indigo-500" />
            <span>{{ testingLlm ? '测试中...' : '测试 LLM 连通性' }}</span>
          </button>
          <span v-if="llmTestResult" class="text-xs" :class="llmTestResult.ok ? 'text-emerald-600' : 'text-rose-600'">
            {{ llmTestResult.message }}
          </span>
        </div>
      </section>

      <!-- 2. Embedding Settings -->
      <section class="rounded-xl border border-slate-200 bg-white p-6 shadow-xs">
        <div class="flex items-center justify-between border-b border-slate-100 pb-4">
          <div class="flex items-center space-x-2">
            <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
              <Network class="h-4 w-4" />
            </div>
            <div>
              <h2 class="text-base font-semibold text-slate-900">向量检索服务 (Embedding)</h2>
              <p class="text-xs text-slate-500">用于学习资料语义切片检索与真题考点智能关联</p>
            </div>
          </div>
          <span
            class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
            :class="credentialsStatus?.embedding.configured ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'"
          >
            {{ credentialsStatus?.embedding.configured ? '已配置' : '待配置' }}
          </span>
        </div>

        <!-- Embedding lock note -->
        <div v-if="status?.vector_locked" class="mt-4 flex flex-col gap-2 rounded-lg border border-amber-300 bg-amber-50 p-4 text-xs text-amber-900">
          <div class="flex items-center gap-2 font-medium">
            <Lock class="h-4 w-4 text-amber-600" />
            <span>向量索引已被锁定：{{ status?.vector_lock_reason || '模型来源不一致或信息缺失' }}</span>
          </div>
          <p class="text-amber-800">
            锁定期间普通文本搜索与文档浏览不受影响，但禁止追加与语义检索。您可以在配置好上方向量模型与密钥后，点击下方按钮重建索引以解除锁定。
          </p>
          <div class="mt-1">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-3 py-1.5 font-medium text-white shadow-xs hover:bg-amber-700 disabled:opacity-50"
              :disabled="loading || reindexing"
              @click="handleReindex"
            >
              <RefreshCw v-if="!reindexing" class="h-3.5 w-3.5" />
              <Loader2 v-else class="h-3.5 w-3.5 animate-spin" />
              <span>{{ reindexing ? '正在重建索引...' : '确认模型并重建索引' }}</span>
            </button>
          </div>
        </div>
        <div v-else-if="credentialsStatus?.embedding.locked" class="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          <Lock class="mr-1.5 inline h-3.5 w-3.5 text-amber-600" />
          当前资料库已建立语义向量。为避免不同模型维度混用导致检索异常，已锁定服务地址与模型名称，仅允许更新密钥。
        </div>

        <div class="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label class="block text-xs font-medium text-slate-700">API 服务地址 (Base URL)</label>
            <input
              v-model="form.embedding_base_url"
              type="text"
              :disabled="credentialsStatus?.embedding.locked"
              placeholder="https://api.openai.com/v1"
              class="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
            />
          </div>

          <div>
            <label class="block text-xs font-medium text-slate-700">模型名称 (Model)</label>
            <input
              v-model="form.embedding_model"
              type="text"
              :disabled="credentialsStatus?.embedding.locked"
              placeholder="text-embedding-3-small"
              class="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
            />
          </div>

          <!-- Sensitive Input with Feedback Masking -->
          <div class="sm:col-span-2" data-feedback-capture-mask>
            <div class="flex items-center justify-between">
              <label class="block text-xs font-medium text-slate-700">API 密钥 (API Key)</label>
              <button
                v-if="credentialsStatus?.embedding.configured"
                type="button"
                class="text-[11px] text-rose-600 hover:underline"
                @click="toggleClearEmbeddingKey"
              >
                {{ clearEmbeddingKey ? '撤销清除' : '清除已存密钥' }}
              </button>
            </div>
            <div class="relative mt-1">
              <input
                v-if="!clearEmbeddingKey"
                v-model="form.embedding_api_key"
                type="password"
                autocomplete="off"
                :placeholder="credentialsStatus?.embedding.configured ? '已保存密钥 (留空表示保持当前密钥不变)' : '请输入 API 密钥'"
                class="block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none"
              />
              <div v-else class="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                保存后将清除此向量服务的已存密钥。
              </div>
            </div>
          </div>
        </div>

        <div class="mt-4 flex items-center justify-between">
          <button
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            :disabled="testingEmbedding"
            @click="testEmbedding"
          >
            <Loader2 v-if="testingEmbedding" class="h-3.5 w-3.5 animate-spin" />
            <Sparkles v-else class="h-3.5 w-3.5 text-emerald-500" />
            <span>{{ testingEmbedding ? '测试中...' : '测试 Embedding 连通性' }}</span>
          </button>
          <span v-if="embeddingTestResult" class="text-xs" :class="embeddingTestResult.ok ? 'text-emerald-600' : 'text-rose-600'">
            {{ embeddingTestResult.message }}
          </span>
        </div>
      </section>

      <!-- 3. Baidu PaddleOCR API Settings -->
      <section class="rounded-xl border border-slate-200 bg-white p-6 shadow-xs">
        <div class="flex items-center justify-between border-b border-slate-100 pb-4">
          <div class="flex items-center space-x-2">
            <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-50 text-blue-600">
              <ScanText class="h-4 w-4" />
            </div>
            <div>
              <h2 class="text-base font-semibold text-slate-900">百度 PaddleOCR API (PP-OCRv5)</h2>
              <p class="text-xs text-slate-500">用于扫描版真题与图片讲义的高精度逐页文字识别</p>
            </div>
          </div>
          <span
            class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
            :class="credentialsStatus?.baidu_ocr.configured ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'"
          >
            {{ credentialsStatus?.baidu_ocr.configured ? '已配置' : '待配置' }}
          </span>
        </div>

        <div class="mt-6 grid grid-cols-1 gap-4">
          <div>
            <label class="block text-xs font-medium text-slate-700">完整 API 服务地址 (Full API URL)</label>
            <input
              v-model="form.baidu_ocr_api_url"
              type="text"
              placeholder="https://aistudio.baidu.com/serving/api/v1/ocr..."
              class="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none"
            />
            <p class="mt-1 text-[11px] text-slate-400">
              请填写完整的 OCR 预测接口 URL。
            </p>
          </div>

          <!-- Sensitive Input with Feedback Masking -->
          <div data-feedback-capture-mask>
            <div class="flex items-center justify-between">
              <label class="block text-xs font-medium text-slate-700">访问令牌 (Token)</label>
              <button
                v-if="credentialsStatus?.baidu_ocr.configured"
                type="button"
                class="text-[11px] text-rose-600 hover:underline"
                @click="toggleClearOcrToken"
              >
                {{ clearOcrToken ? '撤销清除' : '清除已存 Token' }}
              </button>
            </div>
            <div class="relative mt-1">
              <input
                v-if="!clearOcrToken"
                v-model="form.baidu_ocr_token"
                type="password"
                autocomplete="off"
                :placeholder="credentialsStatus?.baidu_ocr.configured ? '已保存 Token (留空表示保持当前 Token 不变)' : '请输入 Authorization Token'"
                class="block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none"
              />
              <div v-else class="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                保存后将清除此百度 OCR 的访问令牌。
              </div>
            </div>
          </div>
        </div>

        <div class="mt-4 flex items-center justify-between">
          <button
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            :disabled="testingOcr"
            @click="testOcr"
          >
            <Loader2 v-if="testingOcr" class="h-3.5 w-3.5 animate-spin" />
            <Sparkles v-else class="h-3.5 w-3.5 text-blue-500" />
            <span>{{ testingOcr ? '测试中...' : '测试百度 OCR 识别能力' }}</span>
          </button>
          <span v-if="ocrTestResult" class="text-xs" :class="ocrTestResult.ok ? 'text-emerald-600' : 'text-rose-600'">
            {{ ocrTestResult.message }}
          </span>
        </div>
      </section>

      <!-- Save Button -->
      <div class="flex items-center justify-end gap-3 pt-2">
        <button
          type="submit"
          class="inline-flex cursor-pointer items-center gap-2 rounded-xl bg-blue-600 px-6 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 focus:outline-none disabled:opacity-50"
          :disabled="loading"
        >
          <Loader2 v-if="loading" class="h-4 w-4 animate-spin" />
          <span>保存所有配置</span>
        </button>
      </div>
    </form>

    <!-- 4. Backup & Restore Section -->
    <div class="mt-12 border-t border-slate-200 pt-8">
      <div class="mb-4">
        <h2 class="text-lg font-bold text-slate-900">数据备份与恢复</h2>
        <p class="text-xs text-slate-500">
          通过系统文件对话框完整导出或恢复您的学习数据、题目与真题切片。
        </p>
      </div>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <!-- Export Card -->
        <div class="rounded-xl border border-slate-200 bg-white p-5 shadow-xs">
          <div class="flex items-center space-x-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
              <Download class="h-5 w-5" />
            </div>
            <div>
              <h3 class="text-sm font-semibold text-slate-900">导出数据备份</h3>
              <p class="text-xs text-slate-500">校验哈希并生成压缩归档文件</p>
            </div>
          </div>
          <p class="mt-3 text-xs text-slate-600">服务安全停机后完整归档，自动排除系统日志与密钥敏感信息。</p>
          <button
            type="button"
            class="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3.5 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            :disabled="loading"
            @click="handleCreateBackup"
          >
            <Download class="h-3.5 w-3.5" />
            <span>选择保存路径并导出</span>
          </button>
        </div>

        <!-- Restore Card -->
        <div class="rounded-xl border border-slate-200 bg-white p-5 shadow-xs">
          <div class="flex items-center space-x-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-amber-50 text-amber-600">
              <Upload class="h-5 w-5" />
            </div>
            <div>
              <h3 class="text-sm font-semibold text-slate-900">从备份文件恢复</h3>
              <p class="text-xs text-slate-500">安全恢复历史资料并保留当前快照</p>
            </div>
          </div>
          <p class="mt-3 text-xs text-slate-600">
            恢复前在临时目录自检完整性与模型兼容性，并自动保留恢复前副本。
          </p>
          <button
            type="button"
            class="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3.5 py-2 text-xs font-medium text-amber-700 hover:bg-amber-50 disabled:opacity-50"
            :disabled="loading"
            @click="handleRestoreBackup"
          >
            <Upload class="h-3.5 w-3.5" />
            <span>选择备份文件并恢复</span>
          </button>
        </div>
      </div>

      <!-- Logs / Diagnostics -->
      <div class="mt-6 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-slate-50 p-4">
        <div class="text-xs text-slate-600">
          如遇异常需要排查，可在软件内直接查看运行日志并导出诊断包，或使用右下角 Feedback 提交报告。
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <!-- 设置页保留日志入口；主导航同时提供默认可见的「日志」标签页 -->
          <router-link
            v-if="isDesktop"
            to="/logs"
            class="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
          >
            <ScrollText class="h-3.5 w-3.5 text-slate-500" />
            <span>查看运行日志</span>
          </router-link>
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
            @click="openLogDirectory"
          >
            <Folder class="h-3.5 w-3.5 text-slate-500" />
            <span>打开运行日志目录</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue';
import {
  Bot,
  Network,
  ScanText,
  Lock,
  Sparkles,
  Download,
  Upload,
  Folder,
  Loader2,
  RefreshCw,
  ScrollText,
} from 'lucide-vue-next';
import { useDesktop } from '../composables/useDesktop';

const {
  isDesktop,
  loading,
  status,
  credentialsStatus,
  errorMessage,
  fetchStatus,
  saveCredentials,
  testConnection,
  createBackup,
  restoreBackup,
  openLogDirectory,
  reindex,
  updateStatus,
  checkForUpdates,
  downloadUpdate,
  cancelUpdate,
  installUpdate,
} = useDesktop();

const reindexing = ref(false);

const handleCheckUpdates = async () => {
  await checkForUpdates();
};

const handleDownloadUpdate = async () => {
  await downloadUpdate();
};

const handleCancelUpdate = async () => {
  await cancelUpdate();
};

const handleInstallUpdate = async () => {
  if (!window.confirm('更新已下载。安装前软件会安全退出，安装完成后自动重新打开。现在安装吗？')) return;
  await installUpdate();
};

const handleReindex = async () => {
  const confirmed = window.confirm(
    '确认重建索引吗？\n\n系统将使用当前配置的向量模型重新计算所有切片的语义向量。这将消耗接口 Token 配额并产生少量费用。确认继续吗？'
  );
  if (!confirmed) return;

  reindexing.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const res = await reindex();
    if (res?.ok) {
      successMessage.value = res.message || '索引重建成功，向量锁已解除';
    } else {
      errorMessage.value = res?.message || '索引重建失败';
    }
  } catch (err: any) {
    errorMessage.value = err.message || '索引重建异常';
  } finally {
    reindexing.value = false;
  }
};

const showWizard = ref(false);
const successMessage = ref('');

const clearLlmKey = ref(false);
const clearEmbeddingKey = ref(false);
const clearOcrToken = ref(false);

const form = reactive({
  llm_base_url: '',
  llm_model: '',
  llm_api_key: '',
  embedding_base_url: '',
  embedding_model: '',
  embedding_api_key: '',
  baidu_ocr_api_url: '',
  baidu_ocr_token: '',
});

const testingLlm = ref(false);
const llmTestResult = ref<{ ok: boolean; message: string } | null>(null);

const testingEmbedding = ref(false);
const embeddingTestResult = ref<{ ok: boolean; message: string } | null>(null);

const testingOcr = ref(false);
const ocrTestResult = ref<{ ok: boolean; message: string } | null>(null);

onMounted(async () => {
  await fetchStatus();
  if (credentialsStatus.value) {
    form.llm_base_url = credentialsStatus.value.llm.base_url || 'https://api.openai.com/v1';
    form.llm_model = credentialsStatus.value.llm.model || 'gpt-4o-mini';
    form.embedding_base_url = credentialsStatus.value.embedding.base_url || 'https://api.openai.com/v1';
    form.embedding_model = credentialsStatus.value.embedding.model || 'text-embedding-3-small';
    form.baidu_ocr_api_url = credentialsStatus.value.baidu_ocr.api_url || '';

    // Show onboarding wizard if neither LLM nor OCR is configured
    const notConfigured =
      !credentialsStatus.value.llm.configured && !credentialsStatus.value.baidu_ocr.configured;
    const dismissed = localStorage.getItem('rag_wizard_dismissed');
    if (notConfigured && !dismissed) {
      showWizard.value = true;
    }
  }
});

const dismissWizard = () => {
  showWizard.value = false;
  localStorage.setItem('rag_wizard_dismissed', 'true');
};

const toggleClearLlmKey = () => {
  clearLlmKey.value = !clearLlmKey.value;
  if (clearLlmKey.value) form.llm_api_key = '';
};

const toggleClearEmbeddingKey = () => {
  clearEmbeddingKey.value = !clearEmbeddingKey.value;
  if (clearEmbeddingKey.value) form.embedding_api_key = '';
};

const toggleClearOcrToken = () => {
  clearOcrToken.value = !clearOcrToken.value;
  if (clearOcrToken.value) form.baidu_ocr_token = '';
};

const testLlm = async () => {
  testingLlm.value = true;
  llmTestResult.value = null;
  try {
    const res = await testConnection('llm', {
      base_url: form.llm_base_url,
      model: form.llm_model,
      api_key: form.llm_api_key || undefined,
    });
    llmTestResult.value = { ok: res.ok, message: res.message };
  } catch (err: any) {
    llmTestResult.value = { ok: false, message: err.message };
  } finally {
    testingLlm.value = false;
  }
};

const testEmbedding = async () => {
  testingEmbedding.value = true;
  embeddingTestResult.value = null;
  try {
    const res = await testConnection('embedding', {
      base_url: form.embedding_base_url,
      model: form.embedding_model,
      api_key: form.embedding_api_key || undefined,
    });
    embeddingTestResult.value = { ok: res.ok, message: res.message };
  } catch (err: any) {
    embeddingTestResult.value = { ok: false, message: err.message };
  } finally {
    testingEmbedding.value = false;
  }
};

const testOcr = async () => {
  testingOcr.value = true;
  ocrTestResult.value = null;
  try {
    const res = await testConnection('baidu_ocr', {
      baidu_ocr_url: form.baidu_ocr_api_url,
      baidu_ocr_token: form.baidu_ocr_token || undefined,
    });
    ocrTestResult.value = { ok: res.ok, message: res.message };
  } catch (err: any) {
    ocrTestResult.value = { ok: false, message: err.message };
  } finally {
    testingOcr.value = false;
  }
};

const handleSave = async () => {
  successMessage.value = '';
  try {
    const payload: any = {
      llm_base_url: form.llm_base_url,
      llm_model: form.llm_model,
      embedding_base_url: form.embedding_base_url,
      embedding_model: form.embedding_model,
      baidu_ocr_api_url: form.baidu_ocr_api_url,
    };

    if (clearLlmKey.value) {
      payload.llm_api_key = { action: 'clear' };
    } else if (form.llm_api_key) {
      payload.llm_api_key = { action: 'replace', value: form.llm_api_key };
    } else {
      payload.llm_api_key = { action: 'keep' };
    }

    if (clearEmbeddingKey.value) {
      payload.embedding_api_key = { action: 'clear' };
    } else if (form.embedding_api_key) {
      payload.embedding_api_key = { action: 'replace', value: form.embedding_api_key };
    } else {
      payload.embedding_api_key = { action: 'keep' };
    }

    if (clearOcrToken.value) {
      payload.baidu_ocr_token = { action: 'clear' };
    } else if (form.baidu_ocr_token) {
      payload.baidu_ocr_token = { action: 'replace', value: form.baidu_ocr_token };
    } else {
      payload.baidu_ocr_token = { action: 'keep' };
    }

    const res = await saveCredentials(payload);
    successMessage.value = res.message || '配置已成功安全保存';

    // Clear sensitive inputs from UI memory
    form.llm_api_key = '';
    form.embedding_api_key = '';
    form.baidu_ocr_token = '';
    clearLlmKey.value = false;
    clearEmbeddingKey.value = false;
    clearOcrToken.value = false;
  } catch {
    // errorMessage is set by composable
  }
};

const handleCreateBackup = async () => {
  try {
    const res = await createBackup();
    if (res && res.ok) {
      alert(`备份导出成功！\n文件路径: ${res.archive_path}`);
    }
  } catch (err: any) {
    alert(`备份导出失败: ${err.message}`);
  }
};

const handleRestoreBackup = async () => {
  if (!confirm('恢复备份将替换当前数据并重新加载，恢复前会自动保留历史副本。是否继续？')) return;
  try {
    const res = await restoreBackup();
    if (res && res.ok) {
      alert('数据恢复成功！');
    }
  } catch (err: any) {
    alert(`恢复失败: ${err.message}`);
  }
};
</script>
