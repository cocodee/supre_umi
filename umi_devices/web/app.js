'use strict';

/* UMI Studio dashboard — vanilla JS, no build step. */

const $ = (id) => document.getElementById(id);

const app = {
  state: null,
  initialized: false,
  lastStateAt: 0,
  recAnchor: null,
  lastFinalizeKey: null,
  gripperAnalysis: null,
  calibration: null,
  calibrationResult: null,
  lastChartSig: '',
  librarySig: '',
  episodesSig: '',
  activitySig: '',
  confirmResolver: null,
  previewRetry: null,
  counting: false,
};

/* ------------------------------- formatting ------------------------------- */

function fmt(value, digits = 1) {
  return (typeof value === 'number' && Number.isFinite(value))
    ? value.toFixed(digits) : '—';
}

function pct(value) {
  return (typeof value === 'number' && Number.isFinite(value))
    ? `${(value * 100).toFixed(1)}%` : '—';
}

function integer(value) {
  return (typeof value === 'number' && Number.isFinite(value))
    ? Math.round(value).toLocaleString('zh-CN') : '—';
}

function formatTimer(seconds) {
  const value = Math.max(0, seconds);
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  return `${String(minutes).padStart(2, '0')}:${rest.toFixed(1).padStart(4, '0')}`;
}

function timeAgo(unixSeconds) {
  if (!unixSeconds) return '—';
  const delta = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (delta < 60) return '刚刚';
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  const date = new Date(unixSeconds * 1000);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function formatBytes(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function setText(id, value) {
  const element = $(id);
  if (!element) return;
  const text = value == null || value === '' ? '—' : String(value);
  if (element.textContent !== text) element.textContent = text;
}

function setStatus(element, kind, text) {
  if (!element) return;
  element.classList.remove('ok', 'warn', 'off');
  if (kind) element.classList.add(kind);
  const label = element.querySelector('span');
  if (label) label.textContent = text;
}

function setPill(id, textId, kind, text) {
  const pill = $(id);
  const label = $(textId);
  if (pill) {
    pill.classList.remove('ok', 'warn', 'off');
    if (kind) pill.classList.add(kind);
  }
  if (label && label.textContent !== text) label.textContent = text;
}

/* ---------------------------------- api ----------------------------------- */

async function request(url, options = {}) {
  const method = options.method || 'GET';
  const init = { method, cache: 'no-store', headers: {} };
  if (options.body !== undefined && options.body !== null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(url, init);
  let data = null;
  try { data = await response.json(); } catch (error) { /* non-JSON */ }
  if (!response.ok) {
    throw new Error((data && data.error) || `请求失败 (${response.status})`);
  }
  return data;
}

/* --------------------------------- toasts --------------------------------- */

function toast(type, text) {
  const wrap = $('toasts');
  if (!wrap) return;
  const element = document.createElement('div');
  element.className = `toast ${type}`;
  const icon = document.createElement('span');
  icon.className = 't-icon';
  const iconName = type === 'ok' ? 'check' : type === 'info' ? 'wifi' : 'alert';
  icon.innerHTML = `<svg><use href="#i-${iconName}"/></svg>`;
  const span = document.createElement('span');
  span.textContent = text;
  element.append(icon, span);
  wrap.appendChild(element);
  while (wrap.children.length > 4) wrap.firstElementChild.remove();
  setTimeout(() => {
    element.classList.add('out');
    setTimeout(() => element.remove(), 260);
  }, 4400);
}

/* --------------------------------- modals --------------------------------- */

function showModal(id) {
  const modal = $(id);
  if (!modal) return;
  modal.hidden = false;
}

function hideModal(id) {
  const modal = $(id);
  if (modal) modal.hidden = true;
}

function confirmDialog({ title, message, okLabel = '确认', danger = false }) {
  return new Promise((resolve) => {
    app.confirmResolver = resolve;
    setText('confirmTitle', title);
    setText('confirmMessage', message);
    const ok = $('confirmOk');
    ok.textContent = okLabel;
    ok.className = `btn ${danger ? 'danger' : 'primary'}`;
    showModal('confirmModal');
  });
}

function resolveConfirm(value) {
  hideModal('confirmModal');
  if (app.confirmResolver) {
    const resolve = app.confirmResolver;
    app.confirmResolver = null;
    resolve(value);
  }
}

/* --------------------------------- polling -------------------------------- */

async function poll() {
  try {
    const state = await request('/api/state');
    app.state = state;
    app.lastStateAt = performance.now();
    setConnection(true);
    try {
      render(state);
      maybeAnnounceFinalize(state);
    } catch (error) {
      console.error('render failed', error);
    }
    app.initialized = true;
  } catch (error) {
    setConnection(false);
  }
}

function setConnection(online) {
  const conn = $('conn');
  conn.classList.toggle('online', online);
  conn.classList.toggle('offline', !online);
  setText('connText', online ? '实时连接' : '连接中断');
}

function render(state) {
  renderHealth(state);
  renderStage(state);
  renderGripper(state);
  renderTracker(state);
  renderCamera(state);
  renderRecord(state);
  renderEpisodes(state);
  renderActivity(state);
  renderDisk(state);
  renderCalibrationLive(state);
}

function renderHealth(state) {
  const { camera, tracker, gripper } = state.devices;
  if (camera.ok) setPill('pillCamera', 'pillCameraText', 'ok', `${fmt(camera.fps, 0)} fps`);
  else setPill('pillCamera', 'pillCameraText', camera.width ? 'warn' : 'off', '离线');
  if (tracker.ok) setPill('pillTracker', 'pillTrackerText', 'ok', '位姿有效');
  else if (tracker.stale) setPill('pillTracker', 'pillTrackerText', 'off', '数据过期');
  else if (tracker.connected) setPill('pillTracker', 'pillTrackerText', 'warn', '无位姿');
  else setPill('pillTracker', 'pillTrackerText', 'off', '离线');
  if (gripper.ok) setPill('pillGripper', 'pillGripperText', 'ok', '就绪');
  else if (gripper.stale) setPill('pillGripper', 'pillGripperText', 'off', '数据过期');
  else if (gripper.raw_angle != null) setPill('pillGripper', 'pillGripperText', 'warn', '无磁铁');
  else setPill('pillGripper', 'pillGripperText', 'off', '离线');
}

function renderStage(state) {
  const camera = state.devices.camera;
  const recording = !!(state.recording && state.recording.active);
  const stage = $('stage');
  stage.classList.toggle('is-recording', recording);
  stage.classList.toggle('is-offline', !camera.ok);

  setText('stageDevice', camera.name || camera.device || '相机');
  setText('stageRes', camera.width ? `${camera.width}×${camera.height}` : '—');
  setText('stageFps', camera.ok ? `${fmt(camera.fps, 1)} fps` : '— fps');
  setText('stageName', camera.name || camera.device || '—');
  setText('stageRate', camera.ok ? `${fmt(camera.fps, 1)} fps` : '— fps');
  setText('footFps', camera.ok ? `${fmt(camera.fps, 1)} fps` : '—');
  setText('footRes', camera.width ? `${camera.width}×${camera.height}` : '—');
  setText('footFrames', integer(camera.frames));
  setText('footAge', camera.age_s != null ? `${Math.round(camera.age_s * 1000)} ms` : '—');

  const badge = $('liveBadge');
  const badgeLabel = recording ? 'REC' : camera.ok ? 'LIVE' : 'OFFLINE';
  if (badge.dataset.label !== badgeLabel) {
    badge.dataset.label = badgeLabel;
    badge.className = `badge ${recording ? 'rec' : camera.ok ? 'live' : 'off'}`;
    badge.innerHTML = `<i class="dot"></i>${badgeLabel}`;
  }

  const error = $('stageError');
  error.hidden = !!camera.ok;
  if (!camera.ok) setText('stageErrorText', camera.error || '正在尝试连接相机…');

  if (!recording) {
    setText('timecode', new Date().toLocaleTimeString('zh-CN', { hour12: false }));
  }
}

function renderGripper(state) {
  const gripper = state.devices.gripper;
  const width = (typeof gripper.width_mm === 'number') ? gripper.width_mm : null;
  setText('grWidth', width != null ? fmt(width, 1) : '--');
  $('grGauge').style.width = width != null
    ? `${Math.max(0, Math.min(100, (width / 80) * 100))}%` : '0%';
  setText('grSource', gripper.calibration || '理论值');
  const status = gripper.ok ? 'ok' : gripper.stale ? 'off' : gripper.raw_angle != null ? 'warn' : 'off';
  setStatus($('grStatus'), status,
    gripper.ok ? '磁铁正常' : gripper.stale ? '数据过期' : gripper.error ? '离线' : '无磁铁');
  setText('grRaw', gripper.raw_angle != null ? integer(gripper.raw_angle) : '—');
  setText('grSpan', gripper.span != null ? integer(gripper.span) : '—');
  const spanElement = $('grSpan');
  if (spanElement) spanElement.classList.toggle('warn', (gripper.span || 0) > 300);
  setText('grMag', gripper.magnet_detected
    ? (gripper.magnet_too_weak ? '偏弱' : gripper.magnet_too_strong ? '偏强' : '正常')
    : '无');
  setText('grAgc', gripper.agc != null ? gripper.agc : '—');

  const values = (state.history || []).map((item) => item.width).filter((v) => typeof v === 'number');
  const signature = `${values.length}:${values[values.length - 1] || ''}`;
  if (signature !== app.lastChartSig) {
    app.lastChartSig = signature;
    drawSpark($('grChart'), values);
  }
  renderGripperAnalysis();
}

function renderGripperAnalysis() {
  const element = $('gripperAnalysis');
  const data = app.gripperAnalysis;
  if (!data) { element.hidden = true; return; }
  element.hidden = false;
  element.innerHTML = `主频 <b>${fmt(data.dominant_hz, 2)} Hz</b> · ` +
    `跨度 <b>${integer(data.span)}</b> 计数 · ` +
    `标准差 <b>${fmt(data.std, 1)}</b> · ` +
    `采样率 <b>${fmt(data.rate_hz, 0)} Hz</b>`;
}

function quaternionToEuler(x, y, z, w) {
  const roll = Math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y));
  const sinPitch = Math.max(-1, Math.min(1, 2 * (w * y - z * x)));
  const pitch = Math.asin(sinPitch);
  const yaw = Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z));
  const scale = 180 / Math.PI;
  return [roll * scale, pitch * scale, yaw * scale];
}

