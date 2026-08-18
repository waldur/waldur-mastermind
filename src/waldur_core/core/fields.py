import copy
import json
import uuid
from typing import cast

import pycountry
from cryptography.fernet import InvalidToken
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import encryption, utils
from waldur_core.core import validators as core_validators


class MappedChoiceField(serializers.ChoiceField):
    """
    A choice field that maps enum values from representation to model ones and back.

    :Example:

    >>> # models.py
    >>> class IceCream(models.Model):
    >>>     class Meta:
    >>>         app_label = 'myapp'
    >>>
    >>>     CHOCOLATE = 0
    >>>     VANILLA = 1
    >>>
    >>>     FLAVOR_CHOICES = (
    >>>         (CHOCOLATE, _('Chocolate')),
    >>>         (VANILLA, _('Vanilla')),
    >>>     )
    >>>
    >>>     flavor = models.SmallIntegerField(choices=FLAVOR_CHOICES)
    >>>
    >>> # serializers.py
    >>> class IceCreamSerializer(serializers.ModelSerializer):
    >>>     class Meta:
    >>>         model = IceCream
    >>>
    >>>     flavor = MappedChoiceField(
    >>>         choices={
    >>>             'chocolate': _('Chocolate'),
    >>>             'vanilla': _('Vanilla'),
    >>>         },
    >>>         choice_mappings={
    >>>             'chocolate': IceCream.CHOCOLATE,
    >>>             'vanilla': IceCream.VANILLA,
    >>>         },
    >>>     )
    >>>
    >>> model1 = IceCream(flavor=IceCream.CHOCOLATE)
    >>> serializer1 = IceCreamSerializer(instance=model1)
    >>> serializer1.data
    {'flavor': 'chocolate', 'id': None}
    >>>
    >>> data2 = {'flavor': 'vanilla'}
    >>> serializer2 = IceCreamSerializer(data=data2)
    >>> serializer2.is_valid()
    True
    >>> serializer2.validated_data["flavor"] == IceCream.VANILLA
    True
    """

    def __init__(self, choice_mappings, **kwargs):
        super().__init__(**kwargs)

        assert set(self.choices.keys()) == set(choice_mappings.keys()), (
            "Choices do not match mappings"
        )
        assert len(set(choice_mappings.values())) == len(choice_mappings), (
            "Mappings are not unique"
        )

        self.mapped_to_model = choice_mappings
        self.model_to_mapped = {v: k for k, v in choice_mappings.items()}

    def to_internal_value(self, data):
        if data == "" and self.allow_blank:
            return ""

        data = super().to_internal_value(data)

        try:
            return self.mapped_to_model[str(data)]
        except KeyError:
            self.fail("invalid_choice", input=data)

    def to_representation(self, value):
        if value in ("", None):
            return value

        value = self.model_to_mapped[value]

        return super().to_representation(value)


class NaturalChoiceField(MappedChoiceField):
    def __init__(self, choices=None, **kwargs):
        super().__init__(
            choices=[(v, v) for k, v in choices],
            choice_mappings={v: k for k, v in choices},
            **kwargs,
        )


class TimestampField(serializers.Field):
    """
    Unix timestamp field mapped to datetime object.
    """

    def to_representation(self, value):
        return utils.datetime_to_timestamp(value)

    def to_internal_value(self, value):
        try:
            return utils.timestamp_to_datetime(value)
        except ValueError:
            raise serializers.ValidationError(
                _('Value "%s" should be valid UNIX timestamp.') % value
            )


COUNTRIES = [(country.alpha_2, country.name) for country in pycountry.countries] + [
    ("EU", "European Union")
]
COUNTRIES_DICT = cast(dict[str, str], dict(COUNTRIES))


class StringUUID(uuid.UUID):
    """
    Default UUID class __str__ method returns hyphenated string.
    This class returns non-hyphenated string.
    """

    def __unicode__(self):
        return str(str(self))

    def __str__(self):
        return self.hex

    def __len__(self):
        return len(self.__unicode__())


class UUIDField(models.UUIDField):
    """
    This class implements backward-compatible non-hyphenated rendering of UUID values.
    Default field parameters are not exposed in migrations.
    """

    def __init__(self, **kwargs):
        kwargs["default"] = lambda: StringUUID(uuid.uuid4().hex)
        kwargs["editable"] = False
        kwargs["unique"] = True
        super().__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        del kwargs["default"]
        del kwargs["editable"]
        del kwargs["unique"]
        return name, path, args, kwargs

    def _parse_uuid(self, value):
        if not value:
            return None
        try:
            return StringUUID(str(value))
        except ValueError:
            return None

    def from_db_value(self, value, expression, connection):
        return self._parse_uuid(value)

    def to_python(self, value):
        return self._parse_uuid(value)


class BackendURLField(models.URLField):
    default_validators = [core_validators.BackendURLValidator()]


class JSONField(models.TextField):
    def __init__(self, *args, **kwargs):
        self.dump_kwargs = kwargs.pop(
            "dump_kwargs", {"cls": DjangoJSONEncoder, "separators": (",", ":")}
        )
        self.load_kwargs = kwargs.pop("load_kwargs", {})

        super().__init__(*args, **kwargs)

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)

    def to_python(self, value):
        if isinstance(value, str) and value:
            try:
                return json.loads(value, **self.load_kwargs)
            except ValueError:
                raise ValidationError(_("Enter valid JSON"))
        return value

    def get_prep_value(self, value):
        """Convert JSON object to a string"""
        if self.null and value is None:
            return None
        return json.dumps(value, **self.dump_kwargs)

    def get_default(self):
        """
        Returns the default value for this field.
        The default implementation on models.Field calls force_unicode
        on the default, which means you can't set arbitrary Python
        objects as the default. To fix this, we just return the value
        without calling force_unicode on it. Note that if you set a
        callable as a default, the field will still call it. It will
        *not* try to pickle and encode it.
        """
        if self.has_default():
            if callable(self.default):
                return self.default()
            return copy.deepcopy(self.default)
        # If the field doesn't have a default, then we punt to models.Field.
        return super().get_default()


