"""
WSGI config for Waldur Core.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/2.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application  # noqa: F402

import waldur_core  # noqa: F401 pre-load waldur monkey-patching methods

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "waldur_core.server.settings")

application = get_wsgi_application()
