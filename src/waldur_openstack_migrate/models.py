from django.contrib.auth import get_user_model
from django.db import models
from model_utils.models import TimeStampedModel

from waldur_core.core.fields import JSONField
from waldur_core.core.models import StateMixin, UuidMixin
from waldur_mastermind.marketplace.models import Resource

User = get_user_model()


def build_migration_query(user):
    return models.Q(created_by=user)


class Migration(TimeStampedModel, StateMixin, UuidMixin):
    class Permissions:
        build_query = build_migration_query

    created_by = models.ForeignKey(to=User, related_name="+", on_delete=models.CASCADE)
    src_resource = models.ForeignKey(
        to=Resource, related_name="+", on_delete=models.CASCADE
    )
    dst_resource = models.ForeignKey(
        to=Resource, related_name="+", on_delete=models.CASCADE
    )
    mappings = JSONField(null=True, blank=True)
