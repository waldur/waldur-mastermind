import logging

from django.contrib.contenttypes.models import ContentType
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_mastermind.support import models as support_models

logger = logging.getLogger(__name__)


class IssueReferenceSerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    uuid = serializers.CharField(read_only=True)


@extend_schema_field(IssueReferenceSerializer(allow_null=True))
def get_issue(serializer, scope):
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
    fields["issue"] = serializers.SerializerMethodField()
    setattr(sender, "get_issue", get_issue)
