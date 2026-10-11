import logging

from django.contrib.contenttypes.models import ContentType
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_mastermind.support import models as support_models

logger = logging.getLogger(__name__)


class IssueReferenceSerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    uuid = serializers.CharField(read_only=True)


def attach_issues(orders):
    """Load the support issues of a page of orders in one query.

    Each order gets the list ``get_issue`` would otherwise query for, in the
    same (default) order, so lists render the same as detail views.
    """
    orders = [order for order in orders if order is not None]
    if not orders:
        return
    by_order = {order.id: [] for order in orders}
    content_type = ContentType.objects.get_for_model(orders[0])
    for issue in support_models.Issue.objects.filter(
        resource_content_type=content_type, resource_object_id__in=list(by_order)
    ):
        by_order[issue.resource_object_id].append(issue)
    for order in orders:
        order.prefetched_support_issues = by_order[order.id]


@extend_schema_field(IssueReferenceSerializer(allow_null=True))
def get_issue(serializer, scope):
    prefetched = getattr(scope, "prefetched_support_issues", None)
    if prefetched is not None:
        issues = prefetched
        connected_issue_count = len(prefetched)
    else:
        issues = support_models.Issue.objects.filter(
            resource_object_id=scope.id,
            resource_content_type_id=ContentType.objects.get_for_model(scope).id,
        )
        connected_issue_count = issues.count()
    if connected_issue_count == 0:
        return
    if connected_issue_count > 1:
        logger.error(
            "Order has %s instead of 1 issues connected. Unable to select. Order UUID: %s",
            connected_issue_count,
            scope.uuid.hex,
        )

    issue = issues[0]

    issue_map = {"key": issue.key, "uuid": issue.uuid.hex}
    return issue_map


def add_issue(sender, fields, **kwargs):
    """Add an issue field to the serializer."""
    fields["issue"] = serializers.SerializerMethodField()
    setattr(sender, "get_issue", get_issue)
