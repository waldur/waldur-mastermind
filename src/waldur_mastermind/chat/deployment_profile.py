"""What this deployment has switched on, in a vocabulary the docs can name.

Documentation sections declare the site configuration they depend on as
``requires`` terms (waldur/waldur-docs#37):

- ``setting:<KEY>`` -- a runtime setting (``waldur_core.configuration``) is truthy
- ``feature:<section.key>`` -- a feature flag is on
- ``!feature:<section.key>`` -- a feature flag is off
- ``offering_type:<type>`` -- an active offering of that type exists, and the
  type is not in ``DISABLED_OFFERING_TYPES``
- ``capability:<name>`` -- a capability in ``CAPABILITIES`` is available

:func:`get_term_states` classifies the terms it is given as on, off or unknown.
:func:`vocabulary` lists every valid key, which docs CI checks the corpus against
(``waldur print_docs_requires_vocabulary``).

This is meant to be the one place the assistant reads deployment configuration
from, so the quick prompts and system-prompt context of
waldur/waldur-mastermind#1097 should extend it rather than read the settings
again.
"""

import enum
import logging
from collections.abc import Callable, Iterable

from django.core.cache import cache

from waldur_core.configuration import registry as settings_registry
from waldur_core.configuration.models import Setting
from waldur_core.core import features
from waldur_core.core.models import Feature
from waldur_firecrest import workspace
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    normalize_offering_type,
)
from waldur_mastermind.marketplace.plugins import manager as plugins_manager

logger = logging.getLogger(__name__)

CACHE_KEY = "waldur_mastermind.chat.deployment_profile.config_values"
# Settings have no change signal to invalidate on, so this is how long a change
# to a setting, flag or offering takes to reach the assistant.
CACHE_TIMEOUT = 60

# Named capabilities: things a deployment can do that no single setting says.
CAPABILITIES: dict[str, Callable[[], bool]] = {
    # FirecREST or a cluster monitor is allowed, so HPC allocations are reachable.
    "hpc": lambda: workspace.is_configured(),
}


# What a user would call the feature a term switches, for telling them it is
# switched off here. Terms not listed fall back to their setting or feature
# description, which is written for administrators.
TERM_NAMES: dict[str, str] = {
    "setting:PAT_ENABLED": "Personal access tokens",
    "setting:MATRIX_ENABLED": "Project chat",
    "feature:project.show_matrix_chat": "Project chat",
    "setting:AI_ASSISTANT_ENABLED": "AI assistant",
    "feature:support.enable_llm_assistant": "AI assistant",
    "setting:AFFILIATES_ENABLED": "Affiliate program",
    "feature:reseller.affiliates": "Affiliate program",
    "setting:WALDUR_SUPPORT_ENABLED": "Support requests",
    "feature:marketplace.show_call_management_functionality": (
        "Call management (creating and running calls for proposals)"
    ),
    "!feature:marketplace.call_only": "Proposals and reviews for calls",
    "!feature:marketplace.catalogue_only": "Ordering from the marketplace",
    "!feature:marketplace.conceal_prices": "Prices and costs",
    "setting:COI_DETECTION_ENABLED": "Automated conflict-of-interest detection",
    "setting:AUTOMATED_MATCHING_ENABLED": "Automated reviewer matching",
    "setting:REVIEWER_PROFILES_ENABLED": "Reviewer profiles",
    "feature:customer.show_onboarding": "Organization onboarding",
    "feature:customer.show_organisation_reporting": "Organization reports",
    "feature:invitations.show_course_accounts": "Course accounts",
    "feature:marketplace.show_posix_id_pools": "POSIX ID pools",
    "feature:marketplace.show_provider_accounts": "Provider accounts",
    "feature:marketplace.display_software_catalog": "Software catalogs",
    "feature:project.show_custom_metrics": "Custom metrics",
    "feature:project.science_domain": "Science domains for projects",
    "feature:dashboard.usage_include_terminated": "Usage of terminated resources",
    "offering_type:VMware.VirtualMachine": "VMware virtual machines",
    "offering_type:OpenStack.Tenant": "OpenStack private clouds",
    "offering_type:OpenStack.Instance": "OpenStack virtual machines",
    "offering_type:OpenStack.Volume": "OpenStack volumes",
    "capability:hpc": "HPC workspace",
}


