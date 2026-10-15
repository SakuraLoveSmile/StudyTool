import { createRouter, createWebHistory } from 'vue-router';
import MaterialsView from '../views/MaterialsView.vue';
import AnalysisView from '../views/AnalysisView.vue';
import StudyCardsView from '../views/StudyCardsView.vue';
import PracticeView from '../views/PracticeView.vue';
import TodayLearningView from '../views/TodayLearningView.vue';
import RecallCardsView from '../views/RecallCardsView.vue';
import LoginView from '../views/LoginView.vue';
import ChangePasswordView from '../views/ChangePasswordView.vue';
import { useAuth } from '../composables/useAuth';
import { installLogNavigation } from '../composables/useLogNavigation';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/library',
      name: 'library',
      component: () => import('../views/KnowledgeLibraryView.vue'),
      meta: { requiresAuth: true },
    },
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    { path: '/change-password', name: 'change-password', component: ChangePasswordView, meta: { public: true } },
    {
      path: '/',
      redirect: '/today',
    },
    {
      path: '/today',
      name: 'today',
      component: TodayLearningView,
      meta: { requiresAuth: true },
    },
    {
      path: '/recall',
      name: 'recall',
      component: RecallCardsView,
      meta: { requiresAuth: true },
    },
    {
      path: '/materials',
      name: 'materials',
      component: MaterialsView,
      meta: { requiresAuth: true },
    },
    {
      path: '/analysis',
      name: 'analysis',
      component: AnalysisView,
      meta: { requiresAuth: true },
    },
    {
      path: '/cards',
      name: 'cards',
      component: StudyCardsView,
      meta: { requiresAuth: true },
    },
    {
      path: '/practice',
      name: 'practice',
      component: PracticeView,
      meta: { requiresAuth: true },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/SettingsView.vue'),
      meta: { requiresAuth: true },
    },
    {
      // Test-build diagnostic viewer. Lazy-loaded so the web bundle stays untouched;
      // the page itself explains that logs are only available in the desktop client.
      path: '/logs',
      name: 'logs',
      component: () => import('../views/LogsView.vue'),
      meta: { requiresAuth: true },
    },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuth();
  await auth.initialise();
  if (to.meta.public) {
    if (to.name === 'change-password' && auth.authRequired.value && !auth.user.value) return { name: 'login' };
    if (to.name === 'login' && auth.isAuthenticated.value && auth.authRequired.value) return '/today';
    return true;
  }
  if (to.meta.requiresAuth && auth.authRequired.value && !auth.user.value) {
    return { name: 'login', query: { redirect: to.fullPath } };
  }
  if (to.meta.requiresAuth && auth.user.value?.must_change_password && to.name !== 'change-password') {
    return { name: 'change-password', query: { redirect: to.fullPath } };
  }
  return true;
});

/**
 * T2a 外壳级日志导航处理：注册 router.onError / beforeEach / afterEach 监听，
 * 捕获导航失败与懒加载 chunk 加载失败，经 diagnostics.report 上报，并驱动
 * 外壳提示条（LogNavNotice）。幂等；main.ts 再调一次也安全。只监听、不拦截导航。
 */
installLogNavigation(router);

export default router;
