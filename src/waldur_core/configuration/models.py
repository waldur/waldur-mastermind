import copy
import logging

from django.conf import settings
from django.db import DatabaseError, connections, models

from waldur_core.configuration import overrides, registry, secrets
from waldur_core.configuration.signals import setting_updated

logger = logging.getLogger(__name__)


class Source(models.TextChoices):
    """Who wrote a setting's current value.

    - ``api``: a person, through ``/api/override-settings/`` or another
      endpoint that changes settings; ``updated_by`` holds them.
    - ``admin``: a person, through the Django admin settings page;
      ``updated_by`` holds them.
    - ``deployment``: the deployment's configuration, applied by
      ``override_constance_settings`` or ``init_matrix_settings`` on every
      deploy (``--if-unset`` files only seed a setting without a value);
      ``source_detail`` holds the file or command.
    - ``cli``: an operator running a management command; ``source_detail``
      names it where the command records it.
    - ``application``: Waldur itself, with no person acting, such as a
      refreshed token.
    - ``migration``: a data migration; ``source_detail`` names what it copied
      from.

    A source records the last writer only.
    """

    API = "api", "API"
    ADMIN = "admin", "Django admin"
    DEPLOYMENT = "deployment", "Deployment configuration"
    CLI = "cli", "Management command"
    APPLICATION = "application", "Waldur itself"
    MIGRATION = "migration", "Migration"


_unavailable_logged = False

# Stored values when CONFIGURATION_IN_MEMORY is on: a harness that must never
# read settings from, or write them to, whatever database it is pointed at.
_memory = {}


def _in_memory():
    return getattr(settings, "CONFIGURATION_IN_MEMORY", False)


def _log_unavailable(exc):
    global _unavailable_logged
    if not _unavailable_logged:
        # Set first: log handlers read settings too, and would come back here.
        _unavailable_logged = True
        logger.warning("Settings table is not readable, using defaults: %s", exc)


# Whether the settings table is known to exist. Until it is, a read inside a
# transaction checks for the table first instead of querying it: a failed query
# aborts the transaction, and on a fresh database migrations run in
# transactions, log through handlers that read settings, and may run before
# the table is created.
_table_exists = False


def mark_table_exists(**kwargs):
    global _table_exists
    _table_exists = True


class SettingManager(models.Manager):
    def _decode(self, key, stored):
        """What a stored value reads as. A setting without a value reads as its
        default, whether it has no row or a row holding null."""
        if stored is None:
            # A copy: callers may change a list or dict they read in place.
            return copy.deepcopy(registry.default(key))
        if registry.is_secret(key):
            return secrets.decrypt_secret(key, stored)
        return stored

    def _unavailable(self, key):
        """What ``key`` reads as while the table cannot be read.

        A secret reads as an unguessable stand-in rather than its empty
        default: several checks take an empty secret to mean "not configured,
        let the request through".
        """
        if registry.is_secret(key):
            return secrets.undecryptable_value(key)
        return copy.deepcopy(registry.default(key))

    def _stored(self, *keys):
        """Stored values by key: of ``keys``, or of every setting if none are
        given. None while the table cannot be read."""
        if _in_memory():
            return _memory
        global _table_exists
        connection = connections[self.db]
        if not _table_exists and connection.in_atomic_block:
            if self.model._meta.db_table not in connection.introspection.table_names():
                return None
        try:
            queryset = self.filter(key__in=keys) if keys else self.all()
            stored = dict(queryset.values_list("key", "value"))
        except DatabaseError as exc:
            # Tolerated only until the table has been read once: before migrate
            # has created it, or while collectstatic and schema generation run
            # without a database. Later on a failed read is an error, as with
            # any other model, not a reason to fall back to defaults.
            if _table_exists:
                raise
            _log_unavailable(exc)
            return None
        _table_exists = True
        return stored

    def get_value(self, key):
        """The current value of ``key``. Reading never writes."""
        overridden = overrides.lookup(key)
        if overridden is not overrides.MISSING:
            return overridden
        stored = self._stored(key)
        if stored is None:
            return self._unavailable(key)
        return self._decode(key, stored.get(key))

    def values_with_defaults(self):
        """The current value of every setting, in one query."""
        stored = self._stored()
        values = {}
        for key in registry.definitions():
            overridden = overrides.lookup(key)
            if overridden is not overrides.MISSING:
                values[key] = overridden
            elif stored is None:
                values[key] = self._unavailable(key)
            else:
                values[key] = self._decode(key, stored.get(key))
        return values

    def sources(self, *keys):
        """``(source, source_detail)`` of the stored rows of ``keys``."""
        if _in_memory():
            return {}
        rows = self.filter(key__in=keys).values_list("key", "source", "source_detail")
        return {key: (source, detail) for key, source, detail in rows}

    def set(self, key, value, *, source, source_detail="", user=None):
        if not registry.has(key):
            raise AttributeError(key)
        if isinstance(value, set | frozenset):
            value = list(value)
        stored = value
        if registry.is_secret(key):
            # A form that re-submits every setting sends back what it read,
            # which for an undecryptable secret is its stand-in: keep the
            # stored value rather than make the stand-in the real secret.
            if secrets.is_undecryptable_value(value):
                return
            stored = secrets.encrypt_secret(value)
        if user is not None and not getattr(user, "is_authenticated", False):
            user = None

        if _in_memory():
            old_value = _memory.get(key)
            _memory[key] = stored
        else:
            old = list(self.filter(key=key).values_list("value", flat=True)[:1])
            old_value = old[0] if old else None
            self.update_or_create(
                key=key,
                defaults={
                    "value": stored,
                    "source": source,
                    "source_detail": source_detail,
                    "updated_by": user,
                },
            )
        overrides.release(key)
        setting_updated.send(
            sender=Setting, key=key, old_value=old_value, new_value=stored
        )


class Setting(models.Model):
    """A setting someone has given a value. Settings nobody has set have no
    row and read as their default from the registry."""

    key = models.CharField(max_length=255, unique=True)
    value = models.JSONField(null=True, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    source_detail = models.CharField(
        max_length=255,
        blank=True,
        help_text="File, command or other origin within the source.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = SettingManager()

    class Meta:
        ordering = ("key",)

    def __str__(self):
        return self.key
