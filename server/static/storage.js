(() => {
  const simpleStyles = document.createElement('link');
  simpleStyles.rel = 'stylesheet';
  simpleStyles.href = '/static/s3-simple.css?v=20260902-1';
  document.head.appendChild(simpleStyles);

  const credentialStyles = document.createElement('style');
  credentialStyles.textContent = '.saved-credentials{display:block;margin:-8px 0 14px;color:var(--acid);font-size:8px;line-height:1.5}.saved-credentials[hidden]{display:none}';
  document.head.appendChild(credentialStyles);

  const nav = document.querySelector('nav');
  const storageLink = document.createElement('a');
  storageLink.href = '#';
  storageLink.id = 'storageBtn';
  storageLink.innerHTML = '<span>☁</span> S3-хранилище <i id="storageNavState">—</i>';
  nav.appendChild(storageLink);

  const headerActions = document.querySelector('.header-actions');
  const headerButton = document.createElement('button');
  headerButton.className = 'ghost storage-header-button';
  headerButton.innerHTML = '☁ <span>S3-хранилище</span>';
  headerActions.insertBefore(headerButton, document.querySelector('#addBtn'));

  const dialog = document.createElement('dialog');
  dialog.id = 'storageDialog';
  dialog.innerHTML = `
    <form id="storageForm">
      <div class="dialog-head">
        <div><span class="eyebrow">OBJECT STORAGE</span><h2>Подключить S3-хранилище</h2><p>RouterVault проверит bucket и создаст его, если он отсутствует.</p></div>
        <button type="button" class="icon" id="closeStorage">×</button>
      </div>
      <div class="storage-state" id="storageState"><span>☁</span><p><b>Хранилище не настроено</b><small>Добавьте реквизиты S3-совместимого сервера</small></p></div>
      <label>Адрес S3-сервера
        <input name="endpoint" type="url" placeholder="https://s3.example.com">
        <small class="storage-hint">Для Amazon S3 можно оставить поле пустым.</small>
      </label>
      <label>Имя bucket
        <input name="bucket" required minlength="3" maxlength="63" pattern="[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]" title="От 3 до 63 символов: строчные латинские буквы, цифры, точки и дефисы" placeholder="mikrotik-backups" autocomplete="off">
        <small class="storage-hint">Если bucket отсутствует, он будет создан автоматически.</small>
      </label>
      <div class="split equal">
        <label>Access Key<input name="access_key" autocomplete="off" placeholder="Введите access key"></label>
        <label>Secret Key<input name="secret_key" type="password" autocomplete="new-password" placeholder="Введите secret key"></label>
      </div>
      <small class="saved-credentials" id="savedCredentials" hidden>✓ Ключи сохранены. Оставьте поля пустыми, чтобы использовать их повторно.</small>
      <div class="storage-auto-note"><span>✓</span><p><b>Служебные параметры настроятся автоматически</b><small>Регион, адресация и шифрование не требуют ручного выбора.</small></p></div>
      <div class="storage-actions">
        <button type="button" class="connection-check" id="checkStorage"><span>⌁</span><b>Проверить и создать bucket</b><i>Если он ещё не существует</i></button>
        <button class="primary storage-save" id="saveStorage">Сохранить хранилище <span>→</span></button>
      </div>
      <p class="error" id="storageError"></p>
      <div class="secure-note"><span>⌾</span><p><b>Ключи хранятся зашифрованными</b><small>Access Key и Secret Key никогда не возвращаются из API обратно в браузер.</small></p></div>
    </form>`;
  document.body.appendChild(dialog);

  const form = document.querySelector('#storageForm');
  const state = document.querySelector('#storageState');
  const error = document.querySelector('#storageError');
  const checkButton = document.querySelector('#checkStorage');
  const saveButton = document.querySelector('#saveStorage');
  const bucketInput = form.bucket;
  const payload = () => ({
    endpoint: form.endpoint.value,
    bucket: bucketInput.value,
    access_key: form.access_key.value,
    secret_key: form.secret_key.value,
  });

  bucketInput.addEventListener('input', () => {
    bucketInput.value = bucketInput.value.trimStart().toLowerCase().replace(/\s+/g, '-');
  });

  async function refreshStorage() {
    try {
      const result = await api('/api/storage');
      const marker = document.querySelector('#storageNavState');
      marker.textContent = result.configured ? 'OK' : '!';
      marker.className = result.configured ? 'connected' : '';
      document.querySelector('#savedCredentials').hidden = !result.credentials_saved;
      if (result.configured) {
        form.endpoint.value = result.endpoint || '';
        form.bucket.value = result.bucket;
        form.access_key.value = '';
        form.secret_key.value = '';
        form.access_key.placeholder = 'Сохранён — введите только для замены';
        form.secret_key.placeholder = 'Сохранён — введите только для замены';
        state.classList.add('connected');
        state.innerHTML = `<span>✓</span><p><b>${escapeHtml(result.bucket)}</b><small>${escapeHtml(result.endpoint || 'Amazon S3')}</small></p>`;
      }
    } catch {}
  }

  async function openStorage(event) {
    event?.preventDefault();
    error.textContent = '';
    dialog.showModal();
    await refreshStorage();
  }

  storageLink.onclick = openStorage;
  headerButton.onclick = openStorage;
  document.querySelector('#closeStorage').onclick = () => dialog.close();

  checkButton.onclick = async () => {
    error.textContent = '';
    if (!form.reportValidity()) return;
    checkButton.disabled = true;
    checkButton.classList.add('loading');
    checkButton.querySelector('b').textContent = 'Проверяем bucket…';
    try {
      const result = await api('/api/storage/check', {method: 'POST', body: JSON.stringify(payload())});
      checkButton.classList.add('passed');
      checkButton.querySelector('b').textContent = result.created ? 'Bucket создан' : 'Bucket найден';
      checkButton.querySelector('i').textContent = result.message;
      toast(result.message);
    } catch (requestError) {
      checkButton.classList.remove('passed');
      checkButton.querySelector('b').textContent = 'Проверка не пройдена';
      checkButton.querySelector('i').textContent = 'Проверьте реквизиты';
      error.textContent = requestError.message;
    } finally {
      checkButton.disabled = false;
      checkButton.classList.remove('loading');
    }
  };

  form.onsubmit = async event => {
    event.preventDefault();
    error.textContent = '';
    saveButton.disabled = true;
    saveButton.firstChild.textContent = 'Проверяем и сохраняем… ';
    try {
      const result = await api('/api/storage', {method: 'POST', body: JSON.stringify(payload())});
      toast(result.message);
      dialog.close();
      await refreshStorage();
      await load();
    } catch (requestError) {
      error.textContent = requestError.message;
    } finally {
      saveButton.disabled = false;
      saveButton.firstChild.textContent = 'Сохранить хранилище ';
    }
  };

  refreshStorage();
})();