function renderTracker(state) {
  const tracker = state.devices.tracker;
  setStatus($('trStatus'),
    tracker.ok ? 'ok' : tracker.stale ? 'off' : tracker.connected ? 'warn' : 'off',
    tracker.ok ? '位姿有效' : tracker.stale ? '数据过期' : tracker.connected ? '无位姿' : '离线');
  setText('trRate', tracker.hz ? `${fmt(tracker.hz, 0)} Hz` : '— Hz');
  const position = tracker.position || tracker.pose || null;
  setText('trX', position ? fmt(position[0], 3) : '—');
  setText('trY', position ? fmt(position[1], 3) : '—');
  setText('trZ', position ? fmt(position[2], 3) : '—');
  const pose = tracker.pose;
  if (pose && pose.length >= 7) {
    const [roll, pitch, yaw] = quaternionToEuler(pose[3], pose[4], pose[5], pose[6]);
    setText('trRoll', fmt(roll, 1));
    setText('trPitch', fmt(pitch, 1));
    setText('trYaw', fmt(yaw, 1));
    setText('trQuat', `quat: ${pose[3].toFixed(3)} ${pose[4].toFixed(3)} `
      + `${pose[5].toFixed(3)} ${pose[6].toFixed(3)}`);
  } else {
    setText('trRoll', '—');
    setText('trPitch', '—');
    setText('trYaw', '—');
    setText('trQuat', 'quat: —');
  }
  setText('trResult', tracker.tracking_result || '—');
  setText('trSerial', tracker.serial || '—');
  setText('trBattery', tracker.battery != null ? `${Math.round(tracker.battery * 100)}%` : '—');
  const fill = $('trBattFill');
  fill.style.width = tracker.battery != null ? `${tracker.battery * 100}%` : '0%';
  fill.style.background = tracker.battery != null && tracker.battery < 0.2
    ? 'linear-gradient(90deg,#ef4444,#f87171)' : '';
}

function renderCamera(state) {
  const camera = state.devices.camera;
  setText('camFps', camera.ok ? fmt(camera.fps, 1) : '--');
  setStatus($('camStatus'), camera.ok ? 'ok' : camera.width ? 'warn' : 'off',
    camera.ok ? '在线' : '离线');
  setText('camRes', camera.width ? `${camera.width}×${camera.height}` : '—');
  setText('camFrames', integer(camera.frames));
  setText('camAge', camera.age_s != null ? `${Math.round(camera.age_s * 1000)} ms` : '—');
  document.querySelectorAll('.cam-bars i').forEach((bar) => {
    bar.style.animationPlayState = camera.ok ? 'running' : 'paused';
  });
}

function renderRecord(state) {
  const recording = state.recording || {};
  const session = state.session;
  const camera = state.devices.camera;

  const chip = $('sessionChip');
  chip.classList.remove('ok', 'warn', 'danger');
  if (recording.active) {
    chip.classList.add('warn');
    chip.textContent = `EP ${recording.episode_index}`;
  } else if (session) {
    chip.classList.add('ok');
    chip.textContent = `${session.episodes} episodes`;
  } else {
    chip.textContent = '未开始';
  }

  setText('datasetName', session ? session.name : '尚未创建数据集');
  setText('datasetPath', session ? session.path : '开始录制时会自动创建');
  $('datasetNameField').hidden = !!session;
  $('closeSessionBtn').hidden = !session || !!recording.active || !!recording.finalizing;

  const task = $('taskInput');
  if (document.activeElement !== task && task.value !== state.config.task) {
    task.value = state.config.task || '';
  }

  const resolution = camera.width && camera.height
    ? `${camera.width}×${camera.height}`
    : `${state.config.width}×${state.config.height}`;
  const chips = [
    [state.config.format === 'v2.1' ? 'v2.1' : 'v3.0', true],
    [`${state.config.fps} fps`, true],
    [resolution, true],
    [`Tracker ${state.config.enable_tracker ? '开' : '关'}`, state.config.enable_tracker],
    [`夹爪 ${state.config.enable_gripper ? '开' : '关'}`, state.config.enable_gripper],
    [`缺失阈值 ${fmt(state.config.max_missing_ratio * 100, 0)}%`, false],
  ];
  const summary = $('summaryChips');
  const summarySignature = chips.map(([text]) => text).join('|');
  if (summary.dataset.sig !== summarySignature) {
    summary.dataset.sig = summarySignature;
    summary.innerHTML = chips.map(([text, on]) =>
      `<span class="chip${on ? ' on' : ''}">${text}</span>`).join('');
  }

  const button = $('recordBtn');
  const label = $('recordLabel');
  const sub = $('recordSub');
  const icon = $('recordIcon').querySelector('use');
  if (recording.finalizing) {
    button.className = 'record-btn busy';
    button.disabled = true;
    label.textContent = '正在保存…';
    sub.textContent = '编码视频并写入数据集';
    icon.setAttribute('href', '#i-refresh');
  } else if (recording.active) {
    button.className = 'record-btn is-recording';
    button.disabled = false;
    label.textContent = '结束并保存';
    sub.textContent = '按 R 或点击停止';
    icon.setAttribute('href', '#i-stop');
  } else {
    button.className = 'record-btn';
    button.disabled = !camera.ok;
    label.textContent = camera.ok ? '开始录制' : '相机不可用';
    sub.textContent = camera.ok
      ? (session ? '开始下一段 episode · 3 秒倒计时' : '自动创建数据集 · 3 秒倒计时')
      : (camera.error || '请检查相机连接');
    icon.setAttribute('href', '#i-record');
  }

  $('recPanel').hidden = !recording.active;
  if (recording.active) {
    const summaryData = recording.summary || {};
    setText('recFrames', integer(recording.frames));
    setText('recFps', recording.target_fps != null ? recording.target_fps : '—');
    setText('recCamMiss', pct(summaryData.camera_missing_ratio || 0));
    setText('recPoseBad', pct(summaryData.pose_invalid_ratio || 0));
    setQuality('qcCamBar', 'qcCamText', summaryData.camera_missing_ratio,
      state.config.max_missing_ratio);
    setQuality('qcTrBar', 'qcTrText', summaryData.tracker_missing_ratio,
      state.config.max_missing_ratio);
    setQuality('qcGrBar', 'qcGrText', summaryData.gripper_missing_ratio,
      state.config.max_missing_ratio);
  }

  const last = recording.last;
  const resultElement = $('lastResult');
  if (last && session && !recording.active && !recording.finalizing) {
    resultElement.hidden = false;
    resultElement.classList.remove('warn', 'bad');
    if (last.error) {
      resultElement.classList.add('bad');
      resultElement.textContent = `episode ${last.episode_index} 保存失败：${last.error}`;
    } else if (last.dropped) {
      resultElement.classList.add('warn');
      resultElement.textContent = `episode ${last.episode_index} 未通过 QC，已按设置丢弃`;
    } else {
      const summaryData = last.summary || {};
      resultElement.textContent =
        `episode ${last.episode_index} 已保存 · ${last.frames || 0} 帧 · ` +
        `相机缺失 ${pct(summaryData.camera_missing_ratio || 0)} · ` +
        `位姿无效 ${pct(summaryData.pose_invalid_ratio || 0)}`;
    }
  } else {
    resultElement.hidden = true;
  }

  setText('recordHint', recording.finalizing
    ? '正在编码视频，请稍候…'
    : recording.active
      ? '再次按 R 结束并保存当前 episode'
      : camera.ok ? '相机就绪，开始后 3 秒倒计时' : '相机不可用');

  if (recording.active) {
    if (!app.recAnchor || app.recAnchor.episode !== recording.episode_index) {
      app.recAnchor = {
        episode: recording.episode_index,
        base: recording.elapsed_s || 0,
        at: performance.now(),
      };
    }
  } else {
    app.recAnchor = null;
  }
}

