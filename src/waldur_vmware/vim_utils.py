"""Helpers for talking to vCenter over the vim25 (SOAP) API via pyVmomi.

Two constraints drive the shape of this module, both found while validating the
plugin against vcsim (VMware's simulator, shipped inside govmomi):

1. ``pyVim.task.WaitForTask`` is unusable against vcsim: it raises
   ``AttributeError: 'UnknownManagedMethod' object has no attribute 'info'`` for
   ``ResetVM_Task``, ``CloneVM_Task`` and ``Destroy_Task`` even though the
   operation itself succeeds. :func:`wait_for_task` polls ``task.info.state``
   instead, which works for all of them.

2. Whole managed-object properties must never be read. ``vm.runtime`` raises
   ``AttributeError: type object 'vim.VirtualMachine.FaultToleranceState' has no
   attribute ''`` on pyVmomi 8.0.3 (vcsim emits an empty enum value) and
   ``AttributeError: runtime`` on 9.1.0.0. :func:`collect_properties` queries an
   explicit ``pathSet`` through the PropertyCollector, which behaves identically
   on both versions — and is the better-performing pattern regardless, since it
   answers in one round trip instead of one per property.

pyVmomi is imported lazily inside each function: this module is reachable from
``backend.py``, which sits on the Django startup import path via ``apps.py``.
See CLAUDE.md, "Lazy imports for heavy optional dependencies".
"""

import logging
import time

logger = logging.getLogger(__name__)

# How long to wait for a vCenter task before giving up, and how often to ask.
# Cloning a large template is minutes of work, so the ceiling is generous; the
# poll interval is what keeps a fast operation fast.
TASK_TIMEOUT = 30 * 60
TASK_POLL_INTERVAL = 1


class VMwareTaskError(Exception):
    """A vCenter task finished in the `error` state."""


class VMwareTaskTimeout(Exception):
    """A vCenter task did not settle within the timeout."""


class VMwareMissingProperty(Exception):
    """vCenter did not answer for a property the caller cannot do without."""


def wait_for_task(
    task, timeout=TASK_TIMEOUT, poll_interval=TASK_POLL_INTERVAL, description=None
):
    """Block until a vCenter task settles, and return its result.

    :param task: the ``vim.Task`` returned by a ``*_Task`` method.
    :param timeout: seconds to wait before raising :class:`VMwareTaskTimeout`.
    :param poll_interval: seconds between ``task.info.state`` reads.
    :param description: human-readable operation name, used in log messages.
    :raises VMwareTaskError: the task reported an error.
    :raises VMwareTaskTimeout: the task neither succeeded nor failed in time.
    """
    label = description or getattr(task, "_moId", "task")
    deadline = time.monotonic() + timeout

    while True:
        info = task.info
        state = info.state

        if state == "success":
            logger.debug("VMware task %s succeeded.", label)
            return info.result

        if state == "error":
            # `error` is a vmodl.MethodFault. localizedMessage is what an
            # operator would see in vCenter; msg is the raw fault message.
            error = info.error
            message = (
                getattr(error, "localizedMessage", None)
                or getattr(error, "msg", None)
                or str(error)
            )
            raise VMwareTaskError(message)

        if time.monotonic() >= deadline:
            raise VMwareTaskTimeout(
                f"VMware task {label} did not complete in {timeout} seconds; "
                f"last known state is {state}."
            )

        time.sleep(poll_interval)


def get_moref(service_instance, vim_type, moid):
    """Build a managed object reference from its type and MoID.

    This constructs the reference directly instead of walking the inventory to
    find a matching ``_moId``: a lookup by identity should not cost a full
    ``CreateContainerView`` over every object of that type.

    The reference is not validated here — vCenter reports a stale or wrong MoID
    as ``ManagedObjectNotFound`` on first use.
    """
    return vim_type(moid, service_instance._stub)


def _check_answered(result, required, missing, moid):
    """Fail loudly for a property the caller declared it cannot do without.

    vCenter has two ways of not answering, and neither raises on its own:

    * it reports the path in ``missingSet`` — an inaccessible or orphaned
      object, or a permission the account does not hold;
    * it omits the path from the answer altogether, which is what vcsim does
      for ``guest.guestState`` and ``config.tools.toolsInstallType``.

    Callers that index the result would then get a bare ``KeyError`` from inside
    a pull task, where only ``ServiceBackendError`` is caught — so the task
    crashes instead of marking the resource ERRED. Declaring a path in
    ``required`` turns both cases into one typed error the backend can wrap.
    """
    if missing:
        logger.debug(
            "vCenter did not answer for %s of %s.", ", ".join(sorted(missing)), moid
        )

    if not required:
        return

    absent = sorted(path for path in required if path not in result)
    if absent:
        raise VMwareMissingProperty(
            f"vCenter did not report {', '.join(absent)} for {moid}."
        )


