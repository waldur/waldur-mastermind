"""Keep pytest from importing the settings modules in this package.

They are named ``test_settings*.py`` and so match pytest's default
``test_*.py`` collection pattern. Importing one as a test module runs it, and
``test_settings_ws2.py`` does:

    from waldur_core.server.test_settings_local import *
    DATABASES["default"]["TEST"] = {"NAME": "test_waldur_ws2"}

The star-import binds the very dict object Django is already using, so that
assignment mutates live settings *in place*, after Django has normalised
them — replacing a ``TEST`` dict holding MIRROR, CHARSET, COLLATION and
MIGRATE with one holding only NAME. ``setup_databases()`` then dies on
``KeyError: 'MIRROR'`` and every test class that needs a database errors out.

The result is a suite that is fine per-app but collapses when run as a whole
tree, which is how it went unnoticed: CI shards across 15 jobs and never
collects this directory alongside the rest.
"""

collect_ignore_glob = ["test_settings*.py"]
