# Local-only: dev settings with the OpenPortal plugin switched on, so the
# frontend receives ENV.plugins.WALDUR_OPENPORTAL.ENABLED = true.
from waldur_core.server.dev_settings import *  # noqa

WALDUR_OPENPORTAL = {
    "ENABLED": True,
    "DEFAULT_LIMITS": {"NODE": 1000},
}
