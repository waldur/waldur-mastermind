from __future__ import unicode_literals

import collections
import itertools
import logging

from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions
from waldur_ansible.common.exceptions import AnsibleBackendError
from waldur_mastermind.packages import models as package_models
from waldur_openstack.openstack_tenant import serializers as openstack_serializers

from . import serializers

logger = logging.getLogger(__name__)


class InMemoryJob(object):
    """
    This class is used instead of database object for Ansible Job model.
    """

    def __init__(self, **kwargs):
        self.output = ''
        self.__dict__.update(kwargs)

    def get_tag(self):
        return 'VALID_JOB_TAG'

    def save(self, *args, **kwargs):
        """Skip database interaction"""


class InMemoryResource(object):
    """
    This class allows to treat dictionary as an object
    For example, use resource.flavor.cores instead of resource['flavor'].cores
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def get_job(request):
    """
    Construct in-memory Ansible job object using default values for SSH key and name.
    """
    serializer = serializers.JobEstimateSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)

    return InMemoryJob(
        name='VALID_JOB_NAME',
        **serializer.validated_data
    )


def get_prices(service_settings):
    """
    Fetch prices for service settings.
    Here's diagram to illustrate relationships between involved entities:

    Job => Service project link => Service => Service settings =>
    Tenant as scope => Extra configuration dict => Package template => Package component.

    Raises a validation exception if any link is broken.
    """
    tenant = service_settings.scope
    if not tenant:
        raise exceptions.ValidationError(_('Provider is not connected to the OpenStack tenant.'))

    package_uuid = tenant.extra_configuration.get('package_uuid')
    if not package_uuid:
        raise exceptions.ValidationError(_('OpenStack tenant is not connected to the VPC package.'))

    try:
        package_template = package_models.PackageTemplate.objects.get(uuid=package_uuid)
    except package_models.PackageTemplate.DoesNotExist:
        raise exceptions.ValidationError(_('VPC package is not connected to the package template.'))

    components = package_template.components

    try:
        return {
            'cpu': components.get(type=package_models.PackageComponent.Types.CORES).price,
            'ram': components.get(type=package_models.PackageComponent.Types.RAM).price,
            'disk': components.get(type=package_models.PackageComponent.Types.STORAGE).price,
        }
    except package_models.PackageComponent.DoesNotExist:
        raise exceptions.ValidationError(_('Package template does not have required components.'))


def evaluate_job(job):
    """
    Extract JSON dictionaries for resources provisioning from Ansible playbook.
    """
    backend = job.playbook.get_backend()
    try:
        backend.run_job(job, check_mode=True)
        return backend.decode_output(job.output)
    except AnsibleBackendError as e:
        logger.debug('Unable to process Ansible playbook. '
                     'Error message is %s, job output is %s',
                     e.message, job.output)
        return []


def get_resources(request, job):
    """
    Get list of resources to be provisioned for Ansible job.
    Raises an exception if provisioning is not possible.
    """
    items = evaluate_job(job)
    errors = []
    resources = []

    settings = job.service_project_link.service.settings
    link = job.service_project_link

    for item in items:
        if 'ssh_public_key' in item:
            item['ssh_public_key'] = request.data['ssh_public_key']
        serializer = openstack_serializers.InstanceSerializer(data=item, context={'request': request})
        if not serializer.is_valid():
            errors.append(serializer.errors)
        else:
            resource = InMemoryResource(**serializer.validated_data)

            quota_errors = list(itertools.chain(
                settings.validate_quota_change({settings.Quotas.instances: 1}),
                settings.validate_quota_change({settings.Quotas.ram: resource.flavor.ram}),
                settings.validate_quota_change({settings.Quotas.vcpu: resource.flavor.cores}),
                link.validate_quota_change({link.Quotas.ram: resource.flavor.ram}),
                link.validate_quota_change({link.Quotas.vcpu: resource.flavor.cores}),
            ))
            if quota_errors:
                errors.append(_('One or more quotas were exceeded: %s') % ';'.join(quota_errors))
            else:
                resources.append(resource)

    if errors:
        raise exceptions.ValidationError(errors)
    return resources


def get_requirements(resources):
    """
    Calculate quotas requirements for CPU, RAM and disk.
    """
    requirements = collections.defaultdict(float)
    for resource in resources:
        requirements['cpu'] += resource.flavor.cores
        requirements['ram'] += resource.flavor.ram
        requirements['disk'] += resource.system_volume_size + getattr(resource, 'data_volume_size', 0)
    return requirements


def get_total_cost(requirements, prices):
    """
    Calculate total cost of provisioned resources
    with respect to requirements and package prices.
    """
    return sum(
        float(prices[component]) * float(requirements[component])
        for component in ('cpu', 'ram', 'disk')
    )


def get_report(request):
    """
    Calculate prices, requirements and total cost for resource provisioning.
    Raises an error if provisioning parameters are invalid or quota is exceeded.
    Please note that currently it is limited to OpenStack instance provisioning.
    """
    job = get_job(request)
    service_settings = job.service_project_link.service.settings
    resources = get_resources(request, job)
    requirements = get_requirements(resources)

    prices = {'cpu': 0, 'ram': 0, 'disk': 0}
    try:
        prices = get_prices(service_settings)
    except exceptions.ValidationError as e:
        logger.debug('Unable to get prices for service settings %s. Error is %s.', service_settings, e)

    cost = get_total_cost(requirements, prices)

    return {
        'prices': prices,
        'requirements': requirements,
        'cost': cost
    }