function setQuality(barId, textId, ratio, threshold) {
  const value = typeof ratio === 'number' ? ratio : 0;
  const bar = $(barId);
  const limit = threshold > 0 ? threshold : 0.05;
  bar.style.width = `${Math.min(100, (value / limit) * 100)}%`;
  bar.className = value > limit ? 'bad' : value > limit * 0.7 ? 'warn' : '';
  setText(textId, pct(value));
}

function renderEpisodes(state) {
  const episodes = state.episodes || [];
  setText('episodeCount', episodes.length);
  const list = $('episodeList');
  const signature = episodes.map((ep) =>
    `${ep.episode_index}:${ep.accepted}:${ep.frames}:${ep.camera_missing_ratio}`).join('|');
  if (app.episodesSig === signature) return;
  app.episodesSig = signature;
  list.innerHTML = '';
  if (!episodes.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = '还没有 episode';
    list.appendChild(empty);
    return;
  }
  episodes.slice().reverse().forEach((episode) => {
    const row = document.createElement('div');
    row.className = 'ep-row';

    const index = document.createElement('span');
    index.className = 'ep-idx';
    index.textContent = `#${episode.episode_index}`;

    const meta = document.createElement('div');
    meta.className = 'ep-meta';
    const duration = document.createElement('b');
    duration.textContent = episode.duration_s != null
      ? `${episode.duration_s.toFixed(2)} s` : `${episode.frames || 0} 帧`;
    const frames = document.createElement('span');
    frames.textContent = `${episode.frames || 0} 帧`;
    meta.append(duration, frames);

    const badge = document.createElement('span');
    badge.className = `chip ${episode.accepted ? 'ok' : 'warn'}`;
    badge.textContent = episode.accepted ? 'QC 通过' : 'QC 未通过';

    const missing = document.createElement('span');
    missing.className = 'ep-miss';
    missing.textContent = `缺失 ${pct(episode.camera_missing_ratio || 0)}`;

    row.append(index, meta, badge, missing);
    list.appendChild(row);
  });
}

function renderActivity(state) {
  const messages = (state.messages || []).slice().reverse();
  const list = $('activityList');
  const signature = messages.map((message) => `${message.t}:${message.text}`).join('|');
  if (app.activitySig === signature) return;
  app.activitySig = signature;
  list.innerHTML = '';
  if (!messages.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = '暂无事件';
    list.appendChild(empty);
    return;
  }
  messages.slice(0, 14).forEach((message) => {
    const row = document.createElement('div');
    row.className = `act-row ${message.kind || 'info'}`;
    const time = document.createElement('span');
    time.className = 'act-time';
    time.textContent = new Date(message.t * 1000)
      .toLocaleTimeString('zh-CN', { hour12: false });
    const text = document.createElement('span');
    text.className = 'act-text';
    text.textContent = message.text;
    row.append(time, text);
    list.appendChild(row);
  });
}

function renderDisk(state) {
  const disk = state.disk;
  setText('diskText', disk
    ? `剩余 ${fmt(disk.free_gb, 1)} / ${fmt(disk.total_gb, 1)} GB`
    : '磁盘信息不可用');
  if (state.config) setText('outputRoot', state.config.output_root);
}

/* --------------------------------- charts --------------------------------- */

function drawSpark(canvas, values) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const width = rect.width;
  const height = rect.height || 56;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  ctx.strokeStyle = 'rgba(148,163,184,0.12)';
  ctx.lineWidth = 1;
  for (let row = 0; row <= 2; row += 1) {
    const y = (height * row) / 2;
    ctx.beginPath();
    ctx.moveTo(0, y + 0.5);
    ctx.lineTo(width, y + 0.5);
    ctx.stroke();
  }

  if (!values || values.length < 2) return;

  let min = Math.min(...values);
  let max = Math.max(...values);
  if (max - min < 1) { min -= 0.5; max += 0.5; }
  const pad = (max - min) * 0.22;
  const low = min - pad;
  const high = max + pad;
  const x = (index) => (index / (values.length - 1)) * width;
  const y = (value) => height - 5 - ((value - low) / (high - low)) * (height - 10);

  const gradient = ctx.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, 'rgba(34,211,238,0.32)');
  gradient.addColorStop(1, 'rgba(34,211,238,0.01)');
  ctx.beginPath();
  values.forEach((value, index) => {
    if (index === 0) ctx.moveTo(x(index), y(value));
    else ctx.lineTo(x(index), y(value));
  });
  ctx.lineTo(width, height);
  ctx.lineTo(0, height);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  values.forEach((value, index) => {
    if (index === 0) ctx.moveTo(x(index), y(value));
    else ctx.lineTo(x(index), y(value));
  });
  ctx.strokeStyle = '#22d3ee';
  ctx.lineWidth = 1.8;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();

  const lastX = x(values.length - 1);
  const lastY = y(values[values.length - 1]);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 2.6, 0, Math.PI * 2);
  ctx.fillStyle = '#a5f3fc';
  ctx.fill();
}

/* -------------------------------- recording ------------------------------- */

function countdown(seconds) {
  return new Promise((resolve) => {
    const overlay = $('countdown');
    const value = $('countdownValue');
    let left = seconds;
    let timer = null;
    let finished = false;

    const finish = (result) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      overlay.classList.remove('show');
      setTimeout(() => { overlay.hidden = true; }, 220);
      document.removeEventListener('keydown', onKey);
      overlay.onclick = null;
      resolve(result);
    };
    const onKey = (event) => { if (event.key === 'Escape') finish(false); };

    overlay.hidden = false;
    overlay.classList.remove('show');
    requestAnimationFrame(() => overlay.classList.add('show'));
    document.addEventListener('keydown', onKey);
    overlay.onclick = () => finish(false);

    const step = () => {
      if (left <= 0) { finish(true); return; }
      value.textContent = String(left);
      value.classList.remove('pop');
      void value.offsetWidth;
      value.classList.add('pop');
      left -= 1;
      timer = setTimeout(step, 1000);
    };
    step();
  });
}

async function startFlow() {
  const state = app.state;
  if (!state) return;
  if (state.recording.active) return stopFlow();
  if (state.recording.finalizing) return;
  if (app.counting) return;
  if (!state.devices.camera.ok) {
    toast('error', state.devices.camera.error || '相机不可用，无法录制');
    return;
  }
  app.counting = true;
  const ok = await countdown(3);
  app.counting = false;
  if (!ok) return;
  const body = {};
  if (!state.session) {
    const name = $('datasetNameInput').value.trim();
    if (name) body.name = name;
  }
  try {
    const result = await request('/api/record/start', { method: 'POST', body });
    toast('ok', `开始录制 episode ${result.episode_index} · ${result.task}`);
    $('datasetNameInput').value = '';
    await poll();
    refreshLibrary(true);
  } catch (error) {
    toast('error', error.message);
  }
}

async function stopFlow() {
  try {
    const result = await request('/api/record/stop', { method: 'POST', body: {} });
    toast('info', `episode ${result.episode_index} 已停止，正在保存…`);
    app.recAnchor = null;
    await poll();
  } catch (error) {
    toast('error', error.message);
  }
}

function maybeAnnounceFinalize(state) {
  const last = state.recording && state.recording.last;
  if (!last) return;
  const key = `${state.session ? state.session.path : ''}:${last.episode_index}:${last.saved}:${last.accepted}:${last.dropped}:${last.error || ''}`;
  if (!app.initialized) { app.lastFinalizeKey = key; return; }
  if (app.lastFinalizeKey === key) return;
  app.lastFinalizeKey = key;
  if (last.error) toast('error', `episode ${last.episode_index} 保存失败：${last.error}`);
  else if (last.dropped) toast('warn', `episode ${last.episode_index} 未通过 QC，已丢弃`);
  else if (last.saved) toast('ok', `episode ${last.episode_index} 已保存 · ${last.frames} 帧`);
  refreshLibrary(true);
}

/* -------------------------------- sessions -------------------------------- */

async function closeSessionFlow() {
  const state = app.state;
  if (!state || !state.session) return;
  const ok = await confirmDialog({
    title: '完成数据集',
    message: `将校验并关闭 ${state.session.name}，之后可以创建新的数据集。`,
    okLabel: '完成并校验',
  });
  if (!ok) return;
  try {
    const result = await request('/api/session/close', { method: 'POST', body: {} });
    if (result.problems && result.problems.length) {
      toast('warn', `数据集已关闭，校验发现 ${result.problems.length} 个问题`);
    } else {
      toast('ok', '数据集已关闭，校验通过');
    }
    await poll();
    refreshLibrary(true);
  } catch (error) {
    toast('error', error.message);
  }
}

async function reconnectDevices() {
  try {
    await request('/api/devices/reconnect', { method: 'POST', body: {} });
    toast('info', '正在重新连接设备…');
    setTimeout(() => {
      reloadPreview();
      poll();
    }, 1300);
  } catch (error) {
    toast('error', error.message);
  }
}

function reloadPreview() {
  const preview = $('preview');
  preview.src = `/api/preview.mjpg?t=${Date.now()}`;
}

