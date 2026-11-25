"""The micro-apps this portal's homeport image holds, read from its registry.

Homeport writes ``micro-apps.json`` next to its ``index.html`` when the image
is built, listing exactly the apps the image contains. The image stays the
source of truth: this module only reads that file, so that a setting can be
refused for an app the portal does not have. It never fails a request because
the file cannot be read; it then knows nothing.
"""

import logging

import requests
from django.core.cache import cache

from waldur_core.configuration import config

logger = logging.getLogger(__name__)

CACHE_KEY = "waldur_core.users.micro_apps_registry"
CACHE_TIMEOUT = 300
# A failed read is remembered for less long, so a portal that comes up after
# the backend is noticed soon.
FAILURE_CACHE_TIMEOUT = 60
REQUEST_TIMEOUT = 3
_UNKNOWN = "unknown"


def registry_url() -> str | None:
    base = (config.HOMEPORT_URL or "").strip()
    if not base:
        return None
    return base.rstrip("/") + "/micro-apps.json"


def _fetch() -> frozenset[str] | None:
    url = registry_url()
    if not url:
        return None
    try:
        response = requests.get(
            url, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"}
        )
        response.raise_for_status()
        apps = response.json().get("apps")
    except (requests.RequestException, ValueError, AttributeError) as error:
        logger.info("Micro-apps registry at %s is not readable: %s", url, error)
        return None
    if not isinstance(apps, list):
        return None
    return frozenset(
        app["name"]
        for app in apps
        if isinstance(app, dict) and isinstance(app.get("name"), str)
    )


def deployed_micro_app_names() -> frozenset[str] | None:
    """Names of the apps in the portal's image, or None when they are unknown."""
    cached = cache.get(CACHE_KEY)
    if cached == _UNKNOWN:
        return None
    if cached is not None:
        return cached
    names = _fetch()
    if names is None:
        cache.set(CACHE_KEY, _UNKNOWN, FAILURE_CACHE_TIMEOUT)
    else:
        cache.set(CACHE_KEY, names, CACHE_TIMEOUT)
    return names
