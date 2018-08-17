from __future__ import unicode_literals

import logging
import sys

from django.db import transaction
from django.utils import timezone
import requests
import six

from waldur_core.structure import log_backend_action, ServiceBackend, ServiceBackendError
from waldur_core.structure.utils import (
    update_pulled_fields, handle_resource_not_found, handle_resource_update_success)

from . import models
from .client import RijkscloudClient


logger = logging.getLogger(__name__)


class RijkscloudBackendError(ServiceBackendError):
    pass


def reraise(exc):
    """
    Reraise RijkscloudBackendError while maintaining traceback.
    """
    six.reraise(RijkscloudBackendError, exc, sys.exc_info()[2])


class RijkscloudBackend(ServiceBackend):

    def __init__(self, settings):
        """
        :type settings: :class:`waldur_core.structure.models.ServiceSettings`
        """
        self.settings = settings
        self.client = RijkscloudClient(
            userid=settings.username,
            apikey=settings.token,
        )

    def ping(self, raise_exception=False):
        try:
            self.client.list_flavors()
        except requests.RequestException as e:
            if raise_exception:
                reraise(e)
            return False
        else:
            return True

    def sync(self):
        self.pull_flavors()
        self.pull_floating_ips()
        self.pull_networks()
        self.pull_volumes()
        self.pull_instances()

    def _get_current_properties(self, model):
        return {p.backend_id: p for p in model.objects.filter(settings=self.settings)}

    def _get_backend_resource(self, model, resources):
        registered_backend_ids = model.objects.filter(
            service_project_link__service__settings=self.settings).values_list('backend_id', flat=True)
        return [instance for instance in resources if instance.backend_id not in registered_backend_ids]

    def pull_flavors(self):
        try:
            flavors = self.client.list_flavors()
        except requests.RequestException as e:
            reraise(e)

        with transaction.atomic():
            cur_flavors = self._get_current_properties(models.Flavor)
            for backend_flavor in flavors:
                cur_flavors.pop(backend_flavor['name'], None)
                models.Flavor.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_flavor['name'],
                    defaults={
                        'name': backend_flavor['name'],
                        'cores': backend_flavor['vcpus'],
                        'ram': backend_flavor['ram'],
                    })

            models.Flavor.objects.filter(backend_id__in=cur_flavors.keys()).delete()

    def pull_volumes(self):
        backend_volumes = self.get_volumes()
        volumes = models.Volume.objects.filter(
            service_project_link__service__settings=self.settings,
            state__in=[models.Volume.States.OK, models.Volume.States.ERRED]
        )
        backend_volumes_map = {backend_volume.backend_id: backend_volume for backend_volume in backend_volumes}
        for volume in volumes:
            try:
                backend_volume = backend_volumes_map[volume.backend_id]
            except KeyError:
                handle_resource_not_found(volume)
            else:
                update_pulled_fields(volume, backend_volume, models.Volume.get_backend_fields())
                handle_resource_update_success(volume)

    def get_volumes(self):
        try:
            backend_volumes = self.client.list_volumes()
        except requests.RequestException as e:
            reraise(e)
        else:
            return [self._backend_volume_to_volume(backend_volume)
                    for backend_volume in backend_volumes]

    def _backend_volume_to_volume(self, backend_volume):
        return models.Volume(
            name=backend_volume['name'],
            backend_id=backend_volume['name'],
            size=backend_volume['size'] * 1024,
            metadata=backend_volume['metadata'],
            runtime_state=backend_volume['status'],
            state=models.Volume.States.OK,
        )

    @log_backend_action()
    def pull_volume(self, volume, update_fields=None):
        import_time = timezone.now()
        imported_volume = self.import_volume(volume.backend_id, save=False)

        volume.refresh_from_db()
        if volume.modified < import_time:
            if not update_fields:
                update_fields = models.Volume.get_backend_fields()

            update_pulled_fields(volume, imported_volume, update_fields)

    def import_volume(self, backend_volume_id, save=True, service_project_link=None):
        try:
            backend_volume = self.client.get_volume(backend_volume_id)
        except requests.RequestException as e:
            reraise(e)
        volume = self._backend_volume_to_volume(backend_volume)
        if service_project_link is not None:
            volume.service_project_link = service_project_link
        if save:
            volume.save()

        return volume

    def get_volumes_for_import(self):
        return self._get_backend_resource(models.Volume, self.get_volumes())

    @log_backend_action()
    def pull_volume_runtime_state(self, volume):
        try:
            backend_volume = self.client.get_volume(volume.backend_id)
        except requests.RequestException as e:
            reraise(e)
        else:
            if backend_volume['status'] != volume.runtime_state:
                volume.runtime_state = backend_volume['status']
                volume.save(update_fields=['runtime_state'])

    @log_backend_action()
    def delete_volume(self, volume):
        try:
            self.client.delete_volume(volume.backend_id)
        except requests.RequestException as e:
            reraise(e)

    @log_backend_action('check is volume deleted')
    def is_volume_deleted(self, instance):
        try:
            self.client.get_volume(instance.backend_id)
            return False
        except requests.RequestException as e:
            if e.response.status_code == 404:
                return True
            else:
                reraise(e)

    def pull_instances(self):
        backend_instances = self.get_instances()
        instances = models.Instance.objects.filter(
            service_project_link__service__settings=self.settings,
            state__in=[models.Instance.States.OK, models.Instance.States.ERRED],
        )
        backend_instances_map = {backend_instance.backend_id: backend_instance
                                 for backend_instance in backend_instances}
        for instance in instances:
            try:
                backend_instance = backend_instances_map[instance.backend_id]
            except KeyError:
                handle_resource_not_found(instance)
            else:
                self.update_instance_fields(instance, backend_instance)
                handle_resource_update_success(instance)

    def update_instance_fields(self, instance, backend_instance):
        # Preserve flavor fields in Waldur database if flavor is deleted in Rijkscloud
        fields = set(models.Instance.get_backend_fields())
        flavor_fields = {'flavor_name', 'ram', 'cores'}
        if not backend_instance.flavor_name:
            fields = fields - flavor_fields
        fields = list(fields)

        update_pulled_fields(instance, backend_instance, fields)

    def get_instances(self):
        try:
            backend_instances = self.client.list_instances()
            backend_flavors = self.client.list_flavors()
        except requests.RequestException as e:
            reraise(e)

        backend_flavors_map = {flavor['name']: flavor for flavor in backend_flavors}
        instances = []
        for backend_instance in backend_instances:
            instance_flavor = backend_flavors_map.get(backend_instance['flavor'])
            instances.append(self._backend_instance_to_instance(backend_instance, instance_flavor))
        return instances

    def _backend_instance_to_instance(self, backend_instance, backend_flavor=None):
        instance = models.Instance(
            name=backend_instance['name'],
            state=models.Instance.States.OK,
            backend_id=backend_instance['name'],
        )
        if backend_flavor:
            instance.flavor_name = backend_flavor['name']
            instance.cores = backend_flavor['vcpus']
            instance.ram = backend_flavor['ram']

        # It is assumed that addresses list contains either one or two items, where
        # first item is internal IP address and second item is floating IP address.
        # This code does not handle case when internal subnet CIDR overlaps with other internal subnet.
        addresses = backend_instance['addresses']
        instance.internal_ip = models.InternalIP.objects.filter(
            settings=self.settings, address=addresses[0]).first()
        if len(addresses) == 2:
            instance.floating_ip = models.FloatingIP.objects.filter(
                settings=self.settings, address=addresses[1]).first()
        return instance

    @log_backend_action()
    def pull_instance(self, instance, update_fields=None):
        import_time = timezone.now()
        imported_instance = self.import_instance(instance.backend_id, save=False)

        instance.refresh_from_db()
        if instance.modified < import_time:
            if update_fields is None:
                update_fields = models.Instance.get_backend_fields()
            update_pulled_fields(instance, imported_instance, update_fields)

    def import_instance(self, backend_instance_id, save=True, service_project_link=None):
        try:
            backend_instance = self.client.get_instance(backend_instance_id)
            flavor = self.client.get_flavor(backend_instance['flavor'])
        except requests.RequestException as e:
            reraise(e)

        instance = self._backend_instance_to_instance(backend_instance, flavor)
        if service_project_link:
            instance.service_project_link = service_project_link
        if save:
            instance.save()
        return instance

    def get_instances_for_import(self):
        return self._get_backend_resource(models.Instance, self.get_instances())

    @log_backend_action()
    def create_volume(self, volume):
        kwargs = {
            'size': max(1, int(volume.size / 1024)),
            'name': volume.name,
            'description': volume.description,
        }
        try:
            self.client.create_volume(kwargs)
        except requests.RequestException as e:
            reraise(e)

        backend_volume = self.client.get_volume(volume.name)
        volume.backend_id = volume.name
        volume.runtime_state = backend_volume['status']
        volume.save()
        return volume

    @log_backend_action()
    def create_instance(self, instance):
        # It's impossible to specify custom security group
        # because Rijkscloud API does not provide security groups API yet.
        kwargs = {
            'name': instance.name,
            'flavor': instance.flavor_name,
            'userdata': instance.user_data or 'normal',
            'interfaces': [
                {
                    'subnets': [
                        {
                            'ip': instance.internal_ip.address,
                            'name': instance.internal_ip.subnet.name,
                        }
                    ],
                    'network': instance.internal_ip.subnet.network.name,
                    'security_groups': ['any-any']
                }
            ]
        }

        if instance.floating_ip:
            kwargs['interfaces'][0]['float'] = instance.floating_ip.address

        try:
            self.client.create_instance(kwargs)
        except requests.RequestException as e:
            reraise(e)

        instance.backend_id = instance.name
        instance.save()
        return instance

    @log_backend_action()
    def delete_instance(self, instance):
        try:
            self.client.delete_instance(instance.backend_id)
        except requests.RequestException as e:
            reraise(e)

    @log_backend_action('check is instance deleted')
    def is_instance_deleted(self, instance):
        try:
            self.client.get_instance(instance.backend_id)
            return False
        except requests.RequestException as e:
            if e.response.status_code == 404:
                return True
            else:
                reraise(e)

    def pull_floating_ips(self):
        try:
            backend_floating_ips = self.client.list_floatingips()
        except requests.RequestException as e:
            reraise(e)
            return

        with transaction.atomic():
            cur_floating_ips = self._get_current_properties(models.FloatingIP)
            for backend_fip in backend_floating_ips:
                cur_floating_ips.pop(backend_fip['float_ip'], None)
                models.FloatingIP.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_fip['float_ip'],
                    defaults={
                        'address': backend_fip['float_ip'],
                        'is_available': backend_fip['available'],
                    })

            models.FloatingIP.objects.filter(backend_id__in=cur_floating_ips.keys()).delete()

    def pull_networks(self):
        try:
            backend_networks = self.client.list_networks()
        except requests.RequestException as e:
            reraise(e)
            return

        with transaction.atomic():
            current_networks = self._get_current_properties(models.Network)
            for backend_network in backend_networks:
                current_networks.pop(backend_network['name'], None)
                network, _ = models.Network.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_network['name'],
                    defaults=dict(name=backend_network['name']),
                )
                self.pull_subnets(network, backend_network['subnets'])

            models.Network.objects.filter(backend_id__in=current_networks.keys()).delete()

    def pull_subnets(self, network, backend_subnets):
        for backend_subnet in backend_subnets:
            gateway_ip = backend_subnet['gateway_ip']
            if isinstance(gateway_ip, list):
                gateway_ip = gateway_ip[0]
            subnet, _ = models.SubNet.objects.update_or_create(
                settings=self.settings,
                network=network,
                backend_id=backend_subnet['name'],
                defaults=dict(
                    name=backend_subnet['name'],
                    cidr=backend_subnet['cidr'],
                    gateway_ip=gateway_ip,
                    allocation_pools=backend_subnet['allocation_pools'],
                    dns_nameservers=backend_subnet['dns_nameservers'],
                )
            )
            self.pull_internal_ips(subnet, backend_subnet['ips'])

    def pull_internal_ips(self, subnet, internal_ips):
        for internal_ip in internal_ips:
            models.InternalIP.objects.update_or_create(
                settings=self.settings,
                subnet=subnet,
                backend_id=internal_ip['ip'],
                defaults={
                    'name': internal_ip['ip'],
                    'address': internal_ip['ip'],
                    'is_available': internal_ip['available'],
                }
            )
