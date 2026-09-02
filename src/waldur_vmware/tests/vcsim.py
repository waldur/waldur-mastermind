"""Shared setup for the vcsim integration tests.

These tests talk to a real simulator over the wire rather than to a MagicMock,
so they validate the call shapes and payloads the plugin actually sends. What
they do not validate is vCenter itself: vcsim is a developer tool, not a
complete implementation, and it diverges in documented ways (see
`waldur_vmware.vim_utils`). Acceptance against a live vCenter is still required.

Bring the simulator up with `docker/vcsim-dev/`, then:

    DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local \\
      uv run pytest --reuse-db -m vcsim src/waldur_vmware/ -v
"""

import os
import uuid

import requests
import urllib3
from django.test import TestCase

from waldur_vmware.backend import VMwareBackend

from . import factories

VCSIM_URL = os.environ.get("VCSIM_URL", "https://localhost:8989")
VCSIM_USERNAME = os.environ.get("VCSIM_USERNAME", "waldur")
VCSIM_PASSWORD = os.environ.get("VCSIM_PASSWORD", "waldur")


class VcsimTestCase(TestCase):
    """Base class wiring a VMwareBackend to the simulator."""

    def setUp(self):
        super().setUp()
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.service_settings = factories.VMwareServiceSettingsFactory(
            backend_url=VCSIM_URL,
            username=VCSIM_USERNAME,
            password=VCSIM_PASSWORD,
        )
        self.backend = VMwareBackend(self.service_settings)

    # -- Content Library helpers -------------------------------------------
    #
    # vcsim starts with no content libraries, so a test that needs a template
    # creates one. This goes over REST directly rather than through the plugin's
    # client, because creating libraries and VMTX items is operator-side setup
    # the plugin never performs itself.

    def _rest_session(self):
        session = requests.Session()
        session.verify = False
        session.post(
            f"{VCSIM_URL}/rest/com/vmware/cis/session",
            auth=(VCSIM_USERNAME, VCSIM_PASSWORD),
        )
        return session

    def create_library_template(self, name="waldur-test-template"):
        """Publish one of vcsim's stock VMs as a VM template library item.

        Names are suffixed per call: vcsim keeps libraries for the lifetime of
        the process and rejects a duplicate name, so a reused simulator would
        otherwise fail every run after the first.

        :return: the library item id, which is what Template.backend_id holds.
        """
        session = self._rest_session()
        name = f"{name}-{uuid.uuid4().hex[:8]}"

        datastore = self.backend.get_default_datastore()
        folder = self.backend.get_default_vm_folder()
        resource_pool = self.backend.get_default_resource_pool()
        cluster = self._first_cluster()
        source_vm = self._first_stock_vm()

        response = session.post(
            f"{VCSIM_URL}/rest/com/vmware/content/local-library",
            json={
                "create_spec": {
                    "name": f"{name}-library",
                    "type": "LOCAL",
                    "storage_backings": [
                        {"datastore_id": datastore, "type": "DATASTORE"}
                    ],
                }
            },
        )
        library_id = response.json()["value"]

        response = session.post(
            f"{VCSIM_URL}/rest/vcenter/vm-template/library-items",
            json={
                "spec": {
                    "source_vm": source_vm,
                    "library": library_id,
                    "name": name,
                    # vcsim needs all three placement fields here; naming only
                    # the folder and pool makes it answer with an empty object
                    # instead of an item id.
                    "placement": {
                        "folder": folder,
                        "cluster": cluster,
                        "resource_pool": resource_pool,
                    },
                    "vm_home_storage": {"datastore": datastore},
                }
            },
        )
        item_id = response.json()["value"]
        # vcsim reports a rejected placement as an empty object rather than an
        # error, which would otherwise surface much later as a puzzling 404.
        if not isinstance(item_id, str) or not item_id:
            raise AssertionError(
                f"vcsim did not create a VM template library item: {response.text}"
            )
        return item_id

    def clear_template_flag(self, backend_id):
        """Undo a vcsim quirk: a VM deployed from a VMTX item stays a template.

        A live vCenter hands back an ordinary virtual machine from
        `?action=deploy`; vcsim leaves `config.template` set, so every
        subsequent power operation fails with "cannot powerOn a template". That
        is a defect in the simulator rather than in the plugin, so it is
        compensated for here instead of in the backend.
        """
        from pyVmomi import vim

        from waldur_vmware import vim_utils

        si = self.backend.soap_client
        vm = vim_utils.get_moref(si, vim.VirtualMachine, backend_id)
        properties = vim_utils.collect_object_properties(si, vm, ["config.template"])
        if not properties.get("config.template"):
            return

        pool = vim_utils.get_moref(
            si, vim.ResourcePool, self.backend.get_default_resource_pool()
        )
        vm.MarkAsVirtualMachine(pool=pool)

    def _collect(self, vim_type):
        from waldur_vmware import vim_utils

        return vim_utils.collect_properties(
            self.backend.soap_client, vim_type, ["name"]
        )

    def _first_stock_vm(self):
        """Pick one of vcsim's own VMs, ignoring anything a test left behind.

        vcsim has no reset, so a locally reused simulator accumulates VMs from
        earlier runs. Selecting by the stock naming prefix keeps this stable;
        picking the lowest MoID would not, because MoIDs sort as strings.
        """
        from pyVmomi import vim

        stock = sorted(
            (item["name"], item["moid"])
            for item in self._collect(vim.VirtualMachine)
            if item["name"].startswith("DC0_")
        )
        if not stock:
            raise AssertionError("vcsim has none of its stock virtual machines.")
        return stock[0][1]

    def _first_cluster(self):
        from pyVmomi import vim

        return sorted(
            item["moid"] for item in self._collect(vim.ClusterComputeResource)
        )[0]
