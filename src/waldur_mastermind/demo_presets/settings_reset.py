"""Return settings and features to their defaults around a preset load.

A cleanup load deletes every user. ``configuration_setting.updated_by`` refers
to ``core_user``, so the fast cleanup's ``TRUNCATE core_user CASCADE`` empties
the settings table too. A load therefore snapshots the settings the
deployment owns before cleanup and puts them back afterwards; every other
setting and every feature reads its default until the preset sets it.

A setting is owned by the deployment when its row is locked, names what
manages it, or was written by the deployment's configuration; secrets and the
settings Waldur changes while it runs are owned whether stored or not. A
preset may not set an owned setting.
"""

from django.core.cache import cache
from django.db.models import Q

from waldur_core.configuration import registry
from waldur_core.configuration.models import Setting, Source
from waldur_core.core.models import Feature

API_CONFIGURATION_CACHE_KEY = "API_CONFIGURATION"

_OWNED_ROWS = Q(locked=True) | ~Q(managed_by="") | Q(source=Source.DEPLOYMENT)


def _always_owned():
    return registry.secret_keys() | registry.RUNTIME_SETTINGS


def owned_keys() -> frozenset[str]:
    """Keys of the settings a preset may not set."""
    stored = Setting.objects.filter(_OWNED_ROWS).values_list("key", flat=True)
    return _always_owned() | frozenset(stored)


def snapshot() -> list[Setting]:
    """Unsaved copies of the stored settings the deployment owns.

    ``updated_by`` is dropped: the user is deleted by the cleanup. Values are
    copied as stored, so secrets stay encrypted.
    """
    rows = Setting.objects.filter(_OWNED_ROWS | Q(key__in=_always_owned()))
    return [
        Setting(
            key=row.key,
            value=row.value,
            source=row.source,
            source_detail=row.source_detail,
            locked=row.locked,
            managed_by=row.managed_by,
        )
        for row in rows
    ]


def reset(owned: list[Setting]) -> None:
    """Leave only ``owned`` stored, and no feature set."""
    Setting.objects.all().delete()
    Setting.objects.bulk_create(owned)
    Feature.objects.all().delete()
    clear_configuration_cache()


def clear_configuration_cache() -> None:
    # Bulk writes send no setting_updated, which is what clears it otherwise.
    cache.delete(API_CONFIGURATION_CACHE_KEY)
