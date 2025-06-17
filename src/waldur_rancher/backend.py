import logging
import time
from typing import cast
from urllib.parse import parse_qs, urlparse

import requests
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db.models import QuerySet
from django.utils.functional import cached_property

import hvac
from hvac import exceptions as vault_exceptions
from keycloak import KeycloakAdmin
from keycloak import exceptions as keycloak_exceptions
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.media.utils import guess_image_extension
from waldur_core.structure.backend import ServiceBackend
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.registry import get_resource_type
from waldur_core.structure.utils import update_pulled_fields
from waldur_mastermind.common.utils import parse_datetime
from waldur_openstack.models import Instance
from waldur_rancher.enums import (
    SERVER_ROLE,
)
from waldur_rancher.exceptions import NotFound, RancherException, VaultException

from . import client, models, utils

logger = logging.getLogger(__name__)

CLOUD_INIT_TEMPLATE = """\
#cloud-config

package_update: true
package_upgrade: true

cloud_init_modules:
 - migrator
 - seed_random
 - bootcmd
 - write_files
 - growpart
 #- resizefs  # commented out to disable
 - disk_setup
 - set_hostname
 - update_hostname
 - update_etc_hosts
 - ca_certs
 - rsyslog
 - users_groups
 - ssh
 - mounts

disk_setup:
  /dev/sdb:
    table_type: gpt
    layout: true
    overwrite: false

fs_setup:
  - label: longhorn_data
    filesystem: btrfs
    device: /dev/sdb1
    overwrite: false

mounts:
- [/dev/sdb1, /opt/rke2_storage, auto, "defaults", "0", "2"]

users:
- name: root
    # Add public SSH key here
    ssh_authorized_keys: []

write_files:
  - path: /etc/vault/role-id
    content: |
      {vault_role_id}

  - path: /etc/vault/secret-id
    content: |
      {vault_role_secret_id}


  - path: /etc/sysctl.d/90-kubernetes.conf
    content: |
      # Kubernetes recommended settings
      net.ipv4.conf.all.forwarding=1
      net.ipv6.conf.all.forwarding=1
      net.ipv4.ip_forward=1

      # OOM settings
      vm.panic_on_oom=0
      vm.overcommit_memory=1

      # Kernel panic settings
      kernel.panic=10
      kernel.panic_on_oops=1
      kernel.keys.root_maxbytes=25000000

      # For large scale environments
      fs.inotify.max_user_watches=524288
      fs.inotify.max_user_instances=512

      # Improve network performance
      net.core.somaxconn=32768
      net.ipv4.tcp_tw_reuse=1
      net.ipv4.tcp_fin_timeout=15
      net.core.netdev_max_backlog=16384

      # Prevent ip spoofing
      net.ipv4.conf.all.rp_filter=1
      net.ipv4.conf.default.rp_filter=1

      # Ensure source routing is disabled
      net.ipv4.conf.all.accept_source_route=0
      net.ipv4.conf.default.accept_source_route=0

      # Bridge netfilter
      net.bridge.bridge-nf-call-iptables=1
      net.bridge.bridge-nf-call-ip6tables=1

  # Custom transactional update service
  - path: /etc/systemd/system/custom-transactional-update.service
    content: |
      [Unit]
      Description=Custom Transactional Update
      Requires=network-online.target
      After=network-online.target

      [Service]
      Type=oneshot
      ExecStart=/var/opt/scripts/custom-update.sh

      [Install]
      WantedBy=multi-user.target

  # Custom update timer (daily at 2am)
  - path: /etc/systemd/system/custom-transactional-update.timer
    content: |
      [Unit]
      Description=Custom Timer for Transactional Update

      [Timer]
      OnCalendar=*-*-* 02:00:00
      RandomizedDelaySec=1800

      [Install]
      WantedBy=timers.target

  - path: /var/opt/scripts/custom-update.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      # Run transactional update without rebooting
      /usr/sbin/transactional-update cleanup dup --non-interactive

      # Check if reboot is needed and notify kured
      if [ -f /var/run/reboot-needed ]; then
        touch /run/reboot-required
      fi

  - path: /etc/modules-load.d/k8s.conf
    permissions: '0644'
    content: |
      overlay
      br_netfilter

  - path: /etc/systemd/system/rke2-setup.service
    content: |
      [Unit]
      Description=RKE2 Installation and Setup
      Wants=network-online.target
      After=network-online.target
      # Run only once after first boot
      ConditionPathExists=!/var/lib/rke2-setup.done

      [Service]
      Type=oneshot
      ExecStart=/bin/bash /var/opt/scripts/rke-install.sh
      ExecStartPost=/bin/touch /var/lib/rke2-setup.done
      RemainAfterExit=yes

      [Install]
      WantedBy=multi-user.target

  - path: /tmp/first-boot-packages.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      zypper refresh
      zypper install -y qemu-guest-agent nano htop container-selinux open-iscsi nfs-client unzip curl ca-certificates jq

  - path: /etc/profile.d/kube.sh
    append: true
    content: |
      export PATH=$PATH:/var/opt/bin:/opt/rke2/bin:/var/lib/rancher/rke2/bin/
      export KUBECONFIG=/etc/rancher/rke2/rke2.yaml

  - path: /var/lib/rancher/rke2/server/manifests/rke2-ingress-nginx-config.yaml
    permissions: '0640'
    content: |
      apiVersion: helm.cattle.io/v1
      kind: HelmChartConfig
      metadata:
        name: rke2-ingress-nginx
        namespace: kube-system
      spec:
        valuesContent: |-
          controller:
            config:
              allow-snippet-annotations: "true"
              proxy-buffering: "off"
              proxy-buffers: 100 "2M"
              proxy-buffer-size: "2M"
              use-forwarded-headers: "true"

  - path: /var/opt/scripts/rke-install.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      # --- Install Vault CLI ---
      VAULT_VERSION="1.19.0"
      mkdir -p /var/opt/bin
      curl -s -L -o /tmp/vault.zip "https://releases.hashicorp.com/vault/${{VAULT_VERSION}}/vault_${{VAULT_VERSION}}_linux_amd64.zip"
      unzip -o /tmp/vault.zip -d /var/opt/bin/ vault
      rm -f /tmp/vault.zip
      chmod 755 /var/opt/bin/vault
      /var/opt/bin/vault --version || {{ echo "Vault CLI installation failed"; exit 1; }}

      # --- Fetch variables from metadata server ---
      RKE_ROLE="{rke_role}"
      VAULT_ADDR="{vault_addr}"

      VAULT_ROLE_ID="{vault_role_id}"
      VAULT_SECRET_ID="{vault_role_secret_id}"

      export VAULT_ADDR

      # Ensure PATH includes our custom bin directory
      export PATH=$PATH:/var/opt/bin

      # --- Get token from Vault ---

      echo "[DEBUG] Using Vault Address: ${{VAULT_ADDR}}"
      echo "[DEBUG] Using Vault Role ID: ${{VAULT_ROLE_ID}}"
      export VAULT_TOKEN=$(/var/opt/bin/vault write -tls-skip-verify --field=token auth/approle/login role_id=${{VAULT_ROLE_ID}} secret_id=${{VAULT_SECRET_ID}})

      if [ -z "$VAULT_TOKEN" ]; then
        echo "[ERROR] Failed to obtain Vault token. Exiting."
        exit 1
      else
        echo "[INFO] Successfully obtained Vault token."
      fi

      RKE_TOKEN=$(/var/opt/bin/vault kv get -tls-skip-verify -field=token {vault_secret_path})
      export RKE_TOKEN

      if [ -z "$RKE_TOKEN" ]; then
        echo "[ERROR] Failed to obtain RKE token from Vault. Exiting."
        exit 1
      else
        echo "[INFO] Successfully obtained RKE token."
      fi

      # --- Install RKE2 ---

      if [ "$RKE_ROLE" == "server" ]; then
        CATTLE_AGENT_VAR_DIR="/opt/rke2_storage/agent" curl -fL https://rancher-aio.cloud.ut.ee/system-agent-install.sh | sudo CATTLE_AGENT_VAR_DIR="/opt/rke2_storage/agent" sh -s - --server https://rancher-aio.cloud.ut.ee --label 'cattle.io/os=linux' --token $RKE_TOKEN --etcd --controlplane
      else
        CATTLE_AGENT_VAR_DIR="/opt/rke2_storage/agent" curl -fL https://rancher-aio.cloud.ut.ee/system-agent-install.sh | sudo CATTLE_AGENT_VAR_DIR="/opt/rke2_storage/agent" sh -s - --server https://rancher-aio.cloud.ut.ee --label 'cattle.io/os=linux' --token $RKE_TOKEN --worker
      fi


  - path: /tmp/system-config.sh
    permissions: '0755'
    content: |
      #!/bin/bash

      # --- Setup GRUB config for faster reboot ---
      sed -i "s/GRUB_TIMEOUT=10/GRUB_TIMEOUT=1/g" /etc/default/grub
      grub2-mkconfig > /boot/grub2/grub.cfg

      # --- Setup Kured reboot method ---
      echo "REBOOT_METHOD=kured" > /etc/transactional-update.conf


runcmd:
  # Create necessary directories
  - mkdir -p /var/opt/bin /var/opt/scripts
  - chmod 755 /var/opt/bin /var/opt/scripts

  # Disable default update mechanisms
  - systemctl disable rebootmgr.service || echo "Failed to disable rebootmgr"
  - systemctl disable transactional-update.timer || echo "Failed to disable transactional-update.timer"
  - systemctl mask rebootmgr transactional-update.timer || echo "Failed to mask default update timers"

  # Enable our custom update service and timer
  - systemctl enable custom-transactional-update.timer

  # Install basic packages first (they go into the base snapshot)
  # NB! DO NOT TRY TO REMOVE THE `sh -c $(cat <file>)` workaround!
  ## The `transactional-update` shell does not see files from `/tmp`, making this fail without cat redirection.
  - transactional-update run sh -c "$(cat /tmp/first-boot-packages.sh)"
  - transactional-update --continue run sh -c "$(cat /tmp/system-config.sh)"

  # Enable and disable services
  - systemctl enable iscsid
  - systemctl disable podman || echo "podman not found or already disabled"
  - systemctl mask podman || echo "podman not found or already masked"
  - systemctl enable rke2-setup.service
  - reboot -h now
"""


