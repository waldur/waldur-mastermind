"""The models a resource and its API keys may call.

A resource's ``models`` option and a key's ``allowed_models`` take the same three
forms: ``["*"]`` is every model, ``[]`` none, and anything else those names. A key
narrows its resource: ``["*"]`` on a key is every model the resource allows, and
the site agent receives the effective list, the resource's selection and then the
key's, less any name the offering no longer lists, while the key keeps its own
list as chosen. See docs/resource-api-keys.md.

A ``models`` option of multiple choice is a model selection on any offering:
it takes ``*``, a new resource starts on it, and an empty one means none, so an
offering that later turns on ``enable_api_key_provisioning`` finds its resources
already on every model. It narrows keys only under key management. A
single-choice ``models`` option has no room for ``*`` and narrows nothing.
"""

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_mastermind.marketplace import models

ALL_MODELS = models.ResourceApiKey.ALL_MODELS
EVERY_MODEL = [ALL_MODELS]
# The resource option that holds the resource's selection.
OPTION = "models"


def _models_option(resource_options: dict | None) -> dict | None:
    option = ((resource_options or {}).get("options") or {}).get(OPTION)
    if isinstance(option, dict) and option.get("type") == "select_string_multi":
        return option
    return None


def models_option(offering: models.Offering) -> dict | None:
    """The offering's ``models`` option, when it is a model selection."""
    return _models_option(offering.resource_options)


def narrows(plugin_options: dict | None, resource_options: dict | None) -> bool:
    """Whether resource selections narrow keys under these offering settings."""
    return bool(
        (plugin_options or {}).get("enable_api_key_provisioning")
        and _models_option(resource_options) is not None
    )


def narrows_keys(offering: models.Offering) -> bool:
    """Whether a resource's selection narrows its keys: under key management."""
    return narrows(offering.plugin_options, offering.resource_options)


def same(first: list[str], second: list[str]) -> bool:
    """Whether two model lists allow the same models; order does not count."""
    return sorted(first) == sorted(second)


def same_choices(first: list[str] | None, second: list[str] | None) -> bool:
    """Whether two offered lists (None: the offering lists none) are the same."""
    if first is None or second is None:
        return first is second
    return set(first) == set(second)


def offered(resource_options: dict | None) -> list[str] | None:
    """The models these offering settings list, or None when they list none."""
    option = ((resource_options or {}).get("options") or {}).get(OPTION)
    choices = option.get("choices") if isinstance(option, dict) else None
    return choices if isinstance(choices, list) else None


def offered_models(offering: models.Offering) -> list[str] | None:
    """The models the offering lists, or None when it lists none."""
    return offered(offering.resource_options)


def selection(options: dict | None) -> list[str]:
    """The model selection held in a resource's option values.

    A resource that never chose, ordered before the option existed, is on every
    model, as it was when an empty selection still meant that.
    """
    value = (options or {}).get(OPTION)
    # An unset single choice may be stored as an empty string.
    if value is None or value == "":
        return list(EVERY_MODEL)
    if isinstance(value, str):
        return [value]
    return list(value)


def allowed(
    plugin_options: dict | None, resource_options: dict | None, options: dict | None
) -> list[str]:
    """What a resource with these option values allows its keys under these
    offering settings: its selection under key management, every model otherwise.
    """
    if not narrows(plugin_options, resource_options):
        return list(EVERY_MODEL)
    return selection(options)


def resource_models(resource: models.Resource) -> list[str]:
    """The models a resource allows its keys."""
    offering = resource.offering
    return allowed(offering.plugin_options, offering.resource_options, resource.options)


def narrow(resource_list: list[str], key_list: list[str]) -> list[str]:
    """A key's list narrowed by its resource's: what the key can call."""
    if resource_list == EVERY_MODEL:
        return list(key_list)
    if key_list == EVERY_MODEL:
        return list(resource_list)
    return [model for model in key_list if model in resource_list]


def still_offered(names: list[str], choices: list[str] | None) -> list[str]:
    """``names`` less those the offering no longer lists.

    Every model stays every model: it covers what the offering lists now.
    Names are kept as stored, so a model the provider lists again comes back.
    """
    if names == EVERY_MODEL or choices is None:
        return list(names)
    return [model for model in names if model in choices]


def key_models(
    resource_list: list[str], key_list: list[str], choices: list[str] | None
) -> list[str]:
    """What a key can call: its list narrowed by its resource's, and by what
    the offering (``choices``) still lists.
    """
    return still_offered(narrow(resource_list, key_list), choices)


def effective_models(api_key: models.ResourceApiKey) -> list[str]:
    """The list the site agent applies for a key."""
    resource = api_key.resource
    return key_models(
        resource_models(resource),
        api_key.allowed_models,
        offered_models(resource.offering),
    )


def disallowed_models(
    resource: models.Resource, names: list[str], kept: list[str] = ()
) -> list[str]:
    """Those of ``names`` the resource does not allow, sorted.

    A resource on every model allows what the offering lists, or any name when
    the offering lists none; a name the offering no longer lists is allowed by
    no resource. ``kept`` are names the key already holds: a key
    keeps its list as chosen, so one the resource has since stopped allowing
    may be sent back unchanged.
    """
    choices = offered_models(resource.offering)
    allowed_names = resource_models(resource)
    if allowed_names == EVERY_MODEL:
        if choices is None:
            return []
        allowed_names = choices
    return sorted(set(names) - set(still_offered(allowed_names, choices)) - set(kept))


def selection_options(offering: models.Offering, options: dict) -> dict:
    """Option metadata to validate values with: ``*`` is a choice too.

    ``options`` is the offering's resource or ordering option metadata; its own
    ``models`` entry gains ``*`` when the offering has a model selection, so an
    order form asking for the models takes ``*`` as the resource does.
    validate_selection refuses ``*`` next to a name, which a multiple choice
    cannot express.
    """
    if models_option(offering) is None:
        return options
    option = _models_option({"options": options})
    if option is None or not isinstance(option.get("choices"), list):
        return options
    return {**options, OPTION: {**option, "choices": [*option["choices"], ALL_MODELS]}}


def validate_selection(offering: models.Offering, values: dict | None) -> None:
    """Refuse ``*`` anywhere but alone in a resource's model selection."""
    if models_option(offering) is None:
        return
    value = (values or {}).get(OPTION)
    if isinstance(value, list) and ALL_MODELS in value and value != EVERY_MODEL:
        raise serializers.ValidationError(
            {
                OPTION: _("%s stands for every model and must be the only entry.")
                % ALL_MODELS
            }
        )


def with_default_selection(offering: models.Offering, values: dict) -> dict:
    """A new resource's option values: on every model unless it chose."""
    if models_option(offering) is None or OPTION in values:
        return values
    return {**values, OPTION: list(EVERY_MODEL)}
