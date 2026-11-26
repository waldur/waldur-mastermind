from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_pid import models


class OfferingReferralSerializer(
    serializers.HyperlinkedModelSerializer,
    core_serializers.AugmentedSerializerMixin,
):
    scope = core_serializers.GenericRelatedField(read_only=True)
    scope_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")

    class Meta:
        model = models.DataciteReferral
        fields = (
            "url",
            "uuid",
            "scope",
            "scope_uuid",
            "pid",
            "relation_type",
            "resource_type",
            "creator",
            "publisher",
            "published",
            "title",
            "referral_url",
        )
        extra_kwargs = dict(
            url={
                "lookup_field": "uuid",
                "view_name": "marketplace-offering-referral-detail",
            },
            offering={
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        )
