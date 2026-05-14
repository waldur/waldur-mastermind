from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_autoprovisioning import evaluators, models
from waldur_core.core.models import User
from waldur_core.core.views import ActionsViewSet
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions

from . import serializers


@extend_schema(description="Manage autoprovisioning rules.")
class RuleViewSet(ActionsViewSet):
    queryset = models.Rule.objects.all().order_by("-customer")
    serializer_class = serializers.RuleSerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_staff
    ]

    @extend_schema(
        summary="Dry-run rule evaluation against a target user.",
        request=serializers.RuleTestMatchRequestSerializer,
        responses={200: serializers.RuleTestMatchResponseSerializer},
        description=(
            "Evaluate this rule against the given user without provisioning. "
            "Returns per-filter outcomes, the customer-lookup verdict (when the "
            "rule uses use_user_organization_as_customer_name) and a top-line "
            "would_provision flag together with a human-readable block_reason."
        ),
    )
    @action(detail=True, methods=["post"], url_path="test-match")
    def test_match(self, request, uuid=None):
        rule = self.get_object()
        request_serializer = serializers.RuleTestMatchRequestSerializer(
            data=request.data
        )
        request_serializer.is_valid(raise_exception=True)
        user = get_object_or_404(
            User, uuid=request_serializer.validated_data["user_uuid"]
        )
        payload = evaluators.compute_test_match(rule, user)
        response_serializer = serializers.RuleTestMatchResponseSerializer(
            payload, context={"request": request}
        )
        return Response(response_serializer.data)

    test_match_permissions = [structure_permissions.is_staff]
    test_match_serializer_class = serializers.RuleTestMatchRequestSerializer
