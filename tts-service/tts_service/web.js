const $ = id => document.getElementById(id);
let audioUrl, connected = false, generating = false;
let checking = false, reconnectTimer, voiceSignature;

function updateButton() {
  $('generate').disabled = !connected || generating;
}

function disconnected() {
  connected = false;
  $('status').textContent = '无法连接语音服务，正在自动重连。请确认服务已启动。';
  updateButton();
}

async function getStatus(path) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    return await fetch(path, { cache: 'no-store', signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

function setChoices(id, choices, defaultValue) {
  const current = $(id).value;
  const selected = choices.some(choice => choice.value === current) ? current : defaultValue;
  $(id).replaceChildren();
  for (const choice of choices) {
    const option = document.createElement('option');
    option.value = choice.value;
    option.textContent = choice.label;
    option.selected = choice.value === selected;
    $(id).append(option);
  }
}

async function checkConnection() {
  if (checking) return;
  checking = true;
  clearTimeout(reconnectTimer);
  try {
    const [healthResponse, voicesResponse] = await Promise.all([getStatus('/health'), getStatus('/voices')]);
    if (!healthResponse.ok || !voicesResponse.ok) {
      connected = false;
      $('status').textContent = '语音服务正在准备，连接恢复后即可生成。';
      updateButton();
      return;
    }
    const health = await healthResponse.json(), voices = await voicesResponse.json();
    if (health.status !== 'ready') throw Error('not ready');
    const signature = JSON.stringify(voices);
    if (voiceSignature !== signature) {
      setChoices('voice', voices.voices.map(voice => ({ value: voice.id, label: voice.name })), voices.default_voice);
      const names = { ja: '日语', zh: '中文', en: '英语', ko: '韩语', yue: '粤语' };
      setChoices('language', voices.languages.map(language => ({ value: language, label: names[language] || language })), voices.default_language);
      $('text').maxLength = voices.max_text_length;
      voiceSignature = signature;
    }
    connected = true;
    const modelName = health.backend.model_name ? ` · ${health.backend.model_name}` : '';
    $('status').textContent = `${health.backend.engine} ${health.backend.version || ''}${modelName} · 服务已就绪`;
    updateButton();
  } catch {
    disconnected();
  } finally {
    checking = false;
    reconnectTimer = setTimeout(checkConnection, 3000);
  }
}

$('form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!connected || generating) return;
  generating = true;
  updateButton();
  $('result').textContent = '正在生成，请稍候…';
  try {
    const response = await fetch('/tts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('text').value, voice: $('voice').value,
                             language: $('language').value, speed: Number($('speed').value) }),
    });
    if (!response.ok) {
      const error = await response.json();
      const detail = error.detail;
      throw Error(typeof detail === 'string' ? detail : (detail?.message || JSON.stringify(detail)));
    }
    const blob = await response.blob();
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = URL.createObjectURL(blob);
    $('audio').src = audioUrl;
    $('audio').hidden = false;
    $('download').href = audioUrl;
    $('download').hidden = false;
    $('result').textContent = `生成完成，用时 ${response.headers.get('X-Synthesis-Seconds')} 秒；音频 ${response.headers.get('X-Audio-Duration')} 秒。`;
  } catch (error) {
    if (error instanceof TypeError || error.name === 'AbortError') {
      disconnected();
      $('result').textContent = '连接已中断，未收到音频。服务恢复后请重新点击生成。';
    } else {
      $('result').textContent = error.message;
    }
  } finally {
    generating = false;
    updateButton();
    if (!connected) checkConnection();
  }
});
window.addEventListener('focus', checkConnection);
checkConnection();
