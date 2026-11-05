"""
Django settings for Smart Gym Backend.
"""

import os
from pathlib import Path
from datetime import timedelta
from decouple import config, Csv
from django.core.exceptions import ImproperlyConfigured
import dj_database_url


BASE_DIR = Path(__file__).resolve().parent.parent

# ============================================
# SECURITY
# ============================================
# No default: a missing SECRET_KEY must crash startup, not fall back to a guessable key.
SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=True, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())


# ============================================
# APPLICATIONS
# ============================================
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',

    # Local apps
    'apps.core',
    'apps.accounts',
    'apps.members',
    'apps.workouts',
    'apps.catalog',
    'apps.payments',
    'apps.nutrition',
    'apps.shop',
    'apps.notifications',
    'apps.admin_api',
    'apps.facilities',
    'apps.classes',
    'apps.staff',
    'apps.reports',
    'apps.cms',
    'apps.attendance',
    'apps.dashboard',
    'apps.public_api',
    'apps.site_content',
    'apps.zatca',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',           # first
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',      # <-- must be here
    'django.contrib.sessions.middleware.SessionMiddleware',
    'apps.core.middleware.LanguageMiddleware',         # Accept-Language: ar|en from the portals
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ============================================
# DATABASE
# ============================================
DATABASES = {
    'default': dj_database_url.config(
        default=config('DATABASE_URL', default=f'sqlite:///{BASE_DIR / "db.sqlite3"}'),
        conn_max_age=600,
        ssl_require=not DEBUG,
    )
}

# Safety: DEBUG=True means local development, and local development must never
# touch the production (Neon) database. Crash instead of silently using it.
# A deliberate production task needs DEBUG=False in that shell.
if DEBUG and 'neon.tech' in str(DATABASES['default'].get('HOST', '')):
    raise ImproperlyConfigured(
        'DEBUG=True with the production Neon database. Local work must use '
        'DATABASE_URL=sqlite:///db.sqlite3 (check .env and any "set DATABASE_URL" in this window). '
        'For a deliberate production task, also set DEBUG=False.'
    )

# ============================================
# AUTH
# ============================================
AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ============================================
# INTERNATIONALIZATION
# ============================================
LANGUAGE_CODE = 'en-us'
LANGUAGES = [('en', 'English'), ('ar', 'العربية')]
TIME_ZONE = config('TIME_ZONE', default='Asia/Riyadh')   # gym-local dates, trainer hours, check-in days
GYM_CAPACITY = config('GYM_CAPACITY', default=0, cast=int)   # 0 = sum of active facility capacities
SHOP_VAT_RATE = config('SHOP_VAT_RATE', default='0.15')   # Saudi VAT
# Seller details printed on receipts; with a VAT number receipts also carry the ZATCA QR.
GYM_LEGAL_NAME = config('GYM_LEGAL_NAME', default='Smart Gym')
GYM_VAT_NUMBER = config('GYM_VAT_NUMBER', default='')
# Locker credit (top-ups, paying with credit, refunds to credit) — off for now ("coming soon");
# cancellations are refunded to the card instead. Nothing is deleted: set True to bring it back.
LOCKER_CREDIT_ENABLED = config('LOCKER_CREDIT_ENABLED', default=False, cast=bool)
# Referrals: the public website (its signup page is /join) and the locker credit a referrer gets.
PUBLIC_SITE_URL = config('PUBLIC_SITE_URL', default='https://smart-gym-three.vercel.app')
REFERRAL_REWARD_SAR = config('REFERRAL_REWARD_SAR', default='50')
GYM_LEGAL_NAME_AR = config('GYM_LEGAL_NAME_AR', default='سمارت جيم')
GYM_ADDRESS_AR = config('GYM_ADDRESS_AR', default='شارع الإمام محمد بن سعود، المنتزه، الخرج 16942، المملكة العربية السعودية')
GYM_ADDRESS = config('GYM_ADDRESS', default='Al Imam Muhammad Ibn Saud St, Al Muntazah, Al-Kharj 16942, Saudi Arabia')
# ZATCA Phase 2 e-invoicing (apps.zatca). The key encrypts the device's private key and ZATCA tokens at
# rest; the cron secret guards the retry endpoint that an external scheduler calls every few minutes.
ZATCA_ENCRYPTION_KEY = config('ZATCA_ENCRYPTION_KEY', default='')
ZATCA_CRON_SECRET = config('ZATCA_CRON_SECRET', default='')
ZATCA_SEND_INLINE = config('ZATCA_SEND_INLINE', default=False, cast=bool)   # True: report in the request (tests)
# Public base URL of this API, used in emailed receipt links.
BACKEND_PUBLIC_URL = config('BACKEND_PUBLIC_URL', default='https://gym-backend-xi-seven.vercel.app')
USE_I18N = True
USE_TZ = True

