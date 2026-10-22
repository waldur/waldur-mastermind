"""Resource options shown as the limit of a usage-based component.

A provider's backend may enforce a quota for a component that Waldur only
meters. The quota is held in a resource option; ``display_limit_config`` on
that option names the usage-based component it belongs to, and the resource
API then reports the option's value in ``display_limits``, so the usage is
shown against it.

The value is never stored in ``Resource.limits`` nor returned in the API's
``limits``: those are the quantities of limit-based components, which clients
send back in orders and compare with a backend's limits, and every path that
validates, bills or reallocates them rejects any other key. It is computed when
a resource is serialized, so it always follows the option.
"""

import math
from decimal import Decimal, InvalidOperation

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

CONFIG_KEY = "display_limit_config"
# The value is a quantity, so the option has to be a number.
OPTION_TYPES = ("integer",)


def _target(option):
    return ((option or {}).get(CONFIG_KEY) or {}).get("component_type")


def display_limit_options(resource_options):
    """Map each component type to the name of the resource option shown as its limit."""
    result = {}
    for name, option in ((resource_options or {}).get("options") or {}).items():
        component_type = _target(option)
        if component_type:
            result.setdefault(component_type, name)
    return result


def validate_order_options(options):
    """A display limit is read from the resource, so an order option cannot hold one."""
    for name, option in (options or {}).items():
        if _target(option):
            raise serializers.ValidationError(
                {
                    "options": _(
                        "Option %s: display_limit_config is only available "
                        "for resource options."
                    )
                    % name
                }
            )


def validate_resource_options(options):
    """Check the display limit settings that need nothing but the options."""
    targets = {}
    for name, option in (options or {}).items():
        component_type = _target(option)
        if not component_type:
            continue
        if option.get("type") not in OPTION_TYPES:
            raise serializers.ValidationError(
                {
                    "options": _(
                        "Option %s: display_limit_config is only allowed "
                        "for integer options."
                    )
                    % name
                }
            )
        if component_type in targets:
            raise serializers.ValidationError(
                {
                    "options": _(
                        "Options %(first)s and %(second)s are both shown as the "
                        "limit of component %(component)s."
                    )
                    % {
                        "first": targets[component_type],
                        "second": name,
                        "component": component_type,
                    }
                }
            )
        targets[component_type] = name


def validate_components(resource_options, usage_types, capped_types=()):
    """Each display limit has to name a usage-based component of the offering.

    ``capped_types`` are components customers set spending limits on: a quota
    shown as their limit would compete with the customer's own.
    """
    for component_type, name in display_limit_options(resource_options).items():
        if component_type in capped_types:
            raise serializers.ValidationError(
                {
                    "resource_options": _(
                        "Option %(name)s refers to %(component)s, which takes "
                        "spending limits."
                    )
                    % {"name": name, "component": component_type}
                }
            )
        if component_type not in usage_types:
            raise serializers.ValidationError(
                {
                    "resource_options": _(
                        "Option %(name)s refers to %(component)s, which is not "
                        "a usage-based component of this offering."
                    )
                    % {"name": name, "component": component_type}
                }
            )


def _number(value):
    """An option value as a limit, or None when it is not a usable quantity."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    if number == number.to_integral_value():
        return int(number)
    result = float(number)
    return result if math.isfinite(result) else None


def get_display_limits(resource_options, option_values):
    """Limits to show for a resource: {component type: the option's value}.

    An option without a value, or with one that is not a non-negative number,
    shows no limit.
    """
    result = {}
    option_values = option_values or {}
    for component_type, name in display_limit_options(resource_options).items():
        value = _number(option_values.get(name))
        if value is not None:
            result[component_type] = value
    return result
