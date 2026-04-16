from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    OrderStates,
    OrderStatesType,
    RemoteResourceSyncStatus,
    ResourceStates,
    ResourceStatesType,
)
from waldur_mastermind.marketplace_remote import constants, models


class RemoteCredentialsSerializer(serializers.Serializer):
    api_url = serializers.URLField()
    token = serializers.CharField()


class RemoteOfferingCreateSerializer(RemoteCredentialsSerializer):
    remote_offering_uuid = serializers.UUIDField()
    local_category_uuid = serializers.UUIDField()
    local_customer_uuid = serializers.UUIDField()
    remote_customer_uuid = serializers.UUIDField()


class RemoteOfferingCreateResponseSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)


class RemoteProjectUpdateRequestSerializer(serializers.ModelSerializer):
    state = serializers.CharField(read_only=True, source="get_state_display")
    customer_name = serializers.CharField(
        read_only=True, source="project.customer.name"
    )
    customer_uuid = serializers.CharField(
        read_only=True, source="project.customer.uuid"
    )
    offering_name = serializers.CharField(read_only=True, source="offering.name")
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")

    reviewed_by_full_name = serializers.CharField(
        read_only=True, source="reviewed_by.full_name"
    )
    reviewed_by_uuid = serializers.UUIDField(read_only=True, source="reviewed_by.uuid")

    old_oecd_fos_2007_label = serializers.CharField(
        read_only=True, source="get_old_oecd_fos_2007_code_display"
    )
    new_oecd_fos_2007_label = serializers.CharField(
        read_only=True, source="get_new_oecd_fos_2007_code_display"
    )

    class Meta:
        model = models.ProjectUpdateRequest

        fields = (
            "uuid",
            "state",
            "customer_name",
            "customer_uuid",
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
    local_category_name = serializers.CharField(
        read_only=True, source="local_category.title"
    )
    local_category_uuid = serializers.UUIDField(
        read_only=True, source="local_category.uuid"
    )

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
    get_state_display = serializers.CharField(read_only=True)

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

    def _create_or_update_category_mapping(
        self, remote_synchronisation: models.RemoteSynchronisation, categories
    ):
        if not categories:
            raise serializers.ValidationError(
                _("At least one category must be specified.")
            )

        # Check for duplicate remote categories
        remote_categories = [c["remote_category"] for c in categories]
        if len(remote_categories) != len(set(remote_categories)):
            raise serializers.ValidationError(
                _("Duplicate remote categories are not allowed.")
            )

        # Create a lookup dictionary for new categories, use "Unknown" as default name
        new_categories_dict = {
            (c["local_category"].id, str(c["remote_category"])): c.get(
                "remote_category_name", "Unknown"
            )
            for c in categories
        }

        # Get existing categories
        existing_categories = remote_synchronisation.remotelocalcategory_set.all()

        # Update or delete existing categories
        for existing_category in existing_categories:
            key = (
                existing_category.local_category_id,
                str(existing_category.remote_category),
            )
            if key in new_categories_dict:
                # Update name if different
                new_name = new_categories_dict[key]
                if existing_category.remote_category_name != new_name:
                    existing_category.remote_category_name = new_name
                    existing_category.save()
                # Remove from dict to track what needs to be created
                del new_categories_dict[key]
            else:
                # Remove if not in new categories
                existing_category.delete()

        # Create new categories
        for (
            local_category_id,
            remote_category,
        ), remote_category_name in new_categories_dict.items():
            category_data = next(
                c
                for c in categories
                if c["local_category"].id == local_category_id
                and str(c["remote_category"]) == remote_category
            )
            models.RemoteLocalCategory.objects.create(
                local_category=category_data["local_category"],
                remote_category=category_data["remote_category"],
                remote_category_name=remote_category_name,  # This will be "Unknown" if not provided
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
    if serializer.instance and serializer.instance.type == REMOTE_OFFERING:
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


class RemoteCustomerSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    abbreviation = serializers.CharField(read_only=True)
    phone_number = serializers.CharField(read_only=True)
    email = serializers.CharField(read_only=True)


class RemoteOfferingSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    type = serializers.CharField(read_only=True)
    state = serializers.CharField(read_only=True)
    category_title = serializers.CharField(read_only=True)


class RemoteResourceSyncStatusSerializer(serializers.Serializer):
    """Serializer for remote resource sync status"""

    local_state = serializers.SerializerMethodField(
        read_only=True, help_text="Local resource state"
    )
    remote_state = serializers.ChoiceField(
        read_only=True,
        choices=ResourceStates.CHOICES,
        allow_null=True,
        help_text="Remote resource state",
    )
    sync_status = serializers.ChoiceField(
        read_only=True,
        choices=RemoteResourceSyncStatus.CHOICES,
        help_text="Sync status: in_sync, out_of_sync, sync_failed",
    )
    last_sync = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Last sync timestamp"
    )

    def get_local_state(self, obj) -> ResourceStatesType:
        return obj.get("local_state")


class RemoteResourceOrderSerializer(serializers.Serializer):
    """Serializer for remote resource orders"""

    order_uuid = serializers.UUIDField(read_only=True, help_text="Order UUID")
    remote_state = serializers.ChoiceField(
        read_only=True, choices=OrderStates.CHOICES, help_text="Remote order state"
    )
    local_state = serializers.SerializerMethodField(
        read_only=True, help_text="Local order state"
    )
    sync_status = serializers.ChoiceField(
        read_only=True,
        choices=RemoteResourceSyncStatus.CHOICES,
        help_text="Sync status: in_sync, out_of_sync, sync_failed",
    )

    def get_local_state(self, obj) -> OrderStatesType:
        return obj.get("local_state")


class RemoteResourceTeamMemberSerializer(serializers.Serializer):
    """Serializer for remote resource team members"""

    full_name = serializers.CharField(read_only=True, help_text="Full name")
    local_role = serializers.CharField(read_only=True, help_text="Local role")
    remote_role = serializers.CharField(read_only=True, help_text="Remote role")
    sync_status = serializers.ChoiceField(
        read_only=True,
        choices=RemoteResourceSyncStatus.CHOICES,
        help_text="Sync status: in_sync, out_of_sync, sync_failed",
    )
