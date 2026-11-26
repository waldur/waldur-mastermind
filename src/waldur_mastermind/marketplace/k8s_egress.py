"""Egress rules of a Kubernetes cluster offering after provisioning.

A customer lists the outbound destinations of a cluster in the order form's
cluster configuration (the ``egress_rules`` key of a k8s config order
option). To change them later the offering adds a ``k8s_egress_rules``
resource option. It pairs with the offering's one k8s config order option:
it takes its egress policy (enabled, domains allowed, baseline rules) from
that option's default_configs, and a new resource takes its value from the
ordered ``egress_rules``.
"""

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_mastermind.common.serializers import (
    K8S_CLUSTER_CONFIG_FIELD_TYPE,
    K8S_EGRESS_POLICY_KEYS,
    K8S_EGRESS_RULES_FIELD_TYPE,
)
from waldur_mastermind.marketplace import models

SOURCE_OPTION_KEY = "k8s_config_option"


def is_egress_option(option):
    return (option or {}).get("type") == K8S_EGRESS_RULES_FIELD_TYPE


def _egress_option_names(options):
    return [
        name for name, option in (options or {}).items() if is_egress_option(option)
    ]


def validate_order_options(options):
    """Refuse a k8s_egress_rules order option: it only edits a resource."""
    for name in _egress_option_names(options):
        raise serializers.ValidationError(
            {
                "options": _(
                    "Option %s: k8s_egress_rules is only available as a "
                    "resource option; customers give egress rules in the "
                    "Kubernetes cluster configuration when ordering."
                )
                % name
            }
        )


def pair_resource_options(resource_options, order_options):
    """Pair each k8s_egress_rules resource option with the k8s config order option.

    The resource option records which order option it pairs with and carries
    a copy of that option's egress policy, so that a value changed later is
    checked exactly as an ordered one. Returns the resource options normalised.
    """
    if not resource_options:
        return resource_options
    options = dict(resource_options.get("options") or {})
    names = _egress_option_names(options)
    if not names:
        return resource_options
    sources = [
        name
        for name, option in (order_options or {}).items()
        if option.get("type") == K8S_CLUSTER_CONFIG_FIELD_TYPE
    ]
    if len(sources) != 1:
        raise serializers.ValidationError(
            {
                "resource_options": _(
                    "Option %(name)s: a k8s_egress_rules resource option needs "
                    "exactly one Kubernetes cluster order option, but the "
                    "offering has %(count)s."
                )
                % {"name": names[0], "count": len(sources)}
            }
        )
    source = sources[0]
    default_configs = order_options[source].get("default_configs") or {}
    policy = {
        key: default_configs[key]
        for key in K8S_EGRESS_POLICY_KEYS
        if key in default_configs
    }
    for name in names:
        options[name] = {
            **options[name],
            SOURCE_OPTION_KEY: source,
            "default_configs": policy,
        }
    return {**resource_options, "options": options}


def ordered_egress_rules(resource_options, attributes):
    """Values of the k8s_egress_rules resource options for a new resource.

    Each takes the ``egress_rules`` of the cluster configuration it pairs
    with, an empty list when none were given. An option whose cluster
    configuration was not ordered gets no value.
    """
    values = {}
    for name in _egress_option_names(resource_options):
        config = (attributes or {}).get(resource_options[name].get(SOURCE_OPTION_KEY))
        if isinstance(config, dict):
            values[name] = list(config.get("egress_rules") or [])
    return values


def backfill_resource_values(offering):
    """Give existing resources a value for the offering's k8s_egress_rules options.

    A resource created before the option was added has none, which would read
    as unrestricted outbound traffic. It gets the egress rules of the cluster
    configuration it was ordered with. Values already set are kept.
    """
    resource_options = (offering.resource_options or {}).get("options") or {}
    names = _egress_option_names(resource_options)
    if not names:
        return
    resources = (
        models.Resource.objects.filter(offering=offering)
        .exclude(state=models.Resource.States.TERMINATED)
        .only("id", "options", "attributes")
    )
    with transaction.atomic():
        for resource in resources.iterator():
            current = resource.options or {}
            missing = {
                name: value
                for name, value in ordered_egress_rules(
                    resource_options, resource.attributes
                ).items()
                if name in names and name not in current
            }
            if missing:
                resource.options = {**current, **missing}
                resource.save(update_fields=["options"])
