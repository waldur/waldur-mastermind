from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers

from . import PLUGIN_NAME, constants, models


class CredentialsSerializer(serializers.Serializer):
    api_url = serializers.URLField()
    token = serializers.CharField()


class OfferingCreateSerializer(CredentialsSerializer):
    remote_offering_uuid = serializers.CharField()
    local_category_uuid = serializers.CharField()
    local_customer_uuid = serializers.CharField()
    remote_customer_uuid = serializers.CharField()


class ProjectUpdateRequestSerializer(serializers.ModelSerializer):
    state = serializers.ReadOnlyField(source="get_state_display")
    customer_name = serializers.ReadOnlyField(source="project.customer.name")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    offering_uuid = serializers.ReadOnlyField(source="offering.uuid")

    reviewed_by_full_name = serializers.ReadOnlyField(source="reviewed_by.full_name")
    reviewed_by_uuid = serializers.ReadOnlyField(source="reviewed_by.uuid")

    old_oecd_fos_2007_label = serializers.ReadOnlyField(
        source="get_old_oecd_fos_2007_code_display"
    )
    new_oecd_fos_2007_label = serializers.ReadOnlyField(
        source="get_new_oecd_fos_2007_code_display"
    )

    class Meta:
        model = models.ProjectUpdateRequest

        fields = (
            "uuid",
            "state",
            "customer_name",
            "offering_name",
            "offering_uuid",
            "created",
            "reviewed_at",
            "reviewed_by_full_name",
            "reviewed_by_uuid",
            "review_comment",
            "old_name",
            "new_name",
            "old_description",
            "new_description",
            "old_end_date",
            "new_end_date",
            "old_oecd_fos_2007_code",
            "old_oecd_fos_2007_label",
            "new_oecd_fos_2007_code",
            "new_oecd_fos_2007_label",
            "old_is_industry",
            "new_is_industry",
            "created_by",
        )


class NestedRemoteLocalCategorySerializer(serializers.HyperlinkedModelSerializer):
    local_category_name = serializers.ReadOnlyField(source="local_category.title")
    local_category_uuid = serializers.ReadOnlyField(source="local_category.uuid")

    class Meta:
        fields = (
            "local_category",
            "remote_category",
            "local_category_name",
            "local_category_uuid",
            "remote_category_name",
        )
        model = models.RemoteLocalCategory
        extra_kwargs = {
            "local_category": {
                "lookup_field": "uuid",
                "view_name": "marketplace-category-detail",
            },
        }


class RemoteSynchronisationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    local_service_provider_name = serializers.ReadOnlyField(
        source="local_service_provider.customer.name"
    )
    remotelocalcategory_set = NestedRemoteLocalCategorySerializer(many=True)

    class Meta:
        model = models.RemoteSynchronisation
        view_name = "marketplace-remote-synchronisation-detail"
        fields = [
            "uuid",
            "url",
            "api_url",
            "token",
            "remote_organization_uuid",
            "remote_organization_name",
            "local_service_provider",
            "local_service_provider_name",
            "is_active",
            "last_execution",
            "last_output",
            "get_state_display",
            "error_message",
            "created",
            "modified",
            "remotelocalcategory_set",
        ]
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "local_service_provider": {
                "lookup_field": "uuid",
                "view_name": "marketplace-service-provider-detail",
            },
        }
        read_only_fields = ["error_message"]
        protected_fields = ("remote_organization_uuid",)

    def validate(self, attrs):
        if structure_models.Customer.objects.filter(
            uuid=attrs.get("remote_organization_uuid")
        ).exists():
            raise serializers.ValidationError(
                _("Synchronization cannot reference the same Waldur instance.")
            )
        return attrs

    def _create_or_update_category_mapping(self, remote_synchronisation, categories):
        if not categories:
            raise serializers.ValidationError(
                _("At least one category must be specified.")
            )

        # Check for duplicate local categories
        local_categories = [c["local_category"] for c in categories]
        if len(local_categories) != len(set(local_categories)):
            raise serializers.ValidationError(
                _("Duplicate local categories are not allowed.")
            )

        # Check for duplicate remote categories
        remote_categories = [c["remote_category"] for c in categories]
        if len(remote_categories) != len(set(remote_categories)):
            raise serializers.ValidationError(
                _("Duplicate remote categories are not allowed.")
            )

        remote_synchronisation.remotelocalcategory_set.all().delete()

        for c in categories:
            models.RemoteLocalCategory.objects.create(
                local_category=c["local_category"],
                remote_category=c["remote_category"],
                remote_synchronisation=remote_synchronisation,
            )

    def create(self, validated_data):
        categories = validated_data.pop("remotelocalcategory_set", [])
        remote_synchronisation = super().create(validated_data)
        self._create_or_update_category_mapping(remote_synchronisation, categories)
        return remote_synchronisation

    def update(self, remote_synchronisation, validated_data):
        if "remotelocalcategory_set" in validated_data:
            categories = validated_data.pop("remotelocalcategory_set", [])
            self._create_or_update_category_mapping(remote_synchronisation, categories)
        return super().update(remote_synchronisation, validated_data)


def mark_synced_fields_as_read_only(sender, fields, serializer, **kwargs):
    if serializer.instance and serializer.instance.type == PLUGIN_NAME:
        for field_name in constants.OFFERING_FIELDS:
            if field_name in fields:
                fields[field_name] = serializers.ReadOnlyField()


core_signals.pre_serializer_fields.connect(
    mark_synced_fields_as_read_only,
    sender=marketplace_serializers.OfferingOptionsUpdateSerializer,
)


core_signals.pre_serializer_fields.connect(
    mark_synced_fields_as_read_only,
    sender=marketplace_serializers.OfferingOverviewUpdateSerializer,
)