def collect_properties(service_instance, vim_type, path_set, root=None, required=None):
    """Retrieve `path_set` for every object of `vim_type` in one round trip.

    :param vim_type: e.g. ``vim.VirtualMachine``.
    :param path_set: explicit property paths, e.g. ``["name", "runtime.powerState"]``.
        Reading a whole property (``["runtime"]``) is exactly what this helper
        exists to avoid — see the module docstring.
    :param root: container to search, defaulting to the root folder.
    :param required: subset of `path_set` whose absence is an error rather than
        a value to default — see :func:`_check_answered`.
    :return: list of dicts, each holding the requested paths plus ``obj`` (the
        managed object reference) and ``moid`` (its identifier, which is what
        the plugin stores as ``backend_id``).

    A property missing from vCenter's answer is simply absent from the dict, so
    callers must use ``.get()`` for anything optional. vcsim, for instance, does
    not populate ``guest.guestState`` on its stock inventory.
    """
    from pyVmomi import vim, vmodl

    # pyVmomi builds its type hierarchy at import time, so a static checker
    # cannot see through `vmodl.query`. Bind it once per call site rather than
    # scattering ignores over every reference.
    pc = vmodl.query.PropertyCollector  # type: ignore[attr-defined]

    content = service_instance.content
    container = content.viewManager.CreateContainerView(
        root or content.rootFolder, [vim_type], True
    )
    try:
        filter_spec = pc.FilterSpec(
            objectSet=[
                pc.ObjectSpec(
                    obj=container,
                    skip=True,
                    selectSet=[
                        pc.TraversalSpec(
                            path="view", skip=False, type=vim.view.ContainerView
                        )
                    ],
                )
            ],
            propSet=[pc.PropertySpec(type=vim_type, pathSet=path_set, all=False)],
        )
        result = []
        for object_content in content.propertyCollector.RetrieveContents([filter_spec]):
            item = {"obj": object_content.obj, "moid": object_content.obj._moId}
            for prop in object_content.propSet:
                item[prop.name] = prop.val
            _check_answered(
                item,
                required,
                [fault.path for fault in object_content.missingSet or []],
                item["moid"],
            )
            result.append(item)
        return result
    finally:
        container.Destroy()


def collect_object_properties(service_instance, obj, path_set, required=None):
    """Retrieve `path_set` for a single managed object in one round trip.

    Same rules as :func:`collect_properties` — explicit paths only, missing
    properties are absent from the result unless named in `required`.
    """
    from pyVmomi import vmodl

    pc = vmodl.query.PropertyCollector  # type: ignore[attr-defined]

    content = service_instance.content
    filter_spec = pc.FilterSpec(
        objectSet=[pc.ObjectSpec(obj=obj, skip=False)],
        propSet=[pc.PropertySpec(type=type(obj), pathSet=path_set, all=False)],
    )
    result = {"obj": obj, "moid": obj._moId}
    missing = []
    for object_content in content.propertyCollector.RetrieveContents([filter_spec]):
        for prop in object_content.propSet:
            result[prop.name] = prop.val
        missing.extend(fault.path for fault in object_content.missingSet or [])
    _check_answered(result, required, missing, result["moid"])
    return result


# ---------------------------------------------------------------------------
# State mapping
#
# The database stores the identifiers the REST API used (POWERED_ON, RUNNING,
# ...) and views.py validates every VM action against them, so vim25's own
# spelling (poweredOn, guestToolsRunning, ...) is translated at the boundary
# rather than migrated. See models.VirtualMachine.RuntimeStates and friends.
# ---------------------------------------------------------------------------

POWER_STATE_MAP = {
    "poweredOn": "POWERED_ON",
    "poweredOff": "POWERED_OFF",
    "suspended": "SUSPENDED",
}

GUEST_POWER_STATE_MAP = {
    "running": "RUNNING",
    "shuttingDown": "SHUTTING_DOWN",
    "resetting": "RESETTING",
    "standby": "STANDBY",
    "notRunning": "NOT_RUNNING",
    "unknown": "UNAVAILABLE",
}

TOOLS_STATE_MAP = {
    "guestToolsExecutingScripts": "STARTING",
    "guestToolsRunning": "RUNNING",
    "guestToolsNotRunning": "NOT_RUNNING",
}

