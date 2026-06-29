import os
from cvat.settings.production import *

SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_VERIFICATION = False
ACCOUNT_EMAIL_VERIFICATION = False
SOCIALACCOUNT_LOGIN_ON_GET = True

SOCIALACCOUNT_PROVIDERS = {
    "openid_connect": {
        "OAUTH_PKCE_ENABLED": True,
        "APPS": [
            {
                "provider_id": "keycloak",
                "name": "Keycloak",
                "client_id": os.environ["OIDC_CLIENT_ID"],
                "secret": os.environ["OIDC_CLIENT_SECRET"],
                "settings": {
                    "server_url": os.environ["OIDC_SERVER_URL"],
                    "fetch_userinfo": True,
                },
            },
        ]
    },
}

CSRF_TRUSTED_ORIGINS = [
    "https://*.quantrium.ai"
]

USE_X_FORWARDED_HOST = True
TIME_ZONE = "Asia/Kolkata"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "http")

INSTALLED_APPS += ['allauth.socialaccount.providers.openid_connect']

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_DOMAIN = "cvat.quantrium.ai"

ACCOUNT_FORMS = {
    'add_email': None,
    'change_password': None,
    'confirm_login_code': None,
    'login': None,
    'request_login_code': None,
    'reset_password': None,
    'reset_password_from_key': None,
    'set_password': None,
    'signup': None,
    'user_token': None,
}
