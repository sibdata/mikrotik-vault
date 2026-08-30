const $ = (selector) => document.querySelector(selector);
const formatDate = (date) => date ? new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(date)) : 'Ещё не запускался';
const formatBytes = (size) => { if (!size) return '0 Б'; const units = ['Б','КБ','МБ','ГБ']; let i = 0; while (size > 1024 && i < 3) { size /= 1024; i++; } return `${size.toFixed(i ? 1 : 0)} ${units[i]}`; };
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const statusName = (status) => ({success:'Готово',failed:'Ошибка',partial:'Частично',running:'Выполняется',never:'Нет копий'}[status] || status);
const stageName = (stage) => ({queued:'Ожидание запуска',ssh_connect:'Подключение к SSH',create_files:'Создание файлов RouterOS',download:'Скачивание с MikroTik',upload_backup:'Загрузка .backup в S3',upload_export:'Загрузка .rsc в S3',uploaded:'Файл загружен'}[stage] || 'Выполнение операции');
let data = { routers: [], backups: [], backup_runs: [] };
let editingRouterId = null;
const pendingBackups = new Set();
let errorRetry = null;

function ensureErrorDialog() {
  let dialog = $('#errorDialog');
  if (dialog) return dialog;
  dialog = document.createElement('dialog');
  dialog.id = 'errorDialog';
  dialog.className = 'error-dialog';
  dialog.innerHTML = `<div class="error-dialog-shell"><div class="error-signal"><span>!</span><i></i></div><div class="error-dialog-copy"><span class="eyebrow">ACTION REQUIRED</span><h2 id="errorDialogTitle">Не удалось выполнить действие</h2><p id="errorDialogMessage"></p><details id="errorDialogDetails"><summary>Технические подробности</summary><code id="errorDialogCode"></code></details></div><div class="error-dialog-actions"><button type="button" class="primary" id="errorRetry">Повторить <span>↻</span></button><button type="button" class="ghost" id="errorReload">Обновить страницу</button><button type="button" class="danger-ghost" id="errorLogout">Выйти и войти снова</button></div><button type="button" class="icon error-close" id="errorClose" aria-label="Закрыть">×</button></div>`;
  document.body.appendChild(dialog);
  $('#errorClose').onclick = () => dialog.close();
  $('#errorReload').onclick = () => location.reload();
  $('#errorRetry').onclick = () => { const retry = errorRetry; dialog.close(); if (retry) retry(); else location.reload(); };
  $('#errorLogout').onclick = async () => { try { await fetch('/api/logout', {method:'POST'}); } finally { location.replace('/login'); } };
  return dialog;
}

function showErrorDialog(message, options = {}) {
  const dialog = ensureErrorDialog();
  $('#errorDialogTitle').textContent = options.title || 'Не удалось выполнить действие';
  $('#errorDialogMessage').textContent = message || 'Произошла неизвестная ошибка. Попробуйте повторить действие.';
  const details = $('#errorDialogDetails');
  $('#errorDialogCode').textContent = options.code || '';
  details.hidden = !options.code;
  errorRetry = options.retry || null;
  $('#errorRetry').firstChild.textContent = options.retryLabel || 'Повторить ';
  if (!dialog.open) dialog.showModal();
}

function toast(message, kind = 'success') {
  const element = $('#toast'); element.textContent = message; element.dataset.kind = kind; element.classList.add('show');
  setTimeout(() => element.classList.remove('show'), 4000);
}

async function api(url, options = {}) {
  let response;
  try { response = await fetch(url, { headers: {'Content-Type':'application/json'}, ...options }); }
  catch (error) { showErrorDialog('Сервер недоступен или соединение прервано.', {title:'Нет связи с RouterVault', code:error.message}); throw error; }
  if (response.status === 401) { const error = new Error('Сессия завершена. Войдите снова, чтобы продолжить.'); showErrorDialog(error.message, {title:'Требуется повторный вход', retryLabel:'Перейти ко входу', retry:()=>location.replace('/login'), code:`HTTP 401 · ${url}`}); throw error; }
  if (!response.ok) { let message = 'Ошибка запроса'; try { const body = await response.json(); message = body.detail || message; } catch {} const error = new Error(message); showErrorDialog(message, {code:`HTTP ${response.status} · ${url}`}); throw error; }
  return response.status === 204 ? null : response.json();
}

