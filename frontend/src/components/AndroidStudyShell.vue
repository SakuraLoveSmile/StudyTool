<template>
  <div class="study-shell">
    <aside class="study-rail" aria-label="平板学习导航">
      <router-link to="/today" class="study-brand"><BookOpen :size="26" /><span>知学<small>学习手账</small></span></router-link>
      <nav class="study-rail-links">
        <router-link v-for="item in primaryLinks" :key="item.to" :to="item.to" :aria-current="route.path === item.to ? 'page' : undefined"><component :is="item.icon" :size="22" />{{ item.label }}</router-link>
      </nav>
      <div class="study-rail-note">读懂一章<br />记牢一点</div>
      <button type="button" class="study-more-button" @click="openMore"><Grid2X2 :size="22" />更多学习工具</button>
    </aside>
    <div class="study-workspace">
      <header class="study-topbar">
        <div><span class="study-eyebrow">知学 · 学习手账</span><p>{{ pageTitle }}</p></div>
        <button type="button" class="study-task-button" @click="toggleDrawer"><ListTodo :size="19" /><span>任务</span><span v-if="activeCount" class="study-count">{{ activeCount }}</span></button>
      </header>
      <main class="study-main"><slot /></main>
    </div>
    <nav class="study-bottom" aria-label="手机学习导航">
      <router-link v-for="item in primaryLinks" :key="item.to" :to="item.to" :aria-current="route.path === item.to ? 'page' : undefined"><component :is="item.icon" :size="22" /><span>{{ item.label }}</span></router-link>
      <button type="button" :class="{ 'study-selected': moreActive }" :aria-expanded="moreOpen" aria-haspopup="dialog" @click="openMore"><Grid2X2 :size="22" /><span>更多</span></button>
    </nav>
    <Teleport to="body">
      <div v-if="moreOpen" class="study-sheet-backdrop" @click.self="closeMore()" @keydown="onSheetKeydown">
        <section ref="sheet" role="dialog" aria-modal="true" aria-labelledby="study-more-title" tabindex="-1" class="study-sheet">
          <header><div><span class="study-eyebrow">学习工具箱</span><h2 id="study-more-title">让每一步学习更顺手</h2></div><button type="button" aria-label="关闭更多工具" @click="closeMore()"><X :size="22" /></button></header>
          <nav aria-label="更多学习功能" class="study-tool-grid"><router-link v-for="item in moreLinks" :key="item.to" :to="item.to"><component :is="item.icon" :size="23" /><span>{{ item.label }}</span></router-link></nav>
          <ThemeToggle variant="menu" />
          <div v-if="auth.user.value || auth.authRequired.value" data-feedback-capture-mask class="study-account"><span>{{ auth.user.value?.username }}</span><button v-if="auth.authRequired.value" type="button" :disabled="loggingOut" @click="logout"><LogOut :size="18" />退出登录</button></div>
          <p v-if="logoutError" role="alert" class="text-err text-sm">{{ logoutError }}</p>
        </section>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BookOpen, CalendarDays, BrainCircuit, Grid2X2, ListTodo, X, FolderArchive, BarChart3, Layers, CheckSquare, Sliders, LogOut } from 'lucide-vue-next';
import { useAuth } from '../composables/useAuth';
import { useTaskManager } from '../composables/useTaskManager';
import ThemeToggle from './ThemeToggle.vue';
const route = useRoute();
const router = useRouter();
const auth = useAuth();
const { activeCount, toggleDrawer } = useTaskManager();
const primaryLinks = [{ to: '/today', label: '今天', icon: CalendarDays }, { to: '/library', label: '教材', icon: BookOpen }, { to: '/recall', label: '回忆卡', icon: BrainCircuit }];
const moreLinks = [{ to: '/materials', label: '资料与真题', icon: FolderArchive }, { to: '/analysis', label: '考点分析', icon: BarChart3 }, { to: '/cards', label: '考点讲义', icon: Layers }, { to: '/practice', label: '模拟与批改', icon: CheckSquare }, { to: '/settings', label: '设置', icon: Sliders }];
const pageTitle = computed(() => [...primaryLinks, ...moreLinks].find(item => item.to === route.path)?.label ?? '学习手账');
const moreActive = computed(() => moreLinks.some(item => item.to === route.path));
const moreOpen = ref(false);
const sheet = ref<HTMLElement>();
const loggingOut = ref(false);
const logoutError = ref('');
let previousFocus: HTMLElement | null = null;
let previousOverflow = '';
async function openMore() {
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  previousOverflow = document.body.style.overflow;
  document.body.style.overflow = 'hidden';
  moreOpen.value = true;
  await nextTick();
  sheet.value?.focus();
}
function closeMore(restore = true) {
  if (!moreOpen.value) return;
  moreOpen.value = false;
  document.body.style.overflow = previousOverflow;
  if (restore) previousFocus?.focus({ preventScroll: true });
}
function onSheetKeydown(event: KeyboardEvent) {
  event.stopPropagation();
  if (event.key === 'Escape') { event.preventDefault(); closeMore(); }
  if (event.key !== 'Tab') return;
  const controls = Array.from(sheet.value?.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), [tabindex="0"]') ?? []);
  const first = controls[0]; const last = controls[controls.length - 1];
  if (event.shiftKey && (document.activeElement === first || document.activeElement === sheet.value)) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && (document.activeElement === last || document.activeElement === sheet.value)) { event.preventDefault(); first?.focus(); }
}
async function logout() {
  loggingOut.value = true; logoutError.value = '';
  try { await auth.logout(); await router.replace({ name: 'login' }); }
  catch { logoutError.value = auth.errorMessage.value || '退出失败，请重试'; }
  finally { loggingOut.value = false; }
}
watch(() => route.fullPath, () => closeMore(false));
onBeforeUnmount(() => closeMore(false));
</script>
