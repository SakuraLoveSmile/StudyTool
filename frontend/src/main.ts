import { createApp } from 'vue';
import './style.css';
import App from './App.vue';
import router from './router';
import { installFrontendDiagnostics, setDiagnosticsContext } from './utils/diagnostics';
import { useTheme } from './composables/useTheme';
import { initializeInstallApp } from './composables/useInstallApp';

// 挂载前初始化主题：data-theme 已由 index.html 内联脚本/桌面 preload 写入，
// 这里接管后续的偏好变更、系统跟随与多标签同步。
useTheme();
initializeInstallApp();

const app = createApp(App);
app.use(router);

// Test-build diagnostics: Vue component errors and unhandled promise rejections are
// reported over the restricted desktop IPC channel. Installed before mount so failures
// during the first render are captured. Only the route NAME is attached as context —
// never the query string, which can carry user input.
setDiagnosticsContext(() => ({ route: String(router.currentRoute.value.name ?? 'unknown') }));
installFrontendDiagnostics(app);

app.mount('#app');
