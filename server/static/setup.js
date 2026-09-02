const form = document.querySelector('#setupForm');
const steps = [...document.querySelectorAll('.step')];
const dots = [...document.querySelectorAll('.setup-progress i')];
const prev = document.querySelector('#prevBtn');
const next = document.querySelector('#nextBtn');
const finish = document.querySelector('#finishBtn');
const error = document.querySelector('#setupError');
const bucketInput = form.s3_bucket;
const smtpTestButton = document.querySelector('#testSmtpBtn');
const smtpTestStatus = document.querySelector('#smtpTestStatus');
let current = 0;
let smtpChecked = false;

function show() {
  steps.forEach((step, index) => step.classList.toggle('active', index === current));
  dots.forEach((dot, index) => {
    dot.classList.toggle('active', index === current);
    dot.classList.toggle('done', index < current);
  });
  prev.hidden = current === 0;
  next.hidden = current === steps.length - 1;
  finish.hidden = current !== steps.length - 1;
}

function valid() {
  for (const input of steps[current].querySelectorAll('input,select')) {
    if (!input.reportValidity()) return false;
  }
  if (current === 0 && form.password.value !== form.password_confirm.value) {
    error.textContent = 'Пароли не совпадают';
    return false;
  }
  error.textContent = '';
  return true;
}

function errorMessage(detail) {
  if (Array.isArray(detail)) {
    return detail.map(item => String(item.msg || 'Проверьте заполненные поля').replace(/^Value error,\s*/i, '')).join(' · ');
  }
  return detail || 'Настройка не завершена';
}

function smtpPayload() {
  return {
    email: form.email.value,
    smtp: {
      host: form.smtp_host.value,
      port: Number(form.smtp_port.value),
      username: form.smtp_username.value,
      password: form.smtp_password.value,
      sender_email: form.smtp_sender.value,
      security: form.smtp_security.value,
    },
  };
}

function resetSmtpCheck() {
  smtpChecked = false;
  smtpTestButton.classList.remove('passed');
  smtpTestButton.querySelector('span').textContent = '✉';
  smtpTestButton.querySelector('b').textContent = 'Отправить тестовое письмо';
  smtpTestStatus.textContent = 'На email администратора';
}

bucketInput.addEventListener('input', () => {
  bucketInput.value = bucketInput.value.trimStart().toLowerCase().replace(/\s+/g, '-');
});

next.onclick = () => {
  if (current === 1 && !smtpChecked) {
    error.textContent = 'Сначала отправьте тестовое письмо и убедитесь, что SMTP работает.';
    return;
  }
  if (valid()) {
    current++;
    show();
  }
};

for (const input of [form.email, form.smtp_host, form.smtp_port, form.smtp_security, form.smtp_sender, form.smtp_username, form.smtp_password]) {
  input.addEventListener('input', resetSmtpCheck);
  input.addEventListener('change', resetSmtpCheck);
}

smtpTestButton.onclick = async () => {
  if (!valid()) return;
  error.textContent = '';
  smtpTestButton.disabled = true;
  smtpTestButton.classList.add('testing');
  smtpTestButton.querySelector('b').textContent = 'Отправляем письмо…';
  smtpTestStatus.textContent = form.email.value;
  try {
    const response = await fetch('/api/setup/smtp/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(smtpPayload()),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(errorMessage(body.detail));
    smtpChecked = true;
    smtpTestButton.classList.add('passed');
    smtpTestButton.querySelector('span').textContent = '✓';
    smtpTestButton.querySelector('b').textContent = 'Письмо отправлено';
    smtpTestStatus.textContent = body.message;
  } catch (requestError) {
    smtpChecked = false;
    smtpTestButton.classList.remove('passed');
    smtpTestButton.querySelector('span').textContent = '!';
    smtpTestButton.querySelector('b').textContent = 'Отправка не удалась';
    smtpTestStatus.textContent = 'Проверьте настройки и повторите';
    error.textContent = requestError.message;
  } finally {
    smtpTestButton.disabled = false;
    smtpTestButton.classList.remove('testing');
  }
};

prev.onclick = () => {
  current--;
  show();
};

form.onsubmit = async event => {
  event.preventDefault();
  if (!valid()) return;
  finish.disabled = true;
  finish.textContent = 'Проверяем SMTP и S3…';
  const [hour, minute] = form.backup_time.value.split(':').map(Number);
  const payload = {
    username: form.username.value,
    password: form.password.value,
    display_name: form.display_name.value,
    email: form.email.value,
    organization: form.organization.value,
    smtp: smtpPayload().smtp,
    storage: {
      endpoint: form.s3_endpoint.value,
      bucket: bucketInput.value,
      access_key: form.s3_access_key.value,
      secret_key: form.s3_secret_key.value,
    },
    backup_hour: hour,
    backup_minute: minute,
    timezone: form.timezone.value,
    retention_runs: Number(form.retention.value),
  };
  try {
    const response = await fetch('/api/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(errorMessage(body.detail));
    location.replace('/login');
  } catch (requestError) {
    error.textContent = requestError.message;
    finish.disabled = false;
    finish.textContent = 'Проверить и завершить';
  }
};
