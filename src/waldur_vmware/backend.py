import contextlib
import logging
import ssl
from urllib.parse import urlencode

from django.utils import timezone
from django.utils.functional import cached_property

from waldur_core.core.enums import CoreStates
from waldur_core.structure.backend import ServiceBackend, log_backend_action
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_core.structure.utils import update_pulled_fields
from waldur_mastermind.common.utils import parse_datetime
from waldur_vmware import sessions, vim_utils
from waldur_vmware.client import VMwareClient
from waldur_vmware.exceptions import VMwareError
from waldur_vmware.utils import is_basic_mode

from . import models, signals

logger = logging.getLogger(__name__)

# pyVmomi / pyVim (~13 MB resident at import) are imported lazily inside the
# methods that use them, so they are not loaded at Django startup for deployments
# that never use VMware. See CLAUDE.md, "Lazy imports for heavy optional
# backends". vim_utils follows the same rule, so importing it here is cheap.

# Everything the plugin needs to know about a VM, fetched in one PropertyCollector
# round trip. Reading a whole property (`runtime` rather than `runtime.powerState`)
# raises AttributeError on both pinned and current pyVmomi — see vim_utils.
VM_PROPERTIES = (
    "name",
    "runtime.powerState",
    "config.hardware.numCPU",
    "config.hardware.numCoresPerSocket",
    "config.hardware.memoryMB",
    "config.hardware.device",
    "config.tools.toolsInstallType",
    "guest.toolsRunningStatus",
    "guest.guestState",
)


# The subset of VM_PROPERTIES that _backend_vm_to_vm indexes directly. vCenter
# leaves a property out of its answer for an inaccessible or orphaned VM, and a
# bare KeyError from a pull task is not caught by BackgroundPullTask — only
# ServiceBackendError is. See vim_utils._check_answered.
VM_REQUIRED_PROPERTIES = (
    "name",
    "runtime.powerState",
    "config.hardware.numCPU",
    "config.hardware.numCoresPerSocket",
    "config.hardware.memoryMB",
)

# Values an operator may have typed into the options dict for a boolean.
TRUE_OPTION_VALUES = ("true", "yes", "1", "on")

# Service settings already warned about disabled TLS verification. The warning
# describes a configuration rather than an event, and a backend is constructed
# fresh for every task, so without this it would repeat on every pull of every
# resource.
_tls_warning_emitted = set()


class VMwareBackendError(ServiceBackendError):
    pass


