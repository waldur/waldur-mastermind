import contextlib
import copy
import importlib.resources
import json
from datetime import timedelta

import psycopg
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import override_settings
from django.utils import timezone

from waldur_core.core.models import DESCRIPTION_LENGTH, PersonalAccessToken

# 4004 characters containing 154 ampersands: shorter than DESCRIPTION_LENGTH as
# typed, but HTML sanitisation escapes & -> &amp; and pushes it over the limit.
# Any HTMLCleanField over a bounded column must reject it with 400 rather than
# let it reach the database and raise DataError.
EXPANDING_DESCRIPTION = "Norouzi, M., & Hinton, G. " * 154

assert len(EXPANDING_DESCRIPTION) < DESCRIPTION_LENGTH


def override_waldur_core_settings(**kwargs):
    waldur_settings = copy.deepcopy(settings.WALDUR_CORE)
    waldur_settings.update(kwargs)
    return override_settings(WALDUR_CORE=waldur_settings)


def load_json_resource(path, root):
    if root.startswith("src"):
        parts = root.split(".")
        root = ".".join(parts[1:-1])
    with importlib.resources.files(root).joinpath(path).open() as fp:
        return json.load(fp)


def create_pat(user, name="test token", scopes=None, **kwargs):
    """Create a PAT and return it with its plaintext token.

    There is no factory: only the sha256 hash is stored, so the plaintext exists
    solely as the return value of PersonalAccessToken.generate_token.
    """
    expires_at = kwargs.pop("expires_at", timezone.now() + timedelta(days=30))
    full_token, prefix, token_hash = PersonalAccessToken.generate_token(expires_at)
    pat = PersonalAccessToken.objects.create(
        user=user,
        name=name,
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=scopes or [],
        expires_at=expires_at,
        **kwargs,
    )
    return pat, full_token


def dropped_connection(noticed=True):
    """Open a connection to the default database, and have the server drop it.

    This is what a process holds once the server has ended its connection -- a
    restart, a failover, an idle timeout. By default the process has tried to
    use it since: psycopg reports it closed, and ``fileno()`` raises
    ``OperationalError``. With ``noticed=False`` it has not, so psycopg still
    reports it open and the code under test is the first to find it dropped.
    """
    wrapper = connections[DEFAULT_DB_ALIAS]
    params = wrapper.get_connection_params()
    lost = wrapper.get_new_connection(params)
    killer = wrapper.get_new_connection(params)
    try:
        (terminated,) = killer.execute(
            "SELECT pg_terminate_backend(%s, 5000)", (lost.info.backend_pid,)
        ).fetchone()
    finally:
        killer.close()
    assert terminated, "the server did not drop the connection"
    if noticed:
        with contextlib.suppress(psycopg.OperationalError):
            lost.execute("SELECT 1")
        assert lost.closed, "psycopg did not notice the dropped connection"
    return lost


@contextlib.contextmanager
def lost_default_connection(noticed=True):
    """Put a connection the server has dropped in place of the default one.

    See ``dropped_connection`` for ``noticed``. The default connection is
    restored on exit, and one that the code under test opened in its place is
    closed.
    """
    wrapper = connections[DEFAULT_DB_ALIAS]
    lost = dropped_connection(noticed)
    saved = wrapper.connection
    wrapper.connection = lost
    try:
        yield lost
    finally:
        if wrapper.connection not in (None, lost):
            wrapper.connection.close()
        wrapper.connection = saved
        lost.close()