class RancherBackend(ServiceBackend):
    DEFAULTS = {
        "cloud_init_template": CLOUD_INIT_TEMPLATE,
        "node_disk_driver": "sd",
        "k8s_version": "v1.31.7+rke2r1",
        "private_registry_url": None,
        "private_registry_user": None,
        "private_registry_password": None,
        "management_tenant_access_port": 443,
        "vault_host": None,
        "vault_port": "8200",
        "vault_token": "root",
        "vault_verify": True,
        "keycloak_url": "http://localhost:8080/auth/",
        "keycloak_realm": "Waldur",
        "keycloak_user_realm": "master",
        "keycloak_username": "admin",
        "keycloak_password": "admin",
        "keycloak_sync_frequency": 15,
    }

    def __init__(self, settings: ServiceSettings):
        self.settings = settings

    @cached_property
    def client(self):
        """
        Construct Rancher REST API client using credentials specified in the service settings.
        """
        rancher_client = client.RancherClient(self.host, verify_ssl=False)
        rancher_client.login(self.settings.username, self.settings.password)
        return rancher_client

    @cached_property
    def host(self):
        if self.settings.backend_url:
            return self.settings.backend_url.strip("/")

    def pull_service_properties(self):
        self.pull_clusters()
        self.pull_projects()
        self.pull_namespaces()
        self.pull_catalogs()
        self.pull_templates()
        self.pull_template_icons()
        self.pull_workloads()
        self.pull_hpas()
        self.pull_apps()
        self.pull_ingresses()
        self.pull_services()

    def pull_clusters(self):
        """
        Mark stale clusters as erred.
        """
        remote_clusters = self.client.list_clusters()
        remote_clusters_map = {item["id"]: item for item in remote_clusters}

        local_clusters_map = {
            cluster.backend_id: cluster
            for cluster in models.Cluster.objects.filter(settings=self.settings)
        }

        stale_ids = set(local_clusters_map.keys()) - set(remote_clusters_map.keys())

        # exclude not yet created clusters
        stale_clusters = models.Cluster.objects.filter(
            settings=self.settings, backend_id__in=stale_ids
        ).exclude(backend_id="")
        stale_clusters.update(state=CoreStates.ERRED, error_message="Resource is gone.")

    def get_kubeconfig_file(self, cluster: models.Cluster):
        return self.client.get_kubeconfig_file(cluster.backend_id)

    def create_cluster(self, cluster: models.Cluster):
        private_registry = None
        private_registry_url = self.settings.get_option("private_registry_url")
        private_registry_user = self.settings.get_option("private_registry_user")
        private_registry_password = self.settings.get_option(
            "private_registry_password"
        )
        k8s_version = cast(str, self.settings.get_option("k8s_version"))
        if private_registry_url and private_registry_user and private_registry_password:
            private_registry = {
                "url": private_registry_url,
                "user": private_registry_user,
                "password": private_registry_password,
            }

        # TODO: come up with a better solution for version suffix
        self.client._base_url = self.client._base_url.replace("/v3", "/v1")
        backend_cluster = self.client.create_cluster(
            cluster.name, k8s_version, private_registry=private_registry
        )
        backend_cluster_id = backend_cluster["id"]
        # as rancher API is not transactional, give it 2s to write cluster state to etcd
        time.sleep(2)
        cluster_id = self.client.get_v3_cluster_id(backend_cluster_id)
        self.client._base_url = self.client._base_url.replace("/v1", "/v3")

        self._backend_cluster_to_cluster(backend_cluster, cluster)
        # Use v3 backend ID as RancherClient supports only v3 API
        cluster.backend_id = cluster_id
        # as rancher API is not transactional, give it 2s to write cluster state to etcd
        time.sleep(2)
        self.client.create_cluster_registration_token(cluster.backend_id)
        cluster.kubernetes_version = k8s_version
        cluster.save()

    def delete_cluster(self, cluster: models.Cluster):
        if cluster.backend_id:
            try:
                self.client.delete_cluster(cluster.backend_id)
            except NotFound:
                logger.debug(
                    "Cluster %s is not present in the backend " % cluster.backend_id
                )

        cluster.delete()

    def delete_node(self, node: models.Node):
        if node.backend_id:
            try:
                self.client.delete_node(node.backend_id)
            except NotFound:
                logger.debug("Node %s is not present in the backend " % node.backend_id)
        node.delete()

    def update_cluster(self, cluster: models.Cluster):
        backend_cluster = self._cluster_to_backend_cluster(cluster)
        self.client.update_cluster(cluster.backend_id, backend_cluster)

    def _backend_cluster_to_cluster(self, backend_cluster, cluster: models.Cluster):
        cluster_type = backend_cluster.get("type", "")
        cluster.backend_id = backend_cluster["id"]
        if cluster_type == "provisioning.cattle.io.cluster":
            cluster.name = backend_cluster["metadata"]["name"]
            cluster.runtime_state = backend_cluster["metadata"]["state"]["name"]
            cluster.kubernetes_version = backend_cluster["spec"].get(
                "kubernetesVersion", ""
            )
        else:
            cluster.name = backend_cluster["name"]
            cluster.runtime_state = backend_cluster["state"]
            cluster.kubernetes_version = backend_cluster["version"]["gitVersion"]
            cluster.capacity = backend_cluster["capacity"]
            cluster.requested = backend_cluster["requested"]

    def _cluster_to_backend_cluster(self, cluster: models.Cluster):
        return {"name": cluster.name}

    def _backend_node_to_node(self, backend_node: dict):
        return {
            "backend_id": backend_node["id"],
            "name": backend_node["requestedHostname"],
            "runtime_state": backend_node.get("state", ""),
        }

    def get_nodes_count(self, remote_cluster: dict):
        spec = remote_cluster.get("appliedSpec", {})
        config = spec.get("rancherKubernetesEngineConfig", {})
        backend_nodes = config.get("nodes", [])
        return len(backend_nodes)

    def get_importable_clusters(self):
        remote_clusters = [
            {
                "type": get_resource_type(models.Cluster),
                "name": cluster["name"],
                "backend_id": cluster["id"],
                "extra": [
                    {"name": "Description", "value": cluster["description"]},
                    {"name": "Number of nodes", "value": self.get_nodes_count(cluster)},
                    {"name": "Created at", "value": cluster["created"]},
                ],
            }
            for cluster in self.client.list_clusters()
            if cluster.get("state") == "active"
        ]
        return self.get_importable_resources(models.Cluster, remote_clusters)

    def import_cluster(self, backend_id, project):
        backend_cluster = self.client.get_cluster(backend_id)

        if not backend_cluster.get("state", "") == models.Cluster.RuntimeStates.ACTIVE:
            raise RancherException("Cannot import K8s cluster in non-active state.")

        cluster = models.Cluster.objects.create(
            backend_id=backend_id,
            service_settings=self.settings,
            project=project,
            vm_project=project,
            state=CoreStates.OK,
            runtime_state=backend_cluster["state"],
            settings=self.settings,
        )
        self.pull_cluster(cluster, backend_cluster)
        return cluster

    def pull_cluster(self, cluster: models.Cluster, backend_cluster=None):
        """
        Pull order is important because subsequent objects depend on previous ones.
        For example, namespaces and catalogs depend on projects.
        """
        self.pull_cluster_details(cluster, backend_cluster)
        self.pull_cluster_nodes(cluster)
        self.pull_projects_for_cluster(cluster)
        self.pull_namespaces_for_cluster(cluster)
        self.pull_catalogs_for_cluster(cluster)
        self.pull_templates_for_cluster(cluster)
        self.pull_cluster_workloads(cluster)
        self.pull_cluster_hpas(cluster)
        self.pull_cluster_apps(cluster)
        self.pull_cluster_ingresses(cluster)

    def pull_cluster_details(self, cluster: models.Cluster, backend_cluster=None):
        backend_cluster = backend_cluster or self.client.get_cluster(cluster.backend_id)
        self._backend_cluster_to_cluster(backend_cluster, cluster)
        cluster.save()

    def pull_cluster_nodes(self, cluster: models.Cluster):
        backend_nodes = self.get_cluster_nodes(cluster.backend_id)

        for backend_node in backend_nodes:
            # If the node has not been requested from Waldur, so it will be created
            node, created = models.Node.objects.get_or_create(
                name=backend_node["name"],
                cluster=cluster,
                defaults=dict(
                    backend_id=backend_node["backend_id"],
                ),
            )

            if not node.backend_id:
                # If the node has been requested from Waldur, but it has not been synchronized
                node.backend_id = backend_node["backend_id"]
                node.save(update_fields=["backend_id"])

            # Update details in all cases.
            self.pull_node(node)

        # Update nodes states.
        utils.update_cluster_nodes_states(cluster.id)

    def check_cluster_nodes(self, cluster: models.Cluster):
        self.pull_cluster_details(cluster)

        if cluster.runtime_state == models.Cluster.RuntimeStates.ACTIVE:
            # We don't need change cluster state here, because it will make in an executor.
            return

        for node in cluster.node_set.filter(role=SERVER_ROLE):
            vm = cast(Instance, node.instance)
            if vm.state not in [
                CoreStates.ERRED,
                CoreStates.DELETING,
                CoreStates.DELETION_SCHEDULED,
            ]:
                # Return if one or more VMs with 'server' role exist
                # and they haven't a state 'error' or 'delete'.
                # Here 'return' means that cluster state checking must be retry later.
                return

        cluster.error_message = (
            "The cluster is not connected with any "
            "non-failed VM's with 'server' role."
        )
        cluster.runtime_state = "error"
        cluster.save()

    def get_cluster_nodes(self, backend_id):
        nodes = self.client.get_cluster_nodes(backend_id)
        return [self._backend_node_to_node(node) for node in nodes]

    def node_is_active(self, backend_id):
        backend_node = self.client.get_node(backend_id)
        return backend_node["state"] == models.Node.RuntimeStates.ACTIVE

    def pull_node(self, node: models.Node):
        if not node.backend_id:
            return

        backend_node = self.client.get_node(node.backend_id)

        # rancher can skip return of some fields when node is being created,
        # so avoid crashing by supporting missing values
        def get_backend_node_field(*args):
            value = backend_node

            for arg in args:
                if isinstance(value, dict):
                    value = value.get(arg)
                else:
                    return

            return value

        def update_node_field(*args, field):
            value = get_backend_node_field(*args)
            if value:
                setattr(node, field, value)

        update_node_field("labels", field="labels")
        update_node_field("annotations", field="annotations")
        update_node_field("info", "os", "dockerVersion", field="docker_version")
        update_node_field("info", "kubernetes", "kubeletVersion", field="k8s_version")
        cpu_allocated = get_backend_node_field("requested", "cpu")

        if cpu_allocated:
            node.cpu_allocated = (
                core_utils.parse_int(cpu_allocated) / 1000
            )  # convert data from 380m to 0.38

        ram_allocated = get_backend_node_field("requested", "memory")
        update_node_field("allocatable", "cpu", field="cpu_total")

        if ram_allocated:
            node.ram_allocated = int(
                core_utils.parse_int(ram_allocated) / 2**20
            )  # convert data to Mi

        ram_total = get_backend_node_field("allocatable", "memory")

        if ram_total:
            node.ram_total = int(
                core_utils.parse_int(ram_total) / 2**20
            )  # convert data to Mi

        update_node_field("requested", "pods", field="pods_allocated")
        update_node_field("allocatable", "pods", field="pods_total")
        update_node_field("state", field="runtime_state")

        return node.save()

    def drain_node(self, node: models.Node):
        return self.client.drain_node(node.backend_id)

    def get_node_drain_status(self, node: models.Node):
        backend_node = self.client.get_node(node.backend_id)
        conditions = backend_node.get("status", {}).get("conditions", [])
        condition = next(
            condition for condition in conditions if condition["type"] == "Drained"
        )
        if not condition:
            return "unknown"
        if condition["status"] == "True":
            return "ok"
        if condition["transitioning"]:
            return "pending"
        if condition["error"]:
            return "error"

    def get_or_create_cluster_group_role(self, group_id, cluster_id, role):
        if not self.client.get_cluster_group_role(group_id, cluster_id, role):
            logger.info(
                "Creating group role binding %s in cluster %s and role %s",
                group_id,
                cluster_id,
                role,
            )
            self.client.create_cluster_group_role(group_id, cluster_id, role)
            return True
        return False

    def delete_cluster_group_role(self, group_id, cluster_id, role):
        existing_bindings = self.client.get_cluster_group_role(
            group_id, cluster_id, role
        )
        if existing_bindings:
            binding_name = existing_bindings[0]["name"]
            logger.info("Deleting cluster group role binding %s", binding_name)
            self.client.delete_cluster_group_role(cluster_id, binding_name)
            return True
        return False

    def delete_project_group_role(self, group_id: str, project_id: str, role):
        existing_bindings = self.client.get_project_group_role(
            group_id, project_id, role
        )
        # Expects project_id to be in the format of 'cluster_id:project_id'
        project_id = project_id.split(":")[-1]
        if existing_bindings:
            binding_name = existing_bindings[0]["name"]
            logger.info("Deleting project group role binding %s", binding_name)
            self.client.delete_project_group_role(project_id, binding_name)
            return True
        return False

    def get_or_create_project_group_role(self, group_id, project_id, role):
        if not self.client.get_project_group_role(group_id, project_id, role):
            logger.info(
                "Creating group role binding %s in project %s and role %s",
                group_id,
                project_id,
                role,
            )
            self.client.create_project_group_role(group_id, project_id, role)
            return True
        return False

    def list_roles(self) -> list[dict]:
        return self.client.list_role_templates()

    def pull_catalogs_for_cluster(self, cluster: models.Cluster):
        self.pull_cluster_catalogs_for_cluster(cluster)
        self.pull_project_catalogs_for_cluster(cluster)

    def pull_cluster_catalogs_for_cluster(self, cluster: models.Cluster):
        remote_catalogs = self.client.list_cluster_catalogs(cluster.backend_id)
        self.pull_catalogs_for_scope(remote_catalogs, cluster)

    def pull_project_catalogs_for_cluster(self, cluster: models.Cluster):
        for project in models.Project.objects.filter(cluster=cluster):
            self.pull_project_catalogs_for_project(project)

    def pull_project_catalogs_for_project(self, project):
        remote_catalogs = self.client.list_project_catalogs(project.backend_id)
        self.pull_catalogs_for_scope(remote_catalogs, project)

    def pull_catalogs(self):
        self.pull_global_catalogs()
        self.pull_cluster_catalogs()
        self.pull_project_catalogs()

    def pull_global_catalogs(self):
        remote_catalogs = self.client.list_global_catalogs()
        self.pull_catalogs_for_scope(remote_catalogs, self.settings)

    def pull_cluster_catalogs(self):
        remote_catalogs = self.client.list_cluster_catalogs()
        for cluster in models.Cluster.objects.filter(settings=self.settings):
            self.pull_catalogs_for_scope(remote_catalogs, cluster)

    def pull_project_catalogs(self):
        remote_catalogs = self.client.list_project_catalogs()
        for project in models.Project.objects.filter(settings=self.settings):
            self.pull_catalogs_for_scope(remote_catalogs, project)

    def pull_catalogs_for_scope(self, remote_catalogs, scope):
        content_type = ContentType.objects.get_for_model(scope)
        local_catalogs = models.Catalog.objects.filter(
            content_type=content_type,
            object_id=scope.id,
        )

        remote_catalog_map = {
            catalog["id"]: self.remote_catalog_to_local(catalog, content_type, scope.id)
            for catalog in remote_catalogs
        }
        local_catalog_map = {catalog.backend_id: catalog for catalog in local_catalogs}
        remote_catalog_ids = set(remote_catalog_map.keys())
        local_catalog_ids = set(local_catalog_map.keys())

        stale_catalogs = local_catalog_ids - remote_catalog_ids

        new_catalogs = [
            remote_catalog_map[catalog_id]
            for catalog_id in remote_catalog_ids - local_catalog_ids
        ]

        existing_catalogs = remote_catalog_ids & local_catalog_ids
        pulled_fields = {
            "name",
            "description",
            "catalog_url",
            "branch",
            "commit",
            "username",
            "password",
            "runtime_state",
        }
        for catalog_id in existing_catalogs:
            local_catalog = local_catalog_map[catalog_id]
            remote_catalog = remote_catalog_map[catalog_id]
            update_pulled_fields(local_catalog, remote_catalog, pulled_fields)

        models.Catalog.objects.bulk_create(new_catalogs)
        local_catalogs.filter(backend_id__in=stale_catalogs).delete()

    def remote_catalog_to_local(self, remote_catalog, content_type, object_id):
        return models.Catalog(
            content_type=content_type,
            object_id=object_id,
            backend_id=remote_catalog["id"],
            name=remote_catalog["name"],
            description=remote_catalog["description"],
            created=parse_datetime(remote_catalog["created"]),
            catalog_url=remote_catalog["url"],
            branch=remote_catalog["branch"],
            commit=remote_catalog.get("commit", ""),
            username=remote_catalog.get("username", ""),
            password=remote_catalog.get("password", ""),
            runtime_state=remote_catalog["state"],
            settings=self.settings,
        )

    def refresh_catalog(self, catalog: models.Catalog):
        if isinstance(catalog.scope, ServiceSettings):
            return self.client.refresh_global_catalog(catalog.backend_id)
        elif isinstance(catalog.scope, models.Cluster):
            return self.client.refresh_cluster_catalog(catalog.backend_id)
        else:
            return self.client.refresh_project_catalog(catalog.backend_id)

    def delete_catalog(self, catalog: models.Catalog):
        try:
            if isinstance(catalog.scope, ServiceSettings):
                return self.client.delete_global_catalog(catalog.backend_id)
            elif isinstance(catalog.scope, models.Cluster):
                return self.client.delete_cluster_catalog(catalog.backend_id)
            else:
                return self.client.delete_project_catalog(catalog.backend_id)
        except NotFound:
            logger.debug(
                "Catalog %s is not present in the backend ", catalog.backend_id
            )

    def get_catalog_spec(self, catalog: models.Catalog):
        spec = {
            "name": catalog.name,
            "description": catalog.description,
            "url": catalog.catalog_url,
            "branch": catalog.branch,
        }
        if catalog.username:
            spec["username"] = catalog.username
        if catalog.password:
            spec["password"] = catalog.password
        return spec

    def create_catalog(self, catalog: models.Catalog):
        spec = self.get_catalog_spec(catalog)

        if isinstance(catalog.scope, ServiceSettings):
            remote_catalog = self.client.create_global_catalog(spec)
        elif isinstance(catalog.scope, models.Cluster):
            spec["clusterId"] = catalog.scope.backend_id
            remote_catalog = self.client.create_cluster_catalog(spec)
        else:
            project = cast(models.Project, catalog.scope)
            spec["projectId"] = project.backend_id
            remote_catalog = self.client.create_project_catalog(spec)

        catalog.backend_id = remote_catalog["id"]
        catalog.runtime_state = remote_catalog["state"]
        catalog.save()

    def update_catalog(self, catalog: models.Catalog):
        spec = self.get_catalog_spec(catalog)
        if isinstance(catalog.scope, ServiceSettings):
            return self.client.update_global_catalog(catalog.backend_id, spec)
        elif isinstance(catalog.scope, models.Cluster):
            return self.client.update_cluster_catalog(catalog.backend_id, spec)
        else:
            return self.client.update_project_catalog(catalog.backend_id, spec)

    def pull_projects_for_cluster(self, cluster: models.Cluster):
        """
        Pull projects for one cluster. It is used for cluster synchronization.
        """
        remote_projects = self.client.list_projects(cluster.backend_id)
        local_projects = models.Project.objects.filter(cluster=cluster)
        local_clusters = [cluster]
        self._pull_projects(local_clusters, local_projects, remote_projects)

    def pull_projects(self):
        """
        Pull projects for all clusters. It is used for provider synchronization.
        """
        remote_projects = self.client.list_projects()
        local_projects = models.Project.objects.filter(settings=self.settings)
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
        self._pull_projects(local_clusters, local_projects, remote_projects)

    def _pull_projects(self, local_clusters, local_projects, remote_projects):
        """
        This private method pulls projects for given clusters and projects.
        """
        local_cluster_map = {cluster.backend_id: cluster for cluster in local_clusters}
        remote_project_map = {
            project["id"]: self.remote_project_to_local(project, local_cluster_map)
            for project in remote_projects
        }
        local_project_map = {project.backend_id: project for project in local_projects}
        remote_project_ids = set(remote_project_map.keys())
        local_project_ids = set(local_project_map.keys())

        stale_projects = local_project_ids - remote_project_ids

        new_projects = [
            remote_project_map[project_id]
            for project_id in remote_project_ids - local_project_ids
        ]

        existing_projects = remote_project_ids & local_project_ids
        pulled_fields = {
            "name",
            "description",
            "runtime_state",
            "cluster",
        }
        for project_id in existing_projects:
            local_project = local_project_map[project_id]
            remote_project = remote_project_map[project_id]
            update_pulled_fields(local_project, remote_project, pulled_fields)

        models.Project.objects.bulk_create(new_projects)
        local_projects.filter(backend_id__in=stale_projects).delete()

    def remote_project_to_local(self, remote_project, local_cluster_map):
        return models.Project(
            backend_id=remote_project["id"],
            name=remote_project["name"],
            description=remote_project["description"],
            created=parse_datetime(remote_project["created"]),
            runtime_state=remote_project["state"],
            cluster=local_cluster_map.get(remote_project["clusterId"]),
            settings=self.settings,
        )

    def pull_namespaces(self):
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
        for cluster in local_clusters:
            if cluster.state == CoreStates.OK:
                self.pull_namespaces_for_cluster(cluster)
            else:
                logger.debug(
                    "Skipping namespace pulling for cluster with backend ID %s"
                    "because otherwise one failed cluster leads to provider failure",
                    cluster.backend_id,
                )

    def pull_namespaces_for_cluster(self, cluster: models.Cluster):
        remote_namespaces = self.client.list_namespaces(cluster.backend_id)
        local_namespaces = models.Namespace.objects.filter(project__cluster=cluster)
        local_projects = models.Project.objects.filter(cluster=cluster)

        local_project_map = {project.backend_id: project for project in local_projects}
        remote_namespace_map = {
            namespace["id"]: self.remote_namespace_to_local(
                namespace, local_project_map
            )
            for namespace in remote_namespaces
        }
        local_namespace_map = {
            namespace.backend_id: namespace for namespace in local_namespaces
        }
        remote_namespace_ids = set(remote_namespace_map.keys())
        local_namespace_ids = set(local_namespace_map.keys())

        stale_namespaces = local_namespace_ids - remote_namespace_ids

        new_namespaces = [
            remote_namespace_map[namespace_id]
            for namespace_id in remote_namespace_ids - local_namespace_ids
        ]

        existing_namespaces = remote_namespace_ids & local_namespace_ids
        pulled_fields = {
            "name",
            "runtime_state",
            "project",
        }
        for namespace_id in existing_namespaces:
            local_namespace = local_namespace_map[namespace_id]
            remote_namespace = remote_namespace_map[namespace_id]
            update_pulled_fields(local_namespace, remote_namespace, pulled_fields)

        models.Namespace.objects.bulk_create(new_namespaces)
        local_namespaces.filter(backend_id__in=stale_namespaces).delete()

    def remote_namespace_to_local(self, remote_namespace, local_project_map):
        return models.Namespace(
            backend_id=remote_namespace["id"],
            name=remote_namespace["name"],
            created=parse_datetime(remote_namespace["created"]),
            runtime_state=remote_namespace["state"],
            project=local_project_map.get(remote_namespace["projectId"]),
            settings=self.settings,
        )

    def pull_templates_for_cluster(self, cluster: models.Cluster):
        remote_templates = self.client.list_templates(cluster.backend_id)
        local_templates = models.Template.objects.filter(cluster=cluster)
        content_type = ContentType.objects.get_for_model(cluster)
        local_catalogs = models.Catalog.objects.filter(
            content_type=content_type, object_id=cluster.id
        )
        local_clusters = [cluster]
        local_projects = models.Project.objects.filter(cluster=cluster)
        self._pull_templates(
            local_templates,
            local_catalogs,
            local_clusters,
            local_projects,
            remote_templates,
        )

    def pull_templates(self):
        remote_templates = self.client.list_templates()
        local_templates = models.Template.objects.filter(settings=self.settings)
        local_catalogs = models.Catalog.objects.filter(settings=self.settings)
        local_clusters = list(models.Cluster.objects.filter(settings=self.settings))
        local_projects = models.Project.objects.filter(settings=self.settings)
        self._pull_templates(
            local_templates,
            local_catalogs,
            local_clusters,
            local_projects,
            remote_templates,
        )

    def _pull_templates(
        self,
        local_templates: QuerySet[models.Template],
        local_catalogs: QuerySet[models.Catalog],
        local_clusters: list[models.Cluster],
        local_projects: QuerySet[models.Project],
        remote_templates: list[dict],
    ):
        local_catalog_map = {catalog.backend_id: catalog for catalog in local_catalogs}
        local_cluster_map = {cluster.backend_id: cluster for cluster in local_clusters}
        local_project_map = {project.backend_id: project for project in local_projects}
        local_template_map = {
            template.backend_id: template for template in local_templates
        }
        remote_template_map = {
            template["id"]: self.remote_template_to_local(
                template, local_catalog_map, local_cluster_map, local_project_map
            )
            for template in remote_templates
        }
        remote_template_ids = set(remote_template_map.keys())
        local_template_ids = set(local_template_map.keys())

        stale_templates = local_template_ids - remote_template_ids

        new_templates = [
            remote_template_map[template_id]
            for template_id in remote_template_ids - local_template_ids
        ]

        existing_templates = remote_template_ids & local_template_ids
        pulled_fields = {
            "name",
            "description",
            "runtime_state",
            "project_url",
            "icon_url",
            "default_version",
            "versions",
            "catalog",
            "cluster",
            "project",
        }
        for template_id in existing_templates:
            local_template = local_template_map[template_id]
            remote_template = remote_template_map[template_id]
            update_pulled_fields(local_template, remote_template, pulled_fields)

        models.Template.objects.bulk_create(new_templates)
        local_templates.filter(backend_id__in=stale_templates).delete()

    def remote_template_to_local(
        self, remote_template, local_catalog_map, local_cluster_map, local_project_map
    ):
        catalog_id = remote_template["catalogId"] or remote_template["clusterCatalogId"]
        return models.Template(
            backend_id=remote_template["id"],
            name=remote_template["name"],
            description=remote_template["description"],
            created=parse_datetime(remote_template["created"]),
            runtime_state=remote_template["state"],
            icon_url=remote_template["links"]["icon"],
            project_url=remote_template.get("projectURL", ""),
            default_version=remote_template["defaultVersion"],
            versions=list(remote_template["versionLinks"].keys()),
            catalog=local_catalog_map.get(catalog_id),
            cluster=local_cluster_map.get(remote_template["clusterId"]),
            project=local_project_map.get(remote_template["projectId"]),
            settings=self.settings,
        )

    def _get_external_template_icon(self, icon_url) -> bytes | None:
        try:
            response = requests.get(icon_url, timeout=3)
        except requests.RequestException as e:
            logger.debug(f"Failed to get {icon_url}: {e}")
            return None

        status_code = response.status_code
        if status_code == requests.codes.ok:  # only care about the positive case
            return response.content
        else:
            return None

    def pull_template_icons(self):
        for template in models.Template.objects.filter(settings=self.settings):
            content = self.client.get_template_icon(template.backend_id)
            if (
                not content
                and template.icon_url
                and not urlparse(template.icon_url).netloc == urlparse(self.host).netloc
            ):
                # try to download icon from the icon_url field
                logger.debug(
                    "Rancher did not return icon for a Template, trying with external URL"
                )
                content = self._get_external_template_icon(template.icon_url)
            if not content:
                # Clear icon field so that default icon would be rendered
                template.icon.delete()
                template.save()
                continue
            extension = guess_image_extension(content)
            if not extension:
                continue
            # Overwrite existing file
            if template.icon:
                template.icon.delete()
            template.icon.save(f"{template.uuid}.{extension}", ContentFile(content))

    def list_project_secrets(self, project: models.Project):
        return self.client.list_project_secrets(project.backend_id)

    def pull_cluster_workloads(self, cluster: models.Cluster):
        for project in models.Project.objects.filter(cluster=cluster):
            self.pull_project_workloads(project)

    def pull_workloads(self):
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
        for cluster in local_clusters:
            if cluster.state == CoreStates.OK:
                self.pull_cluster_workloads(cluster)
            else:
                logger.debug(
                    "Skipping workload pulling for cluster with backend ID %s"
                    "because otherwise one failed cluster leads to provider failure",
                    cluster.backend_id,
                )

    def pull_project_workloads(self, project: models.Project):
        remote_workloads = self.client.list_workloads(project.backend_id)
        local_workloads = models.Workload.objects.filter(project=project)
        local_namespaces = models.Namespace.objects.filter(project=project)

        local_namespaces_map = {
            namespace.backend_id: namespace for namespace in local_namespaces
        }
        remote_workload_map = {
            workload["id"]: self.remote_workload_to_local(
                workload, project, local_namespaces_map
            )
            for workload in remote_workloads
        }
        local_workload_map = {
            workload.backend_id: workload for workload in local_workloads
        }
        remote_workload_ids = set(remote_workload_map.keys())
        local_workload_ids = set(local_workload_map.keys())

        stale_workloads = local_workload_ids - remote_workload_ids

        new_workloads = [
            remote_workload_map[workload_id]
            for workload_id in remote_workload_ids - local_workload_ids
        ]

        existing_workloads = remote_workload_ids & local_workload_ids
        pulled_fields = {
            "name",
            "runtime_state",
            "scale",
        }
        for workload_id in existing_workloads:
            local_workload = local_workload_map[workload_id]
            remote_workload = remote_workload_map[workload_id]
            update_pulled_fields(local_workload, remote_workload, pulled_fields)

        models.Workload.objects.bulk_create(new_workloads)
        local_workloads.filter(backend_id__in=stale_workloads).delete()

    def remote_workload_to_local(
        self, remote_workload, project: models.Project, local_namespaces_map
    ):
        return models.Workload(
            backend_id=remote_workload["id"],
            name=remote_workload["name"],
            created=parse_datetime(remote_workload["created"]),
            runtime_state=remote_workload["state"],
            project=project,
            cluster=project.cluster,
            settings=self.settings,
            namespace=local_namespaces_map.get(remote_workload["namespaceId"]),
            scale=remote_workload.get("scale", 0),
        )

    def redeploy_workload(self, workload: models.Workload):
        if not workload.project:
            raise RancherException(
                f"Workload {workload.backend_id} does not have a project."
            )
        self.client.redeploy_workload(workload.project.backend_id, workload.backend_id)

    def delete_workload(self, workload: models.Workload):
        if not workload.project:
            raise RancherException(
                f"Workload {workload.backend_id} does not have a project."
            )
        self.client.delete_workload(workload.project.backend_id, workload.backend_id)

    def get_workload_yaml(self, workload: models.Workload):
        if not workload.project:
            raise RancherException(
                f"Workload {workload.backend_id} does not have a project."
            )
        return self.client.get_workload_yaml(
            workload.project.backend_id, workload.backend_id
        )

    def put_workload_yaml(self, workload: models.Workload, yaml: str):
        if not workload.project:
            raise RancherException(
                f"Workload {workload.backend_id} does not have a project."
            )
        return self.client.put_workload_yaml(
            workload.project.backend_id, workload.backend_id, yaml
        )

    def pull_cluster_hpas(self, cluster: models.Cluster):
        for project in models.Project.objects.filter(cluster=cluster):
            self.pull_project_hpas(project)

    def pull_hpas(self):
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
        for cluster in local_clusters:
            if cluster.state == CoreStates.OK:
                self.pull_cluster_hpas(cluster)
            else:
                logger.debug(
                    "Skipping HPA pulling for cluster with backend ID %s"
                    "because otherwise one failed cluster leads to provider failure",
                    cluster.backend_id,
                )

    def pull_project_hpas(self, project: models.Project):
        local_workloads = models.Workload.objects.filter(project=project)
        local_workloads_map = {
            workload.backend_id: workload for workload in local_workloads
        }

        local_hpas = models.HPA.objects.filter(project=project)
        local_hpa_map = {hpa.backend_id: hpa for hpa in local_hpas}

        remote_hpas = self.client.list_hpas(project.backend_id)
        remote_hpa_map = {
            hpa["id"]: self.remote_hpa_to_local(hpa, local_workloads_map)
            for hpa in remote_hpas
        }

        remote_hpa_ids = set(remote_hpa_map.keys())
        local_hpa_ids = set(local_hpa_map.keys())

        stale_hpas = local_hpa_ids - remote_hpa_ids

        new_hpas = [remote_hpa_map[hpa_id] for hpa_id in remote_hpa_ids - local_hpa_ids]

        existing_hpas = remote_hpa_ids & local_hpa_ids
        pulled_fields = {
            "name",
            "runtime_state",
            "current_replicas",
            "desired_replicas",
            "min_replicas",
            "max_replicas",
            "metrics",
        }
        for hpa_id in existing_hpas:
            local_hpa = local_hpa_map[hpa_id]
            remote_hpa = remote_hpa_map[hpa_id]
            update_pulled_fields(local_hpa, remote_hpa, pulled_fields)

        models.HPA.objects.bulk_create(new_hpas)
        local_hpas.filter(backend_id__in=stale_hpas).delete()

    def remote_hpa_to_local(self, remote_hpa, local_workloads_map):
        workload = local_workloads_map[remote_hpa["workloadId"]]
        return models.HPA(
            backend_id=remote_hpa["id"],
            name=remote_hpa["name"],
            created=parse_datetime(remote_hpa["created"]),
            runtime_state=remote_hpa["state"],
            project=workload.project,
            cluster=workload.cluster,
            settings=self.settings,
            namespace=workload.namespace,
            current_replicas=remote_hpa["currentReplicas"],
            desired_replicas=remote_hpa["desiredReplicas"],
            min_replicas=remote_hpa["minReplicas"],
            max_replicas=remote_hpa["maxReplicas"],
            metrics=remote_hpa["metrics"],
            state=CoreStates.OK,
        )

    def create_hpa(self, hpa: models.HPA):
        if not hpa.project:
            raise RancherException(f"HPA {hpa.backend_id} does not have a project.")

        if not hpa.workload:
            raise RancherException(f"HPA {hpa.backend_id} does not have a workload.")

        if not hpa.namespace:
            raise RancherException(f"HPA {hpa.backend_id} does not have a workload.")

        remote_hpa = self.client.create_hpa(
            hpa.project.backend_id,
            hpa.namespace.backend_id,
            hpa.workload.backend_id,
            hpa.name,
            hpa.description,
            hpa.min_replicas,
            hpa.max_replicas,
            hpa.metrics,
        )
        hpa.backend_id = remote_hpa["id"]
        hpa.runtime_state = remote_hpa["state"]
        hpa.save(update_fields=["backend_id", "runtime_state"])

    def update_hpa(self, hpa: models.HPA):
        if not hpa.project:
            raise RancherException(f"HPA {hpa.backend_id} does not have a project.")

        if not hpa.workload:
            raise RancherException(f"HPA {hpa.backend_id} does not have a workload.")

        if not hpa.namespace:
            raise RancherException(f"HPA {hpa.backend_id} does not have a workload.")

        self.client.update_hpa(
            hpa.project.backend_id,
            hpa.backend_id,
            hpa.namespace.backend_id,
            hpa.workload.backend_id,
            hpa.name,
            hpa.description,
            hpa.min_replicas,
            hpa.max_replicas,
            hpa.metrics,
        )

    def delete_hpa(self, hpa):
        try:
            self.client.delete_hpa(hpa.project.backend_id, hpa.backend_id)
        except NotFound:
            logger.debug("HPA %s is not present in the backend." % hpa.backend_id)

    def get_hpa_yaml(self, hpa: models.HPA):
        if not hpa.project:
            raise RancherException(f"HPA {hpa.backend_id} does not have a project.")
        return self.client.get_hpa_yaml(hpa.project.backend_id, hpa.backend_id)

    def put_hpa_yaml(self, hpa: models.HPA, yaml: str):
        if not hpa.project:
            raise RancherException(f"HPA {hpa.backend_id} does not have a project.")
        return self.client.put_hpa_yaml(hpa.project.backend_id, hpa.backend_id, yaml)

    def pull_apps(self):
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
        for cluster in local_clusters:
            if cluster.state == CoreStates.OK:
                self.pull_cluster_apps(cluster)
            else:
                logger.debug(
                    "Skipping apps pulling for cluster with backend ID %s"
                    "because otherwise one failed cluster leads to provider failure",
                    cluster.backend_id,
                )

    def pull_cluster_apps(self, cluster: models.Cluster):
        for project in models.Project.objects.filter(cluster=cluster):
            self.pull_project_apps(project)

    def pull_project_apps(self, project: models.Project):
        local_namespaces = models.Namespace.objects.filter(project=project)
        local_namespaces_map = {
            namespace.backend_id: namespace for namespace in local_namespaces
        }

        local_apps = models.Application.objects.filter(rancher_project=project)
        local_app_map = {app.backend_id: app for app in local_apps}

        remote_apps = self.client.get_project_applications(project.backend_id)
        remote_app_map = {
            app["id"]: self.remote_app_to_local(app, project, local_namespaces_map)
            for app in remote_apps
        }

        remote_app_ids = set(remote_app_map.keys())
        local_app_ids = set(local_app_map.keys())

        stale_apps = local_app_ids - remote_app_ids

        new_apps = [remote_app_map[app_id] for app_id in remote_app_ids - local_app_ids]

        existing_apps = remote_app_ids & local_app_ids
        pulled_fields = {
            "name",
            "runtime_state",
            "answers",
        }
        for app_id in existing_apps:
            local_app = local_app_map[app_id]
            remote_app = remote_app_map[app_id]
            update_pulled_fields(local_app, remote_app, pulled_fields)

        models.Application.objects.bulk_create(new_apps)
        local_apps.filter(backend_id__in=stale_apps).delete()

    def remote_app_to_local(
        self, remote_app, rancher_project: models.Project, local_namespaces_map
    ):
        if not rancher_project.cluster:
            raise RancherException(
                f"Application {remote_app['id']} does not have a cluster."
            )
        parts = urlparse(remote_app["externalId"])
        params = parse_qs(parts.query)

        template = models.Template.objects.get(
            settings=self.settings,
            name=params["template"][0],
            catalog__name=params["catalog"][0],
        )

        return models.Application(
            settings=self.settings,
            service_settings=rancher_project.cluster.service_settings,
            project=rancher_project.cluster.project,
            rancher_project=rancher_project,
            cluster=rancher_project.cluster,
            namespace=local_namespaces_map.get(remote_app["targetNamespace"]),
            template=template,
            name=remote_app["name"],
            runtime_state=remote_app["state"],
            created=remote_app["created"],
            backend_id=remote_app["id"],
            answers=remote_app.get("answers", {}),
            version=params["version"][0],
        )

    def create_app(self, app: models.Application):
        if not app.rancher_project.cluster:
            raise RancherException(
                f"Application {app.backend_id} does not have a cluster."
            )
        if not app.template.catalog:
            raise RancherException(
                f"Application {app.backend_id} does not have a catalog."
            )
        if not app.namespace.backend_id:
            remote_response = self.client.create_namespace(
                app.rancher_project.cluster.backend_id,
                app.rancher_project.backend_id,
                app.namespace.name,
            )
            app.namespace.backend_id = remote_response["id"]
            app.namespace.save()

        remote_app = self.client.create_application(
            app.template.catalog.backend_id,
            app.template.name,
            app.version,
            app.rancher_project.backend_id,
            app.namespace.backend_id,
            app.name,
            app.answers,
        )
        app.backend_id = remote_app["id"]
        app.runtime_state = remote_app["state"]
        app.save()

    def check_application_state(self, app: models.Application):
        remote_app = self.client.get_application(
            app.rancher_project.backend_id, app.backend_id
        )
        app.runtime_state = remote_app["state"]
        app.save()

    def delete_app(self, app: models.Application):
        try:
            self.client.destroy_application(
                app.rancher_project.backend_id, app.backend_id
            )
        except NotFound:
            logger.debug("App %s is not present in the backend." % app.backend_id)

    def pull_ingresses(self):
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
        for cluster in local_clusters:
            if cluster.state == CoreStates.OK:
                self.pull_cluster_ingresses(cluster)
            else:
                logger.debug(
                    "Skipping ingresses pulling for cluster with backend ID %s"
                    "because otherwise one failed cluster leads to provider failure",
                    cluster.backend_id,
                )

    def pull_cluster_ingresses(self, cluster: models.Cluster):
        for project in models.Project.objects.filter(cluster=cluster):
            self.pull_project_ingresses(project)

    def pull_project_ingresses(self, project: models.Project):
        remote_ingresses = self.client.list_ingresses(project.backend_id)
        local_ingresses = models.Ingress.objects.filter(rancher_project=project)
        local_namespaces = models.Namespace.objects.filter(project=project)

        local_namespaces_map = {
            namespace.backend_id: namespace for namespace in local_namespaces
        }
        remote_ingress_map = {
            ingress["id"]: self.remote_ingress_to_local(ingress, local_namespaces_map)
            for ingress in remote_ingresses
        }
        remote_ingress_map = {
            ingress_id: ingress
            for ingress_id, ingress in remote_ingress_map.items()
            if ingress is not None
        }
        local_ingress_map = {ingress.backend_id: ingress for ingress in local_ingresses}
        remote_ingress_ids = set(remote_ingress_map.keys())
        local_ingress_ids = set(local_ingress_map.keys())

        stale_ingresses = local_ingress_ids - remote_ingress_ids

        new_ingresses = [
            remote_ingress_map[ingress_id]
            for ingress_id in remote_ingress_ids - local_ingress_ids
        ]

        existing_ingresses = remote_ingress_ids & local_ingress_ids
        pulled_fields = {
            "name",
            "runtime_state",
            "rules",
        }
        for ingress_id in existing_ingresses:
            local_ingress = local_ingress_map[ingress_id]
            remote_ingress = remote_ingress_map[ingress_id]
            update_pulled_fields(local_ingress, remote_ingress, pulled_fields)

        models.Ingress.objects.bulk_create(new_ingresses)
        local_ingresses.filter(backend_id__in=stale_ingresses).delete()

    def remote_ingress_to_local(
        self, remote_ingress, local_namespaces_map: dict[str, models.Namespace]
    ):
        namespace = local_namespaces_map.get(remote_ingress["namespaceId"])
        if not namespace:
            logger.debug(
                "Namespace %s is not present in the local database. "
                "Skipping ingress %s",
                remote_ingress["namespaceId"],
                remote_ingress["id"],
            )
            return None
        if not namespace.project:
            logger.debug(
                "Project is not present in the local database. Skipping ingress %s",
                remote_ingress["id"],
            )
            return None
        if not namespace.project.cluster:
            logger.debug(
                "Cluster is not present in the local database. Skipping ingress %s",
                remote_ingress["id"],
            )
            return None
        return models.Ingress(
            backend_id=remote_ingress["id"],
            name=remote_ingress["name"],
            created=parse_datetime(remote_ingress["created"]),
            runtime_state=remote_ingress["state"],
            settings=self.settings,
            service_settings=namespace.project.cluster.service_settings,
            project=namespace.project.cluster.project,
            namespace=namespace,
            cluster=namespace.project.cluster,
            rancher_project=namespace.project,
            rules=remote_ingress["rules"],
            state=CoreStates.OK,
        )

    def get_ingress_yaml(self, ingress: models.Ingress):
        return self.client.get_ingress_yaml(
            ingress.rancher_project.backend_id, ingress.backend_id
        )

    def put_ingress_yaml(self, ingress: models.Ingress, yaml: str):
        return self.client.put_ingress_yaml(
            ingress.rancher_project.backend_id, ingress.backend_id, yaml
        )

    def delete_ingress(self, ingress: models.Ingress):
        return self.client.delete_ingress(
            ingress.rancher_project.backend_id, ingress.backend_id
        )

    def pull_services(self):
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
        for cluster in local_clusters:
            if cluster.state == CoreStates.OK:
                self.pull_cluster_services(cluster)
            else:
                logger.debug(
                    "Skipping services pulling for cluster with backend ID %s"
                    "because otherwise one failed cluster leads to provider failure",
                    cluster.backend_id,
                )

    def pull_cluster_services(self, cluster: models.Cluster):
        for project in models.Project.objects.filter(cluster=cluster):
            self.pull_project_services(project)

    def pull_project_services(self, project: models.Project):
        remote_services = self.client.list_services(project.backend_id)
        local_services = models.Service.objects.filter(namespace__project=project)
        local_namespaces = models.Namespace.objects.filter(project=project)
        local_workloads = models.Workload.objects.filter(project=project)

        local_namespaces_map = {
            namespace.backend_id: namespace for namespace in local_namespaces
        }
        local_workloads_map = {
            workload.backend_id: workload for workload in local_workloads
        }
        remote_service_map = {service["id"]: service for service in remote_services}
        local_service_map = {service.backend_id: service for service in local_services}
        remote_service_ids = set(remote_service_map.keys())
        local_service_ids = set(local_service_map.keys())

        stale_services = local_service_ids - remote_service_ids

        new_services = [
            remote_service_map[service_id]
            for service_id in remote_service_ids - local_service_ids
        ]

        existing_services = remote_service_ids & local_service_ids
        for service_id in existing_services:
            local_service = local_service_map[service_id]
            remote_service = remote_service_map[service_id]
            update_fields = set()
            if remote_service["name"] != local_service.name:
                local_service.name = remote_service["name"]
                update_fields.add("name")
            if remote_service["state"] != local_service.runtime_state:
                local_service.runtime_state = remote_service["state"]
                update_fields.add("runtime_state")
            if remote_service.get("selector") != local_service.selector:
                local_service.selector = remote_service.get("selector")
                update_fields.add("selector")
            if remote_service.get("clusterIp", "") != local_service.cluster_ip:
                local_service.cluster_ip = remote_service.get("clusterIp", "")
                update_fields.add("cluster_ip")
            if update_fields:
                local_service.save(update_fields=update_fields)

            local_service_workload_map = {
                workload.backend_id: workload
                for workload in local_service.target_workloads.all()
            }

            remote_service_workload_map = {
                workload_id: local_workloads_map[workload_id]
                for workload_id in remote_service.get("targetWorkloadIds") or []
            }

            local_service_workload_ids = set(local_service_workload_map.keys())
            remote_service_workload_ids = set(remote_service_workload_map.keys())

            stale_service_workload_ids = (
                local_service_workload_ids - remote_service_workload_ids
            )
            for workload_id in stale_service_workload_ids:
                workload = local_workloads_map[workload_id]
                local_service.target_workloads.remove(workload)

            new_service_workload_ids = (
                remote_service_workload_ids - local_service_workload_ids
            )
            for workload_id in new_service_workload_ids:
                workload = local_workloads_map[workload_id]
                local_service.target_workloads.add(workload)

        for remote_service in new_services:
            namespace = local_namespaces_map.get(remote_service["namespaceId"])
            if not namespace:
                logger.debug(
                    "Namespace %s is not present in the local database. "
                    "Skipping service %s",
                    remote_service["namespaceId"],
                    remote_service["id"],
                )
                return None
            if not namespace.project:
                logger.debug(
                    "Project is not present in the local database. Skipping service %s",
                    remote_service["id"],
                )
                return None
            if not namespace.project.cluster:
                logger.debug(
                    "Cluster is not present in the local database. Skipping service %s",
                    remote_service["id"],
                )
                return None

            local_service = models.Service(
                backend_id=remote_service["id"],
                name=remote_service["name"],
                created=parse_datetime(remote_service["created"]),
                runtime_state=remote_service["state"],
                settings=self.settings,
                service_settings=namespace.project.cluster.service_settings,
                project=namespace.project.cluster.project,
                namespace=namespace,
                cluster_ip=remote_service["clusterIp"],
                selector=remote_service.get("selector"),
                state=CoreStates.OK,
            )
            local_service.save()
            workloads = [
                local_workloads_map[workload_id]
                for workload_id in remote_service.get("targetWorkloadIds", [])
            ]
            local_service.target_workloads.set(workloads)

        local_services.filter(backend_id__in=stale_services).delete()

    def get_service_yaml(self, service: models.Service):
        if not service.namespace.project:
            raise RancherException(
                f"Service {service.uuid} does not have a project backend ID"
            )
        return self.client.get_service_yaml(
            service.namespace.project.backend_id, service.backend_id
        )

    def put_service_yaml(self, service: models.Service, yaml: str):
        if not service.namespace.project:
            raise RancherException(
                f"Service {service.uuid} does not have a project backend ID"
            )
        return self.client.put_service_yaml(
            service.namespace.project.backend_id, service.backend_id, yaml
        )

    def delete_service(self, service: models.Service):
        if not service.namespace.project:
            raise RancherException(
                f"Service {service.uuid} does not have a project backend ID"
            )
        return self.client.delete_service(
            service.namespace.project.backend_id, service.backend_id
        )

    def import_yaml(
        self,
        cluster: models.Cluster,
        yaml: str,
        default_namespace: models.Namespace | None = None,
        namespace: models.Namespace | None = None,
    ):
        return self.client.import_yaml(
            cluster.backend_id,
            yaml,
            default_namespace and default_namespace.backend_id,
            namespace and namespace.backend_id,
        )

    def ping(self, *args, **kwargs):
        return


