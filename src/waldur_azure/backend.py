import collections
import logging
import os
import re
import ssl
import sys
import tempfile
import time

from django.core.files.uploadedfile import File, InMemoryUploadedFile
from django.db import IntegrityError
from django.utils import six
from libcloud.common.types import LibcloudError, InvalidCredsError
from libcloud.compute.base import NodeAuthPassword
from libcloud.compute.drivers import azure
from libcloud.compute.types import NodeState

from waldur_core.structure import ServiceBackend, ServiceBackendError, ServiceBackendNotImplemented, \
    log_backend_action

from . import models
from .driver import AzureNodeDriver, AZURE_COMPUTE_INSTANCE_TYPES


logger = logging.getLogger(__name__)

# libcloud doesn't match Visual Studio images properly
azure.WINDOWS_SERVER_REGEX = re.compile(
    azure.WINDOWS_SERVER_REGEX.pattern + '|VS-201[35]'
)

# there's a hope libcloud will implement this method in further releases
AzureNodeDriver.ex_list_storage_services = lambda self: \
    self._perform_get(self._get_path('services', 'storageservices'), StorageServices)


class SizeQueryset(object):
    def __init__(self):
        self.items = []
        for key, val in AZURE_COMPUTE_INSTANCE_TYPES.items():
            self.items.append(SizeQueryset.Size(uuid=val['id'],
                                                pk=val['id'],
                                                name='{}: {}'.format(key, val['name']),
                                                cores=isinstance(val['cores'], int) and val['cores'] or 1,
                                                ram=val['ram'],
                                                disk=ServiceBackend.gb2mb(val['disk']),
                                                price=float(val['price'])))

        self.items = list(sorted(self.items, key=lambda s: s.price))

    def __len__(self):
        return len(self.items)

    def __getitem__(self, key):
        return self.items[key]

    def all(self):
        return self.items

    def get(self, uuid):
        for item in self.items:
            if item.uuid == uuid:
                return item
        raise ValueError

    class Size(collections.namedtuple('Size', ('uuid', 'pk', 'name', 'cores', 'ram', 'disk', 'price'))):
        def __str__(self):
            return self.name


class StorageServiceProperties(azure.WindowsAzureData):

    def __init__(self):
        self.description = ''
        self.location = ''
        self.affinity_group = ''
        self.label = azure._Base64String()
        self.status = ''
        self.createion_time = ''


class StorageService(azure.WindowsAzureData):
    _repr_attributes = [
        'service_name',
        'url'
    ]

    def __init__(self):
        self.url = ''
        self.service_name = ''
        self.storage_service_properties = StorageServiceProperties()


class StorageServices(azure.WindowsAzureDataTypedList, azure.ReprMixin):
    list_type = StorageService

    _repr_attributes = [
        'items'
    ]


class AzureBackendError(ServiceBackendError):
    pass


def reraise(exc):
    """
    Reraise AzureBackendError while maintaining traceback.
    """
    six.reraise(AzureBackendError, exc, sys.exc_info()[2])


class AzureBaseBackend(ServiceBackend):

    State = NodeState

    def __init__(self, settings, cloud_service_name=None):
        self.deployment = 'production'
        self.location = 'Central US'
        if settings.options and 'location' in settings.options:
            self.location = settings.options['location']

        self.settings = settings
        self.cloud_service_name = cloud_service_name

    def __del__(self):
        try:
            os.remove(self._temp_file)
        except (AttributeError, OSError):
            pass

    # Lazy init
    @property
    def manager(self):
        if not hasattr(self, '_manager'):
            key_file = None
            cert_file = self.settings.certificate.file if self.settings.certificate else None
            if isinstance(cert_file, InMemoryUploadedFile):
                cert_file.seek(0)
                temp_file = tempfile.NamedTemporaryFile(mode='w+t', delete=False)
                temp_file.writelines(cert_file.readlines())
                temp_file.close()
                cert_file.seek(0)
                key_file = self._temp_file = temp_file.name
            elif isinstance(cert_file, File):
                key_file = cert_file.name

            try:
                self._manager = AzureNodeDriver(
                    subscription_id=self.settings.username, key_file=key_file)
            except InvalidCredsError as e:
                logger.exception("Wrong credentials for service settings %s", self.settings.uuid)
                reraise(e)
        return self._manager

    def sync(self):
        self.pull_service_properties()

    def sync_link(self, service_project_link, is_initial=False):
        self.push_link(service_project_link)

    def remove_link(self, service_project_link):
        # TODO: this should remove storage and cloud service
        raise ServiceBackendNotImplemented


