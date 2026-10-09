import os
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get('DEBUG') == '1'
BUILD_STATIC = os.environ.get('BUILD_STATIC') == '1'
SECRET_KEY = os.environ.get('SECRET_KEY', '')
PASSWORD_INDEX_KEY = os.environ.get('PASSWORD_INDEX_KEY', '')
PASSWORD_ENCRYPTION_KEY = os.environ.get('PASSWORD_ENCRYPTION_KEY', '')
if DEBUG or BUILD_STATIC:
    SECRET_KEY = SECRET_KEY or 'local-development-only-never-use-in-production'
    PASSWORD_INDEX_KEY = PASSWORD_INDEX_KEY or 'local-index-key-only-never-use-in-production'
    PASSWORD_ENCRYPTION_KEY = PASSWORD_ENCRYPTION_KEY or 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA='
if len(SECRET_KEY) < 40 or len(PASSWORD_INDEX_KEY) < 40 or SECRET_KEY == PASSWORD_INDEX_KEY:
    raise ImproperlyConfigured('請設定不同且至少 40 字元的 SECRET_KEY 與 PASSWORD_INDEX_KEY。')
try:
    Fernet(PASSWORD_ENCRYPTION_KEY.encode())
except (ValueError, TypeError):
    raise ImproperlyConfigured('請設定有效的 PASSWORD_ENCRYPTION_KEY。') from None
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1' if DEBUG or BUILD_STATIC else '').split(',')
PUBLIC_URL = os.environ.get('PUBLIC_URL', 'http://localhost:8000' if DEBUG or BUILD_STATIC else '').rstrip('/')
if not DEBUG and not BUILD_STATIC and (not ALLOWED_HOSTS[0] or urlparse(PUBLIC_URL).scheme != 'https' or len(PUBLIC_URL) > 200):
    raise ImproperlyConfigured('正式環境需要 ALLOWED_HOSTS 與最多 200 字元的 HTTPS PUBLIC_URL。')
INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'election']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware', 'django.contrib.messages.middleware.MessageMiddleware', 'election.middleware.PrivateMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': os.environ.get('DATABASE_PATH', BASE_DIR / 'data/db.sqlite3'), 'OPTIONS': {'timeout': 10, 'transaction_mode': 'IMMEDIATE'}, 'TEST': {'NAME': str(Path(os.environ.get('DATABASE_PATH', BASE_DIR / 'data/db.sqlite3')).parent / 'test.sqlite3')}}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LANGUAGE_CODE = 'zh-hant'
TIME_ZONE = 'Pacific/Auckland'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_TRUSTED_ORIGINS = [PUBLIC_URL] if PUBLIC_URL else []
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = not DEBUG and not BUILD_STATIC
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_CONTENT_TYPE_NOSNIFF = True
# HTTPS CSRF checks require origin/referer headers on same-site forms.
SECURE_REFERRER_POLICY = 'same-origin'
PDF_FONT_PATH = os.environ.get('PDF_FONT_PATH', str(BASE_DIR / 'fonts/NotoSansTC-Regular.ttf'))
TRUSTED_PROXY_IPS = os.environ.get('TRUSTED_PROXY_IPS', '').split(',')
CSRF_FAILURE_VIEW = 'election.views.csrf_failure'
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000
