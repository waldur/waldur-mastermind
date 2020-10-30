from celery import shared_task

from waldur_core.core import utils as core_utils

from . import utils


@shared_task(name='waldur_mastermind.marketplace_openstack.push_tenant_limits')
def push_tenant_limits(serialized_resource):
    resource = core_utils.deserialize_instance(serialized_resource)
    utils.push_tenant_limits(resource)


@shared_task(name='waldur_mastermind.marketplace_openstack.restore_tenant_limits')
def restore_tenant_limits(serialized_resource):
    resource = core_utils.deserialize_instance(serialized_resource)
    utils.restore_limits(resource)
