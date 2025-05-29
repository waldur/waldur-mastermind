import os

from django.conf import settings


def get_static_path():
    base_path = settings.BASE_DIR
    if os.path.basename(base_path) != "src":
        base_path = os.path.join(base_path, "src")
    return os.path.join(base_path, "waldur_core/core/static")


static_path = get_static_path()

LOGO_MAP = {
    "LOGIN_LOGO": "api/icons/login_logo/",
    "SITE_LOGO": "api/icons/site_logo/",
    "SIDEBAR_LOGO": "api/icons/sidebar_logo/",
    "SIDEBAR_LOGO_MOBILE": "api/icons/sidebar_logo_mobile/",
    "SIDEBAR_LOGO_DARK": "api/icons/sidebar_logo_dark/",
    "POWERED_BY_LOGO": "api/icons/powered_by_logo/",
    "HERO_IMAGE": "api/icons/hero_image/",
    "MARKETPLACE_HERO_IMAGE": "api/icons/marketplace_hero_image/",
    "CALL_MANAGEMENT_HERO_IMAGE": "api/icons/call_management_hero_image/",
    "FAVICON": "api/icons/favicon/",
    "OFFERING_LOGO_PLACEHOLDER": "api/icons/offering_logo_placeholder/",
    "KEYCLOAK_ICON": "api/icons/keycloak_icon/",
}

DEFAULT_LOGOS = {
    "LOGIN_LOGO": static_path + "/waldur_core/img/login_logo.png",
    "FAVICON": static_path + "/waldur_core/img/favicon.ico",
}
