import os
from urllib.parse import urljoin

from django.conf import settings


def get_static_path():
    base_path = settings.BASE_DIR
    if os.path.basename(base_path) != "src":
        base_path = os.path.join(base_path, "src")
    return os.path.join(base_path, "waldur_core/core/static")


static_path = get_static_path()

LOGO_MAP = {
    "LOGIN_LOGO": "api/icons/login_logo/",
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
    "DISCLAIMER_AREA_LOGO": "api/icons/disclaimer_area_logo/",
}

DEFAULT_LOGOS = {
    "LOGIN_LOGO": static_path + "/waldur_core/img/login_logo.png",
    "FAVICON": static_path + "/waldur_core/img/favicon.ico",
}


def build_logo_url(path: str, request=None) -> str:
    """
    Build an absolute URL for a whitelabeling icon endpoint.

    Uses WALDUR_CORE['MASTERMIND_URL'] when configured so that /api/configuration/
    returns stable public URLs"""
    base_url = (settings.WALDUR_CORE.get("MASTERMIND_URL") or "").strip()
    normalized_path = path.lstrip("/")
    if base_url:
        return urljoin(base_url.rstrip("/") + "/", normalized_path)
    if request is not None:
        return request.build_absolute_uri("/" + normalized_path)
    return "/" + normalized_path
