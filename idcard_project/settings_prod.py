from .settings import *
import os
import dj_database_url

DEBUG = False
SECRET_KEY = os.environ.get('SECRET_KEY', SECRET_KEY)
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost').split(',')
CSRF_TRUSTED_ORIGINS = ['https://' + h for h in ALLOWED_HOSTS if h != 'localhost']

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    DATABASES = {'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600)}

STATIC_ROOT = BASE_DIR / 'static_root'
STATICFILES_STORAGE = 'whitenoise.storage.StaticFilesStorage'

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)
