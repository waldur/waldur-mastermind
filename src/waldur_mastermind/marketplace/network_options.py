"""Network and secret options of an offering: cidr, ipsec_vpn_config, secret.

All three are generic option types, valid as order and as resource options of
any offering. Their values are checked by the fields in ``common.serializers``.
This module holds what needs the offering or the resource:

- the references an ipsec_vpn_config option makes to cidr options
  (``no_overlap_with``), checked when the offering is saved;
- which ordered values may seed a new resource's options;
- the values of secret options (a VPN pre-shared key, an API token, a
  kubeconfig). A secret is never stored with the other values: the customer's
  is taken out of the submitted values and stored encrypted in
  ResourceOptionSecret, against the order's resource; the provider's is set
  through the provider action. They are read back only by the reveal actions.
"""

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import encryption
from waldur_mastermind.common.serializers import (
    CIDR_FIELD_TYPE,
    NETWORK_FIELD_TYPES,
    SECRET_SOURCE_CUSTOMER,
    SecretPolicy,
    is_ipsec_option,
    is_secret_option,
    strip_secret_options,
)
from waldur_mastermind.marketplace import log, models


def _options(options_set):
    return (options_set or {}).get("options") or {}


def secret_options(offering):
    """The offering's secret options by name.

    An option defined both for orders and for resources takes its resource
    definition, the one a later change is checked against.
    """
    return {
        name: option
        for options in (
            _options(offering.options),
            _options(offering.resource_options),
        )
        for name, option in options.items()
        if is_secret_option(option)
    }


def secret_policy(offering, option_name):
    """The policy of the offering's secret option, or None when there is none."""
    option = secret_options(offering).get(option_name)
    if option is None:
        return None
    return SecretPolicy(option.get("default_configs"))


def strip_secrets(offering, attributes):
    """Order attributes without the value of any secret option.

    Validated values never hold one. This covers what is stored as sent: the
    attributes of an offering without order options, a value named like a
    resource option only, and the option changes of an update order.
    """
    names = set(secret_options(offering))
    attributes = strip_secret_options(attributes, names)
    if isinstance(attributes, dict):
        for key in ("new_options", "old_options"):
            nested = attributes.get(key)
            stripped = strip_secret_options(nested, names)
            if stripped is not nested:
                attributes = {**attributes, key: stripped}
    return attributes


def can_seed_resource_option(offering, name):
    """Whether a new resource's option ``name`` may take the ordered value.

    A network resource option takes it only from an order option of the same
    name and type, the only value that was checked when ordered.
    """
    option = _options(offering.resource_options).get(name) or {}
    if option.get("type") not in NETWORK_FIELD_TYPES:
        return True
    ordered = _options(offering.options).get(name) or {}
    return ordered.get("type") == option.get("type")


def validate_option_references(options, order_options=None, field="options"):
    """Refuse a no_overlap_with that names no cidr option.

    Order options name order options. Resource options name resource options,
    or order options, whose value the resource's attributes hold.
    """
    for name, option in (options or {}).items():
        if not is_ipsec_option(option):
            continue
        for reference in (option.get("default_configs") or {}).get(
            "no_overlap_with"
        ) or []:
            target = (options or {}).get(reference) or (order_options or {}).get(
                reference
            )
            if (target or {}).get("type") != CIDR_FIELD_TYPE:
                raise serializers.ValidationError(
                    {
                        field: _(
                            "Option %(name)s: no_overlap_with names %(reference)s, "
                            "which is not a cidr option of this offering."
                        )
                        % {"name": name, "reference": reference}
                    }
                )


def store_secret(resource, option_name, value, source, user=None):
    """Store, or replace, the value of a resource's secret option. Audited."""
    models.ResourceOptionSecret.objects.update_or_create(
        resource=resource,
        option_name=option_name,
        defaults={
            "value": encryption.encrypt_value(value),
            "source": source,
            "set_by": user if user and user.is_authenticated else None,
        },
    )
    log.log_resource_option_secret_set(resource, option_name, user)


def store_submitted_secrets(resource, secrets, user, source=SECRET_SOURCE_CUSTOMER):
    """Store the secrets a customer sent with the option values, by option name."""
    if not secrets or resource is None:
        return
    with transaction.atomic():
        for option_name, value in secrets.items():
            store_secret(resource, option_name, value, source, user)


def get_secret(resource, option_name):
    """The resource's stored secret of the option, or None when none is set."""
    secret = models.ResourceOptionSecret.objects.filter(
        resource=resource, option_name=option_name
    ).first()
    if secret is None or not secret.value:
        return None
    return secret


def reveal_secret(secret):
    """The secret in plain text.

    Raises cryptography's InvalidToken when the configured keys cannot decrypt
    it.
    """
    return encryption.decrypt_value(secret.value)


def _status(secret):
    if secret is None or not secret.value:
        return {"is_set": False, "source": None, "modified": None}
    return {"is_set": True, "source": secret.source, "modified": secret.modified}


def load_secret_statuses(items):
    """``secret_statuses`` for many resources in one query, by resource id.

    ``items`` are (resource id, offering) pairs, so a list of orders can pass
    the offering it has already loaded.
    """
    names_by_resource = {
        resource_id: sorted(secret_options(offering)) for resource_id, offering in items
    }
    with_options = [pk for pk, names in names_by_resource.items() if names]
    secrets = {}
    if with_options:
        for secret in models.ResourceOptionSecret.objects.filter(
            resource_id__in=with_options
        ):
            secrets[(secret.resource_id, secret.option_name)] = secret
    return {
        pk: {name: _status(secrets.get((pk, name))) for name in names}
        for pk, names in names_by_resource.items()
    }


def secret_statuses(resource):
    """Whether each secret option of the resource's offering is set, by name.

    Never the value itself. Options without one are listed with is_set false.
    """
    return load_secret_statuses([(resource.id, resource.offering)])[resource.id]
