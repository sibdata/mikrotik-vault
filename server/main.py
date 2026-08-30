import base64
import hashlib
import hmac
import ipaddress
import os
import re
import smtplib
import ssl
import secrets
import sqlite3
import tempfile
import time
import uuid
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
import paramiko
from apscheduler.schedulers.background import BackgroundScheduler
from botocore.config import Config
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", "/data/routervault.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="RouterVault", docs_url=None, redoc_url=None)


def now(): return datetime.now(timezone.utc).isoformat()


def session_secret(): return hashlib.sha256((os.getenv("MASTER_KEY", "") + ":routervault-session").encode()).digest()


def make_session(username: str):
    expires = int(time.time()) + int(os.getenv("SESSION_TTL_HOURS", "12")) * 3600
    payload = f"{username}:{expires}"
    signature = hmac.new(session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def read_session(token: str | None):
    if not token: return None
    try:
        username, expires, signature = base64.urlsafe_b64decode(token.encode()).decode().rsplit(":", 2)
        payload = f"{username}:{expires}"
        expected = hmac.new(session_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if int(expires) < int(time.time()) or not secrets.compare_digest(signature, expected): return None
        if not secrets.compare_digest(username, admin_username()): return None
        return username
    except (ValueError, UnicodeDecodeError): return None


def auth(request: Request):
    username = read_session(request.cookies.get("routervault_session"))
    if not username: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход")
    return username


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=512)


class ProfileIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=50)
    organization: str = Field(default="", max_length=150)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class SMTPSettingsIn(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=512)
    sender_email: str = Field(min_length=3, max_length=255)
    security: str = Field(default="starttls", pattern="^(starttls|ssl|none)$")


class ForgotPasswordIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    new_password: str = Field(min_length=12, max_length=512)


def cipher():
    secret = os.getenv("MASTER_KEY", "")
    if len(secret) < 24: raise RuntimeError("MASTER_KEY должен содержать не менее 24 символов")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally: conn.close()


def admin_username():
    try:
        with db() as c: row = c.execute("SELECT username FROM admin_profile WHERE id=1").fetchone()
        return row["username"] if row else os.getenv("ADMIN_USER", "admin")
    except sqlite3.Error: return os.getenv("ADMIN_USER", "admin")


def password_digest(password: str, salt: bytes):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def verify_admin_password(password: str):
    with db() as c: row = c.execute("SELECT password_salt,password_hash FROM admin_profile WHERE id=1").fetchone()
    if row and row["password_salt"] and row["password_hash"]:
        salt = base64.b64decode(row["password_salt"]); expected = base64.b64decode(row["password_hash"])
        return secrets.compare_digest(password_digest(password, salt), expected)
    return secrets.compare_digest(password, os.getenv("ADMIN_PASSWORD", "change-me"))


def setup_is_complete():
    try:
        with db() as c: row = c.execute("SELECT setup_complete FROM admin_profile WHERE id=1").fetchone()
        return bool(row and row["setup_complete"])
    except sqlite3.Error: return False


def get_smtp_settings():
    with db() as c: row = c.execute("SELECT * FROM smtp_settings WHERE id=1").fetchone()
    if not row: return None
    return {"host":row["host"],"port":row["port"],"username":row["username"] or "","password":cipher().decrypt(row["password_enc"].encode()).decode() if row["password_enc"] else "","sender_email":row["sender_email"],"security":row["security"]}


