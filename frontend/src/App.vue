<template>
  <div class="min-h-screen bg-page flex flex-col">
    <Navbar v-if="showShell" />
    <LogNavNotice v-if="showShell" />

    <main class="max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-1">
      <router-view :key="sessionKey" />
    </main>

    <!-- Global Task Drawer -->
    <TaskDrawer v-if="showShell" />

    <footer v-if="showShell" class="bg-surface border-t border-line py-4 text-center text-xs text-faint">
      <div class="max-w-7xl mx-auto px-4 flex flex-wrap items-center justify-between gap-2">
        <span>考研专业课备考助手</span>
        <span>教材切片依据追溯 · 考查频次统计 · 规范答题框架</span>
      </div>
    </footer>

    <!--
      反馈灵感球宿主容器：实例的创建 / 隔离 / 清理全部由 useFeedbackWidget 负责。
      - 未配置 VITE_FEEDBACK_API_BASE 与 VITE_FEEDBACK_APP_ID 时容器不存在，组件包也不会被加载；
      - 已配置时也只在会话初始化完成且允许展示应用外壳时挂载实例：
        登录页与强制改密页不出现入口、不加载组件包。
    -->
    <div v-if="feedbackConfigured" ref="feedbackHost"></div>
  </div>
</template>

<script setup lang="ts">
import Navbar from './components/Navbar.vue';
import LogNavNotice from './components/LogNavNotice.vue';
import TaskDrawer from './components/TaskDrawer.vue';
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuth } from './composables/useAuth';
import { useFeedbackWidget } from './composables/useFeedbackWidget';
import { useTheme } from './composables/useTheme';

const route = useRoute();
const router = useRouter();
const auth = useAuth();
const sessionKey = computed(() => `${auth.user.value?.id ?? 'anonymous'}:${auth.sessionEpoch.value}:${auth.ready.value}`);
const showShell = computed(() => auth.isAuthenticated.value && route.name !== 'login' && route.name !== 'change-password');

const handleAuthRequired = () => {
  auth.logout({ remote: false }).finally(() => {
    if (route.name !== 'login') router.replace({ name: 'login', query: { redirect: route.fullPath } });
  });
};
onMounted(() => window.addEventListener('rag:auth-required', handleAuthRequired));
onBeforeUnmount(() => window.removeEventListener('rag:auth-required', handleAuthRequired));

/** 页面标签只取路由名称，不带查询参数（避免把用户输入带进归档）。 */
const pageLabel = computed(() => String(route.name ?? 'unknown'));
/** 会话初始化完成且允许展示应用外壳时，才允许挂载反馈组件。 */
const feedbackEnabled = computed(() => auth.ready.value && showShell.value);
/** 实例键 = RAG 用户身份 + 会话世代；变化即重建实例，新实例不继承旧令牌与旧草稿。 */
const feedbackInstanceKey = computed(() => `${auth.user.value?.id ?? 'anonymous'}:${auth.sessionEpoch.value}`);

const feedbackHost = ref<HTMLElement | null>(null);
const theme = useTheme();
const { configured: feedbackConfigured } = useFeedbackWidget({
  container: feedbackHost,
  enabled: feedbackEnabled,
  instanceKey: feedbackInstanceKey,
  pageLabel,
  theme: theme.effective,
});
</script>
