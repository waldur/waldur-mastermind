import re

from django.core.validators import RegexValidator
from django.utils.translation import ugettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_openstack.openstack_tenant.views import (
    InstanceViewSet,
    MarketplaceInstanceViewSet,
)

from . import utils

ClusterNameValidator = RegexValidator(
    regex=re.compile(r"^[a-z0-9]([-a-z0-9])+[a-z0-9]$"),
    message=_(
        'Name must consist of lower case alphanumeric characters or \'-\', '
        'and must start and end with an alphanumeric character'
    ),
)


def related_vm_can_be_deleted(node):
    validators = MarketplaceInstanceViewSet.force_destroy_validators

    for validator in validators:
        if node.instance:
            validator(node.instance)


def all_cluster_related_vms_can_be_deleted(cluster):
    for node in cluster.node_set.all():
        related_vm_can_be_deleted(node)


def console_validator(node):
    validators = InstanceViewSet.console_validators

    for validator in validators:
        if node.instance:
            validator(node.instance)


def creation_of_management_security_group_is_available(cluster):
    if cluster.management_security_group:
        raise ValidationError('Management security group already exists.')

    tenant = utils.get_management_tenant(cluster)

    if not tenant:
        raise ValidationError('Management tenant is not set.')
