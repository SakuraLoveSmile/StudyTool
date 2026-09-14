import { computed, ref } from 'vue';
import { apiClient, sessionTransport, type AuthUser } from '../api/client';
import { useTaskManager } from './useTaskManager';

const user = ref<AuthUser | null>(null);
const authRequired = ref(true);
const ready = ref(false);
const loading = ref(false);
const errorMessage = ref('');
const sessionEpoch = ref(0);
const sessionChannel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('rag-session') : null;

function clearLocalSession() {
  // Optional browser storage must never prevent revoking local UI state.
  try {
    if (typeof window !== 'undefined') {
      Object.keys(window.localStorage).filter((key) => key.startsWith('practice_draft_')).forEach((key) => window.localStorage.removeItem(key));
    }
  } catch { /* Storage may be disabled in a private browser context. */ }
  finally {
    sessionTransport.reset();
    useTaskManager().reset();
    sessionEpoch.value += 1;
    user.value = null;
  }
}

if (sessionChannel) sessionChannel.onmessage = () => {
  clearLocalSession();
  ready.value = false;
  // Cookies are shared between tabs. Recreate the page under the new identity.
  window.location.reload();
};

function notifyOtherTabs() { sessionChannel?.postMessage('session-changed'); }

export function useAuth() {
  const isAuthenticated = computed(() => !authRequired.value || user.value !== null);

  const apply = (response: Awaited<ReturnType<typeof apiClient.getCurrentUser>>) => {
    if (typeof response.auth_required === 'boolean') authRequired.value = response.auth_required;
    else if (response.user) authRequired.value = true;
    user.value = response.user;
    sessionTransport.setCsrfToken(response.csrf_token);
  };

  const initialise = async () => {
    if (ready.value) return;
    const epoch = sessionTransport.epoch;
    try {
      const response = await apiClient.getCurrentUser();
      if (epoch !== sessionTransport.epoch) return;
      apply(response);
    } catch (error) {
      if (epoch !== sessionTransport.epoch) return;
      if ((error as { response?: { status?: number } })?.response?.status === 401) {
        authRequired.value = true;
        user.value = null;
      } else {
        errorMessage.value = '无法连接服务器，请稍后重试';
      }
    } finally {
      if (epoch === sessionTransport.epoch) ready.value = true;
    }
  };

  const login = async (username: string, password: string) => {
    loading.value = true;
    errorMessage.value = '';
    try {
      sessionTransport.reset();
      useTaskManager().reset();
      apply(await apiClient.login(username, password));
      sessionEpoch.value += 1;
      ready.value = true;
      notifyOtherTabs();
    } catch (error) {
      errorMessage.value = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '用户名或密码错误';
      throw error;
    } finally {
      loading.value = false;
    }
  };

  const changePassword = async (currentPassword: string, newPassword: string) => {
    loading.value = true;
    errorMessage.value = '';
    try {
      apply(await apiClient.changePassword(currentPassword, newPassword));
      notifyOtherTabs();
    } finally {
      loading.value = false;
    }
  };

  const logout = async (options: { remote?: boolean } = {}) => {
    if (options.remote !== false) {
      try { await apiClient.logout(); } catch (error) {
        if ((error as { response?: { status?: number } })?.response?.status !== 401) {
          errorMessage.value = '退出请求失败，请检查网络后重试';
          throw error;
        }
      }
    }
    clearLocalSession();
    notifyOtherTabs();
    if (authRequired.value) ready.value = true;
  };

  return { user, authRequired, ready, loading, errorMessage, sessionEpoch, isAuthenticated, initialise, login, logout, changePassword };
}