# ============================================
# STATIC & MEDIA
# ============================================
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles_build' / 'static'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Media uploads (registration photos, avatars), first match wins:
#   1. Cloudinary  — when CLOUDINARY_CLOUD_NAME is set (apps/core/storage.py)
#   2. Cloudflare R2 — when R2_ACCESS_KEY_ID is set
#   3. local disk  — ./media (on Vercel: /tmp, lost on cold start)
CLOUDINARY_CLOUD_NAME    = config('CLOUDINARY_CLOUD_NAME', default='')
CLOUDINARY_API_KEY       = config('CLOUDINARY_API_KEY', default='')
CLOUDINARY_API_SECRET    = config('CLOUDINARY_API_SECRET', default='')
CLOUDINARY_FOLDER        = config('CLOUDINARY_FOLDER', default='smart-gym')
# "authenticated" = private, served only through signed URLs; "upload" = public
CLOUDINARY_DELIVERY_TYPE = config('CLOUDINARY_DELIVERY_TYPE', default='authenticated')
USE_CLOUDINARY = bool(CLOUDINARY_CLOUD_NAME)

if USE_CLOUDINARY and not all([CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
    raise ImproperlyConfigured(
        'CLOUDINARY_CLOUD_NAME is set, so CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET '
        'must be set too (or unset CLOUDINARY_CLOUD_NAME).'
    )
if CLOUDINARY_DELIVERY_TYPE not in ('authenticated', 'upload'):
    raise ImproperlyConfigured("CLOUDINARY_DELIVERY_TYPE must be 'authenticated' or 'upload'.")

R2_ACCESS_KEY_ID     = config('R2_ACCESS_KEY_ID', default='')
R2_SECRET_ACCESS_KEY = config('R2_SECRET_ACCESS_KEY', default='')
R2_BUCKET_NAME       = config('R2_BUCKET_NAME', default='')
R2_ENDPOINT_URL      = config('R2_ENDPOINT_URL', default='')
USE_R2 = bool(R2_ACCESS_KEY_ID)

if USE_R2 and not all([R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_ENDPOINT_URL]):
    raise ImproperlyConfigured(
        'R2_ACCESS_KEY_ID is set, so R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME and '
        'R2_ENDPOINT_URL must be set too (or unset R2_ACCESS_KEY_ID to use local storage).'
    )

if not (USE_CLOUDINARY or USE_R2) and os.environ.get('VERCEL'):
    # Vercel's deployment dir is read-only; /tmp is writable but wiped on cold
    # start and not shared between instances — uploads there are temporary.
    MEDIA_ROOT = Path('/tmp/media')
    # warnings, not logging — LOGGING isn't configured yet while settings load.
    import warnings
    warnings.warn('No media storage configured on Vercel (Cloudinary/R2): uploads go to /tmp and will be lost.')

if USE_CLOUDINARY:
    DEFAULT_STORAGE = {"BACKEND": "apps.core.storage.CloudinaryMediaStorage"}
elif USE_R2:
    DEFAULT_STORAGE = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "access_key": R2_ACCESS_KEY_ID,
            "secret_key": R2_SECRET_ACCESS_KEY,
            "bucket_name": R2_BUCKET_NAME,
            "endpoint_url": R2_ENDPOINT_URL,
            "region_name": "auto",
            "signature_version": "s3v4",
            "default_acl": None,        # R2 ignores ACLs; bucket stays private
            "querystring_auth": True,   # photos served via signed URLs
            "querystring_expire": 3600,
            "file_overwrite": False,
        },
    }
