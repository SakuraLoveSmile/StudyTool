const form = document.getElementById('connection-form');
const input = document.getElementById('origin');
const save = document.getElementById('save');
const retry = document.getElementById('retry');
const error = document.getElementById('error');
const status = document.getElementById('status');

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

async function submit(action) {
  save.disabled = retry.disabled = input.disabled = true;
  showError('');
  status.hidden = false;
  status.textContent = '正在连接学习服务…';
  try {
    const result = await action();
    if (!result.ok) { showError(result.error); status.hidden = true; }
  } catch {
    showError('连接设置暂时无法使用，请重新打开客户端。');
    status.hidden = true;
  } finally {
    save.disabled = retry.disabled = input.disabled = false;
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  void submit(() => window.connectedSettings.save(input.value));
});
retry.addEventListener('click', () => void submit(() => window.connectedSettings.retry()));

window.connectedSettings.getState().then(state => {
  input.value = state.origin;
  retry.hidden = !state.origin;
  showError(state.error);
  input.focus();
}).catch(() => showError('连接设置暂时无法使用，请重新打开客户端。'));
