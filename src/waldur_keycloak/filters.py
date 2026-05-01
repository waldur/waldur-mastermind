import django_filters

from waldur_core.core import filters as core_filters

from . import models


class OfferingKeycloakGroupFilter(django_filters.FilterSet):
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
    )
    role_uuid = core_filters.RelatedUUIDFilter(
        view_name="role-detail",
        field_name="role__uuid",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
    )

    class Meta:
        model = models.OfferingKeycloakGroup
        fields = (
            "offering_uuid",
            "role_uuid",
            "resource_uuid",
        )


class OfferingKeycloakMembershipFilter(django_filters.FilterSet):
    group_uuid = core_filters.RelatedUUIDFilter(
        view_name="offering-keycloak-group-detail",
        field_name="group__uuid",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="group__offering__uuid",
    )
    role_uuid = core_filters.RelatedUUIDFilter(
        view_name="role-detail",
        field_name="group__role__uuid",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="group__resource__uuid",
    )
    username = django_filters.CharFilter()
    email = django_filters.CharFilter()
    first_name = django_filters.CharFilter()
    last_name = django_filters.CharFilter()
    state = django_filters.MultipleChoiceFilter(
        choices=models.KeycloakMembershipState.CHOICES
    )

    class Meta:
        model = models.OfferingKeycloakMembership
        fields = (
            "group_uuid",
            "offering_uuid",
            "role_uuid",
            "resource_uuid",
            "username",
            "email",
            "first_name",
            "last_name",
            "state",
        )
