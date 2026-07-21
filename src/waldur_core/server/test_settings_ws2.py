# Local ws2 test settings: isolate the test DB so both workspaces can run
# pytest against the shared local postgres without colliding.
from waldur_core.server.test_settings_local import *  # noqa

DATABASES["default"]["TEST"] = {"NAME": "test_waldur_ws2"}  # noqa: F405
