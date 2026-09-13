import os

from waldur_core.server.dev_settings import *  # noqa: F401,F403

# Let staff open `waldur shell` from the user menu. The page itself is served
# by a separate process started with these same settings:
#   uv run waldur web_shell --fetch-assets   (once)
#   uv run waldur web_shell
WALDUR_CORE["WEB_SHELL_ENABLED"] = True  # noqa: F405
WALDUR_CORE["WEB_SHELL_URL"] = os.environ.get(  # noqa: F405
    "WALDUR_WEB_SHELL_URL", "http://localhost:18090/webshell/"
)