class VaultBackend:
    def __init__(
        self,
        vault_host: str,
        vault_port: int,
        vault_token: str,
        vault_tls_verify: bool = True,
    ):
        self.client = hvac.Client(
            url=f"https://{vault_host}:{vault_port}",
            token=vault_token,
            verify=vault_tls_verify,
        )

    def create_or_update_policy(self, name: str, policy: dict):
        try:
            logger.info("Creating (updating) %s policy in Vault", name)
            response = self.client.sys.create_or_update_policy(name=name, policy=policy)
        except vault_exceptions.VaultError as e:
            logger.error("Unable to create a vault policy %s, reason: %s", name, e)
            raise
        if response.status_code != 204:
            raise VaultException(
                f"Vault server responded with {response.status_code} code: {response.content}"
            )

    def create_or_update_role(self, role_name: str, policy_name: str, params=None):
        try:
            if params is None:
                params = {}
            logger.info(
                "Creating (updating) role %s for with %s in Vault",
                role_name,
                policy_name,
            )
            # Requires: vault auth enable approle
            response = self.client.auth.approle.create_or_update_approle(
                role_name=role_name, token_policies=[policy_name], **params
            )
        except vault_exceptions.VaultError as e:
            logger.error("Unable to create a Vault role %s, reason: %s", role_name, e)
            raise
        if response.status_code != 204:
            raise VaultException(
                f"Vault server responded with {response.status_code} code: {response.text}"
            )

    def get_role_id(self, role_name):
        try:
            logger.info("Reading role ID for %s role", role_name)
            role_id_data = self.client.auth.approle.read_role_id(role_name=role_name)
            return role_id_data["data"]["role_id"]
        except vault_exceptions.VaultError as e:
            logger.error(
                "Unable to read an ID of the Vault role %s, reason: %s", role_name, e
            )
            raise

    def generate_role_secret_id(self, role_name: str):
        try:
            logger.info("Generating secret ID for role %s", role_name)
            secret_id_data = self.client.auth.approle.generate_secret_id(
                role_name=role_name
            )
            return secret_id_data["data"]["secret_id"]
        except vault_exceptions.VaultError as e:
            logger.error(
                "Unable to get client ID for the Vault role %s, reason: %s",
                role_name,
                e,
            )
            raise

    def create_or_update_secret(self, path: str, secret: dict[str, str]):
        try:
            logger.info("Creating (updating) secret %s in Vault", path)
            secret_data = self.client.secrets.kv.v2.create_or_update_secret(
                path=path,
                secret=secret,
            )
            return secret_data
        except vault_exceptions.VaultError as e:
            logger.error("Unable to create a Vault secret %s, reason: %s", path, e)
            raise

    def delete_policy(self, name: str):
        try:
            logger.info("Deleting %s policy in Vault", name)
            response = self.client.sys.delete_policy(name=name)
        except vault_exceptions.VaultError as e:
            logger.error("Unable to delete a vault policy %s, reason: %s", name, e)
            raise
        if response.status_code != 204:
            raise VaultException(
                f"Vault server responded with {response.status_code} code, body: {response.content}"
            )

    def delete_role(self, role_name: str):
        try:
            logger.info("Deleting role %s in Vault", role_name)
            response = self.client.auth.approle.delete_role(role_name=role_name)
        except vault_exceptions.VaultError as e:
            logger.error("Unable to delete a Vault role %s, reason: %s", role_name, e)
            raise
        if response.status_code != 204:
            raise VaultException(
                f"Vault server responded with {response.status_code} code, body: {response.text}"
            )

    def delete_secret(self, path: str):
        try:
            logger.info("Deleting secret %s in Vault", path)
            response = self.client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=path
            )
        except vault_exceptions.VaultError as e:
            logger.error("Unable to delete a Vault secret %s, reason: %s", path, e)
            raise
        if response.status_code != 204:
            raise VaultException(
                f"Vault server responded with {response.status_code} code, body: {response.text}"
            )


