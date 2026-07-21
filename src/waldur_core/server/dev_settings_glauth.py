"""Dev settings variant for the local glauth scenario.

Identical to dev_settings, but also trusts the Docker-gateway hostname so the
glauth LDAP container (running under Docker Desktop) can reach this host-run
backend at http://host.docker.internal:<port>/api/ without tripping Django's
ALLOWED_HOSTS check.
"""

from waldur_core.server.dev_settings import *  # noqa

ALLOWED_HOSTS = [*ALLOWED_HOSTS, "host.docker.internal"]  # noqa: F405