async function analyzeGripper() {
  const button = $('analyzeBtn');
  button.disabled = true;
  const original = button.textContent;
  button.textContent = '分析中…';
  try {
    const data = await request('/api/gripper/analyze', {
      method: 'POST', body: { seconds: 2 },
    });
    app.gripperAnalysis = data;
    renderGripperAnalysis();
    toast('info', `抖动主频 ${fmt(data.dominant_hz, 1)} Hz，跨度 ${integer(data.span)} 计数`);
  } catch (error) {
    toast('error', error.message);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* --------------------------- gripper calibration -------------------------- */

function openCalibration() {
  app.calibration = null;
  app.calibrationResult = null;
  $('calSetup').hidden = false;
  $('calWizard').hidden = true;
  $('calResult').hidden = true;
  $('calError').hidden = true;
  $('calResultError').hidden = true;
  syncCalibrationMode();
  showModal('calibrationModal');
}

function syncCalibrationMode() {
  const mode = $('calMode').value;
  $('calQuickFields').hidden = mode !== 'quick';
  $('calFullFields').hidden = mode !== 'full';
}

async function calStart() {
  const mode = $('calMode').value;
  const body = { mode, name: $('calName').value.trim() };
  if (mode === 'quick') {
    body.closed_mm = Number($('calClosedMm').value);
    body.open_mm = Number($('calOpenMm').value);
  } else {
    body.levels = $('calLevels').value;
    body.trips = Number($('calTrips').value);
    body.samples = Number($('calSamples').value);
  }
  try {
    app.calibration = await request('/api/calibration/start',
      { method: 'POST', body });
    app.calibrationResult = null;
    $('calError').hidden = true;
    $('calSetup').hidden = true;
    $('calResult').hidden = true;
    $('calWizard').hidden = false;
    renderCalibration();
  } catch (error) {
    $('calError').hidden = false;
    $('calError').textContent = error.message;
  }
}

async function calRecord() {
  try {
    app.calibration = await request('/api/calibration/record',
      { method: 'POST', body: {} });
    renderCalibration();
  } catch (error) {
    toast('error', error.message);
  }
}

async function calUndo() {
  try {
    app.calibration = await request('/api/calibration/undo',
      { method: 'POST', body: {} });
    renderCalibration();
  } catch (error) {
    toast('error', error.message);
  }
}

async function calCancel() {
  const ok = await confirmDialog({
    title: '取消标定', message: '已记录的点会丢失，确定取消？',
    okLabel: '取消标定', danger: true,
  });
  if (!ok) return;
  try { await request('/api/calibration/cancel', { method: 'POST', body: {} }); } catch (e) { /* ignore */ }
  app.calibration = null;
  app.calibrationResult = null;
  $('calWizard').hidden = true;
  $('calResult').hidden = true;
  $('calSetup').hidden = false;
  syncCalibrationMode();
}

async function calFit() {
  try {
    app.calibrationResult = await request('/api/calibration/fit',
      { method: 'POST', body: { name: $('calName').value.trim() } });
    $('calWizard').hidden = true;
    $('calResult').hidden = false;
    $('calResultError').hidden = true;
    renderCalibrationResult();
  } catch (error) {
    $('calResultError').hidden = false;
    $('calResultError').textContent = error.message;
    $('calResult').hidden = false;
  }
}

async function calApply() {
  if (!app.calibrationResult) return;
  try {
    await request('/api/calibration/apply',
      { method: 'POST', body: { path: app.calibrationResult.path } });
    toast('ok', `已应用标定 ${app.calibrationResult.name}`);
    hideModal('calibrationModal');
    app.calibrationResult = null;
    await poll();
  } catch (error) {
    toast('error', error.message);
  }
}

async function calRestart() {
  try { await request('/api/calibration/cancel', { method: 'POST', body: {} }); } catch (e) { /* ignore */ }
  app.calibration = null;
  app.calibrationResult = null;
  $('calWizard').hidden = true;
  $('calResult').hidden = true;
  $('calSetup').hidden = false;
  syncCalibrationMode();
}

function renderCalibration() {
  const state = app.calibration;
  if (!state || !state.active) return;
  const total = state.total || 0;
  const index = state.index || 0;
  const chips = $('calProgressChips');
  chips.innerHTML = `<span class="chip on">进度 ${index}/${total}</span>`
    + (state.mode === 'quick'
      ? '<span class="chip">快速两点</span>'
      : `<span class="chip">多点 ${state.levels ? state.levels.length : ''} 级</span>`);
  const bar = $('calProgressBar');
  if (bar) bar.style.width = total ? `${Math.min(100, (index / total) * 100)}%` : '0%';
  const done = !!state.done;
  $('calRecordBtn').hidden = done;
  $('calFitBtn').hidden = !done;
  if (done) {
    setText('calTarget', '全部点已记录，点击“拟合标定”');
  } else {
    const current = state.current || {};
    setText('calTarget', `${current.label || ''} → ${fmt(current.width_mm, 1)} mm`);
  }
  const list = $('calRecords');
  list.innerHTML = (state.records || []).map((record, i) =>
    `<div class="cal-row"><span>#${i + 1}</span><span>${record.label}</span>`
    + `<span>${fmt(record.width_mm, 1)} mm</span><span>${record.raw_angle} 计数</span></div>`
  ).join('') || '<div class="empty-state">还没有记录</div>';
  list.scrollTop = list.scrollHeight;
}

function renderCalibrationResult() {
  const result = app.calibrationResult;
  if (!result) return;
  const chips = $('calResultChips');
  chips.innerHTML = `<span class="chip on">${result.name}</span>`
    + `<span class="chip">${result.mode === 'quick' ? '两点线性' : '多点分段'}</span>`;
  const metrics = result.metrics || {};
  const lines = [];
  if (metrics.train_rmse_mm != null) lines.push(`训练 RMSE: ${fmt(metrics.train_rmse_mm, 3)} mm`);
  if (metrics.test_rmse_mm != null) lines.push(`留出 RMSE: ${fmt(metrics.test_rmse_mm, 3)} mm`);
  if (metrics.linear_test_rmse_mm != null) lines.push(`线性基线 RMSE: ${fmt(metrics.linear_test_rmse_mm, 3)} mm`);
  if (metrics.n != null) lines.push(`样本数: ${metrics.n}`);
  lines.push(`分段数: ${(result.knots || []).length}`);
  if (result.offsets) lines.push(`迟滞偏置: ${fmt(result.offsets.close, 3)} mm`);
  lines.push(`文件: ${result.path}`);
  $('calMetrics').textContent = lines.join('\n');
}

function renderCalibrationLive(state) {
  const modal = $('calibrationModal');
  if (!modal || modal.hidden) return;
  const wizard = $('calWizard');
  if (!wizard || wizard.hidden) return;
  const gripper = state.devices.gripper || {};
  setText('calLiveWidth', gripper.width_mm != null ? `${fmt(gripper.width_mm, 1)} mm` : '—');
  setText('calLiveRaw', gripper.raw_angle != null ? `${integer(gripper.raw_angle)} 计数` : '—');
}

/* -------------------------------- settings -------------------------------- */

function fillSettings(state) {
  const config = state.config;
  $('setOutputRoot').value = config.output_root || '';
  $('setFps').value = config.fps;
  $('setFormat').value = config.format;
  $('setEnableTracker').checked = !!config.enable_tracker;
  $('setEnableGripper').checked = !!config.enable_gripper;
  $('setCalibration').value = config.calibration_file || '';
  $('setSampleAge').value = config.max_sample_age;
  $('setMissingRatio').value = config.max_missing_ratio;
  $('setPoseRatio').value = config.max_invalid_pose_ratio;
  $('setDropBad').checked = !!config.drop_bad_episodes;
  $('setQcPerFrame').checked = !!config.qc_per_frame;

  const cameraSelect = $('setCamera');
  cameraSelect.innerHTML = '';
  const cameras = (state.cameras || []).slice();
  if (!cameras.some((camera) => camera.device === config.camera)) {
    cameras.unshift({ device: config.camera, name: config.camera });
  }
  cameras.forEach((camera) => {
    const option = document.createElement('option');
    option.value = camera.device;
    option.textContent = `${camera.name || camera.device} · ${camera.device}`;
    cameraSelect.appendChild(option);
  });
  cameraSelect.value = config.camera;

  const resolutionSelect = $('setResolution');
  resolutionSelect.innerHTML = '';
  const values = (state.resolutions || []).slice();
  const current = `${config.width}x${config.height}`;
  if (!values.includes(current)) values.unshift(current);
  values.forEach((value) => {
    const option = document.createElement('option');
    option.value = value;
    const [width, height] = value.split('x');
    option.textContent = `${width}×${height}`;
    resolutionSelect.appendChild(option);
  });
  resolutionSelect.value = current;

  const locked = !!state.session;
  [cameraSelect, resolutionSelect, $('setFps'), $('setFormat')].forEach((element) => {
    element.disabled = locked;
  });
  $('sessionLockNote').hidden = !locked;
  $('settingsError').hidden = true;
}

async function openSettings() {
  if (!app.state) return;
  fillSettings(app.state);
  showModal('settingsModal');
  try {
    const data = await request('/api/calibrations');
    const list = $('calibList');
    list.innerHTML = '';
    (data.files || []).forEach((file) => {
      const option = document.createElement('option');
      option.value = file.path;
      option.label = file.name;
      list.appendChild(option);
    });
    setText('calibHint', data.files && data.files.length
      ? `已发现 ${data.files.length} 个标定文件，可直接输入路径或从列表选择`
      : '未发现标定文件；留空时使用理论值：计数 × 80 / 4096');
  } catch (error) { /* non-fatal */ }
}

async function saveSettings() {
  const errorBox = $('settingsError');
  errorBox.hidden = true;
  const [width, height] = ($('setResolution').value || '640x480').split('x').map(Number);
  const patch = {
    output_root: $('setOutputRoot').value.trim(),
    camera: $('setCamera').value,
    width,
    height,
    fps: Number($('setFps').value),
    format: $('setFormat').value,
    enable_tracker: $('setEnableTracker').checked,
    enable_gripper: $('setEnableGripper').checked,
    calibration_file: $('setCalibration').value.trim(),
    max_sample_age: Number($('setSampleAge').value),
    max_missing_ratio: Number($('setMissingRatio').value),
    max_invalid_pose_ratio: Number($('setPoseRatio').value),
    drop_bad_episodes: $('setDropBad').checked,
    qc_per_frame: $('setQcPerFrame').checked,
  };
  try {
    const result = await request('/api/settings', { method: 'POST', body: patch });
    app.state.config = result.config;
    hideModal('settingsModal');
    toast('ok', '设置已保存');
    await poll();
    refreshLibrary(true);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  }
}

/* --------------------------------- library -------------------------------- */

async function refreshLibrary(silent = true) {
  const button = $('refreshLibrary');
  if (button) button.classList.add('refreshing');
  try {
    const data = await request('/api/datasets');
    renderLibrary(data.datasets || []);
    if (!silent) toast('info', '数据集列表已刷新');
  } catch (error) {
    if (!silent) toast('error', error.message);
  } finally {
    setTimeout(() => { if (button) button.classList.remove('refreshing'); }, 450);
  }
}

function renderLibrary(datasets) {
  const grid = $('libraryGrid');
  const signature = datasets.map((item) =>
    `${item.name}:${item.episodes}:${item.qc_accepted}:${item.modified}`).join('|');
  if (app.librarySig === signature) return;
  app.librarySig = signature;
  grid.innerHTML = '';
  if (!datasets.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state wide';
    empty.textContent = '暂无数据集，开始录制后会出现在这里';
    grid.appendChild(empty);
    return;
  }
  datasets.forEach((dataset) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'ds-card';

    const top = document.createElement('div');
    top.className = 'ds-top';
    const name = document.createElement('b');
    name.textContent = dataset.name;
    const format = document.createElement('span');
    format.className = 'chip ghost';
    format.textContent = dataset.format || '—';
    top.append(name, format);

    const stats = document.createElement('div');
    stats.className = 'ds-stats';
    const pairs = [
      [integer(dataset.episodes), 'episodes'],
      [integer(dataset.frames), 'frames'],
      [dataset.fps != null ? `${dataset.fps} fps` : '—', '帧率'],
    ];
    pairs.forEach(([value, label]) => {
      const cell = document.createElement('div');
      const strong = document.createElement('b');
      strong.textContent = value;
      const span = document.createElement('span');
      span.textContent = label;
      cell.append(strong, span);
      stats.appendChild(cell);
    });

    const foot = document.createElement('div');
    foot.className = 'ds-foot';
    const qc = document.createElement('span');
    const total = dataset.qc_total || 0;
    const accepted = dataset.qc_accepted || 0;
    qc.className = `qc ${total === 0 ? '' : accepted === total ? 'ok' : accepted === 0 ? 'bad' : 'warn'}`;
    qc.innerHTML = '<svg><use href="#i-check"/></svg>';
    qc.append(document.createTextNode(`QC ${accepted}/${total}`));
    const modified = document.createElement('time');
    modified.textContent = timeAgo(dataset.modified);
    foot.append(qc, modified);

    card.append(top, stats, foot);
    card.addEventListener('click', () => openDataset(dataset.name));
    grid.appendChild(card);
  });
}

/* ------------------------------ dataset modal ----------------------------- */

async function openDataset(name) {
  try {
    const data = await request(`/api/dataset?name=${encodeURIComponent(name)}`);
    renderDatasetDetail(data);
    $('validateBtn').dataset.name = name;
    $('browseDatasetBtn').dataset.name = name;
    showModal('datasetModal');
  } catch (error) {
    toast('error', error.message);
  }
}

function renderDatasetDetail(data) {
  setText('dsTitle', data.name);
  setText('dsPath', data.path);
  const info = data.info || {};
  const body = $('dsBody');
  body.innerHTML = '';

  const hero = document.createElement('div');
  hero.className = 'ds-hero';
  const stats = [
    [integer(info.total_episodes), 'episodes'],
    [integer(info.total_frames), 'frames'],
    [info.fps != null ? `${info.fps} fps` : '—', '帧率'],
    [formatBytes(data.size_bytes), '占用空间'],
  ];
  stats.forEach(([value, label]) => {
    const cell = document.createElement('div');
    cell.className = 'ds-stat';
    const strong = document.createElement('b');
    strong.textContent = value;
    const span = document.createElement('span');
    span.textContent = label;
    cell.append(strong, span);
    hero.appendChild(cell);
  });
  body.appendChild(hero);

  const featureSection = document.createElement('div');
  featureSection.className = 'ds-section';
  const featureTitle = document.createElement('h4');
  featureTitle.textContent = '数据字段';
  const features = document.createElement('div');
  features.className = 'feature-list';
  (data.features || []).forEach((feature) => {
    const chip = document.createElement('span');
    chip.className = 'chip ghost';
    chip.textContent = `${feature.key} · ${feature.dtype || '?'}` +
      (feature.shape ? ` [${feature.shape.join(',')}]` : '');
    features.appendChild(chip);
  });
  featureSection.append(featureTitle, features);
  body.appendChild(featureSection);

  const qcSection = document.createElement('div');
  qcSection.className = 'ds-section';
  const qcTitle = document.createElement('h4');
  qcTitle.textContent = `QC 记录（${(data.episodes || []).length}）`;
  qcSection.appendChild(qcTitle);
  if (data.episodes && data.episodes.length) {
    const table = document.createElement('table');
    table.className = 'ds-table';
    table.innerHTML = '<thead><tr><th>#</th><th>时长</th><th>帧数</th>'
      + '<th>相机缺失</th><th>位姿无效</th><th>结果</th></tr></thead>';
    const tbody = document.createElement('tbody');
    data.episodes.forEach((episode) => {
      const row = document.createElement('tr');
      const cells = [
        [episode.episode_index, 'mono'],
        [episode.duration_s != null ? `${episode.duration_s.toFixed(2)} s` : '—', ''],
        [integer(episode.frames), ''],
        [pct(episode.camera_missing_ratio || 0), ''],
        [pct(episode.pose_invalid_ratio || 0), ''],
        [episode.accepted ? '通过' : '未通过', ''],
      ];
      cells.forEach(([value, className]) => {
        const td = document.createElement('td');
        td.textContent = value;
        if (className) td.className = className;
        if (value === '通过') td.style.color = 'var(--success)';
        if (value === '未通过') td.style.color = 'var(--warn)';
        row.appendChild(td);
      });
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    qcSection.appendChild(table);
  } else {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = '还没有 QC 记录';
    qcSection.appendChild(empty);
  }
  body.appendChild(qcSection);
}

async function validateDataset() {
  const name = $('validateBtn').dataset.name;
  if (!name) return;
  const button = $('validateBtn');
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '校验中…';
  try {
    const result = await request('/api/dataset/validate', {
      method: 'POST', body: { name },
    });
    let box = $('dsValidate');
    if (!box) {
      box = document.createElement('div');
      box.id = 'dsValidate';
      $('dsBody').appendChild(box);
    }
    box.className = `validate-result${result.ok ? '' : ' bad'}`;
    box.textContent = result.ok
      ? '校验通过：目录结构与 meta 一致。'
      : `发现 ${result.problems.length} 个问题：\n${result.problems.join('\n')}`;
    toast(result.ok ? 'ok' : 'warn', result.ok ? '校验通过' : '校验发现问题');
  } catch (error) {
    toast('error', error.message);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ------------------------------ dataset viewer ---------------------------- */

const VIEWER_PANELS = {
  width: {
    canvas: 'chartWidth', legend: 'legendWidth', range: 'rangeWidth',
    series: [{ key: 'width', label: '宽度', color: '#22d3ee', unit: 'mm' }],
  },
  position: {
    canvas: 'chartPosition', legend: 'legendPosition', range: 'rangePosition',
    series: [
      { key: 'x', label: 'X', color: '#22d3ee', unit: 'm' },
      { key: 'y', label: 'Y', color: '#818cf8', unit: 'm' },
      { key: 'z', label: 'Z', color: '#fbbf24', unit: 'm' },
    ],
  },
  quaternion: {
    canvas: 'chartQuaternion', legend: 'legendQuaternion', range: 'rangeQuaternion',
    series: [
      { key: 'qx', label: 'QX', color: '#22d3ee', unit: '' },
      { key: 'qy', label: 'QY', color: '#818cf8', unit: '' },
      { key: 'qz', label: 'QZ', color: '#fbbf24', unit: '' },
      { key: 'qw', label: 'QW', color: '#34d399', unit: '' },
    ],
  },
};

const viewer = {
  name: null,
  catalog: null,
  episodes: [],
  index: null,
  data: null,
  cache: new Map(),
  duration: 1,
  time: 0,
  hover: null,
  dragging: false,
  hidden: {
    width: new Set(),
    position: new Set(),
    quaternion: new Set(),
  },
  geo: { width: null, position: null, quaternion: null },
  raf: 0,
  lastDraw: 0,
  token: 0,
};

async function openDatasetViewer(name) {
  const token = ++viewer.token;
  viewer.name = name;
  viewer.catalog = null;
  viewer.episodes = [];
  viewer.index = null;
  viewer.data = null;
  viewer.cache.clear();
  viewer.duration = 1;
  viewer.time = 0;
  viewer.hover = null;
  viewer.dragging = false;
  Object.values(viewer.hidden).forEach((set) => set.clear());
  hideChartTooltip();
  setViewerLoading(true, '加载数据集…');
  setText('viewerTitle', name);
  setText('viewerPath', '—');
  setText('viewerFormat', '—');
  setText('viewerFps', '—');
  setText('viewerEpisodeChip', '—');
  setText('viewerEpisodeCount', '0');
  $('viewerEpisodes').innerHTML = '';
  $('viewerMain').hidden = true;
  showModal('viewerModal');
  resetViewerVideo();
  try {
    const catalog = await request(`/api/dataset/episodes?name=${encodeURIComponent(name)}`);
    if (token !== viewer.token) return;
    viewer.catalog = catalog;
    viewer.episodes = catalog.episodes || [];
    setText('viewerPath', catalog.path || '—');
    setText('viewerFormat', catalog.format || '—');
    setText('viewerFps', catalog.fps != null ? `${catalog.fps} fps` : '—');
    setText('viewerEpisodeCount', viewer.episodes.length);
    renderViewerEpisodes();
    if (!viewer.episodes.length) {
      setViewerLoading(true, '该数据集还没有 episode');
      return;
    }
    await loadViewerEpisode(viewer.episodes[0].index);
  } catch (error) {
    setViewerLoading(true, error.message);
    toast('error', error.message);
  }
}

function closeDatasetViewer() {
  viewer.token += 1;
  stopViewerLoop();
  resetViewerVideo();
  viewer.data = null;
  viewer.hover = null;
  hideChartTooltip();
  hideModal('viewerModal');
}

function setViewerLoading(show, text = '加载数据…') {
  $('viewerLoading').hidden = !show;
  setText('viewerLoadingText', text);
  if (show) $('viewerMain').hidden = true;
}

function resetViewerVideo() {
  const video = $('viewerVideo');
  try { video.pause(); } catch (error) { /* ignore */ }
  video.removeAttribute('src');
  video.removeAttribute('poster');
  try { video.load(); } catch (error) { /* ignore */ }
  video.hidden = false;
  $('viewerVideoEmpty').hidden = true;
  stopViewerLoop();
}

function renderViewerEpisodes() {
  const list = $('viewerEpisodes');
  list.innerHTML = '';
  viewer.episodes.forEach((episode) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `viewer-ep${episode.index === viewer.index ? ' active' : ''}`;
    const index = document.createElement('span');
    index.className = 'vep-index';
    index.textContent = `#${episode.index}`;
    const meta = document.createElement('span');
    meta.className = 'vep-meta';
    const duration = document.createElement('b');
    duration.textContent = `${(episode.duration_s || 0).toFixed(2)} s`;
    const frames = document.createElement('small');
    frames.textContent = `${episode.length || 0} 帧${episode.task ? ` · ${episode.task}` : ''}`;
    meta.append(duration, frames);
    const badge = document.createElement('span');
    if (episode.accepted === true) {
      badge.className = 'chip ok';
      badge.textContent = 'QC';
    } else if (episode.accepted === false) {
      badge.className = 'chip warn';
      badge.textContent = '!';
    } else {
      badge.className = 'chip ghost';
      badge.textContent = '—';
    }
    button.append(index, meta, badge);
    button.addEventListener('click', () => loadViewerEpisode(episode.index));
    list.appendChild(button);
  });
}

async function loadViewerEpisode(index) {
  if (viewer.index === index && viewer.data) return;
  const token = ++viewer.token;
  viewer.index = index;
  viewer.data = null;
  viewer.hover = null;
  viewer.time = 0;
  hideChartTooltip();
  renderViewerEpisodes();
  setViewerLoading(true, `加载 episode ${index}…`);
  resetViewerVideo();
  try {
    let data = viewer.cache.get(index);
    if (!data) {
      data = await request(`/api/dataset/episode?name=${encodeURIComponent(viewer.name)}` +
        `&episode=${index}&max_points=4000`);
      viewer.cache.set(index, data);
    }
    if (token !== viewer.token) return;
    viewer.data = data;
    viewer.duration = Math.max(0.001, data.duration_s || 1);
    setText('viewerEpisodeChip', `Episode ${index} · ${data.length || 0} 帧`);
    setText('viewerHint', data.sampled
      ? `曲线采样 ${data.points}/${data.total_frames} 点（stride ${data.stride}），当前数值取最近采样点；点击曲线可跳转视频`
      : `共 ${data.total_frames} 点；点击或拖动曲线可跳转视频，悬停查看数值`);
    const seek = $('viewerSeek');
    seek.max = viewer.duration;
    seek.step = 0.01;
    seek.value = 0;
    setText('timelineStart', '0.00 s');
    setText('timelineEnd', `${viewer.duration.toFixed(2)} s`);
    configureViewerVideo(data.video || {});
    buildChartLegends();
    updateViewerNow(0);
    setViewerLoading(false);
    $('viewerMain').hidden = false;
    requestAnimationFrame(() => drawViewerCharts());
  } catch (error) {
    setViewerLoading(true, error.message);
    toast('error', error.message);
  }
}

function configureViewerVideo(info) {
  const video = $('viewerVideo');
  const empty = $('viewerVideoEmpty');
  if (info && info.available) {
    video.hidden = false;
    empty.hidden = true;
    if (info.poster) video.poster = info.poster;
    video.src = info.url;
    try { video.load(); } catch (error) { /* ignore */ }
  } else {
    video.hidden = true;
    empty.hidden = false;
    video.removeAttribute('src');
    try { video.load(); } catch (error) { /* ignore */ }
  }
}

function buildChartLegends() {
  if (!viewer.data) return;
  Object.entries(VIEWER_PANELS).forEach(([panel, config]) => {
    const box = $(config.legend);
    box.innerHTML = '';
    config.series.forEach((series) => {
      if (!viewer.data.series[series.key]) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `legend-item${viewer.hidden[panel].has(series.key) ? ' off' : ''}`;
      const dot = document.createElement('i');
      dot.style.background = series.color;
      const label = document.createElement('span');
      label.textContent = series.label;
      button.append(dot, label);
      button.addEventListener('click', () => {
        if (viewer.hidden[panel].has(series.key)) {
          viewer.hidden[panel].delete(series.key);
        } else {
          viewer.hidden[panel].add(series.key);
        }
        button.classList.toggle('off');
        updatePanelRange(panel);
        drawViewerCharts();
      });
      box.appendChild(button);
    });
    updatePanelRange(panel);
  });
}

function updatePanelRange(panel) {
  const config = VIEWER_PANELS[panel];
  if (!viewer.data) {
    setText(config.range, '—');
    return;
  }
  const stats = config.series
    .filter((series) => !viewer.hidden[panel].has(series.key))
    .map((series) => viewer.data.series[series.key] && viewer.data.series[series.key].stats)
    .filter((item) => item && item.min != null && item.max != null);
  if (!stats.length) {
    setText(config.range, '已隐藏');
    return;
  }
  const min = Math.min(...stats.map((item) => item.min));
  const max = Math.max(...stats.map((item) => item.max));
  const unit = config.series[0].unit || '';
  setText(config.range, `${fmtAxis(min)} – ${fmtAxis(max)}${unit ? ` ${unit}` : ''}`);
}

function fmtAxis(value) {
  const absolute = Math.abs(value);
  if (absolute >= 100) return value.toFixed(0);
  if (absolute >= 10) return value.toFixed(1);
  if (absolute >= 1) return value.toFixed(2);
  if (absolute >= 0.01) return value.toFixed(3);
  return value.toFixed(4);
}

function nearestViewerIndex(time) {
  const times = (viewer.data && viewer.data.time) || [];
  if (!times.length) return -1;
  let low = 0;
  let high = times.length - 1;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (times[middle] < time) low = middle + 1;
    else high = middle;
  }
  if (low > 0
      && Math.abs(times[low - 1] - time) < Math.abs(times[low] - time)) {
    low -= 1;
  }
  return low;
}

function updateViewerNow(time) {
  if (!viewer.data) return;
  const index = nearestViewerIndex(time);
  if (index < 0) return;
  const series = viewer.data.series;
  setText('nowTime', `${Math.max(0, time).toFixed(2)} s`);
  const frame = viewer.data.frame && viewer.data.frame[index] != null
    ? Math.round(viewer.data.frame[index]) : '—';
  setText('nowFrame', frame);
  const width = series.width && series.width.values[index];
  setText('nowWidth', width != null ? Number(width).toFixed(2) : '—');
  const x = series.x && series.x.values[index];
  const y = series.y && series.y.values[index];
  const z = series.z && series.z.values[index];
  setText('nowPos', [x, y, z].every((value) => value != null && Number.isFinite(value))
    ? `${Number(x).toFixed(3)} / ${Number(y).toFixed(3)} / ${Number(z).toFixed(3)}`
    : '—');
  const validElement = $('nowValid');
  validElement.classList.remove('ok', 'bad');
  const valid = series.valid && series.valid.values[index];
  if (valid == null) {
    setText('nowValid', '—');
  } else if (valid >= 0.5) {
    validElement.classList.add('ok');
    setText('nowValid', '有效');
  } else {
    validElement.classList.add('bad');
    setText('nowValid', '无效');
  }
  const seek = $('viewerSeek');
  if (document.activeElement !== seek) seek.value = Math.min(viewer.duration, Math.max(0, time));
}

function seekViewer(time, fromVideo = false) {
  if (!viewer.data) return;
  const clamped = Math.max(0, Math.min(viewer.duration, Number(time) || 0));
  viewer.time = clamped;
  const video = $('viewerVideo');
  if (!fromVideo && !video.hidden && video.getAttribute('src')
      && Number.isFinite(video.duration)) {
    try { video.currentTime = clamped; } catch (error) { /* ignore */ }
  }
  const seek = $('viewerSeek');
  if (document.activeElement !== seek) seek.value = clamped;
  updateViewerNow(clamped);
  drawViewerCharts();
}

function drawViewerCharts() {
  if (!viewer.data) return;
  Object.keys(VIEWER_PANELS).forEach((panel) => drawChartPanel(panel));
}

function drawChartPanel(panel) {
  const config = VIEWER_PANELS[panel];
  const canvas = $(config.canvas);
  if (!canvas || !viewer.data) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  if (rect.width < 20 || rect.height < 20) return;
  const dpr = window.devicePixelRatio || 1;
  const width = rect.width;
  const height = rect.height;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 48, right: 10, top: 8, bottom: 18 };
  const plotWidth = Math.max(10, width - pad.left - pad.right);
  const plotHeight = Math.max(10, height - pad.top - pad.bottom);
  const duration = Math.max(viewer.duration, 0.001);
  const times = viewer.data.time || [];
  const visible = config.series.filter((series) =>
    !viewer.hidden[panel].has(series.key) && viewer.data.series[series.key]);

  let min = Infinity;
  let max = -Infinity;
  visible.forEach((series) => {
    (viewer.data.series[series.key].values || []).forEach((value) => {
      if (value != null && Number.isFinite(value)) {
        if (value < min) min = value;
        if (value > max) max = value;
      }
    });
  });
  if (!Number.isFinite(min)) { min = 0; max = 1; }
  if (max - min < 1e-9) {
    const delta = Math.max(Math.abs(max) * 0.05, 0.001);
    min -= delta;
    max += delta;
  }
  const padding = (max - min) * 0.12;
  min -= padding;
  max += padding;

  const x = (time) => pad.left
    + (Math.max(0, Math.min(duration, time)) / duration) * plotWidth;
  const y = (value) => pad.top
    + (1 - (value - min) / (max - min)) * plotHeight;

  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(148,163,184,0.10)';
  for (let row = 0; row <= 2; row += 1) {
    const position = pad.top + (plotHeight * row) / 2;
    ctx.beginPath();
    ctx.moveTo(pad.left, position + 0.5);
    ctx.lineTo(pad.left + plotWidth, position + 0.5);
    ctx.stroke();
  }
  for (let column = 0; column <= 4; column += 1) {
    const position = pad.left + (plotWidth * column) / 4;
    ctx.beginPath();
    ctx.moveTo(position + 0.5, pad.top);
    ctx.lineTo(position + 0.5, pad.top + plotHeight);
    ctx.stroke();
  }

  const valid = viewer.data.series.valid && viewer.data.series.valid.values;
  if (valid) {
    let started = null;
    for (let index = 0; index < valid.length; index += 1) {
      const bad = valid[index] != null && valid[index] < 0.5;
      if (bad && started === null) started = times[index] != null ? times[index] : 0;
      if (started !== null && (!bad || index === valid.length - 1)) {
        const endTime = bad && index === valid.length - 1
          ? duration : (times[index] != null ? times[index] : duration);
        const left = x(started);
        const right = Math.max(x(endTime), left + 1);
        ctx.fillStyle = 'rgba(248,113,113,0.07)';
        ctx.fillRect(left, pad.top, right - left, plotHeight);
        started = null;
      }
    }
  }

  ctx.font = '9px "SFMono-Regular", Consolas, monospace';
  ctx.fillStyle = 'rgba(148,163,184,0.72)';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  ctx.fillText(fmtAxis(max), pad.left - 6, pad.top);
  ctx.fillText(fmtAxis((max + min) / 2), pad.left - 6, pad.top + plotHeight / 2);
  ctx.fillText(fmtAxis(min), pad.left - 6, pad.top + plotHeight);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  ctx.fillText('0 s', pad.left, height - 4);
  ctx.fillText(`${duration.toFixed(1)} s`, pad.left + plotWidth, height - 4);
  if (duration > 4) {
    ctx.fillText(`${(duration / 2).toFixed(1)} s`, pad.left + plotWidth / 2, height - 4);
  }

  visible.forEach((series) => {
    const values = viewer.data.series[series.key].values || [];
    ctx.beginPath();
    let started = false;
    values.forEach((value, index) => {
      if (value == null || !Number.isFinite(value)) { started = false; return; }
      const px = x(times[index] != null ? times[index] : 0);
      const py = y(value);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = series.color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
  });

  const playX = x(viewer.time);
  ctx.strokeStyle = 'rgba(251,91,91,0.9)';
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(playX, pad.top);
  ctx.lineTo(playX, pad.top + plotHeight);
  ctx.stroke();

  if (viewer.hover && viewer.hover.panel === panel) {
    const hoverX = x(viewer.hover.time);
    ctx.strokeStyle = 'rgba(233,238,251,0.35)';
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(hoverX, pad.top);
    ctx.lineTo(hoverX, pad.top + plotHeight);
    ctx.stroke();
    ctx.setLineDash([]);
    const index = nearestViewerIndex(viewer.hover.time);
    visible.forEach((series) => {
      const value = viewer.data.series[series.key].values[index];
      if (value == null || !Number.isFinite(value)) return;
      ctx.beginPath();
      ctx.arc(hoverX, y(value), 3, 0, Math.PI * 2);
      ctx.fillStyle = series.color;
      ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,0.45)';
      ctx.lineWidth = 1;
      ctx.stroke();
    });
  }

  viewer.geo[panel] = {
    left: pad.left, width: plotWidth, height: plotHeight, duration,
  };
}

function viewerTimeFromEvent(panel, event) {
  const geometry = viewer.geo[panel];
  if (!geometry) return 0;
  const canvas = $(VIEWER_PANELS[panel].canvas);
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1,
    (event.clientX - rect.left - geometry.left) / geometry.width));
  return ratio * geometry.duration;
}

function showChartTooltip(panel, time, event) {
  if (!viewer.data) return;
  const index = nearestViewerIndex(time);
  if (index < 0) return;
  const tip = $('chartTooltip');
  tip.innerHTML = '';
  const head = document.createElement('b');
  head.textContent = `${Math.max(0, time).toFixed(2)} s`;
  tip.appendChild(head);
  VIEWER_PANELS[panel].series.forEach((series) => {
    if (viewer.hidden[panel].has(series.key)) return;
    const dataSeries = viewer.data.series[series.key];
    if (!dataSeries) return;
    const value = dataSeries.values[index];
    if (value == null || !Number.isFinite(value)) return;
    const row = document.createElement('div');
    row.className = 'tooltip-row';
    const dot = document.createElement('i');
    dot.style.background = series.color;
    const label = document.createElement('span');
    label.textContent = series.label;
    const number = document.createElement('b');
    const digits = series.unit === 'm' ? 3 : 2;
    number.textContent = `${Number(value).toFixed(digits)}${series.unit ? ` ${series.unit}` : ''}`;
    row.append(dot, label, number);
    tip.appendChild(row);
  });
  tip.hidden = false;
  const offset = 14;
  const margin = 8;
  let left = event.clientX + offset;
  let top = event.clientY + offset;
  const rect = tip.getBoundingClientRect();
  if (left + rect.width > window.innerWidth - margin) {
    left = event.clientX - rect.width - offset;
  }
  if (top + rect.height > window.innerHeight - margin) {
    top = event.clientY - rect.height - offset;
  }
  tip.style.left = `${Math.max(margin, left)}px`;
  tip.style.top = `${Math.max(margin, top)}px`;
}

function hideChartTooltip() {
  $('chartTooltip').hidden = true;
}

function bindViewerChart(panel) {
  const wrap = document.querySelector(`.chart-canvas-wrap[data-panel="${panel}"]`);
  if (!wrap) return;
  wrap.addEventListener('pointermove', (event) => {
    if (!viewer.data) return;
    const time = viewerTimeFromEvent(panel, event);
    viewer.hover = { panel, time };
    showChartTooltip(panel, time, event);
    if (viewer.dragging) seekViewer(time);
    else drawViewerCharts();
  });
  wrap.addEventListener('pointerleave', () => {
    if (viewer.dragging) return;
    viewer.hover = null;
    hideChartTooltip();
    drawViewerCharts();
  });
  wrap.addEventListener('pointerdown', (event) => {
    if (!viewer.data) return;
    event.preventDefault();
    viewer.dragging = true;
    try { wrap.setPointerCapture(event.pointerId); } catch (error) { /* ignore */ }
    seekViewer(viewerTimeFromEvent(panel, event));
  });
  const finish = (event) => {
    viewer.dragging = false;
    try {
      if (wrap.hasPointerCapture(event.pointerId)) wrap.releasePointerCapture(event.pointerId);
    } catch (error) { /* ignore */ }
  };
  wrap.addEventListener('pointerup', finish);
  wrap.addEventListener('pointercancel', finish);
}

function syncViewerFromVideo() {
  const video = $('viewerVideo');
  if (!viewer.data || video.hidden) return;
  const time = Number.isFinite(video.currentTime) ? video.currentTime : 0;
  viewer.time = time;
  const seek = $('viewerSeek');
  if (document.activeElement !== seek) seek.value = Math.min(viewer.duration, time);
  updateViewerNow(time);
  drawViewerCharts();
}

function startViewerLoop() {
  stopViewerLoop();
  const tick = (now) => {
    const video = $('viewerVideo');
    if (!viewer.data || video.hidden || video.paused) {
      viewer.raf = 0;
      return;
    }
    if (viewer.time !== video.currentTime || now - viewer.lastDraw > 60) {
      viewer.time = video.currentTime;
      const seek = $('viewerSeek');
      if (document.activeElement !== seek) {
        seek.value = Math.min(viewer.duration, viewer.time);
      }
      updateViewerNow(viewer.time);
      if (now - viewer.lastDraw > 60) {
        viewer.lastDraw = now;
        drawViewerCharts();
      }
    }
    viewer.raf = requestAnimationFrame(tick);
  };
  viewer.raf = requestAnimationFrame(tick);
}

function stopViewerLoop() {
  if (viewer.raf) {
    cancelAnimationFrame(viewer.raf);
    viewer.raf = 0;
  }
}

function bindViewer() {
  $('viewerBack').addEventListener('click', closeDatasetViewer);
  document.querySelectorAll('[data-viewer-close]').forEach((button) => {
    button.addEventListener('click', closeDatasetViewer);
  });
  $('viewerModal').addEventListener('mousedown', (event) => {
    if (event.target === $('viewerModal')) closeDatasetViewer();
  });
  $('browseDatasetBtn').addEventListener('click', () => {
    const name = $('validateBtn').dataset.name;
    if (name) openDatasetViewer(name);
  });
  ['width', 'position', 'quaternion'].forEach(bindViewerChart);
  $('viewerSeek').addEventListener('input', () => {
    seekViewer(Number($('viewerSeek').value));
  });
  const video = $('viewerVideo');
  video.addEventListener('loadedmetadata', () => {
    if (!viewer.data) return;
    if (viewer.time > 0 && Number.isFinite(video.duration)) {
      try { video.currentTime = Math.min(viewer.time, video.duration); } catch (error) { /* ignore */ }
    }
  });
  video.addEventListener('timeupdate', syncViewerFromVideo);
  video.addEventListener('seeked', syncViewerFromVideo);
  video.addEventListener('play', startViewerLoop);
  video.addEventListener('pause', () => {
    stopViewerLoop();
    syncViewerFromVideo();
  });
  video.addEventListener('ended', () => {
    stopViewerLoop();
    syncViewerFromVideo();
  });
  video.addEventListener('error', () => {
    if (!viewer.data || !viewer.data.video.available) return;
    video.hidden = true;
    $('viewerVideoEmpty').hidden = false;
    toast('warn', '视频加载失败，仍可查看数据曲线');
  });
  window.addEventListener('resize', () => {
    if (!$('viewerModal').hidden) drawViewerCharts();
  });
}

/* --------------------------------- events --------------------------------- */

function isTyping(element) {
  if (!element) return false;
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName) || element.isContentEditable;
}

