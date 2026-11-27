"""The API of an agent configuration import: what it takes and what it answers.

The import itself is the site agent integration's
(``marketplace_site_agent.agent_import``); the offering endpoint that runs it
is marketplace's, so its request and response are described here.
"""

from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_mastermind.marketplace import (
    agent_configuration as site_agent_configuration,
)

# The largest file accepted.
MAX_CONFIGURATION_LENGTH = 512 * 1024

# Component fields the offering's components hold: compared, never imported.
COMPARED_COMPONENT_FIELDS = (
    "measured_unit",
    "unit_factor",
    "accounting_type",
    "label",
    "limit",
)


@extend_schema_field(OpenApiTypes.ANY)
class SettingValueField(serializers.JSONField):
    """A setting's value: a string, number, boolean, list, mapping or null.

    As a plain JSONField the schema would say an object, which a typed client
    cannot read a number or a string into.
    """


class ApplyComponentSerializer(serializers.Serializer):
    component = serializers.CharField()
    field = serializers.ChoiceField(choices=COMPARED_COMPONENT_FIELDS)


class AgentConfigurationImportSerializer(serializers.Serializer):
    configuration = serializers.CharField(
        max_length=MAX_CONFIGURATION_LENGTH,
        trim_whitespace=False,
        help_text=_("The site agent configuration file, as YAML text."),
    )
    offering_index = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        help_text=_(
            "Which entry of the file's offerings list to import. By default the "
            "one whose waldur_offering_uuid is this offering, else the only one."
        ),
    )
    dry_run = serializers.BooleanField(
        default=True,
        help_text=_("Only show what would change; save nothing."),
    )
    values = serializers.DictField(
        child=SettingValueField(allow_null=True),
        required=False,
        help_text=_(
            "Values for settings the file leaves to the agent host's environment "
            "(see unresolved) or does not set, by dotted path such as "
            "backend_settings.cscs_dwdi_api_url. Never a secret."
        ),
    )
    apply_components = ApplyComponentSerializer(
        many=True,
        required=False,
        help_text=_(
            "Component differences to apply to the offering's components; each "
            "must be applicable."
        ),
    )
    policy_actions = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=_(
            "What the SLURM periodic usage policy does when it fires, as action "
            "names from policy.action_choices. Required when apply_policy "
            "creates the policy; when it exists, given replaces its actions and "
            "left out keeps them."
        ),
    )
    apply_policy = serializers.BooleanField(
        default=False,
        help_text=_(
            "Create or update the offering's SLURM periodic usage policy from the "
            "block's policy_settings."
        ),
    )


class ImportedOfferingBlockSerializer(serializers.Serializer):
    index = serializers.IntegerField()
    name = serializers.CharField(allow_null=True)
    waldur_offering_uuid = serializers.CharField(allow_null=True)
    matches_offering = serializers.BooleanField()


class AgentConfigurationChangeSerializer(serializers.Serializer):
    path = serializers.CharField()
    current = SettingValueField(allow_null=True)
    imported = SettingValueField(allow_null=True)


class UnresolvedSettingSerializer(serializers.Serializer):
    path = serializers.CharField()
    placeholder = serializers.CharField(
        help_text=_("The name inside ${...} that the agent host fills in.")
    )


class IgnoredKeySerializer(serializers.Serializer):
    path = serializers.CharField()
    reason = serializers.CharField()


class ComponentDifferenceSerializer(serializers.Serializer):
    component = serializers.CharField()
    field = serializers.CharField()
    offering = SettingValueField(
        allow_null=True,
        help_text=_("The offering component's value; null when it has none."),
    )
    imported = SettingValueField(allow_null=True)
    applicable = serializers.BooleanField(
        help_text=_("Whether apply_components may name this difference.")
    )
    reason = serializers.CharField(
        allow_null=True, help_text=_("Why it cannot be applied, when it cannot.")
    )


class PolicyChangeSerializer(serializers.Serializer):
    field = serializers.CharField()
    current = SettingValueField(allow_null=True)
    imported = SettingValueField(allow_null=True)


class PolicyActionChoiceSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()


class ImportedPolicySerializer(serializers.Serializer):
    exists = serializers.BooleanField(
        help_text=_("Whether the offering has a SLURM periodic usage policy.")
    )
    needs_actions = serializers.BooleanField(
        help_text=_(
            "Whether applying would create the policy, so policy_actions must "
            "say what it does."
        )
    )
    action_choices = PolicyActionChoiceSerializer(
        many=True,
        help_text=_("The actions a SLURM periodic usage policy can take."),
    )
    changes = PolicyChangeSerializer(many=True)
    applicable = serializers.BooleanField(
        help_text=_("Whether apply_policy may be set.")
    )
    reason = serializers.CharField(
        allow_null=True, help_text=_("Why it cannot be applied, when it cannot.")
    )


class AgentConfigurationImportResultSerializer(serializers.Serializer):
    offerings = ImportedOfferingBlockSerializer(many=True)
    selected_index = serializers.IntegerField()
    agent_configuration = site_agent_configuration.AgentConfigurationSerializer()
    current = site_agent_configuration.AgentConfigurationSerializer()
    changes = AgentConfigurationChangeSerializer(many=True)
    dropped_secrets = serializers.ListField(child=serializers.CharField())
    unresolved = UnresolvedSettingSerializer(many=True)
    ignored = IgnoredKeySerializer(many=True)
    component_differences = ComponentDifferenceSerializer(many=True)
    policy = ImportedPolicySerializer(
        allow_null=True,
        help_text=_(
            "The block's policy_settings against the offering's SLURM periodic "
            "usage policy; null when the block has none."
        ),
    )
    errors = serializers.JSONField(
        help_text=_(
            "Why the proposed configuration cannot be saved, in the shape of "
            "update_integration's agent_configuration errors, with "
            "apply_components and policy for the changes opted into; empty "
            "when it can."
        )
    )
    applied = serializers.BooleanField()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # The configurations go out as they are: the nested serializer, which
        # describes them, would add a null for each nullable field left out.
        for name in ("agent_configuration", "current"):
            data[name] = instance[name]
        return data