class SelectiveEncryptionMixin:
    """Encrypts the values under sensitive keys of a JSON-valued field.

    Only the values whose key satisfies :meth:`_is_sensitive_key` are Fernet-encrypted;
    JSON keys and non-sensitive values stay plaintext, so ``has_key`` / value lookups
    keep working against the column.

    Encryption and decryption use the **same** key predicate, and encryption is
    unconditional: a token-shaped value under a sensitive key is wrapped rather than
    passed through, and only sensitive keys are decrypted on read. That symmetry is
    what stops the field being used as a decryption oracle (a caller planting a stolen
    ciphertext under a non-sensitive key, or a token-shaped value under a sensitive
    one, cannot read back another row's plaintext).

    Encryption happens in ``pre_save`` and never writes ciphertext back to the instance
    attribute — the attribute stays the plaintext dict, so FieldTracker compares
    plaintext on both sides and handlers do not fire on an unchanged save.

    Mixed into a concrete JSON field class, so the same behaviour applies to the
    ``jsonb``-backed :class:`EncryptedJSONField` and to the legacy text-backed
    :class:`EncryptedOptionsField` without duplicating the logic. Subclasses must
    implement :meth:`_is_sensitive_key`.
    """

    def _is_sensitive_key(self, key) -> bool:
        """Whether the value under ``key`` must be encrypted. Override in subclass."""
        raise NotImplementedError

    def from_db_value(self, value, expression, connection):
        value = super().from_db_value(value, expression, connection)
        return encryption.decrypt_dict_values(value, self._is_sensitive_key)

    def pre_save(self, model_instance, add):
        value = super().pre_save(model_instance, add)
        return encryption.encrypt_dict_values(value, self._is_sensitive_key)

    def encrypt_for_update(self, value):
        """Encrypt a value bound for ``QuerySet.update()``, which skips ``pre_save``.

        See :func:`waldur_core.core.encryption.encrypt_defaults_for_update`.
        """
        return encryption.encrypt_dict_values(value, self._is_sensitive_key)


class EncryptedJSONField(SelectiveEncryptionMixin, models.JSONField):
    """A ``jsonb`` JSONField that encrypts the values under sensitive keys at rest."""


class EncryptedOptionsField(SelectiveEncryptionMixin, JSONField):
    """The legacy text-backed :class:`JSONField`, encrypting credential-named values.

    Backs ``ServiceSettings.options``, which mixes ordinary backend configuration
    (endpoints, tenant ids, tuning flags) with real credentials — ``client_secret``,
    ``keycloak_password``, ``vault_token``. Only the latter are encrypted, so the
    column stays readable for support and no existing consumer of an option changes
    behaviour.

    Kept on the legacy text-backed base deliberately: ``options`` is a ``text`` column
    holding serialised JSON, and it is never queried through JSON lookups, so there is
    nothing to gain from a ``jsonb`` rewrite of a large table — and a type change would
    turn a metadata-only migration into a full rewrite.
    """

    def _is_sensitive_key(self, key) -> bool:
        return encryption.is_credential_key(key)


class EncryptedTextField(models.TextField):
    """A text field whose whole value is Fernet-encrypted at rest.

    The value is a single credential, so it is always encrypted (unlike the
    selective :class:`EncryptedJSONField`). Encryption happens in ``pre_save`` so the
    instance attribute stays plaintext and FieldTracker compares plaintext; reads
    decrypt by token shape. Backed by TextField (like ``ResourceApiKey.key_ciphertext``)
    so the longer ciphertext never overflows a length bound. Value lookups no longer
    match — acceptable for opaque secrets that are never queried by value.

    Encryption is unconditional (not gated on token shape): a token-shaped value is
    wrapped rather than stored verbatim, so the field cannot be used as a decryption
    oracle for a chosen ciphertext.
    """

    def from_db_value(self, value, expression, connection):
        if value and encryption.is_encrypted(value):
            try:
                return encryption.decrypt_value(value)
            except InvalidToken:
                return value
        return value

    def pre_save(self, model_instance, add):
        value = super().pre_save(model_instance, add)
        if value:
            return encryption.encrypt_value(value)
        return value

    def encrypt_for_update(self, value):
        """Encrypt a value bound for ``QuerySet.update()``, which skips ``pre_save``.

        See :func:`waldur_core.core.encryption.encrypt_defaults_for_update`.
        """
        return encryption.encrypt_value(value) if value else value


class YearMonthField(serializers.CharField):
    """Field that support yearmonth representation in format YYYY-MM"""

    def to_internal_value(self, value):
        try:
            year, month = (int(el) for el in value.split("-"))
        except ValueError:
            raise serializers.ValidationError(
                _('Value "%s" should be in valid format YYYY-MM') % value
            )
        if not 0 < month < 13:
            raise serializers.ValidationError(_("Month has to be from 1 to 12."))
        return year, month
