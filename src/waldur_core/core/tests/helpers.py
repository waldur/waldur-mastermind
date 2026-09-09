import copy
import importlib.resources
import json
from datetime import timedelta

from django.conf import settings
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
