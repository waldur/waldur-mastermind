"""
WSGI config for Waldur Core.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/1.11/howto/deployment/wsgi/
"""

import os
import waldur_core  # noqa: F401 pre-load NC monkey-patching methods

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "waldur_core.server.settings")

from django.core.wsgi import get_wsgi_application  # noqa: F402
application = get_wsgi_application()
