import logging
import ssl
from urllib.parse import urlencode

from django.utils import timezone
from django.utils.functional import cached_property
import pyVim.task
import pyVim.connect
from pyVmomi import vim

from waldur_core.structure import ServiceBackend, ServiceBackendError, log_backend_action
from waldur_core.structure.utils import update_pulled_fields
from waldur_mastermind.common.utils import parse_datetime
from waldur_vmware.client import VMwareClient
from waldur_vmware.exceptions import VMwareError
from waldur_vmware.utils import is_basic_mode

from . import models, signals

logger = logging.getLogger(__name__)


class VMwareBackendError(ServiceBackendError):
    pass


class VMwareBackend(ServiceBackend):
    def __init__(self, settings):
        """
        :type settings: :class:`waldur_core.structure.models.ServiceSettings`
        """
        self.settings = settings

    @cached_property
    def host(self):
        return self.settings.backend_url.split('https://')[-1].split('http://')[-1].strip('/')

    @cached_property
    def client(self):
        """
        Construct VMware REST API client using credentials specified in the service settings.
        """
        client = VMwareClient(self.host, verify_ssl=False)
        client.login(self.settings.username, self.settings.password)
        return client

    @cached_property
    def soap_client(self):
        """
        Construct VMware SOAP API client using credentials specified in the service settings.
        """
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        context.verify_mode = ssl.CERT_NONE
        return pyVim.connect.SmartConnect(
            host=self.host,
            user=self.settings.username,
            pwd=self.settings.password,
            port=443,
            sslContext=context
        )

    def ping(self, raise_exception=False):
        """
        Check if backend is ok.
        """
        try:
            self.client.list_vms()
        except VMwareError as e:
            if raise_exception:
                raise VMwareBackendError(e)
            return False
        else:
            return True

    def pull_service_properties(self):
        self.pull_folders()
        self.pull_templates()
        self.pull_clusters()
        self.pull_networks()
        self.pull_datastores()

    def pull_templates(self):
        """
        Pull VMware templates for virtual machine provisioning from content library
        using VMware REST API to the local database.
        """
        try:
            backend_templates = self.client.list_all_templates()
        except VMwareError as e:
            raise VMwareBackendError(e)

        if is_basic_mode():
            # If basic mode is enabled, we should filter out templates which have more than 1 NIC
            backend_templates = [
                template for template in backend_templates
                if len(template['template']['nics']) == 1
            ]

        backend_templates_map = {
            item['library_item']['id']: item
            for item in backend_templates
        }

        frontend_templates_map = {
            p.backend_id: p
            for p in models.Template.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_templates_map.keys()) - set(backend_templates_map.keys())
        new_ids = set(backend_templates_map.keys()) - set(frontend_templates_map.keys())
        common_ids = set(backend_templates_map.keys()) & set(frontend_templates_map.keys())

        for library_item_id in new_ids:
            template = self._backend_template_to_template(backend_templates_map[library_item_id])
            template.save()

        for library_item_id in common_ids:
            backend_template = self._backend_template_to_template(
                backend_templates_map[library_item_id])
            frontend_template = frontend_templates_map[library_item_id]
            fields = (
                'cores',
                'cores_per_socket',
                'ram',
                'disk',
                'guest_os',
                'modified',
                'description'
            )
            update_pulled_fields(frontend_template, backend_template, fields)

        models.Template.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def _backend_template_to_template(self, backend_template):
        library_item = backend_template['library_item']
        template = backend_template['template']
        total_disk = self._get_total_disk(template['disks'])
        return models.Template(
            settings=self.settings,
            backend_id=library_item['id'],
            name=library_item['name'],
            description=library_item['description'],
            created=parse_datetime(library_item['creation_time']),
            modified=parse_datetime(library_item['last_modified_time']),
            cores=template['cpu']['count'],
            cores_per_socket=template['cpu']['cores_per_socket'],
            ram=template['memory']['size_MiB'],
            disk=total_disk,
            guest_os=template['guest_OS'],
        )

    def _get_total_disk(self, backend_disks):
        # Convert disk size from bytes to MiB
        return sum([disk['value']['capacity'] / 1024 / 1024 for disk in backend_disks])

    @log_backend_action()
    def pull_virtual_machine(self, vm, update_fields=None):
        """
        Pull virtual machine from REST API and update its information in local database.

        :param vm: Virtual machine database object.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        :param update_fields: iterable of fields to be updated
        """
        import_time = timezone.now()
        imported_vm = self.import_virtual_machine(vm.backend_id, save=False)

        vm.refresh_from_db()
        if vm.modified < import_time:
            if not update_fields:
                update_fields = models.VirtualMachine.get_backend_fields()

            update_pulled_fields(vm, imported_vm, update_fields)

    def import_virtual_machine(self, backend_id, save=True, service_project_link=None):
        """
        Import virtual machine by its ID.

        :param backend_id: Virtual machine identifier
        :type backend_id: str
        :param save: Save object in the database
        :type save: bool
        :param service_project_link: Optional service project link model object
        :rtype: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            backend_vm = self.client.get_vm(backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

        tools_installed = self.get_vm_tools_installed(backend_id)
        tools_state = self.get_vm_tools_state(backend_id)

        vm = self._backend_vm_to_vm(backend_vm, tools_installed, tools_state, backend_id)
        if service_project_link is not None:
            vm.service_project_link = service_project_link
        if save:
            vm.save()

        return vm

    def _backend_vm_to_vm(self, backend_vm, tools_installed, tools_state, backend_id):
        """
        Build database model object for virtual machine from REST API spec.

        :param backend_vm: virtual machine specification
        :type backend_vm: dict
        :param tools_installed: whether VMware tools installed or not
        :type tools_installed: bool
        :param tools_state: Status of VMware Tools.
        :type tools_state: str
        :param backend_id: Virtual machine identifier
        :type backend_id: str
        :rtype: :class:`waldur_vmware.models.VirtualMachine`
        """
        return models.VirtualMachine(
            backend_id=backend_id,
            name=backend_vm['name'],
            state=models.VirtualMachine.States.OK,
            runtime_state=backend_vm['power_state'],
            cores=backend_vm['cpu']['count'],
            cores_per_socket=backend_vm['cpu']['cores_per_socket'],
            ram=backend_vm['memory']['size_MiB'],
            disk=self._get_total_disk(backend_vm['disks']),
            tools_installed=tools_installed,
            tools_state=tools_state,
        )

    def pull_clusters(self):
        try:
            backend_clusters = self.client.list_clusters()
        except VMwareError as e:
            raise VMwareBackendError(e)

        backend_clusters_map = {
            item['cluster']: item
            for item in backend_clusters
        }

        frontend_clusters_map = {
            p.backend_id: p
            for p in models.Cluster.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_clusters_map.keys()) - set(backend_clusters_map.keys())
        new_ids = set(backend_clusters_map.keys()) - set(frontend_clusters_map.keys())
        common_ids = set(backend_clusters_map.keys()) & set(frontend_clusters_map.keys())

        for item_id in common_ids:
            backend_item = backend_clusters_map[item_id]
            frontend_item = frontend_clusters_map[item_id]
            if frontend_item.name != backend_item['name']:
                frontend_item.name = backend_item['name']
                frontend_item.save(update_fields=['name'])

        for item_id in new_ids:
            item = backend_clusters_map[item_id]
            models.Cluster.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item['name'],
            )

        models.Cluster.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def pull_networks(self):
        try:
            backend_networks = self.client.list_networks()
        except VMwareError as e:
            raise VMwareBackendError(e)

        backend_networks_map = {
            item['network']: item
            for item in backend_networks
        }

        frontend_networks_map = {
            p.backend_id: p
            for p in models.Network.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_networks_map.keys()) - set(backend_networks_map.keys())
        new_ids = set(backend_networks_map.keys()) - set(frontend_networks_map.keys())
        common_ids = set(frontend_networks_map.keys()) & set(backend_networks_map.keys())

        for item_id in common_ids:
            backend_item = backend_networks_map[item_id]
            frontend_item = frontend_networks_map[item_id]
            if frontend_item.name != backend_item['name']:
                frontend_item.name = backend_item['name']
                frontend_item.save(update_fields=['name'])

        for item_id in new_ids:
            item = backend_networks_map[item_id]
            models.Network.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item['name'],
                type=item['type'],
            )

        models.Network.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def pull_datastores(self):
        try:
            backend_datastores = self.client.list_datastores()
        except VMwareError as e:
            raise VMwareBackendError(e)

        backend_datastores_map = {
            item['datastore']: item
            for item in backend_datastores
        }

        frontend_datastores_map = {
            p.backend_id: p
            for p in models.Datastore.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_datastores_map.keys()) - set(backend_datastores_map.keys())
        new_ids = set(backend_datastores_map.keys()) - set(frontend_datastores_map.keys())
        common_ids = set(backend_datastores_map.keys()) & set(frontend_datastores_map.keys())

        for item_id in new_ids:
            datastore = self._backend_datastore_to_datastore(backend_datastores_map[item_id])
            datastore.save()

        for item_id in common_ids:
            backend_datastore = self._backend_datastore_to_datastore(backend_datastores_map[item_id])
            frontend_datastore = frontend_datastores_map[item_id]
            fields = ('name', 'capacity', 'free_space')
            update_pulled_fields(frontend_datastore, backend_datastore, fields)

        models.Datastore.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def _backend_datastore_to_datastore(self, backend_datastore):
        capacity = backend_datastore.get('capacity')
        # Convert from bytes to MB
        if capacity:
            capacity /= 1024 * 1024

        free_space = backend_datastore.get('free_space')
        # Convert from bytes to MB
        if free_space:
            free_space /= 1024 * 1024

        return models.Datastore(
            settings=self.settings,
            backend_id=backend_datastore['datastore'],
            name=backend_datastore['name'],
            type=backend_datastore['type'],
            capacity=capacity,
            free_space=free_space,
        )

    def get_vm_folders(self):
        try:
            return self.client.list_folders(folder_type='VIRTUAL_MACHINE')
        except VMwareError as e:
            raise VMwareBackendError(e)

    def get_default_vm_folder(self):
        """
        Currently VM folder is required for VM provisioning either from template or from scratch.
        Therefore when folder is not specified for VM, we should use first available folder.
        Please note that it is assumed that there's only one datacenter in this case.
        :return: Virtual machine folder identifier.
        :rtype: str
        """
        return self.get_vm_folders()[0]['folder']

    def get_default_resource_pool(self):
        """
        Currently resource pool is required for VM provisioning from scratch if cluster is not specified.
        Therefore we should use first available resource pool.
        Please note that it is assumed that there's only one datacenter in this case.
        :return: Resource pool identifier.
        :rtype: str
        """
        try:
            return self.client.list_resource_pools()[0]['resource_pool']
        except VMwareError as e:
            raise VMwareBackendError(e)

    def get_default_datastore(self):
        """
        Currently datastore is required for VM provisioning either from template or from scratch.
        Therefore when datastore is not specified for VM, we should use first available datastore.
        Please note that it is assumed that there's only one datacenter in this case.
        :return: Datastore identifier.
        :rtype: str
        """
        try:
            return self.client.list_datastores()[0]['datastore']
        except VMwareError as e:
            raise VMwareBackendError(e)

    def pull_folders(self):
        backend_folders = self.get_vm_folders()

        backend_folders_map = {
            item['folder']: item
            for item in backend_folders
        }

        frontend_folders_map = {
            p.backend_id: p
            for p in models.Folder.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_folders_map.keys()) - set(backend_folders_map.keys())
        new_ids = set(backend_folders_map.keys()) - set(frontend_folders_map.keys())
        common_ids = set(backend_folders_map.keys()) & set(frontend_folders_map.keys())

        for item_id in common_ids:
            backend_item = backend_folders_map[item_id]
            frontend_item = frontend_folders_map[item_id]
            if frontend_item.name != backend_item['name']:
                frontend_item.name = backend_item['name']
                frontend_item.save(update_fields=['name'])

        for item_id in new_ids:
            item = backend_folders_map[item_id]
            models.Folder.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item['name'],
            )

        models.Folder.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def create_virtual_machine(self, vm):
        """
        Creates a virtual machine.

        :param vm: Virtual machine to be created
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        if vm.template:
            backend_id = self.create_virtual_machine_from_template(vm)
        else:
            backend_id = self.create_virtual_machine_from_scratch(vm)

        try:
            backend_vm = self.client.get_vm(backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

        vm.backend_id = backend_id
        vm.runtime_state = backend_vm['power_state']
        vm.save(update_fields=['backend_id', 'runtime_state'])

        for disk in backend_vm['disks']:
            disk = self._backend_disk_to_disk(disk['value'], disk['key'])
            disk.vm = vm
            disk.service_project_link = vm.service_project_link
            disk.save()

        # If virtual machine is not deployed from template, it does not have any networks.
        # Therefore we should create network interfaces manually according to VM spec.
        if not vm.template:
            for network in vm.networks.all():
                try:
                    self.client.create_nic(vm.backend_id, network.backend_id)
                except VMwareError as e:
                    raise VMwareBackendError(e)

        signals.vm_created.send(self.__class__, vm=vm)
        return vm

    def _get_vm_placement(self, vm):
        placement = {}

        if vm.folder:
            placement['folder'] = vm.folder.backend_id
        else:
            logger.warning('Folder is not specified for VM with ID: %s. '
                           'Trying to assign default folder.', vm.id)
            placement['folder'] = self.get_default_vm_folder()

        if vm.cluster:
            placement['cluster'] = vm.cluster.backend_id
        else:
            logger.warning('Cluster is not specified for VM with ID: %s. '
                           'Trying to assign default resource pool.', vm.id)
            placement['resource_pool'] = self.get_default_resource_pool()

        return placement

    def _get_template_nics(self, template):
        """
        Fetch list of NIC IDs assigned to virtual machine template.

        :param template: Virtual machine template.
        :type template: :class:`waldur_vmware.models.Template`
        :rtype: list[str]
        """

        try:
            backend_template = self.client.get_template_library_item(template.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)
        else:
            return [nic['key'] for nic in backend_template['nics']]

    def _get_vm_nics(self, vm):
        """
        Serialize map of Ethernet network adapters for virtual machine template deployment.

        :param vm: Virtual machine to be created.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        :return: list[dict]
        """

        nics = self._get_template_nics(vm.template)
        networks = list(vm.networks.all())

        if is_basic_mode():
            if len(networks) != 1:
                logger.warning('Skipping network assignment because VM does not have '
                               'exactly one network in basic mode. VM ID: %s', vm.id)
                return
            elif len(nics) != 1:
                logger.warning('Skipping network assignment because related template does '
                               'not have exactly one NIC in basic mode. VM ID: %s', vm.id)

        if len(networks) != len(nics):
            logger.warning('It is not safe to update network assignment when '
                           'number of interfaces and networks do not match. VM ID: %s', vm.id)

        return [
            {
                'key': nic,
                'value': {
                    'network': network.backend_id
                }
            }
            for (nic, network) in zip(nics, networks)
        ]

    def create_virtual_machine_from_template(self, vm):
        spec = {
            'name': vm.name,
            'description': vm.description,
            'hardware_customization': {
                'cpu_update': {
                    'num_cpus': vm.cores,
                    'num_cores_per_socket': vm.cores_per_socket,
                },
                'memory_update': {
                    'memory': vm.ram,
                },
            },
            'placement': self._get_vm_placement(vm),
        }

        if vm.datastore:
            spec['disk_storage'] = {'datastore': vm.datastore.backend_id}
            spec['vm_home_storage'] = {'datastore': vm.datastore.backend_id}

        nics = self._get_vm_nics(vm)
        if nics:
            spec['hardware_customization']['nics'] = nics

        try:
            return self.client.deploy_vm_from_template(vm.template.backend_id, spec)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def create_virtual_machine_from_scratch(self, vm):
        spec = {
            'name': vm.name,
            'guest_OS': vm.guest_os,
            'cpu': {
                'count': vm.cores,
                'cores_per_socket': vm.cores_per_socket,
                'hot_add_enabled': True,
                'hot_remove_enabled': True
            },
            'memory': {
                'size_MiB': vm.ram,
                'hot_add_enabled': True,
            },
            'placement': self._get_vm_placement(vm),
        }

        if vm.datastore:
            spec['placement']['datastore'] = vm.datastore.backend_id
        else:
            spec['placement']['datastore'] = self.get_default_datastore()

        try:
            return self.client.create_vm(spec)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def delete_virtual_machine(self, vm):
        """
        Deletes a virtual machine.

        :param vm: Virtual machine to be deleted
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.delete_vm(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def start_virtual_machine(self, vm):
        """
        Powers on a powered-off or suspended virtual machine.

        :param vm: Virtual machine to be started
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.start_vm(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def stop_virtual_machine(self, vm):
        """
        Powers off a powered-on or suspended virtual machine.

        :param vm: Virtual machine to be stopped
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.stop_vm(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def reset_virtual_machine(self, vm):
        """
        Resets a powered-on virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.reset_vm(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def suspend_virtual_machine(self, vm):
        """
        Suspends a powered-on virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.suspend_vm(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def shutdown_guest(self, vm):
        """
        Issues a request to the guest operating system asking
        it to perform a clean shutdown of all services.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.shutdown_guest(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def reboot_guest(self, vm):
        """
        Issues a request to the guest operating system asking it to perform a reboot.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.reboot_guest(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def is_virtual_machine_shutted_down(self, vm):
        try:
            guest_power = self.client.get_guest_power(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)
        else:
            return guest_power['state'] == models.VirtualMachine.GuestPowerStates.NOT_RUNNING

    def is_virtual_machine_tools_running(self, vm):
        """
        Check VMware tools status and update cache only if its running.
        If VMware tools are not running, state is not updated.
        It is needed in order to skip extra database updates.
        Otherwise VMware tools state in database would be updated
        from RUNNING to NOT RUNNING twice when optimistic update is used.
        """
        tools_state = self.get_vm_tools_state(vm.backend_id)
        result = tools_state == models.VirtualMachine.ToolsStates.RUNNING
        if result:
            vm.tools_state = tools_state
            vm.save(update_fields=['tools_state'])
        self.pull_virtual_machine_runtime_state(vm)
        return result

    def pull_virtual_machine_runtime_state(self, vm):
        try:
            backend_vm = self.client.get_vm(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)
        else:
            backend_power_state = backend_vm['power_state']
            if backend_power_state != vm.runtime_state:
                vm.runtime_state = backend_power_state
                vm.save(update_fields=['runtime_state'])

    def is_virtual_machine_tools_not_running(self, vm):
        tools_state = self.get_vm_tools_state(vm.backend_id)
        result = tools_state == models.VirtualMachine.ToolsStates.NOT_RUNNING
        if result:
            vm.tools_state = tools_state
            vm.save(update_fields=['tools_state'])
        return result

    def update_virtual_machine(self, vm):
        """
        Updates CPU and RAM of virtual machine.
        """
        self.update_cpu(vm)
        self.update_memory(vm)
        signals.vm_updated.send(self.__class__, vm=vm)

    def update_cpu(self, vm):
        """
        Updates CPU of virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            cpu_spec = self.client.get_cpu(vm.backend_id)
            if cpu_spec['cores_per_socket'] != vm.cores_per_socket or cpu_spec['count'] != vm.cores:
                self.client.update_cpu(vm.backend_id, {
                    'cores_per_socket': vm.cores_per_socket,
                    'count': vm.cores,
                })
        except VMwareError as e:
            raise VMwareBackendError(e)

    def update_memory(self, vm):
        """
        Updates RAM of virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            memory_spec = self.client.get_memory(vm.backend_id)
            if memory_spec['size_MiB'] != vm.ram:
                self.client.update_memory(vm.backend_id, {
                    'size_MiB': vm.ram
                })
        except VMwareError as e:
            raise VMwareBackendError(e)

    def create_port(self, port):
        """
        Creates an Ethernet port for given VM and network.

        :param port: Port to be created
        :type port: :class:`waldur_vmware.models.Port`
        """
        try:
            backend_id = self.client.create_nic(port.vm.backend_id, port.network.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)
        else:
            port.backend_id = backend_id
            port.save(update_fields=['backend_id'])
            return port

    def delete_port(self, port):
        """
        Deletes an Ethernet port.

        :param port: Port to be deleted.
        :type port: :class:`waldur_vmware.models.Port`
        """
        try:
            self.client.delete_nic(port.vm.backend_id, port.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

    @log_backend_action()
    def pull_port(self, port, update_fields=None):
        """
        Pull Ethernet port from REST API and update its information in local database.

        :param port: Port to be updated.
        :type port: :class:`waldur_vmware.models.Port`
        :param update_fields: iterable of fields to be updated
        :return: None
        """
        import_time = timezone.now()
        imported_port = self.import_port(port.vm.backend_id, port.backend_id, save=False)

        port.refresh_from_db()
        if port.modified < import_time:
            if not update_fields:
                update_fields = models.Port.get_backend_fields()

            update_pulled_fields(port, imported_port, update_fields)

    def import_port(self, backend_vm_id, backend_port_id, save=True, service_project_link=None):
        """
        Import Ethernet port by its ID.

        :param backend_vm_id: Virtual machine identifier
        :type backend_vm_id: str
        :param backend_port_id: Ethernet port identifier
        :type backend_port_id: str
        :param save: Save object in the database
        :type save: bool
        :param service_project_link: Service project link model object
        :rtype: :class:`waldur_vmware.models.Disk`
        """
        try:
            backend_port = self.client.get_nic(backend_vm_id, backend_port_id)
            backend_port['nic'] = backend_port_id
        except VMwareError as e:
            raise VMwareBackendError(e)

        port = self._backend_port_to_port(backend_port)
        if service_project_link is not None:
            port.service_project_link = service_project_link
        if save:
            port.save()

        return port

    def _backend_port_to_port(self, backend_port):
        """
        Build database model object for Ethernet port from REST API spec.

        :param backend_port: Ethernet port specification
        :type backend_port: dict
        :rtype: :class:`waldur_vmware.models.Port`
        """
        return models.Port(
            backend_id=backend_port['nic'],
            name=backend_port['label'],
            # MAC address is optional
            mac_address=backend_port.get('mac_address'),
            state=models.Port.States.OK,
            runtime_state=backend_port['state'],
        )

    def pull_vm_ports(self, vm):
        try:
            backend_ports = self.client.list_nics(vm.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

        backend_ports_map = {
            item['nic']: item
            for item in backend_ports
        }

        frontend_ports_map = {
            p.backend_id: p
            for p in models.Port.objects.filter(vm=vm)
        }

        networks_map = {
            p.backend_id: p
            for p in models.Network.objects.filter(settings=vm.service_settings)
        }

        stale_ids = set(frontend_ports_map.keys()) - set(backend_ports_map.keys())
        new_ids = set(backend_ports_map.keys()) - set(frontend_ports_map.keys())
        common_ids = set(backend_ports_map.keys()) & set(frontend_ports_map.keys())

        for item_id in new_ids:
            backend_port = backend_ports_map[item_id]
            port = self._backend_port_to_port(backend_port)
            port.service_project_link = vm.service_project_link
            network_id = backend_port['backing']['network']
            port.network = networks_map.get(network_id)
            port.vm = vm
            port.save()

        for item_id in common_ids:
            backend_port = self._backend_port_to_port(backend_ports_map[item_id])
            frontend_port = frontend_ports_map[item_id]
            fields = ('mac_address', 'runtime_state')
            update_pulled_fields(frontend_port, backend_port, fields)

        models.Port.objects.filter(vm=vm, backend_id__in=stale_ids).delete()

    def create_disk(self, disk):
        """
        Creates a virtual disk.

        :param disk: Virtual disk to be created
        :type disk: :class:`waldur_vmware.models.Disk`
        """
        spec = {
            'new_vmdk': {
                # Convert from mebibytes to bytes because VMDK is specified in bytes
                'capacity': 1024 * 1024 * disk.size,
            }
        }

        try:
            backend_id = self.client.create_disk(disk.vm.backend_id, spec)
        except VMwareError as e:
            raise VMwareBackendError(e)
        else:
            disk.backend_id = backend_id
            disk.save(update_fields=['backend_id'])
            signals.vm_updated.send(self.__class__, vm=disk.vm)
            return disk

    def delete_disk(self, disk, delete_vmdk=True):
        """
        Deletes a virtual disk.

        :param disk: Virtual disk to be deleted
        :type disk: :class:`waldur_vmware.models.Disk`
        :param delete_vmdk: Delete backing VMDK file.
        """
        backend_disk = self.get_backend_disk(disk)

        try:
            self.client.delete_disk(disk.vm.backend_id, disk.backend_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

        if delete_vmdk:
            vdm = self.soap_client.content.virtualDiskManager
            task = vdm.DeleteVirtualDisk(
                name=backend_disk.backing.fileName,
                datacenter=self.get_disk_datacenter(backend_disk),
            )
            try:
                pyVim.task.WaitForTask(task)
            except Exception:
                logger.exception('Unable to delete VMware disk. Disk ID: %s.', disk.id)
                raise VMwareBackendError('Unknown error.')
            signals.vm_updated.send(self.__class__, vm=disk.vm)

    def extend_disk(self, disk):
        """
        Increase disk capacity.

        :param disk: Virtual disk to be extended.
        :type disk: :class:`waldur_vmware.models.Disk`
        """
        backend_vm = self.get_backend_vm(disk.vm)
        backend_disk = self.get_backend_disk(disk)

        virtual_disk_spec = vim.vm.device.VirtualDeviceSpec()
        virtual_disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        virtual_disk_spec.device = backend_disk
        virtual_disk_spec.device.capacityInKB = disk.size * 1024
        virtual_disk_spec.device.capacityInBytes = disk.size * 1024 * 1024

        spec = vim.vm.ConfigSpec()
        spec.deviceChange = [virtual_disk_spec]
        task = backend_vm.ReconfigVM_Task(spec=spec)

        try:
            pyVim.task.WaitForTask(task)
        except Exception:
            logger.exception('Unable to extend VMware disk. Disk ID: %s.', disk.id)
            raise VMwareBackendError('Unknown error.')
        signals.vm_updated.send(self.__class__, vm=disk.vm)

    def get_object(self, vim_type, vim_id):
        """
        Get object by type and ID from SOAP client.
        """
        content = self.soap_client.content
        try:
            items = [item for item in content.viewManager.CreateContainerView(
                content.rootFolder, [vim_type], recursive=True
            ).view]
        except Exception:
            logger.exception('Unable to get VMware object. Type: %s, ID: %s.', vim_type, vim_id)
            raise VMwareBackendError('Unknown error.')
        for item in items:
            if item._moId == vim_id:
                return item

    def get_backend_vm(self, vm):
        """
        Get virtual machine object from SOAP client.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        :rtype: :class:`pyVmomi.VmomiSupport.vim.VirtualMachine`
        """
        return self._get_backend_vm(vm.backend_id)

    def get_vm_tools_state(self, backend_id):
        """
        Get running status of VMware Tools.

        :param backend_id: Virtual machine identifier.
        :type backend_id: str
        :rtype: str
        """
        backend_vm = self._get_backend_vm(backend_id)
        backend_tools_state = backend_vm.guest.toolsRunningStatus
        if backend_tools_state == 'guestToolsExecutingScripts':
            return models.VirtualMachine.ToolsStates.STARTING
        elif backend_tools_state == 'guestToolsNotRunning':
            return models.VirtualMachine.ToolsStates.NOT_RUNNING
        elif backend_tools_state == 'guestToolsRunning':
            return models.VirtualMachine.ToolsStates.RUNNING

    def get_vm_tools_installed(self, backend_id):
        """
        Check if VMware Tools are installed.

        :param backend_id: Virtual machine identifier.
        :type backend_id: str
        :rtype: bool
        """
        backend_vm = self._get_backend_vm(backend_id)
        return backend_vm.config.tools.toolsInstallType != 'guestToolsTypeUnknown'

    def _get_backend_vm(self, backend_id):
        return self.get_object(vim.VirtualMachine, backend_id)

    def get_backend_disk(self, disk):
        """
        Get virtual disk object from SOAP client.

        :param disk: Virtual disk.
        :type disk: :class:`waldur_vmware.models.Disk`
        :rtype: :class:`pyVmomi.VmomiSupport.vim.vm.device.VirtualDisk`
        """
        backend_vm = self.get_backend_vm(disk.vm)
        for device in backend_vm.config.hardware.device:
            if isinstance(device, vim.VirtualDisk) and str(device.key) == disk.backend_id:
                return device

    def get_disk_datacenter(self, backend_disk):
        """
        Find the datacenter where virtual disk is located.

        :param backend_disk: Virtual disk object returned by SOAP API.
        :type backend_disk: :class:`pyVmomi.VmomiSupport.vim.vm.device.VirtualDisk`
        :return: VMware datacenter where disk is located.
        :rtype: :class:`pyVmomi.VmomiSupport.vim.Datacenter`
        """
        parent = backend_disk.backing.datastore.parent
        while parent and not isinstance(parent, vim.Datacenter):
            parent = parent.parent
        return parent

    @log_backend_action()
    def pull_disk(self, disk, update_fields=None):
        """
        Pull virtual disk from REST API and update its information in local database.

        :param disk: Virtual disk database object.
        :type disk: :class:`waldur_vmware.models.Disk`
        :param update_fields: iterable of fields to be updated
        :return: None
        """
        import_time = timezone.now()
        imported_disk = self.import_disk(disk.vm.backend_id, disk.backend_id, save=False)

        disk.refresh_from_db()
        if disk.modified < import_time:
            if not update_fields:
                update_fields = models.Disk.get_backend_fields()

            update_pulled_fields(disk, imported_disk, update_fields)

    def import_disk(self, backend_vm_id, backend_disk_id, save=True, service_project_link=None):
        """
        Import virtual disk by its ID.

        :param backend_vm_id: Virtual machine identifier
        :type backend_vm_id: str
        :param backend_disk_id: Virtual disk identifier
        :type backend_disk_id: str
        :param save: Save object in the database
        :type save: bool
        :param service_project_link: Service project link model object
        :rtype: :class:`waldur_vmware.models.Disk`
        """
        try:
            backend_disk = self.client.get_disk(backend_vm_id, backend_disk_id)
        except VMwareError as e:
            raise VMwareBackendError(e)

        disk = self._backend_disk_to_disk(backend_disk, backend_disk_id)
        if service_project_link is not None:
            disk.service_project_link = service_project_link
        if save:
            disk.save()

        return disk

    def _backend_disk_to_disk(self, backend_disk, backend_disk_id):
        """
        Build database model object for virtual disk from REST API spec.

        :param backend_disk: virtual disk specification
        :type backend_disk: dict
        :param backend_disk_id: Virtual disk identifier
        :type backend_disk_id: str
        :rtype: :class:`waldur_vmware.models.Disk`
        """
        return models.Disk(
            backend_id=backend_disk_id,
            name=backend_disk['label'],
            # Convert disk size from bytes to MiB
            size=backend_disk['capacity'] / 1024 / 1024,
            state=models.Disk.States.OK,
        )

    def get_console_url(self, vm):
        """
        Generates a virtual machine's remote console URL (VMRC)

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        ticket = self.soap_client.content.sessionManager.AcquireCloneTicket()
        return 'vmrc://clone:{ticket}@{host}/?moid={vm}'.format(
            ticket=ticket, host=self.host, vm=vm.backend_id)

    def get_web_console_url(self, vm):
        """
        Generates a virtual machine's web console URL (WMKS)

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        backend_vm = self.get_backend_vm(vm)
        ticket = backend_vm.AcquireMksTicket()
        params = {
            'host': ticket.host,
            'port': ticket.port,
            'ticket': ticket.ticket,
            'cfgFile': ticket.cfgFile,
            'thumbprint': ticket.sslThumbprint,
            'vmId': vm.backend_id,
            'encoding': 'UTF-8'
        }
        return 'wss://{host}/ui/webconsole/authd?{params}'.format(
            host=ticket.host,
            params=urlencode(params)
        )