class KeycloakBackend:
    def __init__(self, settings):
        # Initialize Keycloak client using settings from Rancher settings
        keycloak_url = settings.get_option("keycloak_url")
        keycloak_realm = settings.get_option("keycloak_realm")
        keycloak_user_realm = settings.get_option("keycloak_user_realm") or "master"
        keycloak_username = settings.get_option("keycloak_username")
        keycloak_password = settings.get_option("keycloak_password")
        keycloak_verify = settings.get_option("keycloak_ssl_verify")
        keycloak_verify = keycloak_verify if keycloak_verify is not None else True

        # Set up Keycloak client
        self.keycloak = KeycloakAdmin(
            server_url=keycloak_url,
            user_realm_name=keycloak_user_realm,
            realm_name=keycloak_realm,
            username=keycloak_username,
            password=keycloak_password,
            verify=keycloak_verify,
        )

    def find_user_by_username(self, username):
        """Find a user by their username in Keycloak"""
        users = self.keycloak.get_users({"username": username})
        return users[0] if users else None

    def get_group(self, group_id):
        """
        Fetching group data from Keycloak
        """
        try:
            return self.keycloak.get_group(group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to fetch the group %s in Keycloak: %s", group_id, e)
            raise

    def create_group(self, group_name: str, parent_id: str | None = None):
        """
        Creating group in Keycloak
        """
        try:
            # Try to find group
            groups = self.keycloak.get_groups({"search": group_name})
            group = next(
                (
                    g
                    for g in groups
                    if g["name"] == group_name
                    or (
                        group_name
                        in {sub_group["name"] for sub_group in g["subGroups"]}
                    )
                ),
                None,
            )

            if group:
                logger.info(
                    "The group %s already exists, skipping creation", group_name
                )
            else:
                logger.info("Creating a group %s, parent %s", group_name, parent_id)
                payload = {"name": group_name}
                if parent_id is None:
                    group_id = self.keycloak.create_group(payload)
                else:
                    group_id = self.keycloak.create_group(payload, parent=parent_id)
                group = {"id": group_id}
            return group

        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to create the group %s in Keycloak: %s", group_name, e)
            raise

    def delete_group(self, group_id):
        """
        Delete group from Keycloak
        """
        try:
            # Try to find group
            group = self.get_group(group_id)
            if group:
                logger.info(
                    "Deleting group %s (%s) in Keycloak", group["name"], group_id
                )
                self.keycloak.delete_group(group_id)
            else:
                logger.info("The group %s is already deleted in Keycloak", group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to delete the group %s in Keycloak: %s", group_id, e)
            raise

    def list_groups(self):
        """
        List groups in Keycloak
        """
        try:
            return self.keycloak.get_groups()
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to list groups in Keycloak: %s", e)
            raise

    def list_group_members(self, group_id: str):
        """
        Get members of the group in Keycloak
        """
        try:
            return self.keycloak.get_group_members(group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to get group members in Keycloak: %s", e)
            raise

    def add_user_to_group(self, user_id, group_id):
        """
        Add user to the Keycloak group
        """
        try:
            logger.info("Adding user %s to group %s", user_id, group_id)
            self.keycloak.group_user_add(user_id, group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error(
                "Failed to add user %s to group %s in Keycloak: %s",
                user_id,
                group_id,
                e,
            )
            raise

    def remove_user_from_group(self, user_id, group_id):
        """
        Remove user from the Keycloak group
        """
        try:
            logger.info("Removing user %s from group %s", user_id, group_id)
            self.keycloak.group_user_remove(user_id, group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error(
                "Failed to revoke user %s role in Keycloak group %s: %s",
                user_id,
                group_id,
                e,
            )
            raise