TOOLS_INSTALL_TYPE_UNKNOWN = "guestToolsTypeUnknown"

PORT_STATE_CONNECTED = "CONNECTED"
PORT_STATE_NOT_CONNECTED = "NOT_CONNECTED"


def map_power_state(backend_state):
    """Translate ``runtime.powerState`` into the stored runtime state."""
    return POWER_STATE_MAP.get(backend_state)


def map_guest_power_state(backend_state):
    """Translate ``guest.guestState`` into the stored guest power state.

    vCenter reports states this plugin has no constant for on rare occasions;
    they surface as UNAVAILABLE rather than as a crash in a pull task.
    """
    return GUEST_POWER_STATE_MAP.get(backend_state, "UNAVAILABLE")


def map_tools_state(backend_state):
    """Translate ``guest.toolsRunningStatus`` into the stored tools state."""
    return TOOLS_STATE_MAP.get(backend_state)


def map_tools_installed(install_type):
    """Decide whether VMware Tools are installed from ``toolsInstallType``.

    An absent property is not evidence that tools are present: vCenter simply
    did not report an install type, and vcsim omits it entirely. Reading that as
    installed offers guest shutdown and reboot on a VM that cannot perform
    either.
    """
    return install_type not in (None, "", TOOLS_INSTALL_TYPE_UNKNOWN)


def map_port_state(connected):
    """Translate a NIC's ``connectable.connected`` flag into a runtime state."""
    return PORT_STATE_CONNECTED if connected else PORT_STATE_NOT_CONNECTED


NETWORK_TYPE_STANDARD = "STANDARD_PORTGROUP"
NETWORK_TYPE_DISTRIBUTED = "DISTRIBUTED_PORTGROUP"
NETWORK_TYPE_OPAQUE = "OPAQUE_NETWORK"


def map_network_type(network):
    """Classify a network object the way the REST API's `type` field did.

    Order matters: both distributed port groups and opaque networks subclass
    ``vim.Network``, so the specific types have to be tested first.
    """
    from pyVmomi import vim

    if isinstance(network, vim.dvs.DistributedVirtualPortgroup):
        return NETWORK_TYPE_DISTRIBUTED
    if isinstance(network, vim.OpaqueNetwork):
        return NETWORK_TYPE_OPAQUE
    return NETWORK_TYPE_STANDARD


_GUEST_ID_BY_NORMALISED_NAME = None


def _normalise_guest_name(value):
    """Reduce either spelling of a guest OS name to a comparable key.

    ``Guest`` is stripped wherever it appears, not just as a suffix:
    ``otherGuest64`` carries it in the middle, and treating it as a suffix
    turns that identifier into ``othergu``, which then matches nothing.
    """
    return value.replace("Guest", "").replace("_", "").lower()


def guest_os_to_guest_id(guest_os):
    """Translate a stored guest OS value into a vim25 ``guestId``.

    ``VirtualMachine.guest_os`` holds the vSphere Automation API's spelling
    (``UBUNTU_64``, ``WIN_31``) because that is what the REST client stored and
    what ``constants.GUEST_OS_CHOICES`` offers through the API. ``CreateVM_Task``
    wants vim25's spelling (``ubuntu64Guest``, ``win31Guest``).

    The two differ only in separators and case, so the table is derived from
    pyVmomi's own enumeration rather than hardcoded: a 148-entry literal would
    silently rot as VMware adds guest types, while this resolves against
    whatever the installed pyVmomi knows about. Anything that does not resolve
    raises, rather than provisioning a VM with the wrong guest type.

    The table is built from ``GuestOsIdentifier.values``. The enumeration also
    subclasses ``str``, so walking it with ``dir()`` would sweep in every string
    method and produce junk entries — including one that maps an empty guest OS
    to ``guestId="Array"``.
    """
    from pyVmomi import vim

    global _GUEST_ID_BY_NORMALISED_NAME

    if _GUEST_ID_BY_NORMALISED_NAME is None:
        table = {}
        for identifier in vim.vm.GuestOsDescriptor.GuestOsIdentifier.values:
            table.setdefault(_normalise_guest_name(identifier), identifier)
        _GUEST_ID_BY_NORMALISED_NAME = table

    guest_id = _GUEST_ID_BY_NORMALISED_NAME.get(_normalise_guest_name(guest_os))
    if guest_id is None:
        raise ValueError(
            f"Guest OS {guest_os!r} has no known vim25 guestId equivalent."
        )
    return guest_id
