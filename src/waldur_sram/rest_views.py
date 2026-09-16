from constance import config
from django_filters import rest_framework as django_filters
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from waldur_core.core import filters as core_filters
from waldur_core.core.permissions import IsStaff
from waldur_core.core.views import ActionsViewSet

from . import models, rules, serializers


class SramIntegrationEnabledMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not config.SRAM_INTEGRATION_ENABLED:
            raise exceptions.NotFound("SRAM integration is disabled.")


class SramGroupFilter(django_filters.FilterSet):
    customer_uuid = core_filters.RelatedUUIDFilter(
        field_name="customer__uuid", view_name="customer-detail"
    )
    kind = django_filters.ChoiceFilter(choices=models.SramGroup.Kind.choices)
    display_name = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = models.SramGroup
        fields = ("kind",)


@extend_schema(description="SRAM collaborations and groups provisioned into Waldur.")
class SramGroupViewSet(SramIntegrationEnabledMixin, viewsets.ReadOnlyModelViewSet):
    queryset = models.SramGroup.objects.select_related("customer", "role").order_by(
        "urn", "id"
    )
    serializer_class = serializers.SramGroupSerializer
    permission_classes = [IsAuthenticated, IsStaff]
    filter_backends = [django_filters.DjangoFilterBackend]
    filterset_class = SramGroupFilter
    lookup_field = "uuid"


@extend_schema(
    description="Rules granting project roles to holders of SRAM placeholder roles."
)
class SramProjectRuleViewSet(SramIntegrationEnabledMixin, ActionsViewSet):
    queryset = models.SramProjectRule.objects.select_related("project_role")
    serializer_class = serializers.SramProjectRuleSerializer
    permission_classes = [IsAuthenticated, IsStaff]
    lookup_field = "uuid"

    @extend_schema(
        summary="Preview a rule",
        description="Groups the rule matches now, with the projects it selects "
        "and the placeholder holders who would get the project role.",
        responses={200: serializers.SramRulePreviewItemSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def preview(self, request, uuid=None):
        rule = self.get_object()
        items = rules.preview(rule)
        page = self.paginate_queryset(items)
        serializer = serializers.SramRulePreviewItemSerializer(
            page, many=True, context={"request": request}
        )
        return self.get_paginated_response(serializer.data)