window.addEventListener('unhandledrejection', event => showErrorDialog(event.reason?.message || 'Необработанная ошибка интерфейса.', {code:String(event.reason || '')}));
window.addEventListener('error', event => showErrorDialog(event.message || 'Ошибка выполнения интерфейса.', {code:`${event.filename || 'app'}:${event.lineno || 0}`}));

function render() {
  const runs = data.backup_runs || [];
  const successful = runs.filter(item => item.status === 'success').length;
  const completed = runs.filter(item => item.status !== 'running').length;
  $('#routerCount').textContent = data.routers.length; $('#navCount').textContent = data.routers.length;
  $('#successRate').textContent = completed ? `${Math.round(successful / completed * 100)}%` : '—';
  $('#storage').textContent = formatBytes(data.backups.reduce((sum, item) => sum + (item.size || 0), 0)); $('#schedule').textContent = data.schedule; $('#scheduleTimezone').textContent = data.timezone || 'UTC';
  const health = data.system_status || {}; const healthTitle = document.querySelector('.status-strip p b'), healthText = document.querySelector('.status-strip p small');
  healthTitle.textContent = health.state === 'running' ? `Выполняется резервное копирование: ${health.running_devices || 0}` : health.state === 'degraded' ? `Требуют внимания: ${health.failed_devices || 0}` : 'Все системы работают';
  healthText.textContent = `${health.storage_configured ? 'S3 подключён' : 'S3 не настроен'} · ${health.scheduler_running ? 'планировщик работает' : 'планировщик остановлен'}${health.last_system_backup_at ? ` · база: ${formatDate(health.last_system_backup_at)}` : ''}`;
  $('#emptyRouters').hidden = data.routers.length > 0;
  data.routers.forEach(router => { if (router.last_status !== 'running') pendingBackups.delete(router.id); });
  $('#routers').innerHTML = data.routers.map(router => { const busy = router.last_status === 'running' || pendingBackups.has(router.id); return `<div class="row"><div class="device"><span class="device-icon">⌁</span><div><b>${escapeHtml(router.name)}</b><div class="mono">ID ${escapeHtml(router.identity || '—')} · ${escapeHtml(router.host)}:${router.port}</div></div></div><div class="meta"><small>RouterOS</small>${escapeHtml(router.routeros_version || 'Проверите доступ')}<div class="mono">user: ${escapeHtml(router.username)}</div></div><div class="meta"><small>Последний бэкап</small>${formatDate(router.last_backup_at)}</div><div class="actions"><span class="status ${busy ? 'running' : router.last_status}">${busy ? 'Выполняется' : statusName(router.last_status)}</span><button class="icon" title="Редактировать" aria-label="Редактировать ${escapeHtml(router.name)}" onclick="editRouter('${router.id}')" ${busy ? 'disabled' : ''}>✎</button><button class="icon check-access" title="Проверить доступ" aria-label="Проверить доступ к ${escapeHtml(router.name)}" onclick="checkSavedRouter('${router.id}',this)" ${busy ? 'disabled' : ''}>⌁</button><button class="icon" title="${busy ? 'Бэкап уже выполняется' : 'Создать бэкап'}" aria-label="Создать бэкап ${escapeHtml(router.name)}" onclick="backup('${router.id}')" ${busy ? 'disabled' : ''}>${busy ? '…' : '↻'}</button><button class="icon" title="Удалить" aria-label="Удалить ${escapeHtml(router.name)}" onclick="removeRouter('${router.id}')" ${busy ? 'disabled' : ''}>×</button></div></div>`; }).join('');
  $('#emptyBackups').hidden = runs.length > 0;
  $('#backups').innerHTML = runs.map(run => `<div class="run-entry"><div class="row"><div class="device"><span class="device-icon">▣</span><div><b>${escapeHtml(run.router_name)}</b><div class="mono">${escapeHtml(run.host)} · ${run.artifacts.length} ${run.artifacts.length === 1 ? 'файл' : 'файла'}${run.encrypted ? ' · AES' : ''}</div></div></div><div class="meta"><small>Создан</small>${formatDate(run.created_at)}${run.attempts > 1 ? `<div class="mono">SSH-попыток: ${run.attempts}</div>` : ''}</div><div class="meta"><small>Общий размер</small>${formatBytes(run.size)}</div><div class="actions"><span class="status ${run.status}">${statusName(run.status)}</span>${run.artifacts.map(file => `<button class="download-action" title="Скачать ${file.format === 'backup' ? '.backup' : '.rsc'}${file.sha256 ? ` · SHA-256 ${file.sha256}` : ''}" onclick="downloadBackup('${file.id}', '${file.format}', this)"><span>↓</span><b>Скачать ${file.format === 'backup' ? '.backup' : '.rsc'}</b></button>`).join('')}${run.encrypted ? `<button class="icon key-action" title="Скопировать пароль .backup" onclick="copyBackupPassword('${run.id}')">⌘</button>` : ''}${['failed','partial'].includes(run.status) ? `<button class="icon retry-action" title="Повторить запуск" onclick="backup('${run.router_id}')">↻</button>` : ''}</div></div>${run.status === 'running' ? `<div class="run-detail active"><span>●</span><b>${stageName(run.stage)}</b><small>Страница обновится автоматически</small></div>` : ''}${run.error ? `<div class="run-detail error-detail"><span>!</span><b>${stageName(run.stage)}</b><small>${escapeHtml(run.error)}</small></div>` : ''}</div>`).join('');
}