else:
    DEFAULT_STORAGE = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    os.makedirs(MEDIA_ROOT, exist_ok=True)

# Django 5.1+ / 6.x unified storage config.
# BOTH keys are required — "default" handles FileField/ImageField
# uploads, "staticfiles" handles collectstatic.
STORAGES = {
    "default": DEFAULT_STORAGE,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Whitenoise config — makes collectstatic run at every boot on Vercel
WHITENOISE_MANIFEST_STRICT = False
WHITENOISE_ALLOW_ALL_ORIGINS = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ============================================
# DJANGO REST FRAMEWORK
# ============================================
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        # JWT + the "must change password" lock (apps/accounts/authentication.py)
        'apps.accounts.authentication.EnforcingJWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    # Only views that opt in are throttled (e.g. the public contact form).
    'DEFAULT_THROTTLE_RATES': {
        'contact': config('CONTACT_THROTTLE_RATE', default='5/hour'),
    },
    'DEFAULT_RENDERER_CLASSES': (
        'apps.core.renderers.BilingualJSONRenderer',      # messages in the reader's language
    ),
}

# ============================================
# JWT
# ============================================
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# ============================================
# CSRF
# ============================================
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='https://gym-member-and-admin.vercel.app,https://gym-backend-xi-seven.vercel.app',
    cast=Csv(),
)

# ============================================
# CORS
# ============================================
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:5173',
    cast=Csv()
)

CORS_ALLOW_CREDENTIALS = True

CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
]

# ============================================
# CUSTOM SETTINGS
# ============================================
FRONTEND_URL = config('FRONTEND_URL', default='http://localhost:5173')
# Where new public contact-form messages are emailed (optional; they're always in the admin inbox).
CONTACT_NOTIFY_EMAIL = config('CONTACT_NOTIFY_EMAIL', default='')


# ============================================
# PAYMENT GATEWAY
# ============================================
# Moyasar (Mada / STC Pay). The secret key never leaves the backend.
# Missing keys only break the payment endpoints (see apps/public_api/moyasar.py),
# not the whole API.
MOYASAR_SECRET_KEY      = config('MOYASAR_SECRET_KEY', default='')
MOYASAR_PUBLISHABLE_KEY = config('MOYASAR_PUBLISHABLE_KEY', default='')
MOYASAR_WEBHOOK_SECRET  = config('MOYASAR_WEBHOOK_SECRET', default='')
MOYASAR_API_URL         = config('MOYASAR_API_URL', default='https://api.moyasar.com/v1')

# ============================================
# EMAIL (stub)
# ============================================
EMAIL_BACKEND = config(
    'EMAIL_BACKEND',
    default='django.core.mail.backends.console.EmailBackend',
)
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@smartgym.local')

# SMTP (used when EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend).
# Defaults are SendGrid's: the username is literally "apikey", the password is the API key.
EMAIL_HOST          = config('EMAIL_HOST', default='smtp.sendgrid.net')
EMAIL_PORT          = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS       = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER     = config('EMAIL_HOST_USER', default='apikey')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
EMAIL_TIMEOUT       = config('EMAIL_TIMEOUT', default=10, cast=int)   # don't hang a serverless request

if EMAIL_BACKEND == 'django.core.mail.backends.smtp.EmailBackend' and not EMAIL_HOST_PASSWORD:
    raise ImproperlyConfigured('EMAIL_BACKEND is SMTP but EMAIL_HOST_PASSWORD (the SendGrid API key) is not set.')


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}

# ============================================
# PRODUCTION SECURITY
# ============================================
if not DEBUG:
    # Required behind Vercel's proxy so Django knows the request is HTTPS
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

    # Cookies only sent over HTTPS
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    # HSTS — tells browsers to always use HTTPS
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

 