def smtp_send(settings, recipient: str, subject: str, text: str):
    message = EmailMessage(); message["From"] = settings["sender_email"]; message["To"] = recipient; message["Subject"] = subject; message.set_content(text)
    context = ssl.create_default_context()
    if settings["security"] == "ssl": client = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=15, context=context)
    else:
        client = smtplib.SMTP(settings["host"], settings["port"], timeout=15)
        if settings["security"] == "starttls": client.starttls(context=context)
    try:
        if settings["username"]: client.login(settings["username"], settings["password"])
        client.send_message(message)
    finally: client.quit()


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS routers (id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL UNIQUE, port INTEGER NOT NULL DEFAULT 22, username TEXT NOT NULL, password_enc TEXT NOT NULL, identity TEXT, routeros_version TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_backup_at TEXT, last_status TEXT NOT NULL DEFAULT 'never', last_error TEXT);
        CREATE TABLE IF NOT EXISTS backups (id TEXT PRIMARY KEY, router_id TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, s3_key TEXT, filename TEXT, size INTEGER, format TEXT, error TEXT, FOREIGN KEY(router_id) REFERENCES routers(id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS idx_backups_router_created ON backups(router_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_backups_created ON backups(created_at DESC);
        CREATE TABLE IF NOT EXISTS storage_settings (id INTEGER PRIMARY KEY CHECK(id=1), endpoint TEXT, region TEXT NOT NULL, bucket TEXT NOT NULL, access_key_enc TEXT NOT NULL, secret_key_enc TEXT NOT NULL, addressing_style TEXT NOT NULL DEFAULT 'path', sse TEXT NOT NULL DEFAULT 'AES256', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_settings (id INTEGER PRIMARY KEY CHECK(id=1), backup_hour INTEGER NOT NULL DEFAULT 3, backup_minute INTEGER NOT NULL DEFAULT 0, timezone TEXT NOT NULL DEFAULT 'UTC', retention_runs INTEGER NOT NULL DEFAULT 30, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_profile (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT 'Администратор', email TEXT, phone TEXT, organization TEXT, password_salt TEXT, password_hash TEXT, setup_complete INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS smtp_settings (id INTEGER PRIMARY KEY CHECK(id=1), host TEXT NOT NULL, port INTEGER NOT NULL, username TEXT, password_enc TEXT, sender_email TEXT NOT NULL, security TEXT NOT NULL DEFAULT 'starttls', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS password_reset_tokens (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL);
        """)
        columns = {row[1] for row in c.execute("PRAGMA table_info(routers)").fetchall()}
        if "identity" not in columns: c.execute("ALTER TABLE routers ADD COLUMN identity TEXT")
        if "routeros_version" not in columns: c.execute("ALTER TABLE routers ADD COLUMN routeros_version TEXT")
        if "host_key_fingerprint" not in columns: c.execute("ALTER TABLE routers ADD COLUMN host_key_fingerprint TEXT")
        backup_columns = {row[1] for row in c.execute("PRAGMA table_info(backups)").fetchall()}
        if "run_id" not in backup_columns: c.execute("ALTER TABLE backups ADD COLUMN run_id TEXT")
        if "stage" not in backup_columns: c.execute("ALTER TABLE backups ADD COLUMN stage TEXT")
        if "attempts" not in backup_columns: c.execute("ALTER TABLE backups ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "backup_password_enc" not in backup_columns: c.execute("ALTER TABLE backups ADD COLUMN backup_password_enc TEXT")
        if "sha256" not in backup_columns: c.execute("ALTER TABLE backups ADD COLUMN sha256 TEXT")
        c.execute("UPDATE backups SET run_id=created_at WHERE run_id IS NULL")
        settings_columns = {row[1] for row in c.execute("PRAGMA table_info(app_settings)").fetchall()}
        if "telegram_enabled" not in settings_columns: c.execute("ALTER TABLE app_settings ADD COLUMN telegram_enabled INTEGER NOT NULL DEFAULT 0")
        if "telegram_token_enc" not in settings_columns: c.execute("ALTER TABLE app_settings ADD COLUMN telegram_token_enc TEXT")
        if "telegram_chat_id" not in settings_columns: c.execute("ALTER TABLE app_settings ADD COLUMN telegram_chat_id TEXT")
        if "last_system_backup_at" not in settings_columns: c.execute("ALTER TABLE app_settings ADD COLUMN last_system_backup_at TEXT")
        profile_columns = {row[1] for row in c.execute("PRAGMA table_info(admin_profile)").fetchall()}
        if "setup_complete" not in profile_columns:
            c.execute("ALTER TABLE admin_profile ADD COLUMN setup_complete INTEGER NOT NULL DEFAULT 0")
            if c.execute("SELECT EXISTS(SELECT 1 FROM routers) OR EXISTS(SELECT 1 FROM storage_settings)").fetchone()[0]: c.execute("UPDATE admin_profile SET setup_complete=1 WHERE id=1")
        c.execute("INSERT OR IGNORE INTO app_settings(id,backup_hour,backup_minute,timezone,retention_runs,updated_at) VALUES(1,?,?,?,?,?)", (int(os.getenv("BACKUP_HOUR", "3")), int(os.getenv("BACKUP_MINUTE", "0")), os.getenv("TZ", "UTC"), 30, now()))
        c.execute("INSERT OR IGNORE INTO admin_profile(id,username,display_name,updated_at) VALUES(1,?,?,?)", (os.getenv("ADMIN_USER", "admin"), "Администратор", now()))


class RouterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=512)
    enabled: bool = True

    @field_validator("host")
    @classmethod
    def valid_host(cls, value):
        value = value.strip()
        try: ipaddress.ip_address(value)
        except ValueError:
            if not all(part and part.replace("-", "").isalnum() for part in value.split(".")): raise ValueError("Некорректный IP или hostname")
        return value


class RouterUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=80)
    password: str | None = Field(default=None, max_length=512)
    enabled: bool = True

    @field_validator("host")
    @classmethod
    def valid_host(cls, value): return RouterIn.valid_host(value)

    @field_validator("password")
    @classmethod
    def empty_password_is_none(cls, value): return value or None


class AppSettingsIn(BaseModel):
    backup_hour: int = Field(ge=0, le=23)
    backup_minute: int = Field(ge=0, le=59)
    timezone: str = Field(min_length=1, max_length=100)
    retention_runs: int = Field(ge=1, le=365)
    telegram_enabled: bool = False
    telegram_token: str = Field(default="", max_length=512)
    telegram_chat_id: str = Field(default="", max_length=100)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try: ZoneInfo(value)
        except ZoneInfoNotFoundError: raise ValueError("Неизвестный часовой пояс")
        return value


class S3SettingsIn(BaseModel):
    endpoint: str = Field(default="", max_length=500)
    region: str = Field(default="us-east-1", min_length=1, max_length=100)
    bucket: str = Field(min_length=1, max_length=255)
    access_key: str = Field(default="", max_length=512)
    secret_key: str = Field(default="", max_length=1024)
    addressing_style: str = Field(default="path", pattern="^(path|virtual)$")
    sse: str = Field(default="AES256", pattern="^(AES256|aws:kms|none)$")


class SetupIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=512)
    display_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    organization: str = Field(default="", max_length=150)
    smtp: SMTPSettingsIn
    storage: S3SettingsIn
    backup_hour: int = Field(ge=0, le=23)
    backup_minute: int = Field(ge=0, le=59)
    timezone: str = Field(min_length=1, max_length=100)
    retention_runs: int = Field(ge=1, le=365)


def public_router(row):
    data = dict(row); data.pop("password_enc", None); data["enabled"] = bool(data["enabled"]); return data


def get_s3_settings():
    with db() as c: row = c.execute("SELECT * FROM storage_settings WHERE id=1").fetchone()
    if row:
        return {"endpoint": row["endpoint"] or None, "region": row["region"], "bucket": row["bucket"], "access_key": cipher().decrypt(row["access_key_enc"].encode()).decode(), "secret_key": cipher().decrypt(row["secret_key_enc"].encode()).decode(), "addressing_style": row["addressing_style"], "sse": row["sse"]}
    return {"endpoint": os.getenv("S3_ENDPOINT") or None, "region": os.getenv("S3_REGION", "us-east-1"), "bucket": os.getenv("S3_BUCKET", ""), "access_key": os.getenv("S3_ACCESS_KEY", ""), "secret_key": os.getenv("S3_SECRET_KEY", ""), "addressing_style": os.getenv("S3_ADDRESSING_STYLE", "path"), "sse": os.getenv("S3_SSE", "AES256")}


def complete_s3_settings(item: S3SettingsIn):
    settings = item.model_dump(); current = get_s3_settings()
    settings["endpoint"] = settings["endpoint"] or None
    settings["access_key"] = settings["access_key"] or current["access_key"]
    settings["secret_key"] = settings["secret_key"] or current["secret_key"]
    if not settings["access_key"] or not settings["secret_key"]:
        raise HTTPException(422, "Введите Access Key и Secret Key для первого подключения")
    return settings


def s3_client(settings=None):
    settings = settings or get_s3_settings()
    return boto3.client("s3", endpoint_url=settings["endpoint"], region_name=settings["region"], aws_access_key_id=settings["access_key"], aws_secret_access_key=settings["secret_key"], config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=15, retries={"max_attempts": 1}, s3={"addressing_style": settings["addressing_style"]}))


def get_app_settings():
    with db() as c: row = c.execute("SELECT backup_hour,backup_minute,timezone,retention_runs,telegram_enabled,telegram_token_enc IS NOT NULL telegram_configured,telegram_chat_id,last_system_backup_at,updated_at FROM app_settings WHERE id=1").fetchone()
    result = dict(row); result["telegram_enabled"] = bool(result["telegram_enabled"]); result["telegram_configured"] = bool(result["telegram_configured"]); return result


def get_telegram_settings():
    with db() as c: row = c.execute("SELECT telegram_enabled,telegram_token_enc,telegram_chat_id FROM app_settings WHERE id=1").fetchone()
    return {"enabled":bool(row["telegram_enabled"]), "token":cipher().decrypt(row["telegram_token_enc"].encode()).decode() if row["telegram_token_enc"] else "", "chat_id":row["telegram_chat_id"] or ""}


def send_telegram(message: str, force=False):
    settings = get_telegram_settings()
    if not settings["token"] or not settings["chat_id"] or (not settings["enabled"] and not force): return False
    body = urllib.parse.urlencode({"chat_id":settings["chat_id"], "text":message}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{settings['token']}/sendMessage", data=body), timeout=8) as response: return response.status == 200
    except Exception: return False


def backup_routervault_database():
    settings = get_s3_settings()
    if not settings["bucket"]: return False
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        source = sqlite3.connect(DB_PATH); target = sqlite3.connect(tmp.name)
        try: source.backup(target)
        finally: target.close(); source.close()
        key = f"routervault-system/database/routervault-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.sqlite"
        extra = {"ContentType":"application/vnd.sqlite3"}
        if settings["sse"] != "none": extra["ServerSideEncryption"] = settings["sse"]
        s3_client(settings).upload_file(tmp.name, settings["bucket"], key, ExtraArgs=extra)
    with db() as c: c.execute("UPDATE app_settings SET last_system_backup_at=? WHERE id=1", (now(),))
    return True


def schedule_backups():
    settings = get_app_settings()
    app.state.scheduler.reschedule_job("daily-backup", trigger="cron", hour=settings["backup_hour"], minute=settings["backup_minute"], timezone=settings["timezone"])


def enforce_retention(router_id: str):
    settings = get_app_settings(); keep = settings["retention_runs"]
    with db() as c:
        timestamps = [row[0] for row in c.execute("SELECT DISTINCT created_at FROM backups WHERE router_id=? AND status='success' ORDER BY created_at DESC LIMIT -1 OFFSET ?", (router_id, keep)).fetchall()]
        if not timestamps: return 0
        placeholders = ",".join("?" for _ in timestamps)
        rows = c.execute(f"SELECT id,s3_key FROM backups WHERE router_id=? AND status='success' AND created_at IN ({placeholders})", (router_id, *timestamps)).fetchall()
    settings = get_s3_settings(); deleted_ids = []
    for row in rows:
        try:
            s3_client(settings).delete_object(Bucket=settings["bucket"], Key=row["s3_key"])
            deleted_ids.append(row["id"])
        except Exception:
            continue
    if deleted_ids:
        with db() as c:
            placeholders = ",".join("?" for _ in deleted_ids)
            c.execute(f"DELETE FROM backups WHERE id IN ({placeholders})", deleted_ids)
    return len(deleted_ids)


def inspect_bucket(settings):
    client = s3_client(settings)
    try:
        client.head_bucket(Bucket=settings["bucket"])
        return client, True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchBucket", "NotFound"} or status_code == 404: return client, False
        raise


def create_bucket(client, settings):
    bucket = settings["bucket"]; region = settings["region"]
    try:
        if region and region != "us-east-1": client.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
        else: client.create_bucket(Bucket=bucket)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"InvalidLocationConstraint", "InvalidRequest", "NotImplemented"}: client.create_bucket(Bucket=bucket)
        else: raise
    client.head_bucket(Bucket=bucket)


def host_key_fingerprint(key):
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected=None): self.expected = expected

    def missing_host_key(self, client, hostname, key):
        actual = host_key_fingerprint(key)
        if self.expected and not secrets.compare_digest(actual, self.expected):
            raise paramiko.SSHException(f"SSH fingerprint устройства изменился: ожидался {self.expected}, получен {actual}")


def connect_router(host: str, port: int, username: str, password: str, expected_fingerprint=None, attempts: int = 3):
    last_error = None
    for attempt in range(attempts):
        ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(PinnedHostKeyPolicy(expected_fingerprint))
        try:
            ssh.connect(host, port=port, username=username, password=password, timeout=20, banner_timeout=20, auth_timeout=20, look_for_keys=False, allow_agent=False)
            ssh.routervault_attempts = attempt + 1
            return ssh
        except paramiko.AuthenticationException:
            ssh.close(); raise
        except (paramiko.SSHException, OSError, TimeoutError, EOFError) as exc:
            ssh.close(); last_error = exc
            if "fingerprint" in str(exc).lower(): raise
            if attempt + 1 < attempts: time.sleep(2 * (attempt + 1))
    raise last_error


def check_router_access(host: str, port: int, username: str, password: str, expected_fingerprint=None):
    ssh = None
    try:
        ssh = connect_router(host, port, username, password, expected_fingerprint)
        _, stdout, stderr = ssh.exec_command(':put ([/system identity get name]."|".[/system resource get version])', timeout=10)
        code = stdout.channel.recv_exit_status(); device_info = stdout.read().decode().strip(); error = stderr.read().decode().strip()
        if code != 0 or error: raise RuntimeError(error or "RouterOS не разрешил выполнить команду")
        identity, _, version = device_info.partition("|")
        sftp = ssh.open_sftp(); sftp.listdir("."); sftp.close()
        fingerprint = host_key_fingerprint(ssh.get_transport().get_remote_server_key())
        return {"ok": True, "identity": identity or host, "routeros_version": version or "Не определена", "ip_address": host, "host_key_fingerprint": fingerprint, "attempts":ssh.routervault_attempts, "message": "SSH и SFTP доступны, fingerprint подтверждён"}
    except paramiko.AuthenticationException:
        raise HTTPException(422, "Неверный логин или пароль MikroTik")
    except paramiko.ssh_exception.SSHException as exc:
        if "protocol banner" in str(exc).lower(): raise HTTPException(422, "Порт доступен, но не отвечает как SSH. Проверьте SSH-сервис, порт и firewall MikroTik")
        raise HTTPException(422, f"Ошибка SSH: {str(exc)}")
    except TimeoutError:
        raise HTTPException(422, "MikroTik не ответил вовремя. Проверьте IP, порт и firewall")
    except OSError as exc:
        raise HTTPException(422, f"Не удалось подключиться к MikroTik: {str(exc)}")
    except RuntimeError as exc:
        raise HTTPException(422, f"Недостаточно прав RouterOS: {str(exc)}")
    except Exception as exc:
        raise HTTPException(422, f"Проверка подключения завершилась ошибкой: {str(exc)}")
    finally:
        if ssh: ssh.close()


def claim_backup(router_id: str):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        router = c.execute("SELECT * FROM routers WHERE id=?", (router_id,)).fetchone()
        if not router: return None
        active = c.execute("SELECT id,created_at FROM backups WHERE router_id=? AND status='running' ORDER BY created_at DESC LIMIT 1", (router_id,)).fetchone()
        if active:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(active["created_at"])
            if age.total_seconds() < 1800: return None
            c.execute("UPDATE backups SET status='failed',error='Процесс прерван или превысил 30 минут' WHERE id=?", (active["id"],))
        backup_id = str(uuid.uuid4()); started = now()
        c.execute("INSERT INTO backups(id,router_id,created_at,status,run_id,stage) VALUES(?,?,?,?,?,?)", (backup_id, router_id, started, "running", backup_id, "queued"))
        c.execute("UPDATE routers SET last_status='running', last_error=NULL WHERE id=?", (router_id,))
        return backup_id, started


def update_backup_stage(backup_id: str, stage: str, attempts=None):
    with db() as c:
        if attempts is None: c.execute("UPDATE backups SET stage=? WHERE id=?", (stage, backup_id))
        else: c.execute("UPDATE backups SET stage=?,attempts=? WHERE id=?", (stage, attempts, backup_id))


def run_backup(router_id: str, claim=None):
    claim = claim or claim_backup(router_id)
    if not claim: return False
    backup_id, started = claim
    with db() as c:
        router = c.execute("SELECT * FROM routers WHERE id=?", (router_id,)).fetchone()
        if not router: return False
    previous_status = router["last_status"]
    stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    safe_address = re.sub(r"[^A-Za-z0-9._-]", "-", router["host"]).strip(".-") or "device"
    remote_base = f"routervault-{safe_address}-{stamp}"
    files = [(f"{remote_base}.backup", "backup"), (f"{remote_base}.rsc", "export")]
    ssh = None; sftp = None
    try:
        update_backup_stage(backup_id, "ssh_connect")
        password = cipher().decrypt(router["password_enc"].encode()).decode()
        ssh = connect_router(router["host"], router["port"], router["username"], password, router["host_key_fingerprint"])
        fingerprint = host_key_fingerprint(ssh.get_transport().get_remote_server_key())
        with db() as c: c.execute("UPDATE routers SET host_key_fingerprint=? WHERE id=?", (fingerprint, router_id))
        update_backup_stage(backup_id, "create_files", ssh.routervault_attempts)
        backup_password = secrets.token_urlsafe(24)
        with db() as c: c.execute("UPDATE backups SET backup_password_enc=? WHERE id=?", (cipher().encrypt(backup_password.encode()).decode(), backup_id))
        for command in [f'/system backup save name="{remote_base}" password="{backup_password}" encryption=aes-sha256', f'/export file="{remote_base}"']:
            _, stdout, stderr = ssh.exec_command(command, timeout=90)
            code = stdout.channel.recv_exit_status(); output = stdout.read().decode().strip(); err = stderr.read().decode().strip()
            response = "\n".join(part for part in (output, err) if part)
            if code != 0 or any(marker in response.lower() for marker in ("failure:", "not enough permissions", "syntax error", "bad command")):
                raise RuntimeError(response or f"RouterOS вернул код {code}")
        time.sleep(2)
        update_backup_stage(backup_id, "download")
        sftp = ssh.open_sftp()
        for filename, fmt in files:
            with tempfile.NamedTemporaryFile() as tmp:
                sftp.get(filename, tmp.name); size = Path(tmp.name).stat().st_size
                digest = hashlib.sha256(Path(tmp.name).read_bytes()).hexdigest()
                key = f"{router_id}/{datetime.now().strftime('%Y/%m')}/{filename}"
                settings = get_s3_settings()
                if not settings["bucket"]: raise RuntimeError("S3-хранилище не настроено")
                update_backup_stage(backup_id, f"upload_{fmt}")
                extra = {"ContentType": "application/octet-stream"}
                if settings["sse"] != "none": extra["ServerSideEncryption"] = settings["sse"]
                s3_client(settings).upload_file(tmp.name, settings["bucket"], key, ExtraArgs=extra)
            with db() as c:
                record_id = backup_id if fmt == "backup" else str(uuid.uuid4())
                if fmt == "backup": c.execute("UPDATE backups SET status='success',s3_key=?,filename=?,size=?,format=?,stage='uploaded',sha256=? WHERE id=?", (key, filename, size, fmt, digest, record_id))
                else: c.execute("INSERT INTO backups(id,router_id,created_at,status,s3_key,filename,size,format,run_id,stage,attempts,sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (record_id, router_id, started, "success", key, filename, size, fmt, backup_id, "uploaded", ssh.routervault_attempts, digest))
        with db() as c: c.execute("UPDATE routers SET last_backup_at=?,last_status='success',last_error=NULL WHERE id=?", (now(), router_id))
        enforce_retention(router_id)
        if previous_status == "failed": send_telegram(f"✅ RouterVault: {router['name']} снова создаёт резервные копии успешно.")
        return True
    except Exception as exc:
        message = str(exc)[:1000]
        with db() as c:
            current = c.execute("SELECT status,stage,attempts FROM backups WHERE id=?", (backup_id,)).fetchone()
            if current and current["status"] == "success":
                c.execute("INSERT INTO backups(id,router_id,created_at,status,format,error,run_id,stage,attempts) VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()),router_id,started,"failed","error",message,backup_id,current["stage"],current["attempts"]))
            else: c.execute("UPDATE backups SET status='failed',error=? WHERE id=?", (message, backup_id))
            c.execute("UPDATE routers SET last_status='failed',last_error=? WHERE id=?", (message, router_id))
        send_telegram(f"❌ RouterVault: ошибка бэкапа {router['name']} ({router['host']}). Этап: {current['stage'] if current else 'unknown'}. {message}")
    finally:
        if sftp:
            for filename, _ in files:
                try: sftp.remove(filename)
                except OSError: pass
            sftp.close()
        if ssh: ssh.close()


def run_all():
    with db() as c: ids = [r[0] for r in c.execute("SELECT id FROM routers WHERE enabled=1").fetchall()]
    jobs = [(router_id, claim) for router_id in ids if (claim := claim_backup(router_id))]
    if not jobs: return {"started": 0, "completed": 0}
    workers = min(max(1, int(os.getenv("BACKUP_WORKERS", "4"))), len(jobs))
    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="routervault-backup") as pool:
        futures = [pool.submit(run_backup, router_id, claim) for router_id, claim in jobs]
        for future in as_completed(futures):
            try: future.result()
            except Exception: pass
            completed += 1
    try: backup_routervault_database()
    except Exception: pass
    return {"started": len(jobs), "completed": completed}


@app.on_event("startup")
def startup():
    init_db(); cipher()
    settings = get_app_settings()
    scheduler = BackgroundScheduler(timezone=settings["timezone"])
    scheduler.add_job(run_all, "cron", hour=settings["backup_hour"], minute=settings["backup_minute"], timezone=settings["timezone"], id="daily-backup", replace_existing=True, max_instances=1)
    scheduler.start(); app.state.scheduler = scheduler


@app.get("/")
def index(request: Request):
    if not setup_is_complete(): return RedirectResponse("/setup", status_code=303)
    if not read_session(request.cookies.get("routervault_session")): return RedirectResponse("/login", status_code=303)
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/login")
def login_page(request: Request):
    if not setup_is_complete(): return RedirectResponse("/setup", status_code=303)
    if read_session(request.cookies.get("routervault_session")): return RedirectResponse("/", status_code=303)
    return FileResponse(ROOT / "static" / "login.html")


@app.get("/setup")
def setup_page():
    if setup_is_complete(): return RedirectResponse("/login", status_code=303)
    return FileResponse(ROOT / "static" / "setup.html")


@app.get("/forgot-password")
def forgot_page(): return FileResponse(ROOT / "static" / "forgot.html")


@app.get("/reset-password")
def reset_page(): return FileResponse(ROOT / "static" / "reset.html")


@app.post("/api/login")
def login(item: LoginIn):
    valid_user = secrets.compare_digest(item.username, admin_username())
    valid_password = verify_admin_password(item.password)
    if not (valid_user and valid_password): raise HTTPException(401, "Неверный логин или пароль")
    response = {"status": "ok"}
    from fastapi.responses import JSONResponse
    result = JSONResponse(response)
    result.set_cookie("routervault_session", make_session(item.username), httponly=True, samesite="strict", secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", max_age=int(os.getenv("SESSION_TTL_HOURS", "12")) * 3600, path="/")
    return result


@app.get("/api/setup/status")
def setup_status(): return {"required":not setup_is_complete()}


@app.post("/api/setup")
def complete_setup(item: SetupIn):
    if setup_is_complete(): raise HTTPException(409, "Первичная настройка уже завершена")
    try: ZoneInfo(item.timezone)
    except ZoneInfoNotFoundError: raise HTTPException(422, "Неизвестный часовой пояс")
    if not item.storage.access_key or not item.storage.secret_key: raise HTTPException(422, "Укажите ключи доступа к S3")
    storage = item.storage.model_dump(); storage["endpoint"] = storage["endpoint"] or None
    smtp = item.smtp.model_dump()
    try:
        client, exists = inspect_bucket(storage)
        if not exists: create_bucket(client, storage)
    except Exception as exc: raise HTTPException(422, f"Проверка S3 не пройдена: {str(exc)}")
    try: smtp_send(smtp, item.email, "RouterVault — проверка почты", "Почта восстановления настроена. Мастер RouterVault можно завершить.")
    except Exception as exc: raise HTTPException(422, f"Проверка SMTP не пройдена: {str(exc)}")
    salt = secrets.token_bytes(24); digest = password_digest(item.password, salt)
    with db() as c:
        c.execute("UPDATE admin_profile SET username=?,display_name=?,email=?,organization=?,password_salt=?,password_hash=?,setup_complete=1,updated_at=? WHERE id=1", (item.username,item.display_name,item.email,item.organization or None,base64.b64encode(salt).decode(),base64.b64encode(digest).decode(),now()))
        c.execute("INSERT INTO smtp_settings(id,host,port,username,password_enc,sender_email,security,updated_at) VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET host=excluded.host,port=excluded.port,username=excluded.username,password_enc=excluded.password_enc,sender_email=excluded.sender_email,security=excluded.security,updated_at=excluded.updated_at", (smtp["host"],smtp["port"],smtp["username"] or None,cipher().encrypt(smtp["password"].encode()).decode() if smtp["password"] else None,smtp["sender_email"],smtp["security"],now()))
        c.execute("INSERT INTO storage_settings(id,endpoint,region,bucket,access_key_enc,secret_key_enc,addressing_style,sse,updated_at) VALUES(1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET endpoint=excluded.endpoint,region=excluded.region,bucket=excluded.bucket,access_key_enc=excluded.access_key_enc,secret_key_enc=excluded.secret_key_enc,addressing_style=excluded.addressing_style,sse=excluded.sse,updated_at=excluded.updated_at", (storage["endpoint"],storage["region"],storage["bucket"],cipher().encrypt(storage["access_key"].encode()).decode(),cipher().encrypt(storage["secret_key"].encode()).decode(),storage["addressing_style"],storage["sse"],now()))
        c.execute("UPDATE app_settings SET backup_hour=?,backup_minute=?,timezone=?,retention_runs=?,updated_at=? WHERE id=1", (item.backup_hour,item.backup_minute,item.timezone,item.retention_runs,now()))
    schedule_backups()
    return {"ok":True}


@app.post("/api/password/forgot")
def forgot_password(item: ForgotPasswordIn, request: Request):
    with db() as c: profile = c.execute("SELECT email FROM admin_profile WHERE id=1").fetchone()
    if profile and profile["email"] and secrets.compare_digest(profile["email"].lower(), item.email.lower()):
        smtp = get_smtp_settings()
        if smtp:
            token = secrets.token_urlsafe(40); token_hash = hashlib.sha256(token.encode()).hexdigest(); expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            with db() as c:
                c.execute("DELETE FROM password_reset_tokens WHERE used_at IS NOT NULL OR expires_at<?", (now(),))
                c.execute("INSERT INTO password_reset_tokens(id,token_hash,expires_at,created_at) VALUES(?,?,?,?)", (str(uuid.uuid4()),token_hash,expires,now()))
            link = f"{str(request.base_url).rstrip('/')}/reset-password?token={urllib.parse.quote(token)}"
            try: smtp_send(smtp, item.email, "Восстановление пароля RouterVault", f"Для смены пароля откройте ссылку:\n\n{link}\n\nОна действует 30 минут и используется один раз.")
            except Exception: pass
    return {"ok":True,"message":"Если адрес совпадает с профилем, письмо будет отправлено"}


@app.post("/api/password/reset")
def reset_password(item: ResetPasswordIn):
    token_hash = hashlib.sha256(item.token.encode()).hexdigest()
    with db() as c: row = c.execute("SELECT id,expires_at,used_at FROM password_reset_tokens WHERE token_hash=?", (token_hash,)).fetchone()
    if not row or row["used_at"] or datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc): raise HTTPException(422, "Ссылка недействительна или истекла")
    salt = secrets.token_bytes(24); digest = password_digest(item.new_password, salt)
    with db() as c:
        c.execute("UPDATE admin_profile SET password_salt=?,password_hash=?,updated_at=? WHERE id=1", (base64.b64encode(salt).decode(),base64.b64encode(digest).decode(),now()))
        c.execute("UPDATE password_reset_tokens SET used_at=? WHERE id=?", (now(),row["id"]))
    return {"ok":True}


@app.get("/api/profile")
def get_profile(_: str = Depends(auth)):
    with db() as c: row = c.execute("SELECT username,display_name,email,phone,organization,updated_at FROM admin_profile WHERE id=1").fetchone()
    return dict(row)


@app.put("/api/profile")
def save_profile(item: ProfileIn, _: str = Depends(auth)):
    with db() as c: c.execute("UPDATE admin_profile SET display_name=?,email=?,phone=?,organization=?,updated_at=? WHERE id=1", (item.display_name,item.email or None,item.phone or None,item.organization or None,now()))
    return {"ok":True}


@app.post("/api/profile/password")
def change_profile_password(item: PasswordChangeIn, _: str = Depends(auth)):
    if not verify_admin_password(item.current_password): raise HTTPException(422, "Текущий пароль указан неверно")
    if secrets.compare_digest(item.current_password, item.new_password): raise HTTPException(422, "Новый пароль должен отличаться от текущего")
    salt = secrets.token_bytes(24); digest = password_digest(item.new_password, salt)
    with db() as c: c.execute("UPDATE admin_profile SET password_salt=?,password_hash=?,updated_at=? WHERE id=1", (base64.b64encode(salt).decode(),base64.b64encode(digest).decode(),now()))
    return {"ok":True}


@app.get("/api/smtp")
def smtp_status(_: str = Depends(auth)):
    settings = get_smtp_settings()
    if not settings: return {"configured":False}
    return {"configured":True,"host":settings["host"],"port":settings["port"],"username":settings["username"],"sender_email":settings["sender_email"],"security":settings["security"],"password_saved":bool(settings["password"])}


@app.put("/api/smtp")
def save_smtp(item: SMTPSettingsIn, _: str = Depends(auth)):
    current = get_smtp_settings(); settings = item.model_dump()
    settings["password"] = settings["password"] or (current["password"] if current else "")
    with db() as c: profile = c.execute("SELECT email FROM admin_profile WHERE id=1").fetchone()
    if not profile or not profile["email"]: raise HTTPException(422, "Сначала укажите email в профиле")
    try: smtp_send(settings, profile["email"], "RouterVault — проверка SMTP", "Настройки почты сохранены. Восстановление пароля доступно.")
    except Exception as exc: raise HTTPException(422, f"Проверка SMTP не пройдена: {str(exc)}")
    with db() as c: c.execute("INSERT INTO smtp_settings(id,host,port,username,password_enc,sender_email,security,updated_at) VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET host=excluded.host,port=excluded.port,username=excluded.username,password_enc=excluded.password_enc,sender_email=excluded.sender_email,security=excluded.security,updated_at=excluded.updated_at", (settings["host"],settings["port"],settings["username"] or None,cipher().encrypt(settings["password"].encode()).decode() if settings["password"] else None,settings["sender_email"],settings["security"],now()))
    return {"ok":True}


@app.post("/api/logout", status_code=204)
def logout():
    from fastapi import Response
    result = Response(status_code=204); result.delete_cookie("routervault_session", path="/"); return result


@app.get("/health", include_in_schema=False)
def health(): return {"status": "ok"}


@app.get("/static/{file_name}")
def static(file_name: str):
    if file_name not in {"app.css", "app.js", "login.css", "login.js", "enhancements.css", "storage.css", "storage.js", "settings.css", "settings.js", "profile.js", "setup.css", "setup.js", "recovery.js"}: raise HTTPException(404)
    return FileResponse(ROOT / "static" / file_name)


@app.get("/api/dashboard")
def dashboard(_: str = Depends(auth)):
    with db() as c:
        routers = [public_router(r) for r in c.execute("SELECT * FROM routers ORDER BY created_at DESC").fetchall()]
        raw_backups = [dict(r) for r in c.execute("SELECT b.*,r.name router_name,r.host FROM backups b JOIN routers r ON r.id=b.router_id ORDER BY b.created_at DESC LIMIT 200").fetchall()]
        storage = c.execute("SELECT endpoint,region,bucket,addressing_style,sse,updated_at FROM storage_settings WHERE id=1").fetchone()
    runs_by_id = {}
    backups = []
    for item in raw_backups:
        encrypted = bool(item.pop("backup_password_enc", None)); backups.append(item)
        run_id = item["run_id"] or item["created_at"]
        run = runs_by_id.setdefault(run_id, {"id":run_id,"router_id":item["router_id"],"router_name":item["router_name"],"host":item["host"],"created_at":item["created_at"],"status":"success","stage":item.get("stage"),"attempts":0,"error":None,"size":0,"encrypted":False,"artifacts":[]})
        run["attempts"] = max(run["attempts"], item.get("attempts") or 0); run["encrypted"] = run["encrypted"] or encrypted
        if item["format"] in {"backup", "export"} and item["filename"]:
            run["artifacts"].append({key:item[key] for key in ("id","filename","size","format","status","sha256")})
            run["size"] += item["size"] or 0
        if item["status"] == "running": run["status"] = "running"
        elif item["status"] == "failed": run["status"] = "partial" if run["artifacts"] else "failed"; run["error"] = item["error"]; run["stage"] = item.get("stage")
    backup_runs = list(runs_by_id.values())[:100]
    settings = get_app_settings()
    failed_count = sum(1 for router in routers if router["last_status"] == "failed"); running_count = sum(1 for router in routers if router["last_status"] == "running")
    system_status = {"state":"running" if running_count else ("degraded" if failed_count else "healthy"), "failed_devices":failed_count, "running_devices":running_count, "storage_configured":bool(storage), "scheduler_running":bool(getattr(app.state, "scheduler", None) and app.state.scheduler.running), "last_system_backup_at":settings["last_system_backup_at"]}
    return {"routers": routers, "backups": backups, "backup_runs":backup_runs, "storage": dict(storage) if storage else None, "system_status":system_status, "schedule": f"{settings['backup_hour']:02d}:{settings['backup_minute']:02d}", "timezone": settings["timezone"], "retention_runs": settings["retention_runs"]}


@app.get("/api/settings")
def app_settings(_: str = Depends(auth)): return get_app_settings()


@app.post("/api/settings")
def save_app_settings(item: AppSettingsIn, _: str = Depends(auth)):
    current = get_telegram_settings()
    token = item.telegram_token or current["token"]
    if item.telegram_enabled and (not token or not item.telegram_chat_id): raise HTTPException(422, "Для Telegram укажите Bot Token и Chat ID")
    with db() as c:
        c.execute("UPDATE app_settings SET backup_hour=?,backup_minute=?,timezone=?,retention_runs=?,telegram_enabled=?,telegram_token_enc=?,telegram_chat_id=?,updated_at=? WHERE id=1", (item.backup_hour,item.backup_minute,item.timezone,item.retention_runs,int(item.telegram_enabled),cipher().encrypt(token.encode()).decode() if token else None,item.telegram_chat_id or None,now()))
    schedule_backups()
    return {"ok": True, **get_app_settings()}


@app.post("/api/settings/telegram/check")
def check_telegram(_: str = Depends(auth)):
    settings = get_telegram_settings()
    if not settings["token"] or not settings["chat_id"]: raise HTTPException(422, "Сначала сохраните Bot Token и Chat ID")
    if not send_telegram("✅ RouterVault: тестовое уведомление доставлено.", force=True): raise HTTPException(422, "Telegram не принял сообщение. Проверьте токен, Chat ID и доступ в интернет")
    return {"ok":True}


@app.get("/api/storage")
def storage_status(_: str = Depends(auth)):
    with db() as c: row = c.execute("SELECT endpoint,region,bucket,addressing_style,sse,updated_at FROM storage_settings WHERE id=1").fetchone()
    return {"configured": bool(row), "credentials_saved": bool(row), **(dict(row) if row else {})}


@app.post("/api/storage/check")
def check_storage(item: S3SettingsIn, _: str = Depends(auth)):
    settings = complete_s3_settings(item)
    try:
        _, exists = inspect_bucket(settings)
        return {"ok": True, "exists": exists, "message": f"Bucket {item.bucket} доступен" if exists else f"Подключение работает. Bucket {item.bucket} будет создан при сохранении"}
    except Exception as exc:
        raise HTTPException(422, f"Не удалось подключиться к S3: {str(exc)}")


@app.post("/api/storage")
def save_storage(item: S3SettingsIn, _: str = Depends(auth)):
    settings = complete_s3_settings(item)
    try:
        client, exists = inspect_bucket(settings)
        if not exists: create_bucket(client, settings)
    except Exception as exc:
        raise HTTPException(422, f"Не удалось проверить или создать bucket: {str(exc)}")
    with db() as c:
        c.execute("INSERT INTO storage_settings(id,endpoint,region,bucket,access_key_enc,secret_key_enc,addressing_style,sse,updated_at) VALUES(1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET endpoint=excluded.endpoint,region=excluded.region,bucket=excluded.bucket,access_key_enc=excluded.access_key_enc,secret_key_enc=excluded.secret_key_enc,addressing_style=excluded.addressing_style,sse=excluded.sse,updated_at=excluded.updated_at", (settings["endpoint"],settings["region"],settings["bucket"],cipher().encrypt(settings["access_key"].encode()).decode(),cipher().encrypt(settings["secret_key"].encode()).decode(),settings["addressing_style"],settings["sse"],now()))
    return {"ok": True, "created": not exists, "message": f"Bucket {item.bucket} создан и подключён" if not exists else f"Bucket {item.bucket} подключён"}


@app.post("/api/routers", status_code=201)
def add_router(item: RouterIn, _: str = Depends(auth)):
    check = check_router_access(item.host, item.port, item.username, item.password)
    router_id = str(uuid.uuid4())
    try:
        with db() as c: c.execute("INSERT INTO routers(id,name,host,port,username,password_enc,identity,routeros_version,host_key_fingerprint,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (router_id,item.name,item.host,item.port,item.username,cipher().encrypt(item.password.encode()).decode(),check["identity"],check["routeros_version"],check["host_key_fingerprint"],int(item.enabled),now()))
    except sqlite3.IntegrityError: raise HTTPException(409, "Устройство с таким адресом уже существует")
    return {"id": router_id, "check": check}


@app.put("/api/routers/{router_id}")
def update_router(router_id: str, item: RouterUpdate, _: str = Depends(auth)):
    with db() as c: router = c.execute("SELECT * FROM routers WHERE id=?", (router_id,)).fetchone()
    if not router: raise HTTPException(404, "Роутер не найден")
    if router["last_status"] == "running": raise HTTPException(409, "Дождитесь завершения текущего бэкапа")
    password = item.password or cipher().decrypt(router["password_enc"].encode()).decode()
    same_endpoint = item.host == router["host"] and item.port == router["port"]
    check = check_router_access(item.host, item.port, item.username, password, router["host_key_fingerprint"] if same_endpoint else None)
    encrypted = cipher().encrypt(password.encode()).decode() if item.password else router["password_enc"]
    try:
        with db() as c:
            c.execute("UPDATE routers SET name=?,host=?,port=?,username=?,password_enc=?,identity=?,routeros_version=?,host_key_fingerprint=?,enabled=?,last_error=NULL WHERE id=?", (item.name,item.host,item.port,item.username,encrypted,check["identity"],check["routeros_version"],check["host_key_fingerprint"],int(item.enabled),router_id))
    except sqlite3.IntegrityError: raise HTTPException(409, "Устройство с таким адресом уже существует")
    return {"id": router_id, "check": check}


@app.post("/api/routers/check")
def check_new_router(item: RouterIn, _: str = Depends(auth)):
    return check_router_access(item.host, item.port, item.username, item.password)


@app.post("/api/routers/{router_id}/check")
def check_saved_router(router_id: str, _: str = Depends(auth)):
    with db() as c: router = c.execute("SELECT * FROM routers WHERE id=?", (router_id,)).fetchone()
    if not router: raise HTTPException(404, "Роутер не найден")
    password = cipher().decrypt(router["password_enc"].encode()).decode()
    try:
        result = check_router_access(router["host"], router["port"], router["username"], password, router["host_key_fingerprint"])
        with db() as c: c.execute("UPDATE routers SET identity=?,routeros_version=?,host_key_fingerprint=?,last_error=NULL WHERE id=?", (result["identity"],result["routeros_version"],result["host_key_fingerprint"],router_id))
        return result
    except HTTPException as exc:
        with db() as c: c.execute("UPDATE routers SET last_error=? WHERE id=?", (str(exc.detail)[:1000], router_id))
        raise


@app.delete("/api/routers/{router_id}", status_code=204)
def delete_router(router_id: str, _: str = Depends(auth)):
    with db() as c:
        router = c.execute("SELECT last_status FROM routers WHERE id=?", (router_id,)).fetchone()
        if not router: raise HTTPException(404, "Роутер не найден")
        if router["last_status"] == "running": raise HTTPException(409, "Нельзя удалить устройство во время бэкапа")
        c.execute("DELETE FROM routers WHERE id=?", (router_id,))


@app.post("/api/routers/{router_id}/backup", status_code=202)
def backup_router(router_id: str, tasks: BackgroundTasks, _: str = Depends(auth)):
    with db() as c:
        if not c.execute("SELECT 1 FROM routers WHERE id=?", (router_id,)).fetchone(): raise HTTPException(404, "Роутер не найден")
    claim = claim_backup(router_id)
    if not claim: raise HTTPException(409, "Бэкап этого устройства уже выполняется")
    tasks.add_task(run_backup, router_id, claim); return {"status":"queued", "backup_id":claim[0]}


@app.post("/api/backups/run", status_code=202)
def backup_all(tasks: BackgroundTasks, _: str = Depends(auth)):
    tasks.add_task(run_all); return {"status":"queued"}


@app.get("/api/backups/{backup_id}/download")
def download(backup_id: str, _: str = Depends(auth)):
    with db() as c: item = c.execute("SELECT * FROM backups WHERE id=? AND status='success'", (backup_id,)).fetchone()
    if not item: raise HTTPException(404, "Бэкап не найден")
    settings = get_s3_settings()
    url = s3_client(settings).generate_presigned_url("get_object", Params={"Bucket":settings["bucket"],"Key":item["s3_key"],"ResponseContentDisposition":f'attachment; filename="{item["filename"]}"'}, ExpiresIn=300)
    return RedirectResponse(url)


@app.get("/api/backup-runs/{run_id}/password")
def backup_password(run_id: str, _: str = Depends(auth)):
    with db() as c: item = c.execute("SELECT backup_password_enc FROM backups WHERE run_id=? AND backup_password_enc IS NOT NULL LIMIT 1", (run_id,)).fetchone()
    if not item: raise HTTPException(404, "Для этого бэкапа пароль не сохранён")
    return {"password":cipher().decrypt(item["backup_password_enc"].encode()).decode()}