async function load() { try { data = await api('/api/dashboard'); render(); } catch (error) { toast(error.message, 'error'); } }
async function backup(id) { if (pendingBackups.has(id)) return; pendingBackups.add(id); render(); try { await api(`/api/routers/${id}/backup`, {method:'POST'}); toast('Бэкап запущен'); await load(); } catch (error) { pendingBackups.delete(id); render(); toast(error.message, 'error'); } }
async function copyBackupPassword(runId) { try { const result = await api(`/api/backup-runs/${encodeURIComponent(runId)}/password`); await navigator.clipboard.writeText(result.password); toast('Пароль восстановления скопирован'); } catch (error) { toast(error.message, 'error'); } }
function downloadBackup(id, format, button) {
  if (button.disabled) return;
  const original = button.innerHTML;
  button.disabled = true;
  button.classList.add('downloading');
  button.innerHTML = '<span class="download-spinner"></span><b>Скачивание…</b>';
  toast(`Загрузка ${format === 'backup' ? '.backup' : '.rsc'} началась`);
  const link = document.createElement('a');
  link.href = `/api/backups/${encodeURIComponent(id)}/download`;
  link.download = '';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => { button.disabled = false; button.classList.remove('downloading'); button.innerHTML = original; }, 1800);
}
async function checkSavedRouter(id, button) { const previous = button.textContent; button.disabled = true; button.textContent = '…'; try { const result = await api(`/api/routers/${id}/check`, {method:'POST'}); toast(`Доступ подтверждён: ${result.identity}`); await load(); } catch (error) { toast(error.message, 'error'); } finally { button.disabled = false; button.textContent = previous; } }
async function removeRouter(id) { if (!confirm('Удалить устройство и историю? Файлы в S3 останутся.')) return; try { await api(`/api/routers/${id}`, {method:'DELETE'}); toast('Устройство удалено'); load(); } catch (error) { toast(error.message, 'error'); } }

const routerForm = $('#routerForm');
const submitButton = routerForm.querySelector('.submit');
const checkButton = document.createElement('button'); checkButton.type = 'button'; checkButton.className = 'connection-check'; checkButton.innerHTML = '<span>⌁</span><b>Проверить подключение</b><i>SSH + SFTP</i>';
routerForm.insertBefore(checkButton, submitButton);

