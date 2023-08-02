import os

from django.conf import settings

static_path = os.path.join(settings.BASE_DIR, 'static')

LOGO_MAP = {
    'LOGIN_LOGO': 'api/icons/login_logo/',
    'SITE_LOGO': 'api/icons/site_logo/',
    'SIDEBAR_LOGO': 'api/icons/sidebar_logo/',
    'SIDEBAR_LOGO_MOBILE': 'api/icons/sidebar_logo_mobile/',
    'POWERED_BY_LOGO': 'api/icons/powered_by_logo/',
    'HERO_IMAGE': 'api/icons/hero_image/',
    'FAVICON': 'api/icons/favicon/',
}

DEFAULT_LOGOS = {
    'LOGIN_LOGO': static_path + '/waldur_core/img/login_logo.png',
    'FAVICON': static_path + '/waldur_core/img/favicon.ico',
}
