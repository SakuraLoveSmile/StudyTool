<template>
  <header class="sticky top-0 z-30 border-b border-slate-200 bg-white shadow-xs">
    <div class="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
      <div class="flex h-16 items-center justify-between gap-3">
        <router-link to="/today" class="flex min-w-0 items-center space-x-3" aria-label="返回今日学习">
          <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-600 text-white shadow-md shadow-blue-200">
            <BookOpen class="h-5 w-5" />
          </div>
          <div class="min-w-0">
            <h1 class="truncate text-base font-bold tracking-tight text-slate-900 sm:text-lg">考研专业课备考助手</h1>
            <p class="hidden text-xs text-slate-400 sm:block">真题追溯 · 主动回忆 · 考点频次 · 智能批改</p>
          </div>
        </router-link>

        <nav class="hidden items-center space-x-1 sm:space-x-1.5 md:flex" aria-label="主导航">
          <router-link to="/today" class="nav-tab" :class="{ 'nav-tab-active': $route.path === '/today' }">
            <CalendarDays class="mr-1.5 inline h-4 w-4" />今日学习
          </router-link>
          <router-link to="/recall" class="nav-tab" :class="{ 'nav-tab-active': $route.path === '/recall' }">
            <BrainCircuit class="mr-1.5 inline h-4 w-4" />回忆卡库
          </router-link>
          <router-link to="/materials" class="nav-tab" :class="{ 'nav-tab-active': $route.path === '/materials' }">
            <FolderArchive class="mr-1.5 inline h-4 w-4" />资料与真题
          </router-link>
          <router-link to="/analysis" class="nav-tab" :class="{ 'nav-tab-active': $route.path === '/analysis' }">
            <BarChart3 class="mr-1.5 inline h-4 w-4" />考点分析
          </router-link>
          <router-link to="/cards" class="nav-tab" :class="{ 'nav-tab-active': $route.path === '/cards' }">
            <Layers class="mr-1.5 inline h-4 w-4" />考点讲义
          </router-link>
          <router-link to="/practice" class="nav-tab" :class="{ 'nav-tab-active': $route.path === '/practice' }">
            <CheckSquare class="mr-1.5 inline h-4 w-4" />模拟与批改
          </router-link>
          <!-- 测试版日志入口：桌面客户端默认可见，无需开发者工具或隐藏配置 -->
          <router-link
            v-if="isDesktop"
            to="/logs"
            class="nav-tab"
            :class="{ 'nav-tab-active': $route.path === '/logs' }"
          >
            <ScrollText class="mr-1.5 inline h-4 w-4" />日志
          </router-link>
          <router-link to="/settings" class="nav-tab" :class="{ 'nav-tab-active': $route.path === '/settings' }">
            <Sliders class="mr-1.5 inline h-4 w-4" />设置
          </router-link>
        </nav>

        <div class="flex shrink-0 items-center space-x-2">
          <button
            type="button"
            class="inline-flex cursor-pointer select-none items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors"
            :class="activeCount > 0 ? 'border-blue-300 bg-blue-50 text-blue-700 hover:bg-blue-100' : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'"
            @click="toggleDrawer"
          >
            <ListTodo class="h-4 w-4" :class="{ 'animate-pulse text-blue-600': activeCount > 0 }" />
            <span>任务</span>
            <span v-if="activeCount > 0" class="flex h-4 w-4 items-center justify-center rounded-full bg-blue-600 text-[10px] font-bold text-white">{{ activeCount }}</span>
          </button>
          <!--
            账号名称与退出按钮属于身份敏感区：标注 data-feedback-capture-mask 后，
            反馈截图的克隆与最终位图都会遮挡整块子树（组件原生能力）。
            学习内容区域（main / router-view）不加此标记，仍可正常截图。
          -->
          <span
            v-if="auth.user.value || auth.authRequired.value"
            data-feedback-capture-mask
            class="flex items-center space-x-2"
          >
            <span v-if="auth.user.value" class="hidden text-xs text-slate-500 sm:inline">{{ auth.user.value.username }}</span>
            <button v-if="auth.authRequired.value" type="button" class="rounded-lg px-2 py-1.5 text-xs text-slate-600 hover:bg-slate-100" @click="logout">退出</button>
          </span>
          <button type="button" class="rounded-lg p-2 text-slate-500 hover:bg-slate-100 md:hidden" aria-label="打开导航菜单" @click="mobileMenuOpen = !mobileMenuOpen">
            <Menu v-if="!mobileMenuOpen" class="h-5 w-5" />
            <X v-else class="h-5 w-5" />
          </button>
        </div>
      </div>

      <nav v-if="mobileMenuOpen" class="space-y-1 border-t border-slate-100 py-3 md:hidden" aria-label="移动端主导航">
        <router-link
          v-for="item in mobileLinks"
          :key="item.to"
          :to="item.to"
          class="block rounded-lg px-3 py-2 text-sm font-medium"
          :class="$route.path === item.to ? 'bg-blue-50 text-blue-700' : 'text-slate-600 hover:bg-slate-50'"
          @click="mobileMenuOpen = false"
        >
          <component :is="item.icon" class="mr-2 inline h-4 w-4" />{{ item.label }}
        </router-link>
      </nav>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref, computed, type Component } from 'vue';
import { useRouter } from 'vue-router';
import { BookOpen, FolderArchive, BarChart3, Layers, CheckSquare, ListTodo, Menu, X, CalendarDays, BrainCircuit, Sliders, ScrollText } from 'lucide-vue-next';
import { useTaskManager } from '../composables/useTaskManager';
import { useAuth } from '../composables/useAuth';

const { activeCount, toggleDrawer } = useTaskManager();
const auth = useAuth();
const router = useRouter();

/** 日志页依赖桌面端受限 IPC 通道，浏览器版本不显示该入口（避免出现空白页）。 */
const isDesktop = computed(() =>
  Boolean(typeof window !== 'undefined' && window.desktopAPI?.isDesktop && window.desktopAPI?.queryLogs)
);
const logout = async () => { try { await auth.logout(); await router.replace({ name: 'login' }); } catch { alert(auth.errorMessage.value); } };
const mobileMenuOpen = ref(false);

const mobileLinks = computed<Array<{ to: string; label: string; icon: Component }>>(() => [
  { to: '/today', label: '今日学习', icon: CalendarDays },
  { to: '/recall', label: '回忆卡库', icon: BrainCircuit },
  { to: '/materials', label: '资料与真题', icon: FolderArchive },
  { to: '/analysis', label: '考点分析', icon: BarChart3 },
  { to: '/cards', label: '考点讲义', icon: Layers },
  { to: '/practice', label: '模拟与批改', icon: CheckSquare },
  ...(isDesktop.value ? [{ to: '/logs', label: '日志', icon: ScrollText }] : []),
  { to: '/settings', label: '设置', icon: Sliders },
]);
</script>

<style scoped>
.nav-tab {
  @apply inline-flex items-center rounded-lg px-3 py-2 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900;
}
.nav-tab-active {
  @apply bg-blue-50 font-semibold text-blue-700 hover:bg-blue-100 hover:text-blue-800;
}
</style>
