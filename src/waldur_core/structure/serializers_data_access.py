"""
Serializers for user data access visibility endpoints.
"""

from rest_framework import serializers

from waldur_core.logging.models import UserDataAccessLog


class AccessTypeEnum:
    """Access type for administrative users (staff/support)."""

    STAFF = "staff"
    SUPPORT = "support"
    STAFF_AND_SUPPORT = "staff_and_support"

    CHOICES = (
        (STAFF, "Staff"),
        (SUPPORT, "Support"),
        (STAFF_AND_SUPPORT, "Staff and Support"),
    )


# Re-export AccessorType from model for convenience
AccessorTypeEnum = UserDataAccessLog.AccessorType


class AdminUserSerializer(serializers.Serializer):
    """Serializer for individual admin/support user in data access response."""

    user_uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()
    access_type = serializers.ChoiceField(choices=AccessTypeEnum.CHOICES)


class AdministrativeAccessSerializer(serializers.Serializer):
    """Serializer for administrative access section."""

    description = serializers.CharField()
    # Counts and users are only included for staff/support viewers
    staff_count = serializers.IntegerField(required=False)
    support_count = serializers.IntegerField(required=False)
    users = AdminUserSerializer(many=True, required=False)


class OrganizationalUserSerializer(serializers.Serializer):
    """Serializer for individual user in organizational access."""

    user_uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()
    role = serializers.CharField(allow_null=True)


class OrganizationalAccessSerializer(serializers.Serializer):
    """Serializer for organizational access (customer/project scope)."""

    scope_type = serializers.CharField()
    scope_uuid = serializers.UUIDField()
    scope_name = serializers.CharField()
    users = OrganizationalUserSerializer(many=True)


class ProviderTeamUserSerializer(serializers.Serializer):
    """Serializer for provider team member."""

    user_uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()
    role = serializers.CharField(allow_null=True)


class ServiceProviderAccessSerializer(serializers.Serializer):
    """Serializer for service provider access (via consent)."""

    offering_uuid = serializers.UUIDField()
    offering_name = serializers.CharField()
    provider_name = serializers.CharField(allow_null=True)
    provider_uuid = serializers.UUIDField(allow_null=True)
    exposed_fields = serializers.ListField(child=serializers.CharField())
    consent_date = serializers.CharField(allow_null=True)
    consent_version = serializers.CharField(allow_null=True)
    provider_team = ProviderTeamUserSerializer(many=True, required=False)


class DataAccessSummarySerializer(serializers.Serializer):
    """Serializer for data access summary."""

    # total_administrative_access is null for regular users (counts hidden)
    total_administrative_access = serializers.IntegerField(allow_null=True)
    total_organizational_access = serializers.IntegerField()
    total_provider_access = serializers.IntegerField()


class UserDataAccessSerializer(serializers.Serializer):
    """Main serializer for user data access visibility response."""

    administrative_access = AdministrativeAccessSerializer()
    organizational_access = OrganizationalAccessSerializer(many=True)
    service_provider_access = ServiceProviderAccessSerializer(many=True)
    summary = DataAccessSummarySerializer()


class AccessorUserSerializer(serializers.Serializer):
    """Serializer for accessor user details (staff/support view only)."""

    uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()


class UserDataAccessLogSerializer(serializers.Serializer):
    """
    Serializer for user data access log entries.

    For regular users viewing own history: anonymized accessors.
    For staff/support viewing any user's history: full details.
    """

    uuid = serializers.UUIDField()
    timestamp = serializers.DateTimeField()
    accessor_type = serializers.ChoiceField(choices=AccessorTypeEnum.CHOICES)
    accessed_fields = serializers.ListField(child=serializers.CharField())

    # Regular users see anonymized category
    accessor_category = serializers.CharField(required=False)

    # Staff/support see full details
    accessor = AccessorUserSerializer(required=False)
    ip_address = serializers.IPAddressField(required=False, allow_null=True)
    context = serializers.DictField(required=False)

    def to_representation(self, instance):
        """
        Conditionally include fields based on viewer permissions.

        Regular users see accessor_category (anonymized).
        Staff/support see accessor details, IP, and context.
        """
        data = {
            "uuid": instance.uuid,
            "timestamp": instance.timestamp,
            "accessor_type": instance.accessor_type,
            "accessed_fields": instance.accessed_fields,
        }

        request = self.context.get("request")
        include_details = (
            request
            and request.user
            and (request.user.is_staff or request.user.is_support)
        )

        if include_details:
            # Staff/support see full details
            if instance.accessor:
                data["accessor"] = {
                    "uuid": str(instance.accessor.uuid),
                    "username": instance.accessor.username,
                    "full_name": instance.accessor.full_name,
                }
            else:
                data["accessor"] = None
            data["ip_address"] = instance.ip_address
            data["context"] = instance.context
        else:
            # Regular users see anonymized category
            data["accessor_category"] = _get_accessor_category(instance)

        return data


def _get_accessor_category(log_entry):
    """
    Get anonymized accessor category description.

    Args:
        log_entry: UserDataAccessLog instance

    Returns:
        str: Human-readable description of accessor category
    """
    category_map = {
        "staff": "Platform administrator",
        "support": "Platform support staff",
        "organization_member": "User in your organization",
        "service_provider": "Service provider",
        "self": "You",
    }
    return category_map.get(log_entry.accessor_type, "Unknown")


class TargetUserSerializer(serializers.Serializer):
    """Serializer for target user whose data was accessed."""

    uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()


class GlobalUserDataAccessLogSerializer(serializers.Serializer):
    """
    Extended serializer for global admin endpoint.

    Includes full details plus target user information.
    Staff/support only - always shows full accessor details.
    """

    uuid = serializers.UUIDField()
    timestamp = serializers.DateTimeField()
    accessor_type = serializers.ChoiceField(choices=AccessorTypeEnum.CHOICES)
    accessed_fields = serializers.ListField(child=serializers.CharField())
    user = TargetUserSerializer(source="target_user")
    accessor = AccessorUserSerializer()
    ip_address = serializers.IPAddressField(allow_null=True)
    context = serializers.DictField()