class AzureBackend(AzureBaseBackend):
    """ Waldur interface to Azure API.
        http://libcloud.readthedocs.org/en/latest/compute/drivers/azure.html
    """

    def ping(self, raise_exception=False):
        try:
            self.manager.list_locations()
        except (AzureBackendError, LibcloudError, ssl.SSLError) as e:
            if raise_exception:
                reraise(e)
            return False
        else:
            return True

    def ping_resource(self, instance):
        try:
            self.get_vm(instance.backend_id)
        except AzureBackendError:
            return False
        else:
            return True

    def pull_service_properties(self):
        self.pull_images()

    def pull_images(self):
        options = self.settings.options or {}
        regex = None
        if 'images_regex' in options:
            try:
                regex = re.compile(options['images_regex'])
            except re.error:
                logger.warning(
                    'Invalid images regexp supplied for service settings %s: %s',
                    self.settings.uuid, options['images_regex'])

        images = {}
        for image in self.manager.list_images():
            images.setdefault(image.name, [])
            images[image.name].append(image)

        cur_images = {i.backend_id: i for i in models.Image.objects.all()}
        for backend_images in images.values():
            backend_image = sorted(backend_images)[-1]  # get last image with same name (perhaps newest one)
            if regex and not regex.match(backend_image.name):
                continue
            cur_images.pop(backend_image.id, None)
            try:
                models.Image.objects.update_or_create(
                    backend_id=backend_image.id,
                    defaults={
                        'name': backend_image.name,
                    })
            except IntegrityError:
                logger.warning(
                    'Could not create Azure image with id %s due to concurrent update',
                    backend_image.id)

        map(lambda i: i.delete(), cur_images.values())

    def push_link(self, service_project_link):
        # define cloud service name
        options = service_project_link.service.settings.options
        if options and 'cloud_service_name' in options:
            cloud_service_name = options['cloud_service_name']
            service_project_link.cloud_service_name = cloud_service_name
            service_project_link.save(update_fields=['cloud_service_name'])
        else:
            cloud_service_name = 'nc-%x' % service_project_link.project.uuid.node

        # create cloud
        services = [s.service_name for s in self.manager.ex_list_cloud_services()]
        if cloud_service_name not in services:
            logger.debug('About to create new azure cloud service for SPL %s', service_project_link.pk)
            self.manager.ex_create_cloud_service(cloud_service_name, self.location)
            service_project_link.cloud_service_name = cloud_service_name
            service_project_link.save(update_fields=['cloud_service_name'])
            logger.info('Successfully created new azure cloud for SPL %s', service_project_link.pk)
        else:
            logger.debug('Skipped azure cloud service creation for SPL %s - such cloud already exists', service_project_link.pk)

        # create storage
        storage_name = self.get_storage_name(cloud_service_name)
        storages = [s.service_name for s in self.manager.ex_list_storage_services()]

        if storage_name not in storages:
            logger.debug('About to create new azure storage for SPL %s', service_project_link.pk)
            self.manager.ex_create_storage_service(storage_name, self.location)

            # XXX: missed libcloud feature
            #      it will block celery worker for a while (5 min max)
            #      but it's easiest workaround for azure and general syncing workflow
            for _ in range(100):
                storage = self.get_storage(storage_name)
                if storage.storage_service_properties.status == 'Created':  # ResolvingDns otherwise
                    break
                time.sleep(30)
            logger.info('Successfully created new azure storage for SPL %s', service_project_link.pk)
        else:
            logger.debug(
                'Skipped azure storage creation for SPL %s - such cloud already exists', service_project_link.pk)

    @log_backend_action()
    def reboot_vm(self, vm):
        self.manager.reboot_node(
            self.get_vm(vm.backend_id),
            ex_cloud_service_name=self.cloud_service_name,
            ex_deployment_slot=self.deployment)

    @log_backend_action()
    def stop_vm(self, vm):
        deployment_name = self.manager._get_deployment(
            service_name=self.cloud_service_name,
            deployment_slot=self.deployment
        ).name

        try:
            response = self.manager._perform_post(
                self.manager._get_deployment_path_using_name(
                    self.cloud_service_name, deployment_name
                ) + '/roleinstances/' + azure._str(vm.backend_id) + '/Operations',
                azure.AzureXmlSerializer.shutdown_role_operation_to_xml()
            )

            self.manager.raise_for_response(response, 202)
            self.manager._ex_complete_async_azure_operation(response)
        except Exception as e:
            reraise(e)

    @log_backend_action()
    def start_vm(self, vm):
        deployment_name = self.manager._get_deployment(
            service_name=self.cloud_service_name,
            deployment_slot=self.deployment
        ).name

        try:
            response = self.manager._perform_post(
                self.manager._get_deployment_path_using_name(
                    self.cloud_service_name, deployment_name
                ) + '/roleinstances/' + azure._str(vm.backend_id) + '/Operations',
                azure.AzureXmlSerializer.start_role_operation_to_xml()
            )

            self.manager.raise_for_response(response, 202)
            self.manager._ex_complete_async_azure_operation(response)
        except Exception as e:
            reraise(e)

    @log_backend_action()
    def destroy_vm(self, vm):
        self.manager.destroy_node(
            self.get_vm(vm.backend_id),
            ex_cloud_service_name=self.cloud_service_name,
            ex_deployment_slot=self.deployment)

    @log_backend_action('check if virtual machine deleted')
    def is_vm_deleted(self, vm):
        try:
            self.get_vm(vm.backend_id)
        except AzureBackendError:
            return True
        else:
            return False

    @log_backend_action()
    def provision_vm(self, vm, backend_image_id=None, backend_size_id=None):
        try:
            backend_vm = self.manager.create_node(
                name=vm.name,
                size=self.get_size(backend_size_id),
                image=self.get_image(backend_image_id),
                ex_cloud_service_name=self.cloud_service_name,
                ex_storage_service_name=self.get_storage_name(),
                ex_deployment_slot=self.deployment,
                ex_custom_data=vm.user_data,
                ex_admin_user_id=vm.user_username,
                auth=NodeAuthPassword(vm.user_password))
        except LibcloudError as e:
            logger.exception('Failed to provision virtual machine %s', vm.name)
            reraise(e)

        vm.backend_id = backend_vm.id
        vm.runtime_state = backend_vm.state
        vm.save(update_fields=['backend_id', 'runtime_state'])

    def pull_virtual_machine_runtime_state(self, vm):
        backend_vm = self.get_vm(vm.backend_id)
        vm.runtime_state = backend_vm.state
        vm.save(update_fields=['runtime_state'])

    def pull_vm_info(self, vm):
        """
        VM network info os available only after instance has been initiated and started.
        :param vm: waldur virtual machine instance to update IPs
        """
        backend_vm = self.get_vm(vm.backend_id)
        endpoints = []
        for endpoint in backend_vm.extra.get('instance_endpoints'):
            endpoints.append(models.InstanceEndpoint(
                name=endpoint.name,
                local_port=int(endpoint.local_port),
                public_port=int(endpoint.public_port),
                protocol=endpoint.protocol,
                instance=vm,
            ))
        models.InstanceEndpoint.objects.bulk_create(endpoints)
        vm.private_ips = backend_vm.private_ips
        vm.public_ips = backend_vm.public_ips
        vm.save(update_fields=['private_ips', 'public_ips'])

    def get_vm(self, vm_id):
        try:
            vm = next(vm for vm in self.manager.list_nodes(self.cloud_service_name) if vm.id == vm_id)
            # XXX: libcloud seems doesn't map size properly
            vm.size = self.get_size(vm.extra['instance_size'])
            return vm
        except (StopIteration, LibcloudError) as e:
            six.reraise(AzureBackendError, e.message or "Virtual machine doesn't exist")

    def get_size(self, size_id):
        try:
            return next(s for s in self.manager.list_sizes() if s.id == size_id)
        except (StopIteration, LibcloudError) as e:
            reraise(e)

    def get_image(self, image_id):
        try:
            return next(s for s in self.manager.list_images() if s.id == image_id)
        except (StopIteration, LibcloudError) as e:
            six.reraise(AzureBackendError, e.message or "Image doesn't exist")

    def get_storage(self, storage_name):
        try:
            return next(s for s in self.manager.ex_list_storage_services() if s.service_name == storage_name)
        except (StopIteration, LibcloudError) as e:
            six.reraise(AzureBackendError, e.message or "Storage doesn't exist")

    def get_storage_name(self, cloud_service_name=None):
        if not cloud_service_name:
            cloud_service_name = self.cloud_service_name
        # Storage account name must be between 3 and 24 characters in length
        # and use numbers and lower-case letters only
        return re.sub(r'[\W_-]+', '', cloud_service_name.lower())[:24]

    def get_resources_for_import(self):
        if not self.cloud_service_name:
            raise AzureBackendError(
                "Resources could be fetched only for specific cloud service, "
                "please supply project_uuid query argument")

        cur_vms = models.VirtualMachine.objects.all().values_list('backend_id', flat=True)
        try:
            vms = self.manager.list_nodes(self.cloud_service_name)
        except LibcloudError as e:
            reraise(e)

        return [{
            'id': vm.id,
            'name': vm.name,
            'flavor_name': vm.extra.get('instance_size')
        } for vm in vms if vm.id not in cur_vms]

    def get_managed_resources(self):
        try:
            ids = []
            services = self.manager.ex_list_cloud_services()
            for service in services:
                for node in self.manager.list_nodes(service.service_name):
                    ids.append(node.id)
        except LibcloudError:
            return []
        return models.VirtualMachine.objects.filter(backend_id__in=ids)