function routerPayload() { const values = Object.fromEntries(new FormData(routerForm)); values.port = Number(values.port); return values; }
checkButton.onclick = async () => { $('#formError').textContent = ''; checkButton.disabled = true; checkButton.classList.add('loading'); checkButton.querySelector('b').textContent = 'Проверяем доступ…'; try { const payload = routerPayload(); let result; if (editingRouterId && !payload.password) result = await api(`/api/routers/${editingRouterId}/check`, {method:'POST'}); else result = await api('/api/routers/check', {method:'POST', body:JSON.stringify(payload)}); checkButton.classList.add('passed'); checkButton.querySelector('b').textContent = `Подключение установлено`; checkButton.querySelector('i').textContent = result.identity; toast('SSH и SFTP доступны'); } catch (error) { checkButton.classList.remove('passed'); checkButton.querySelector('b').textContent = 'Проверка не пройдена'; checkButton.querySelector('i').textContent = 'Исправьте настройки и повторите'; $('#formError').textContent = error.message; } finally { checkButton.disabled = false; checkButton.classList.remove('loading'); } };

function resetRouterDialog() { $('#formError').textContent = ''; checkButton.classList.remove('passed'); checkButton.querySelector('b').textContent = 'Проверить подключение'; checkButton.querySelector('i').textContent = 'SSH + SFTP'; }
$('#addBtn').onclick = () => { editingRouterId = null; routerForm.reset(); routerForm.password.required = true; routerForm.password.placeholder = '••••••••••'; routerForm.querySelector('.dialog-head .eyebrow').textContent = 'NEW ENDPOINT'; routerForm.querySelector('.dialog-head h2').textContent = 'Подключить MikroTik'; routerForm.querySelector('.dialog-head p').textContent = 'Добавьте устройство в защищённый контур резервного копирования.'; submitButton.innerHTML = 'Подключить устройство <span>→</span>'; resetRouterDialog(); $('#routerDialog').showModal(); };
function editRouter(id) { const router = data.routers.find(item => item.id === id); if (!router) return; editingRouterId = id; routerForm.reset(); routerForm.name.value = router.name; routerForm.host.value = router.host; routerForm.port.value = router.port; routerForm.username.value = router.username; routerForm.password.required = false; routerForm.password.placeholder = 'Оставьте пустым без изменений'; routerForm.querySelector('.dialog-head .eyebrow').textContent = 'EDIT ENDPOINT'; routerForm.querySelector('.dialog-head h2').textContent = 'Настроить MikroTik'; routerForm.querySelector('.dialog-head p').textContent = 'Перед сохранением RouterVault повторно проверит SSH и SFTP.'; submitButton.innerHTML = 'Сохранить изменения <span>→</span>'; resetRouterDialog(); $('#routerDialog').showModal(); }
$('#closeDialog').onclick = () => $('#routerDialog').close();
$('#copyRouterCommands').onclick = async () => { try { await navigator.clipboard.writeText($('#routerCommands').textContent); $('#copyRouterCommands').classList.add('copied'); $('#copyRouterCommands').firstChild.textContent = 'Команды скопированы '; setTimeout(() => { $('#copyRouterCommands').classList.remove('copied'); $('#copyRouterCommands').firstChild.textContent = 'Копировать команды '; }, 1800); } catch { toast('Не удалось скопировать команды', 'error'); } };
$('#routerCommands').textContent = $('#routerCommands').textContent.replace('ssh,ftp,read,write,sensitive', 'ssh,ftp,read,write,policy,test,sensitive');
document.querySelector('.device-guide li:last-child small').textContent = 'Политики policy и test нужны RouterOS для создания полного системного backup.';
$('#historyBtn').onclick = () => $('#history').scrollIntoView({behavior:'smooth'});
$('#runAll').onclick = async event => { const button = event.currentTarget; const original = button.innerHTML; if (button.disabled) return; button.disabled = true; button.innerHTML = '<b>Запускаем устройства…</b><span>…</span>'; try { await api('/api/backups/run', {method:'POST'}); toast('Параллельный запуск начался'); setTimeout(load, 600); setTimeout(load, 1800); } catch (error) { toast(error.message, 'error'); } finally { setTimeout(() => { button.disabled = false; button.innerHTML = original; }, 1800); } };
routerForm.onsubmit = async event => { event.preventDefault(); $('#formError').textContent = ''; const original = submitButton.innerHTML; submitButton.disabled = true; submitButton.innerHTML = 'Проверяем и сохраняем…'; try { const target = editingRouterId ? `/api/routers/${editingRouterId}` : '/api/routers'; const result = await api(target, {method:editingRouterId ? 'PUT' : 'POST', body:JSON.stringify(routerPayload())}); routerForm.reset(); $('#routerDialog').close(); toast(editingRouterId ? `Настройки ${result.check.identity} обновлены` : `Устройство ${result.check.identity} подключено`); editingRouterId = null; load(); } catch (error) { $('#formError').textContent = error.message; } finally { submitButton.disabled = false; submitButton.innerHTML = original; } };

