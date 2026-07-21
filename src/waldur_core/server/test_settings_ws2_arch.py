# Throwaway ws2 test settings with a UNIQUE test DB name so this run cannot
# collide with the other workspace's concurrent pytest migrations on the shared
# local postgres. Safe to delete after use.
from waldur_core.server.test_settings_local import *  # noqa

DATABASES["default"]["TEST"] = {"NAME": "test_waldur_ws2_arch"}  # noqa: F405