def term_name(term: str) -> str:
    """A readable name for the feature ``term`` switches."""
    if term in TERM_NAMES:
        return TERM_NAMES[term]
    kind, _, key = term.lstrip("!").partition(":")
    description = ""
    if kind == "setting":
        definition = settings_registry.definitions().get(key)
        description = definition[1] if definition else ""
    elif kind == "feature":
        section_key, _, feature_key = key.partition(".")
        for section in features.FEATURES:
            if section["key"] == section_key:
                for item in section["items"]:
                    if item["key"] == feature_key:
                        description = item["description"]
    first_sentence = description.split(". ")[0].strip().rstrip(".")
    return first_sentence or key


class TermState(enum.Enum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


def vocabulary() -> dict[str, list[str]]:
    """Every key a ``requires`` term may name, by term kind."""
    return {
        "setting": sorted(settings_registry.definitions()),
        "feature": sorted(features.all_feature_keys()),
        "offering_type": sorted(plugins_manager.backends.keys()),
        "capability": sorted(CAPABILITIES),
    }


def _active_offering_types() -> list[str]:
    enabled = set(plugins_manager.get_offering_types())
    present = (
        marketplace_models.Offering.objects.filter(state=OfferingStates.ACTIVE)
        .values_list("type", flat=True)
        .distinct()
    )
    return sorted(
        {normalize_offering_type(t) for t in present}
        & {normalize_offering_type(t) for t in enabled}
    )


def _compute_values() -> dict:
    stored = dict(Feature.objects.values_list("key", "value"))
    return {
        # Only truthiness is kept: the cache must never hold a secret's value.
        "settings": {
            key: bool(value)
            for key, value in Setting.objects.values_with_defaults().items()
        },
        "features": {
            key: stored.get(key, features.FEATURE_DEFAULTS.get(key, False))
            for key in features.all_feature_keys()
        },
        "offering_types": _active_offering_types(),
        "capabilities": {name: bool(check()) for name, check in CAPABILITIES.items()},
    }


def _values() -> dict:
    values = cache.get(CACHE_KEY)
    if values is None:
        values = _compute_values()
        cache.set(CACHE_KEY, values, CACHE_TIMEOUT)
    return values


def invalidate_cache() -> None:
    cache.delete(CACHE_KEY)


def _term_state(term: str, values: dict) -> TermState:
    kind, sep, key = term.partition(":")
    if not sep or not key:
        return TermState.UNKNOWN
    negated = kind.startswith("!")
    kind = kind.removeprefix("!")
    if negated and kind != "feature":
        return TermState.UNKNOWN

    if kind == "setting":
        if key not in values["settings"]:
            return TermState.UNKNOWN
        on = bool(values["settings"][key])
    elif kind == "feature":
        if key not in values["features"]:
            return TermState.UNKNOWN
        on = values["features"][key] != negated
    elif kind == "offering_type":
        if normalize_offering_type(key) not in plugins_manager.backends:
            return TermState.UNKNOWN
        on = normalize_offering_type(key) in values["offering_types"]
    elif kind == "capability":
        if key not in values["capabilities"]:
            return TermState.UNKNOWN
        on = values["capabilities"][key]
    else:
        return TermState.UNKNOWN
    return TermState.ON if on else TermState.OFF


def get_term_states(terms: Iterable[str]) -> dict[str, TermState]:
    """The state of each of ``terms`` on this deployment.

    Reads the configuration through a short-lived cache, so a warm call runs
    no queries.
    """
    values = _values()
    return {term: _term_state(term, values) for term in set(terms)}