class VMwareBackend(ServiceBackend):
    def __init__(self, settings):
        """
        :type settings: :class:`waldur_core.structure.models.ServiceSettings`
        """
        self.settings = settings

    @cached_property
    def netloc(self):
        """Host and, when the URL carries one, port of the vCenter server."""
        return (
            self.settings.backend_url.split("https://")[-1]
            .split("http://")[-1]
            .strip("/")
        )

    @cached_property
    def host(self):
        return self.netloc.split(":")[0]

    @cached_property
    def port(self):
        """
        Port of the vCenter SOAP endpoint.

        SmartConnect takes host and port separately, so a port given in
        backend_url has to be split out rather than passed along in the host.
        Defaults to 443, which is where vCenter serves the SDK.
        """
        _, _, port = self.netloc.partition(":")
        return int(port) if port else 443

    @cached_property
    def verify_ssl(self):
        """
        Whether to validate vCenter's TLS certificate.

        Defaults to off, which is what this plugin has always done. Operators who
        run vCenter with a trusted certificate can turn it on per service
        settings; the warning below is there so that a deployment relying on the
        default can find out from its logs. It describes a configuration rather
        than an event, so it is emitted once per settings object instead of on
        every pull of every resource.
        """
        value = self.settings.options.get("verify_ssl", False)
        if isinstance(value, str):
            # ServiceSettingsSerializer.options is a plain DictField, so options
            # edited directly can hold the string "false" — which is truthy, and
            # would silently turn verification on for a deployment that asked
            # for it off. The integer options next to it use get_int_or_none for
            # the same reason.
            verify_ssl = value.strip().lower() in TRUE_OPTION_VALUES
        else:
            verify_ssl = bool(value)

        if not verify_ssl and self.settings.pk not in _tls_warning_emitted:
            _tls_warning_emitted.add(self.settings.pk)
            logger.warning(
                "TLS certificate verification is disabled for VMware service "
                "settings %s. Set the verify_ssl option to enable it.",
                self.settings,
            )
        return verify_ssl

    @cached_property
    def credentials_fingerprint(self):
        """Digest of everything a connection to this vCenter is opened with.

        Sessions are cached per service settings for the life of the process, so
        an operator editing the URL, the credentials or the TLS setting has to
        invalidate the connection opened with the previous ones. See
        :mod:`waldur_vmware.sessions`.
        """
        return sessions.credentials_fingerprint(
            self.netloc,
            self.settings.username,
            self.settings.password,
            self.verify_ssl,
        )

    @property
    def client(self):
        """
        Return a VMware REST API client for the Content Library.

        The Content Library has no vim25 (SOAP) equivalent — it is REST-only by
        design — so template discovery and deployment stay on this client while
        the rest of the plugin talks to vCenter over SOAP. See
        :mod:`waldur_vmware.client`.

        Cached per process rather than per backend: a backend is built for every
        Celery task, and ping() reaches this endpoint on every one of them.
        """
        return self._rest_client()

    def _rest_client(self, force_check=False):
        return sessions.rest_sessions.acquire(
            self.settings.pk,
            self.credentials_fingerprint,
            self._connect_rest,
            force_check=force_check,
        )

    def _connect_rest(self):
        client = VMwareClient(self.netloc, verify_ssl=self.verify_ssl)
        client.login(self.settings.username, self.settings.password)
        return client

    @property
    def soap_client(self):
        """
        Return a VMware SOAP API client for the vCenter of these service settings.

        The session behind it is shared by every backend built from the same
        settings in this process, and stays open between tasks: a vCenter session
        is server-side state, a backend is constructed fresh for every task, and
        vCenter caps how many sessions it will hold. See
        :mod:`waldur_vmware.sessions` for how it is kept alive and released.
        """
        return sessions.soap_sessions.acquire(
            self.settings.pk, self.credentials_fingerprint, self._connect_soap
        )

    def _connect_soap(self):
        import pyVim.connect

        if self.verify_ssl:
            context = ssl.create_default_context()
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        return pyVim.connect.SmartConnect(
            host=self.host,
            user=self.settings.username,
            pwd=self.settings.password,
            port=self.port,
            sslContext=context,
        )

    def release_sessions(self):
        """Log the process out of this vCenter, both endpoints.

        Not close(): the sessions belong to the process, not to this backend, so
        this ends the ones every backend built from the same service settings is
        sharing — including work in flight elsewhere. Nothing on a normal call
        path needs it, because the cache releases sessions itself once they fall
        idle or the process stops; it is here for a caller that wants them gone
        at a known point, such as a test.

        Safe to call more than once, and safe to call when nothing ever
        connected: neither session is opened until something needs it.
        """
        sessions.rest_sessions.release(self.settings.pk)
        sessions.soap_sessions.release(self.settings.pk)

    # ------------------------------------------------------------------
    # vim25 plumbing
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _vim_errors(self, action):
        """Translate vim25 faults, task failures and transport errors into
        VMwareBackendError.

        Everything the plugin does now goes over SOAP, so an unreachable or
        untrusted vCenter surfaces as a socket or TLS error rather than as a
        fault. Those have to be wrapped too: the pull tasks in
        ``waldur_core.structure.tasks`` only catch ``ServiceBackendError``, so an
        unwrapped one would crash the task instead of marking the resource ERRED.
        ``ssl.SSLError`` is an ``OSError``, and ``vmodl.RuntimeFault`` is a
        ``vmodl.MethodFault``, so two clauses cover all of it.
        """
        from pyVmomi import vmodl

        try:
            yield
        except (
            vim_utils.VMwareTaskError,
            vim_utils.VMwareTaskTimeout,
            vim_utils.VMwareMissingProperty,
        ) as e:
            logger.warning("%s failed: %s", action, e)
            raise VMwareBackendError(f"{action} failed: {e}")
        except vmodl.MethodFault as e:
            message = getattr(e, "msg", None) or str(e)
            logger.warning("%s failed: %s", action, message)
            raise VMwareBackendError(f"{action} failed: {message}")
        except OSError as e:
            logger.warning("%s failed: %s", action, e)
            raise VMwareBackendError(f"{action} failed: {e}")

    def _wait(self, task, action):
        """Run a vCenter task to completion, reporting failure as a backend error."""
        with self._vim_errors(action):
            return vim_utils.wait_for_task(task, description=action)

    def _get_vm_moref(self, backend_id):
        from pyVmomi import vim

        return vim_utils.get_moref(self.soap_client, vim.VirtualMachine, backend_id)

    def _get_vm_properties(self, backend_id, path_set=VM_PROPERTIES, required=None):
        """Fetch the given properties of one VM in a single round trip.

        :param required: paths the caller indexes rather than defaults, so that
            vCenter declining to answer surfaces as a backend error instead of a
            KeyError halfway through building a model object.
        """
        with self._vim_errors(f"Reading virtual machine {backend_id}"):
            return vim_utils.collect_object_properties(
                self.soap_client,
                self._get_vm_moref(backend_id),
                list(path_set),
                required=required,
            )

    def _get_vm_devices(self, backend_id, device_type=None):
        """Return the VM's virtual devices, optionally filtered by device type."""
        properties = self._get_vm_properties(backend_id, ["config.hardware.device"])
        devices = properties.get("config.hardware.device") or []
        if device_type is None:
            return list(devices)
        return [device for device in devices if isinstance(device, device_type)]

    def _reconfigure_vm(self, backend_id, spec, action):
        with self._vim_errors(action):
            task = self._get_vm_moref(backend_id).ReconfigVM_Task(spec=spec)
        self._wait(task, action)

    def _add_device(
        self, backend_id, device, action, file_operation=None, matches=None
    ):
        """Attach a device to a VM and return the key vCenter assigned to it.

        ``ReconfigVM_Task`` does not report the new device's key, so the device
        list has to be re-read afterwards. A plain diff of device keys is not
        enough: the read and the reconfigure are not one atomic step, so a
        second attach on the same VM in between lands in the diff as well, and
        both callers then walk away believing they own the same key. That
        produced two Port rows sharing a backend_id, one of them describing an
        adapter it is not attached to, with nothing raising — and a later
        delete_port removing the wrong adapter.

        `matches` identifies which of several new devices belongs to this call,
        by the backing or placement the caller asked for. It is consulted only
        when the diff is ambiguous, so a predicate that disagrees with how
        vCenter normalised the request cannot break the ordinary single-attach
        path. An ambiguity that survives it raises rather than returning a
        plausible but possibly wrong key.

        :param matches: predicate over a device, or None to accept only an
            unambiguous answer.
        """
        from pyVmomi import vim

        device_type = type(device)
        keys_before = {d.key for d in self._get_vm_devices(backend_id, device_type)}

        change = vim.vm.device.VirtualDeviceSpec(
            operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=device
        )
        if file_operation:
            change.fileOperation = file_operation

        self._reconfigure_vm(
            backend_id, vim.vm.ConfigSpec(deviceChange=[change]), action
        )

        candidates = [
            d
            for d in self._get_vm_devices(backend_id, device_type)
            if d.key not in keys_before
        ]
        if not candidates:
            raise VMwareBackendError(f"{action} did not produce a new device.")

        if len(candidates) > 1 and matches is not None:
            candidates = [d for d in candidates if matches(d)]

        if len(candidates) != 1:
            raise VMwareBackendError(
                f"{action} on virtual machine {backend_id} produced "
                f"{len(candidates)} indistinguishable devices, so the one it "
                "created cannot be identified."
            )

        return str(candidates[0].key)

    def _remove_device(self, backend_id, device, action):
        from pyVmomi import vim

        change = vim.vm.device.VirtualDeviceSpec(
            operation=vim.vm.device.VirtualDeviceSpec.Operation.remove, device=device
        )
        self._reconfigure_vm(
            backend_id, vim.vm.ConfigSpec(deviceChange=[change]), action
        )

    def _get_object_name(self, moref):
        return vim_utils.collect_object_properties(
            self.soap_client, moref, ["name"], required=["name"]
        )["name"]

    def ping(self, raise_exception=False):
        """
        Check if backend is ok.
        """
        try:
            with self._vim_errors("Connecting to vCenter"):
                self.soap_client.content.about.apiVersion
            # The Content Library is a separate endpoint with its own session,
            # and pull_templates is the only thing that touches it. Without this
            # a broken REST route or credential passes the connection check and
            # only surfaces on the next service-properties pull. The check is
            # forced past the cache's liveness interval: reporting an endpoint
            # healthy on the strength of a minute-old answer is the one thing
            # this method must not do.
            try:
                self._rest_client(force_check=True)
            except VMwareError as e:
                raise VMwareBackendError(e)
        except VMwareBackendError:
            if raise_exception:
                raise
            return False
        except Exception as e:
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

    # ------------------------------------------------------------------
    # Templates (Content Library — REST)
    # ------------------------------------------------------------------

    def pull_templates(self):
        """
        Pull VMware templates for virtual machine provisioning from the content
        library to the local database.

        The Content Library is only reachable over REST — vim25 has no equivalent
        — so this path, unlike the rest of the backend, still uses the REST client.
        """
        try:
            backend_templates = self.client.list_all_templates()
        except VMwareError as e:
            raise VMwareBackendError(e)

        backend_templates = [
            template
            for template in backend_templates
            if self._is_template_complete(template)
        ]

        if is_basic_mode():
            # If basic mode is enabled, we should filter out templates which have more than 1 NIC
            backend_templates = [
                template
                for template in backend_templates
                if len(template["template"].get("nics") or []) == 1
            ]

        backend_templates_map = {
            item["library_item"]["id"]: item for item in backend_templates
        }

        frontend_templates_map = {
            p.backend_id: p
            for p in models.Template.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_templates_map.keys()) - set(
            backend_templates_map.keys()
        )
        new_ids = set(backend_templates_map.keys()) - set(frontend_templates_map.keys())
        common_ids = set(backend_templates_map.keys()) & set(
            frontend_templates_map.keys()
        )

        for library_item_id in new_ids:
            template = self._backend_template_to_template(
                backend_templates_map[library_item_id]
            )
            template.save()

        for library_item_id in common_ids:
            backend_template = self._backend_template_to_template(
                backend_templates_map[library_item_id]
            )
            frontend_template = frontend_templates_map[library_item_id]
            fields = (
                "cores",
                "cores_per_socket",
                "ram",
                "disk",
                "guest_os",
                "modified",
                "description",
            )
            update_pulled_fields(frontend_template, backend_template, fields)

        models.Template.objects.filter(
            settings=self.settings, backend_id__in=stale_ids
        ).delete()

    def _is_template_complete(self, backend_template):
        """Whether a library item carries the hardware spec a Template needs.

        vCenter answers for a library item it cannot fully describe with an
        empty skeleton — ``{"cpu": {}, "memory": {}, "vm_home_storage": {}}`` —
        rather than an error. Every field below is non-null on models.Template,
        so such an item has to be skipped; indexing it instead fails the whole
        service-properties pull on a KeyError.
        """
        library_item = backend_template.get("library_item") or {}
        template = backend_template.get("template") or {}
        cpu = template.get("cpu") or {}
        memory = template.get("memory") or {}

        missing = [
            name
            for name, value in (
                ("cpu.count", cpu.get("count")),
                ("cpu.cores_per_socket", cpu.get("cores_per_socket")),
                ("memory.size_MiB", memory.get("size_MiB")),
                ("guest_OS", template.get("guest_OS")),
            )
            if value is None
        ]
        if missing:
            logger.warning(
                "Skipping template %s because vCenter did not report %s for it.",
                library_item.get("id"),
                ", ".join(missing),
            )
            return False
        return True

    def _backend_template_to_template(self, backend_template):
        library_item = backend_template["library_item"]
        template = backend_template["template"]
        total_disk = self._get_total_disk(template.get("disks") or [])
        return models.Template(
            settings=self.settings,
            backend_id=library_item["id"],
            name=library_item["name"],
            description=library_item.get("description") or "",
            created=parse_datetime(library_item["creation_time"]),
            modified=parse_datetime(library_item["last_modified_time"]),
            cores=template["cpu"]["count"],
            cores_per_socket=template["cpu"]["cores_per_socket"],
            ram=template["memory"]["size_MiB"],
            disk=total_disk,
            guest_os=template["guest_OS"],
        )

    def _get_total_disk(self, backend_disks):
        """Total disk size of a Content Library template, in MiB."""
        # Convert disk size from bytes to MiB
        return sum([disk["value"]["capacity"] / 1024 / 1024 for disk in backend_disks])

    def _get_devices_total_disk(self, devices):
        """Total disk size of a VM's virtual disks, in MiB."""
        from pyVmomi import vim

        return sum(
            device.capacityInKB / 1024
            for device in devices
            if isinstance(device, vim.vm.device.VirtualDisk)
        )

    # ------------------------------------------------------------------
    # Virtual machines
    # ------------------------------------------------------------------

    @log_backend_action()
    def pull_virtual_machine(self, vm, update_fields=None):
        """
        Pull virtual machine from vCenter and update its information in local database.

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

    def import_virtual_machine(self, backend_id, project=None, save=True):
        """
        Import virtual machine by its ID.

        :param backend_id: Virtual machine identifier
        :type backend_id: str
        :param save: Save object in the database
        :type save: bool
        :param project: Optional service settings model object
        :rtype: :class:`waldur_vmware.models.VirtualMachine`
        """
        backend_vm = self._get_vm_properties(
            backend_id, required=VM_REQUIRED_PROPERTIES
        )

        tools_installed = vim_utils.map_tools_installed(
            backend_vm.get("config.tools.toolsInstallType")
        )
        tools_state = vim_utils.map_tools_state(
            backend_vm.get("guest.toolsRunningStatus")
        )

        vm = self._backend_vm_to_vm(
            backend_vm, tools_installed, tools_state, backend_id
        )
        vm.service_settings = self.settings
        vm.project = project
        if save:
            vm.save()

        return vm

    def _backend_vm_to_vm(self, backend_vm, tools_installed, tools_state, backend_id):
        """
        Build database model object for virtual machine from collected properties.

        :param backend_vm: virtual machine properties, as returned by
            :func:`vim_utils.collect_object_properties`
        :type backend_vm: dict
        :param tools_installed: whether VMware tools installed or not
        :type tools_installed: bool
        :param tools_state: Status of VMware Tools.
        :type tools_state: str
        :param backend_id: Virtual machine identifier
        :type backend_id: str
        :rtype: :class:`waldur_vmware.models.VirtualMachine`
        """
        devices = backend_vm.get("config.hardware.device") or []
        return models.VirtualMachine(
            backend_id=backend_id,
            name=backend_vm["name"],
            state=CoreStates.OK,
            runtime_state=vim_utils.map_power_state(backend_vm["runtime.powerState"]),
            cores=backend_vm["config.hardware.numCPU"],
            cores_per_socket=backend_vm["config.hardware.numCoresPerSocket"],
            ram=backend_vm["config.hardware.memoryMB"],
            disk=self._get_devices_total_disk(devices),
            tools_installed=tools_installed,
            tools_state=tools_state,
        )

    # ------------------------------------------------------------------
    # Service properties
    # ------------------------------------------------------------------

    def _collect(self, vim_type, path_set, action, required=None):
        with self._vim_errors(action):
            return vim_utils.collect_properties(
                self.soap_client, vim_type, list(path_set), required=required
            )

    def pull_clusters(self):
        from pyVmomi import vim

        backend_clusters = self._collect(
            vim.ClusterComputeResource, ["name"], "Pulling clusters", required=["name"]
        )

        backend_clusters_map = {item["moid"]: item for item in backend_clusters}

        frontend_clusters_map = {
            p.backend_id: p
            for p in models.Cluster.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_clusters_map.keys()) - set(backend_clusters_map.keys())
        new_ids = set(backend_clusters_map.keys()) - set(frontend_clusters_map.keys())
        common_ids = set(backend_clusters_map.keys()) & set(
            frontend_clusters_map.keys()
        )

        for item_id in common_ids:
            backend_item = backend_clusters_map[item_id]
            frontend_item = frontend_clusters_map[item_id]
            if frontend_item.name != backend_item["name"]:
                frontend_item.name = backend_item["name"]
                frontend_item.save(update_fields=["name"])

        for item_id in new_ids:
            item = backend_clusters_map[item_id]
            models.Cluster.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item["name"],
            )

        models.Cluster.objects.filter(
            settings=self.settings, backend_id__in=stale_ids
        ).delete()

    def pull_networks(self):
        from pyVmomi import vim

        backend_networks = self._collect(
            vim.Network, ["name"], "Pulling networks", required=["name"]
        )

        backend_networks_map = {item["moid"]: item for item in backend_networks}

        frontend_networks_map = {
            p.backend_id: p
            for p in models.Network.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_networks_map.keys()) - set(backend_networks_map.keys())
        new_ids = set(backend_networks_map.keys()) - set(frontend_networks_map.keys())
        common_ids = set(frontend_networks_map.keys()) & set(
            backend_networks_map.keys()
        )

        for item_id in common_ids:
            backend_item = backend_networks_map[item_id]
            frontend_item = frontend_networks_map[item_id]
            if frontend_item.name != backend_item["name"]:
                frontend_item.name = backend_item["name"]
                frontend_item.save(update_fields=["name"])

        for item_id in new_ids:
            item = backend_networks_map[item_id]
            models.Network.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item["name"],
                type=vim_utils.map_network_type(item["obj"]),
            )

        models.Network.objects.filter(
            settings=self.settings, backend_id__in=stale_ids
        ).delete()

    def pull_datastores(self):
        from pyVmomi import vim

        backend_datastores = self._collect(
            vim.Datastore,
            ["name", "summary.type", "summary.capacity", "summary.freeSpace"],
            "Pulling datastores",
            # Only the name is indexed; the summary fields are defaulted,
            # because a datastore vCenter reports no capacity for must not
            # abort the pull for the rest of them.
            required=["name"],
        )

        backend_datastores_map = {item["moid"]: item for item in backend_datastores}

        frontend_datastores_map = {
            p.backend_id: p
            for p in models.Datastore.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_datastores_map.keys()) - set(
            backend_datastores_map.keys()
        )
        new_ids = set(backend_datastores_map.keys()) - set(
            frontend_datastores_map.keys()
        )
        common_ids = set(backend_datastores_map.keys()) & set(
            frontend_datastores_map.keys()
        )

        for item_id in new_ids:
            datastore = self._backend_datastore_to_datastore(
                backend_datastores_map[item_id]
            )
            datastore.save()

        for item_id in common_ids:
            backend_datastore = self._backend_datastore_to_datastore(
                backend_datastores_map[item_id]
            )
            frontend_datastore = frontend_datastores_map[item_id]
            fields = ("name", "capacity", "free_space")
            update_pulled_fields(frontend_datastore, backend_datastore, fields)

        models.Datastore.objects.filter(
            settings=self.settings, backend_id__in=stale_ids
        ).delete()

    def _backend_datastore_to_datastore(self, backend_datastore):
        capacity = backend_datastore.get("summary.capacity")
        # Convert from bytes to MB
        if capacity:
            capacity /= 1024 * 1024

        free_space = backend_datastore.get("summary.freeSpace")
        # Convert from bytes to MB
        if free_space:
            free_space /= 1024 * 1024

        return models.Datastore(
            settings=self.settings,
            backend_id=backend_datastore["moid"],
            name=backend_datastore["name"],
            # Datastore.type is not nullable, and a datastore vCenter does not
            # report a type for must not abort the whole pull.
            type=backend_datastore.get("summary.type") or "",
            capacity=capacity,
            free_space=free_space,
        )

    def get_vm_folders(self):
        """
        List folders that can hold virtual machines.

        vCenter keeps one folder tree per entity kind; `childType` is what marks a
        folder as the VM one, and it is the equivalent of the REST API's
        `folder_type=VIRTUAL_MACHINE` filter.
        """
        from pyVmomi import vim

        folders = self._collect(
            vim.Folder, ["name", "childType"], "Pulling folders", required=["name"]
        )
        return [
            folder
            for folder in folders
            if "VirtualMachine" in (folder.get("childType") or [])
        ]

    def get_default_vm_folder(self):
        """
        Currently VM folder is required for VM provisioning either from template or from scratch.
        Therefore when folder is not specified for VM, we should use first available folder.
        Please note that it is assumed that there's only one datacenter in this case.
        :return: Virtual machine folder identifier.
        :rtype: str
        """
        return self.get_vm_folders()[0]["moid"]

    def get_default_resource_pool(self):
        """
        Currently resource pool is required for VM provisioning from scratch if cluster is not specified.
        Therefore we should use first available resource pool.
        Please note that it is assumed that there's only one datacenter in this case.
        :return: Resource pool identifier.
        :rtype: str
        """
        from pyVmomi import vim

        return self._collect(vim.ResourcePool, ["name"], "Listing resource pools")[0][
            "moid"
        ]

    def get_default_datastore(self):
        """
        Currently datastore is required for VM provisioning either from template or from scratch.
        Therefore when datastore is not specified for VM, we should use first available datastore.
        Please note that it is assumed that there's only one datacenter in this case.
        :return: Datastore identifier.
        :rtype: str
        """
        from pyVmomi import vim

        return self._collect(vim.Datastore, ["name"], "Listing datastores")[0]["moid"]

    def pull_folders(self):
        backend_folders = self.get_vm_folders()

        backend_folders_map = {item["moid"]: item for item in backend_folders}

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
            if frontend_item.name != backend_item["name"]:
                frontend_item.name = backend_item["name"]
                frontend_item.save(update_fields=["name"])

        for item_id in new_ids:
            item = backend_folders_map[item_id]
            models.Folder.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item["name"],
            )

        models.Folder.objects.filter(
            settings=self.settings, backend_id__in=stale_ids
        ).delete()

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def create_virtual_machine(self, vm):
        """
        Creates a virtual machine.

        :param vm: Virtual machine to be created
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        from pyVmomi import vim

        if vm.template:
            backend_id = self.create_virtual_machine_from_template(vm)
        else:
            backend_id = self.create_virtual_machine_from_scratch(vm)

        backend_vm = self._get_vm_properties(
            backend_id,
            ["runtime.powerState", "config.hardware.device"],
            required=["runtime.powerState"],
        )

        vm.backend_id = backend_id
        vm.runtime_state = vim_utils.map_power_state(backend_vm["runtime.powerState"])
        vm.save(update_fields=["backend_id", "runtime_state"])

        for device in backend_vm.get("config.hardware.device") or []:
            if not isinstance(device, vim.vm.device.VirtualDisk):
                continue
            disk = self._backend_disk_to_disk(device, str(device.key))
            disk.vm = vm
            disk.service_settings = vm.service_settings
            disk.project = vm.project
            disk.save()

        # If virtual machine is not deployed from template, it does not have any networks.
        # Therefore we should create network interfaces manually according to VM spec.
        if not vm.template:
            for network in vm.networks.all():
                self._attach_nic(vm.backend_id, network)

        signals.vm_created.send(self.__class__, vm=vm)
        return vm

    def _get_vm_placement(self, vm):
        """Placement section of a Content Library deployment spec (REST)."""
        placement = {}

        if vm.folder:
            placement["folder"] = vm.folder.backend_id
        else:
            logger.warning(
                "Folder is not specified for VM with ID: %s. "
                "Trying to assign default folder.",
                vm.id,
            )
            placement["folder"] = self.get_default_vm_folder()

        if vm.cluster:
            placement["cluster"] = vm.cluster.backend_id
        else:
            logger.warning(
                "Cluster is not specified for VM with ID: %s. "
                "Trying to assign default resource pool.",
                vm.id,
            )
            placement["resource_pool"] = self.get_default_resource_pool()

        return placement

    def _get_template_nics(self, template):
        """
        Fetch list of NIC IDs assigned to virtual machine template.

        :param template: Virtual machine template.
        :type template: :class:`waldur_vmware.models.Template`
        :rtype: list[str]
        """

        try:
            backend_template = self.client.get_template_library_item(
                template.backend_id
            )
        except VMwareError as e:
            raise VMwareBackendError(e)
        else:
            # A template with no network adapters omits the key rather than
            # reporting an empty list, and an empty body decodes to None.
            return [nic["key"] for nic in (backend_template or {}).get("nics") or []]

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
                logger.warning(
                    "Skipping network assignment because VM does not have "
                    "exactly one network in basic mode. VM ID: %s",
                    vm.id,
                )
                return
            elif len(nics) != 1:
                logger.warning(
                    "Skipping network assignment because related template does "
                    "not have exactly one NIC in basic mode. VM ID: %s",
                    vm.id,
                )

        if len(networks) != len(nics):
            logger.warning(
                "It is not safe to update network assignment when "
                "number of interfaces and networks do not match. VM ID: %s",
                vm.id,
            )

        return [
            {"key": nic, "value": {"network": network.backend_id}}
            for (nic, network) in zip(nics, networks)
        ]

    def create_virtual_machine_from_template(self, vm):
        """
        Deploy a virtual machine from a Content Library template.

        This is the one provisioning path that stays on REST: Content Library
        deployment has no vim25 equivalent.
        """
        spec = {
            "name": vm.name,
            "description": vm.description,
            "hardware_customization": {
                "cpu_update": {
                    "num_cpus": vm.cores,
                    "num_cores_per_socket": vm.cores_per_socket,
                },
                "memory_update": {
                    "memory": vm.ram,
                },
            },
            "placement": self._get_vm_placement(vm),
        }

        if vm.datastore:
            spec["disk_storage"] = {"datastore": vm.datastore.backend_id}
            spec["vm_home_storage"] = {"datastore": vm.datastore.backend_id}

        nics = self._get_vm_nics(vm)
        if nics:
            spec["hardware_customization"]["nics"] = nics

        try:
            return self.client.deploy_vm_from_template(vm.template.backend_id, spec)
        except VMwareError as e:
            raise VMwareBackendError(e)

    def create_virtual_machine_from_scratch(self, vm):
        from pyVmomi import vim

        si = self.soap_client

        folder_id = vm.folder.backend_id if vm.folder else self.get_default_vm_folder()
        folder = vim_utils.get_moref(si, vim.Folder, folder_id)

        if vm.cluster:
            cluster = vim_utils.get_moref(
                si, vim.ClusterComputeResource, vm.cluster.backend_id
            )
            with self._vim_errors("Resolving cluster resource pool"):
                resource_pool = vim_utils.collect_object_properties(
                    si, cluster, ["resourcePool"]
                )["resourcePool"]
        else:
            logger.warning(
                "Cluster is not specified for VM with ID: %s. "
                "Trying to assign default resource pool.",
                vm.id,
            )
            resource_pool = vim_utils.get_moref(
                si, vim.ResourcePool, self.get_default_resource_pool()
            )

        datastore_id = (
            vm.datastore.backend_id if vm.datastore else self.get_default_datastore()
        )
        with self._vim_errors("Resolving datastore"):
            datastore_name = self._get_object_name(
                vim_utils.get_moref(si, vim.Datastore, datastore_id)
            )

        try:
            guest_id = vim_utils.guest_os_to_guest_id(vm.guest_os)
        except ValueError as e:
            raise VMwareBackendError(e)

        # vCenter fits a new VM with IDE, PS2, PCI, SIO and video devices, but no
        # disk controller. Without one, every later disk order fails on "no SCSI
        # controller to attach a disk to", so it is created together with the VM.
        # LSI Logic SAS is the controller vCenter itself defaults to for most
        # guest types and needs no driver injection.
        scsi_controller = vim.vm.device.VirtualDeviceSpec(
            operation=vim.vm.device.VirtualDeviceSpec.Operation.add,
            device=vim.vm.device.VirtualLsiLogicSASController(
                busNumber=0,
                sharedBus=vim.vm.device.VirtualSCSIController.Sharing.noSharing,
            ),
        )

        config = vim.vm.ConfigSpec(
            name=vm.name,
            guestId=guest_id,
            numCPUs=vm.cores,
            numCoresPerSocket=vm.cores_per_socket,
            memoryMB=vm.ram,
            cpuHotAddEnabled=True,
            cpuHotRemoveEnabled=True,
            memoryHotAddEnabled=True,
            # vCenter picks the directory and file names itself when only the
            # datastore is given.
            files=vim.vm.FileInfo(vmPathName=f"[{datastore_name}]"),
            deviceChange=[scsi_controller],
        )

        action = "Creating virtual machine"
        with self._vim_errors(action):
            task = folder.CreateVM_Task(config=config, pool=resource_pool)
        backend_vm = self._wait(task, action)
        return backend_vm._moId

    def delete_virtual_machine(self, vm):
        """
        Deletes a virtual machine.

        :param vm: Virtual machine to be deleted
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        action = "Deleting virtual machine"
        with self._vim_errors(action):
            task = self._get_vm_moref(vm.backend_id).Destroy_Task()
        self._wait(task, action)

    # ------------------------------------------------------------------
    # Power management
    # ------------------------------------------------------------------

    def _power_operation(self, vm, method_name, action):
        with self._vim_errors(action):
            task = getattr(self._get_vm_moref(vm.backend_id), method_name)()
        self._wait(task, action)

    def start_virtual_machine(self, vm):
        """
        Powers on a powered-off or suspended virtual machine.

        :param vm: Virtual machine to be started
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        self._power_operation(vm, "PowerOnVM_Task", "Starting virtual machine")

    def stop_virtual_machine(self, vm):
        """
        Powers off a powered-on or suspended virtual machine.

        :param vm: Virtual machine to be stopped
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        self._power_operation(vm, "PowerOffVM_Task", "Stopping virtual machine")

    def reset_virtual_machine(self, vm):
        """
        Resets a powered-on virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        self._power_operation(vm, "ResetVM_Task", "Resetting virtual machine")

    def suspend_virtual_machine(self, vm):
        """
        Suspends a powered-on virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        self._power_operation(vm, "SuspendVM_Task", "Suspending virtual machine")

    def shutdown_guest(self, vm):
        """
        Issues a request to the guest operating system asking
        it to perform a clean shutdown of all services.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        # Guest power operations are asynchronous requests to VMware Tools rather
        # than vCenter tasks, so there is nothing to wait on here. Completion is
        # observed through is_virtual_machine_shutted_down().
        with self._vim_errors("Shutting down guest OS"):
            self._get_vm_moref(vm.backend_id).ShutdownGuest()

    def reboot_guest(self, vm):
        """
        Issues a request to the guest operating system asking it to perform a reboot.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        with self._vim_errors("Rebooting guest OS"):
            self._get_vm_moref(vm.backend_id).RebootGuest()

    def is_virtual_machine_shutted_down(self, vm):
        properties = self._get_vm_properties(vm.backend_id, ["guest.guestState"])
        guest_state = vim_utils.map_guest_power_state(
            properties.get("guest.guestState")
        )
        return guest_state == models.VirtualMachine.GuestPowerStates.NOT_RUNNING

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
            vm.save(update_fields=["tools_state"])
        self.pull_virtual_machine_runtime_state(vm)
        return result

    def pull_virtual_machine_runtime_state(self, vm):
        properties = self._get_vm_properties(
            vm.backend_id, ["runtime.powerState"], required=["runtime.powerState"]
        )
        backend_power_state = vim_utils.map_power_state(
            properties["runtime.powerState"]
        )
        if backend_power_state != vm.runtime_state:
            vm.runtime_state = backend_power_state
            vm.save(update_fields=["runtime_state"])

    def is_virtual_machine_tools_not_running(self, vm):
        tools_state = self.get_vm_tools_state(vm.backend_id)
        result = tools_state == models.VirtualMachine.ToolsStates.NOT_RUNNING
        if result:
            vm.tools_state = tools_state
            vm.save(update_fields=["tools_state"])
        return result

    # ------------------------------------------------------------------
    # Hardware
    # ------------------------------------------------------------------

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
        from pyVmomi import vim

        cpu_properties = ["config.hardware.numCPU", "config.hardware.numCoresPerSocket"]
        properties = self._get_vm_properties(
            vm.backend_id, cpu_properties, required=cpu_properties
        )
        if (
            properties["config.hardware.numCoresPerSocket"] != vm.cores_per_socket
            or properties["config.hardware.numCPU"] != vm.cores
        ):
            self._reconfigure_vm(
                vm.backend_id,
                vim.vm.ConfigSpec(
                    numCPUs=vm.cores, numCoresPerSocket=vm.cores_per_socket
                ),
                "Updating virtual machine CPU",
            )

    def update_memory(self, vm):
        """
        Updates RAM of virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        from pyVmomi import vim

        properties = self._get_vm_properties(
            vm.backend_id,
            ["config.hardware.memoryMB"],
            required=["config.hardware.memoryMB"],
        )
        if properties["config.hardware.memoryMB"] != vm.ram:
            self._reconfigure_vm(
                vm.backend_id,
                vim.vm.ConfigSpec(memoryMB=vm.ram),
                "Updating virtual machine memory",
            )

    # ------------------------------------------------------------------
    # Network ports
    # ------------------------------------------------------------------

    def _get_network_backing(self, network):
        """Build the backing for a NIC attached to the given network.

        Each of the three network kinds the plugin pulls needs its own backing:
        standard port groups are addressed by name, distributed ones through a
        port connection carrying the switch UUID, and opaque (NSX-backed) ones
        by the NSX network id. vCenter rejects any of them supplied as one of
        the others, and the REST client used to sidestep this entirely by
        passing a network id and letting vCenter pick the backing itself.

        The choice is made from the stored network type rather than from the
        managed object's class: a reference built by :func:`vim_utils.get_moref`
        is an instance of exactly the type it was asked for, so testing it with
        isinstance would always answer `vim.Network` and silently take the
        standard branch for every distributed port group.

        :param network: :class:`waldur_vmware.models.Network`
        """
        from pyVmomi import vim

        if network.type == vim_utils.NETWORK_TYPE_DISTRIBUTED:
            si = self.soap_client
            portgroup = vim_utils.get_moref(
                si, vim.dvs.DistributedVirtualPortgroup, network.backend_id
            )
            with self._vim_errors("Resolving distributed switch"):
                switch = vim_utils.collect_object_properties(
                    si,
                    portgroup,
                    ["config.distributedVirtualSwitch"],
                    required=["config.distributedVirtualSwitch"],
                )["config.distributedVirtualSwitch"]
                switch_uuid = vim_utils.collect_object_properties(
                    si, switch, ["uuid"], required=["uuid"]
                )["uuid"]
            return vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
                port=vim.dvs.PortConnection(
                    portgroupKey=network.backend_id, switchUuid=switch_uuid
                )
            )

        if network.type == vim_utils.NETWORK_TYPE_OPAQUE:
            summary = self._get_opaque_network_summary(network.backend_id)
            return vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo(
                opaqueNetworkId=summary.opaqueNetworkId,
                opaqueNetworkType=summary.opaqueNetworkType,
            )

        si = self.soap_client
        backend_network = vim_utils.get_moref(si, vim.Network, network.backend_id)
        with self._vim_errors("Resolving network"):
            properties = vim_utils.collect_object_properties(
                si, backend_network, ["name"], required=["name"]
            )

        return vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
            network=backend_network, deviceName=properties["name"]
        )

    def _get_opaque_network_summary(self, backend_id):
        """Read an opaque network's NSX identity.

        `summary` is fetched whole here, unlike everywhere else in this backend:
        it is declared on vim.Network as vim.Network.Summary, and the fields
        wanted — opaqueNetworkId, opaqueNetworkType — exist only on the
        OpaqueNetwork subclass, which a nested property path cannot address. The
        rule this bends is about reading a managed object's attributes directly;
        a data object the PropertyCollector returns is safe to walk.
        """
        from pyVmomi import vim

        network = vim_utils.get_moref(self.soap_client, vim.OpaqueNetwork, backend_id)
        with self._vim_errors("Resolving opaque network"):
            return vim_utils.collect_object_properties(
                self.soap_client, network, ["summary"], required=["summary"]
            )["summary"]

    @cached_property
    def _opaque_network_ids(self):
        """Map NSX network ids to the MoIDs stored as Network.backend_id.

        An opaque NIC's backing carries the NSX identifier rather than a managed
        object reference, so a pulled port cannot be matched to its Network row
        without this lookup. Built on first use, which is the first time a NIC
        with an opaque backing is actually seen.
        """
        from pyVmomi import vim

        networks = self._collect(
            vim.OpaqueNetwork, ["summary"], "Pulling opaque networks"
        )
        return {
            item["summary"].opaqueNetworkId: item["moid"]
            for item in networks
            if item.get("summary") is not None
        }

    def _attach_nic(self, backend_vm_id, network):
        """Attach a new NIC to a VM and return its device key.

        :param network: :class:`waldur_vmware.models.Network`
        """
        from pyVmomi import vim

        backing = self._get_network_backing(network)
        nic = vim.vm.device.VirtualVmxnet3(
            backing=backing,
            connectable=vim.vm.device.VirtualDevice.ConnectInfo(
                startConnected=True, connected=True, allowGuestControl=True
            ),
        )
        return self._add_device(
            backend_vm_id,
            nic,
            "Creating network adapter",
            matches=lambda device: self._backing_identity(device.backing)
            == self._backing_identity(backing),
        )

    def _backing_identity(self, backing):
        """Reduce a NIC backing to what identifies the network it points at.

        Used to tell apart adapters added concurrently to one VM. Two adapters
        on the same network are genuinely indistinguishable by this, which is
        why _add_device treats a remaining ambiguity as an error rather than
        picking one.
        """
        from pyVmomi import vim

        card = vim.vm.device.VirtualEthernetCard
        if isinstance(backing, card.DistributedVirtualPortBackingInfo):
            return ("distributed", backing.port.portgroupKey)
        if isinstance(backing, card.OpaqueNetworkBackingInfo):
            return ("opaque", backing.opaqueNetworkId)
        if isinstance(backing, card.NetworkBackingInfo):
            return ("standard", backing.deviceName)
        return ("unknown", None)

    def create_port(self, port):
        """
        Creates an Ethernet port for given VM and network.

        :param port: Port to be created
        :type port: :class:`waldur_vmware.models.Port`
        """
        backend_id = self._attach_nic(port.vm.backend_id, port.network)
        port.backend_id = backend_id
        port.save(update_fields=["backend_id"])
        return port

    def delete_port(self, port):
        """
        Deletes an Ethernet port.

        :param port: Port to be deleted.
        :type port: :class:`waldur_vmware.models.Port`
        """
        backend_port = self._get_backend_nic(port.vm.backend_id, port.backend_id)
        if backend_port is None:
            logger.warning(
                "Network adapter %s is already absent from VM %s.",
                port.backend_id,
                port.vm.backend_id,
            )
            return
        self._remove_device(
            port.vm.backend_id, backend_port, "Deleting network adapter"
        )

    def _get_backend_nic(self, backend_vm_id, backend_port_id):
        from pyVmomi import vim

        for device in self._get_vm_devices(
            backend_vm_id, vim.vm.device.VirtualEthernetCard
        ):
            if str(device.key) == str(backend_port_id):
                return device

    @log_backend_action()
    def pull_port(self, port, update_fields=None):
        """
        Pull Ethernet port from vCenter and update its information in local database.

        :param port: Port to be updated.
        :type port: :class:`waldur_vmware.models.Port`
        :param update_fields: iterable of fields to be updated
        :return: None
        """
        import_time = timezone.now()
        imported_port = self.import_port(
            port.vm.backend_id, port.backend_id, save=False
        )

        port.refresh_from_db()
        if port.modified < import_time:
            if not update_fields:
                update_fields = models.Port.get_backend_fields()

            update_pulled_fields(port, imported_port, update_fields)

    def import_port(
        self,
        backend_vm_id,
        backend_port_id,
        save=True,
        service_settings=None,
        project=None,
    ):
        """
        Import Ethernet port by its ID.

        :param backend_vm_id: Virtual machine identifier
        :type backend_vm_id: str
        :param backend_port_id: Ethernet port identifier
        :type backend_port_id: str
        :param save: Save object in the database
        :type save: bool
        :param service_settings: Optional service settings model object
        :param project: Optional service settings model object
        :rtype: :class:`waldur_vmware.models.Disk`
        """
        backend_port = self._get_backend_nic(backend_vm_id, backend_port_id)
        if backend_port is None:
            raise VMwareBackendError(
                f"Network adapter {backend_port_id} is not found "
                f"on virtual machine {backend_vm_id}."
            )

        port = self._backend_port_to_port(backend_port)
        port.service_settings = service_settings
        port.project = project
        if save:
            port.save()

        return port

    def _backend_port_to_port(self, backend_port):
        """
        Build database model object for Ethernet port from a virtual device.

        :param backend_port: Ethernet card device
        :type backend_port: :class:`pyVmomi.VmomiSupport.vim.vm.device.VirtualEthernetCard`
        :rtype: :class:`waldur_vmware.models.Port`
        """
        connectable = backend_port.connectable
        return models.Port(
            backend_id=str(backend_port.key),
            name=backend_port.deviceInfo.label,
            # MAC address is optional
            mac_address=backend_port.macAddress or "",
            state=CoreStates.OK,
            runtime_state=vim_utils.map_port_state(
                connectable.connected if connectable else False
            ),
        )

    def _get_port_network_id(self, backend_port):
        """Identify the network a NIC is attached to, whichever backing it uses."""
        from pyVmomi import vim

        card = vim.vm.device.VirtualEthernetCard
        backing = backend_port.backing

        if isinstance(backing, card.DistributedVirtualPortBackingInfo):
            return backing.port.portgroupKey

        if isinstance(backing, card.OpaqueNetworkBackingInfo):
            # An opaque backing has no `network` property at all, so it needs
            # translating from the NSX id back to the MoID pull_networks stored.
            return self._opaque_network_ids.get(backing.opaqueNetworkId)

        network = getattr(backing, "network", None)
        if network is not None:
            return network._moId

    def pull_vm_ports(self, vm):
        from pyVmomi import vim

        backend_ports = self._get_vm_devices(
            vm.backend_id, vim.vm.device.VirtualEthernetCard
        )

        backend_ports_map = {str(item.key): item for item in backend_ports}

        frontend_ports_map = {
            p.backend_id: p for p in models.Port.objects.filter(vm=vm)
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
            network = networks_map.get(self._get_port_network_id(backend_port))
            if network is None:
                # Port.network is not nullable, so a NIC on a network the local
                # database does not know about — one added since the last
                # pull_networks, or a kind this plugin does not model — is
                # skipped rather than saved as a broken row.
                logger.warning(
                    "Skipping network adapter %s of VM %s because its network "
                    "is not known locally. Run pull_networks first.",
                    item_id,
                    vm.backend_id,
                )
                continue
            port = self._backend_port_to_port(backend_port)
            port.service_settings = vm.service_settings
            port.project = vm.project
            port.network = network
            port.vm = vm
            port.save()

        for item_id in common_ids:
            backend_port = self._backend_port_to_port(backend_ports_map[item_id])
            frontend_port = frontend_ports_map[item_id]
            fields = ("mac_address", "runtime_state")
            update_pulled_fields(frontend_port, backend_port, fields)

        models.Port.objects.filter(vm=vm, backend_id__in=stale_ids).delete()

    # ------------------------------------------------------------------
    # Disks
    # ------------------------------------------------------------------

    def _get_disk_placement(self, backend_vm_id):
        """Pick a SCSI controller and a unit number free on it.

        Every device attached to a controller occupies a unit number, not only
        disks: a template-derived VM can carry a SCSI CD-ROM or a passthrough
        device, and reusing its unit number makes vCenter reject the whole
        reconfigure. Controllers are tried in order, so a VM whose first
        controller is full still gets its disk.

        :return: (controller, unit_number)
        """
        from pyVmomi import vim

        devices = self._get_vm_devices(backend_vm_id)
        controllers = [
            device
            for device in devices
            if isinstance(device, vim.vm.device.VirtualSCSIController)
        ]
        if not controllers:
            raise VMwareBackendError(
                f"Virtual machine {backend_vm_id} has no SCSI controller "
                "to attach a disk to."
            )

        for controller in controllers:
            used = {
                device.unitNumber
                for device in devices
                if device.controllerKey == controller.key
                and device.unitNumber is not None
            }
            # The controller occupies a unit on its own bus, conventionally 7.
            reserved = controller.scsiCtlrUnitNumber
            used.add(7 if reserved is None else reserved)

            for unit_number in range(16):
                if unit_number not in used:
                    return controller, unit_number

        raise VMwareBackendError(
            f"Every SCSI controller of virtual machine {backend_vm_id} is full."
        )

    def create_disk(self, disk):
        """
        Creates a virtual disk.

        :param disk: Virtual disk to be created
        :type disk: :class:`waldur_vmware.models.Disk`
        """
        from pyVmomi import vim

        backend_vm_id = disk.vm.backend_id
        controller, unit_number = self._get_disk_placement(backend_vm_id)

        backend_disk = vim.vm.device.VirtualDisk(
            # Convert from mebibytes to kibibytes, which is what vim25 uses
            capacityInKB=disk.size * 1024,
            controllerKey=controller.key,
            unitNumber=unit_number,
            backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                diskMode="persistent",
                thinProvisioned=True,
                # vCenter names the VMDK itself when only the datastore is given.
                fileName="",
            ),
        )

        backend_id = self._add_device(
            backend_vm_id,
            backend_disk,
            "Creating virtual disk",
            file_operation=vim.vm.device.VirtualDeviceSpec.FileOperation.create,
            # A unit number is unique on its controller, so it tells this disk
            # apart from one a concurrent order attached to the same VM.
            matches=lambda device: device.controllerKey == controller.key
            and device.unitNumber == unit_number,
        )

        disk.backend_id = backend_id
        disk.save(update_fields=["backend_id"])
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
        if backend_disk is None:
            raise VMwareBackendError(
                f"Virtual disk {disk.backend_id} is not found "
                f"on virtual machine {disk.vm.backend_id}."
            )

        file_name = backend_disk.backing.fileName
        datacenter = self.get_disk_datacenter(backend_disk)

        self._remove_device(disk.vm.backend_id, backend_disk, "Deleting virtual disk")

        if delete_vmdk:
            action = "Deleting virtual disk file"
            with self._vim_errors(action):
                vdm = self.soap_client.content.virtualDiskManager
                task = vdm.DeleteVirtualDisk(name=file_name, datacenter=datacenter)
            self._wait(task, action)
            signals.vm_updated.send(self.__class__, vm=disk.vm)

    def extend_disk(self, disk):
        """
        Increase disk capacity.

        :param disk: Virtual disk to be extended.
        :type disk: :class:`waldur_vmware.models.Disk`
        """
        from pyVmomi import vim

        backend_disk = self.get_backend_disk(disk)
        if backend_disk is None:
            raise VMwareBackendError(
                f"Virtual disk {disk.backend_id} is not found "
                f"on virtual machine {disk.vm.backend_id}."
            )

        backend_disk.capacityInKB = disk.size * 1024
        backend_disk.capacityInBytes = disk.size * 1024 * 1024

        virtual_disk_spec = vim.vm.device.VirtualDeviceSpec(
            operation=vim.vm.device.VirtualDeviceSpec.Operation.edit,
            device=backend_disk,
        )

        self._reconfigure_vm(
            disk.vm.backend_id,
            vim.vm.ConfigSpec(deviceChange=[virtual_disk_spec]),
            "Extending virtual disk",
        )
        signals.vm_updated.send(self.__class__, vm=disk.vm)

    def get_backend_vm(self, vm):
        """
        Get virtual machine object from SOAP client.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        :rtype: :class:`pyVmomi.VmomiSupport.vim.VirtualMachine`
        """
        return self._get_vm_moref(vm.backend_id)

    def get_vm_tools_state(self, backend_id):
        """
        Get running status of VMware Tools.

        :param backend_id: Virtual machine identifier.
        :type backend_id: str
        :rtype: str
        """
        properties = self._get_vm_properties(backend_id, ["guest.toolsRunningStatus"])
        return vim_utils.map_tools_state(properties.get("guest.toolsRunningStatus"))

    def get_vm_tools_installed(self, backend_id):
        """
        Check if VMware Tools are installed.

        :param backend_id: Virtual machine identifier.
        :type backend_id: str
        :rtype: bool
        """
        properties = self._get_vm_properties(
            backend_id, ["config.tools.toolsInstallType"]
        )
        return vim_utils.map_tools_installed(
            properties.get("config.tools.toolsInstallType")
        )

    def get_backend_disk(self, disk):
        """
        Get virtual disk object from SOAP client.

        :param disk: Virtual disk.
        :type disk: :class:`waldur_vmware.models.Disk`
        :rtype: :class:`pyVmomi.VmomiSupport.vim.vm.device.VirtualDisk`
        """
        return self._get_backend_disk(disk.vm.backend_id, disk.backend_id)

    def _get_backend_disk(self, backend_vm_id, backend_disk_id):
        """Find a virtual disk on a VM by its device key."""
        from pyVmomi import vim

        for device in self._get_vm_devices(backend_vm_id, vim.vm.device.VirtualDisk):
            if str(device.key) == str(backend_disk_id):
                return device

    def get_disk_datacenter(self, backend_disk):
        """
        Find the datacenter where virtual disk is located.

        :param backend_disk: Virtual disk object returned by SOAP API.
        :type backend_disk: :class:`pyVmomi.VmomiSupport.vim.vm.device.VirtualDisk`
        :return: VMware datacenter where disk is located.
        :rtype: :class:`pyVmomi.VmomiSupport.vim.Datacenter`
        """
        from pyVmomi import vim

        si = self.soap_client
        with self._vim_errors("Resolving disk datacenter"):
            parent = vim_utils.collect_object_properties(
                si, backend_disk.backing.datastore, ["parent"]
            ).get("parent")
            while parent and not isinstance(parent, vim.Datacenter):
                parent = vim_utils.collect_object_properties(
                    si, parent, ["parent"]
                ).get("parent")
        return parent

    @log_backend_action()
    def pull_disk(self, disk, update_fields=None):
        """
        Pull virtual disk from vCenter and update its information in local database.

        :param disk: Virtual disk database object.
        :type disk: :class:`waldur_vmware.models.Disk`
        :param update_fields: iterable of fields to be updated
        :return: None
        """
        import_time = timezone.now()
        imported_disk = self.import_disk(
            disk.vm.backend_id, disk.backend_id, save=False
        )

        disk.refresh_from_db()
        if disk.modified < import_time:
            if not update_fields:
                update_fields = models.Disk.get_backend_fields()

            update_pulled_fields(disk, imported_disk, update_fields)

    def import_disk(
        self,
        backend_vm_id,
        backend_disk_id,
        save=True,
        project=None,
    ):
        """
        Import virtual disk by its ID.

        :param backend_vm_id: Virtual machine identifier
        :type backend_vm_id: str
        :param backend_disk_id: Virtual disk identifier
        :type backend_disk_id: str
        :param save: Save object in the database
        :type save: bool
        :param project: Project model object
        :rtype: :class:`waldur_vmware.models.Disk`
        """
        backend_disk = self._get_backend_disk(backend_vm_id, backend_disk_id)
        if backend_disk is None:
            raise VMwareBackendError(
                f"Virtual disk {backend_disk_id} is not found "
                f"on virtual machine {backend_vm_id}."
            )

        disk = self._backend_disk_to_disk(backend_disk, backend_disk_id)
        disk.service_settings = self.settings
        disk.project = project
        if save:
            disk.save()

        return disk

    def _backend_disk_to_disk(self, backend_disk, backend_disk_id):
        """
        Build database model object for virtual disk from a virtual device.

        :param backend_disk: virtual disk device
        :type backend_disk: :class:`pyVmomi.VmomiSupport.vim.vm.device.VirtualDisk`
        :param backend_disk_id: Virtual disk identifier
        :type backend_disk_id: str
        :rtype: :class:`waldur_vmware.models.Disk`
        """
        return models.Disk(
            backend_id=str(backend_disk_id),
            name=backend_disk.deviceInfo.label,
            # Convert disk size from KiB to MiB
            size=backend_disk.capacityInKB / 1024,
            state=CoreStates.OK,
        )

    # ------------------------------------------------------------------
    # Consoles
    # ------------------------------------------------------------------

    def get_console_url(self, vm):
        """
        Generates a virtual machine's remote console URL (VMRC)

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        with self._vim_errors("Acquiring console ticket"):
            ticket = self.soap_client.content.sessionManager.AcquireCloneTicket()
        # netloc, not host: a vCenter published on a non-default port has to keep
        # it here, or the console client connects to 443 and fails.
        return f"vmrc://clone:{ticket}@{self.netloc}/?moid={vm.backend_id}"

    def get_web_console_url(self, vm):
        """
        Generates a virtual machine's web console URL (WMKS)

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        with self._vim_errors("Acquiring web console ticket"):
            ticket = self._get_vm_moref(vm.backend_id).AcquireMksTicket()
        params = {
            "host": ticket.host,
            "port": ticket.port,
            "ticket": ticket.ticket,
            "cfgFile": ticket.cfgFile,
            "thumbprint": ticket.sslThumbprint,
            "vmId": vm.backend_id,
            "encoding": "UTF-8",
        }
        return f"wss://{ticket.host}/ui/webconsole/authd?{urlencode(params)}"
