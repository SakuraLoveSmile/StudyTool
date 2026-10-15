import { computed, readonly, ref } from 'vue';

interface InstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>;
}

const installed = ref(false);
const prompting = ref(false);
const message = ref('');
const deferredPrompt = ref<InstallPromptEvent | null>(null);
let initialized = false;

function isDesktop(): boolean {
  return Boolean(window.desktopAPI?.isDesktop);
}

function instructions(): string {
  if (!window.isSecureContext) {
    return '添加到桌面需要使用 HTTPS 安全网址。请通过安全网址打开；当前仍可正常在线学习。';
  }
  return '安卓：用 Chrome 打开，在右上角菜单选择“添加到主屏幕”或“安装应用”。Windows：用 Edge 打开，在菜单“应用”中选择“将此站点作为应用安装”。请保持联网使用。';
}

/** Called before mounting so the browser install event cannot outrun the button. */
export function initializeInstallApp(): void {
  if (initialized || typeof window === 'undefined') return;
  initialized = true;
  if (isDesktop()) return;

  const standalone = window.matchMedia('(display-mode: standalone)');
  installed.value = standalone.matches;
  standalone.addEventListener('change', (event) => { installed.value = event.matches; });
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredPrompt.value = event as InstallPromptEvent;
    message.value = '';
  });
  window.addEventListener('appinstalled', () => {
    installed.value = true;
    deferredPrompt.value = null;
    message.value = '';
  });

  // Dev runs do not leave a worker behind. Use a production preview on localhost
  // for installation checks; localhost is a browser-recognized secure context.
  if (import.meta.env.PROD && window.isSecureContext && 'serviceWorker' in navigator) {
    void navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' })
      .catch(() => {
        // Installation is optional: an unavailable worker must not block learning.
        message.value = '暂时无法准备桌面入口，可以继续在线学习，稍后刷新再试。';
      });
  }
}

export function useInstallApp() {
  initializeInstallApp();

  async function install(): Promise<void> {
    if (prompting.value) return;
    const prompt = deferredPrompt.value;
    if (!prompt) {
      message.value = instructions();
      return;
    }
    prompting.value = true;
    deferredPrompt.value = null;
    try {
      await prompt.prompt();
      const choice = await prompt.userChoice;
      message.value = choice.outcome === 'accepted'
        ? '已确认添加，请按浏览器提示完成。打开后需保持联网。'
        : '已取消添加，你可以继续在线学习，也可以稍后从浏览器菜单添加。';
    } catch {
      message.value = instructions();
    } finally {
      prompting.value = false;
    }
  }

  return {
    visible: computed(() => typeof window !== 'undefined' && !isDesktop() && !installed.value),
    prompting: readonly(prompting),
    message: readonly(message),
    install,
  };
}