const extraStyles = document.createElement('link'); extraStyles.rel = 'stylesheet'; extraStyles.href = '/static/enhancements.css'; document.head.appendChild(extraStyles);
const errorStyles = document.createElement('link'); errorStyles.rel = 'stylesheet'; errorStyles.href = '/static/error-modal.css'; document.head.appendChild(errorStyles);
const storageStyles = document.createElement('link'); storageStyles.rel = 'stylesheet'; storageStyles.href = '/static/storage.css'; document.head.appendChild(storageStyles);
const storageScript = document.createElement('script'); storageScript.src = '/static/storage.js'; storageScript.defer = true; document.body.appendChild(storageScript);
const settingsStyles = document.createElement('link'); settingsStyles.rel = 'stylesheet'; settingsStyles.href = '/static/settings.css'; document.head.appendChild(settingsStyles);
const settingsScript = document.createElement('script'); settingsScript.src = '/static/settings.js'; settingsScript.defer = true; document.body.appendChild(settingsScript);
const profileScript = document.createElement('script'); profileScript.src = '/static/profile.js'; profileScript.defer = true; document.body.appendChild(profileScript);
const runStyles = document.createElement('style'); runStyles.textContent = '.run-entry{border-bottom:1px solid var(--line)}.run-entry:last-child{border:0}.run-entry .row{border:0}.status.partial{color:#ffcf70;border-color:#ffcf7030;background:#ffcf700c}.download-action{height:31px;padding:0 10px;border:1px solid #ff696950;border-radius:8px;background:linear-gradient(180deg,#d94b4b,#b82f35);color:#fff;display:inline-flex;align-items:center;gap:6px;font:700 9px Manrope;white-space:nowrap;cursor:pointer;box-shadow:0 5px 16px #c6343424;transition:.18s}.download-action:hover{background:linear-gradient(180deg,#ec5b5b,#ca373d);transform:translateY(-1px)}.download-action:disabled{cursor:wait;opacity:.8;transform:none}.download-action span{font:700 13px DM Mono}.download-action.downloading{background:#842c31;border-color:#a94146}.download-spinner{width:11px;height:11px;border:2px solid #ffffff55;border-top-color:#fff;border-radius:50%;animation:download-spin .7s linear infinite}@keyframes download-spin{to{transform:rotate(360deg)}}.key-action{color:var(--acid)}.retry-action{color:#ffcf70}.run-detail{margin:-5px 21px 12px;padding:9px 12px;border-radius:8px;display:grid;grid-template-columns:18px auto 1fr;gap:7px;align-items:center;font-size:9px}.run-detail small{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.run-detail.active{background:#7de2d108;border:1px solid #7de2d11d;color:var(--cyan)}.run-detail.error-detail{background:#ff8c8708;border:1px solid #ff8c871d;color:var(--danger)}@media(max-width:800px){.run-detail{grid-template-columns:18px 1fr}.run-detail small{grid-column:2;white-space:normal}}'; document.head.appendChild(runStyles);
load(); setInterval(load, 15000);