function bindEvents() {
  $('recordBtn').addEventListener('click', () => {
    const state = app.state;
    if (!state) return;
    if (state.recording.active) stopFlow();
    else startFlow();
  });

  $('closeSessionBtn').addEventListener('click', closeSessionFlow);
  $('reconnectBtn').addEventListener('click', reconnectDevices);
  $('stageRetry').addEventListener('click', reconnectDevices);
  $('settingsBtn').addEventListener('click', openSettings);
  $('saveSettings').addEventListener('click', saveSettings);
  $('refreshLibrary').addEventListener('click', () => refreshLibrary(false));
  $('analyzeBtn').addEventListener('click', analyzeGripper);
  $('calibrateBtn').addEventListener('click', openCalibration);
  $('calMode').addEventListener('change', syncCalibrationMode);
  $('calStartBtn').addEventListener('click', calStart);
  $('calRecordBtn').addEventListener('click', calRecord);
  $('calUndoBtn').addEventListener('click', calUndo);
  $('calCancelBtn').addEventListener('click', calCancel);
  $('calFitBtn').addEventListener('click', calFit);
  $('calApplyBtn').addEventListener('click', calApply);
  $('calRestartBtn').addEventListener('click', calRestart);
  $('validateBtn').addEventListener('click', validateDataset);
  bindViewer();

  $('taskInput').addEventListener('change', async () => {
    const value = $('taskInput').value.trim() || 'umi_demo';
    try {
      await request('/api/settings', { method: 'POST', body: { task: value } });
      if (app.state) app.state.config.task = value;
    } catch (error) {
      toast('error', error.message);
    }
  });

  $('confirmOk').addEventListener('click', () => resolveConfirm(true));
  $('confirmCancel').addEventListener('click', () => resolveConfirm(false));

  document.querySelectorAll('[data-close]').forEach((button) => {
    button.addEventListener('click', () => {
      const modal = button.closest('.modal-backdrop');
      if (modal) hideModal(modal.id);
    });
  });

  ['settingsModal', 'datasetModal'].forEach((id) => {
    const modal = $(id);
    modal.addEventListener('mousedown', (event) => {
      if (event.target === modal) hideModal(id);
    });
  });

  $('preview').addEventListener('error', () => {
    clearTimeout(app.previewRetry);
    app.previewRetry = setTimeout(reloadPreview, 3000);
  });

  document.addEventListener('keydown', (event) => {
    const viewerOpen = !$('viewerModal').hidden;
    if (viewerOpen) {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeDatasetViewer();
        return;
      }
      if (!isTyping(event.target) && event.target.tagName !== 'BUTTON') {
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          event.preventDefault();
          const fps = (viewer.data && viewer.data.fps) || 30;
          seekViewer(viewer.time
            + (event.key === 'ArrowRight' ? 1 / fps : -1 / fps));
          return;
        }
        if (event.key === ' ') {
          const video = $('viewerVideo');
          if (viewer.data && viewer.data.video.available && !video.hidden) {
            event.preventDefault();
            if (video.paused) video.play().catch(() => { /* ignore */ });
            else video.pause();
          }
          return;
        }
      }
      return;
    }
    if (event.key === 'Escape') {
      if (!$('confirmModal').hidden) return resolveConfirm(false);
      if (!$('settingsModal').hidden) return hideModal('settingsModal');
      if (!$('datasetModal').hidden) return hideModal('datasetModal');
      return;
    }
    if (isTyping(event.target)) return;
    if (event.key === 'Escape') return;
    if (event.key.toLowerCase() === 'r' && !event.metaKey && !event.ctrlKey && !event.altKey) {
      const anyModal = document.querySelector('.modal-backdrop:not([hidden])');
      const counting = $('countdown').classList.contains('show');
      if (anyModal || counting) return;
      event.preventDefault();
      const state = app.state;
      if (!state) return;
      if (state.recording.active) stopFlow();
      else startFlow();
    }
  });
}

function startIntervals() {
  setInterval(() => { if (!document.hidden) poll(); }, 600);
  setInterval(() => { if (!document.hidden) refreshLibrary(true); }, 20000);
  setInterval(() => {
    if (app.recAnchor && app.state && app.state.recording.active) {
      const elapsed = app.recAnchor.base + (performance.now() - app.recAnchor.at) / 1000;
      setText('recTimer', formatTimer(elapsed));
      setText('timecode', `REC ${formatTimer(elapsed)}`);
    } else {
      setText('timecode', new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    }
  }, 100);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
}

function init() {
  bindEvents();
  drawSpark($('grChart'), []);
  startIntervals();
  poll();
  refreshLibrary(true);
}

init();
