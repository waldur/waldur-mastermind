from django.conf import settings as conf_settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.functional import cached_property

from waldur_core.core import utils as core_utils, models as core_models
from waldur_core.structure import ServiceBackend
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.utils import update_pulled_fields
from waldur_mastermind.common.utils import parse_datetime

from . import models, signals, client


class RancherBackend(ServiceBackend):

    DEFAULTS = {
        'cloud_init_template':
            '#cloud-config\n'
            'packages: \n'
            '  - curl\n'
            'runcmd:\n'
            '  - curl -fsSL https://get.docker.com -o get-docker.sh; sh get-docker.sh\n'
            '  - sudo systemctl start docker\n'
            '  - sudo systemctl enable docker\n'
            '  - [ sh, -c, "{command}" ]\n'
    }

    def __init__(self, settings):
        """
        :type settings: :class:`waldur_core.structure.models.ServiceSettings`
        """
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
        return self.settings.backend_url.strip('/')

    def pull_service_properties(self):
        self.pull_catalogs()

    def get_kubeconfig_file(self, cluster):
        return self.client.get_kubeconfig_file(cluster.backend_id)

    def create_cluster(self, cluster):
        backend_cluster = self.client.create_cluster(cluster.name)
        self._backend_cluster_to_cluster(backend_cluster, cluster)
        self.client.create_cluster_registration_token(cluster.backend_id)
        cluster.node_command = self.client.get_node_command(cluster.backend_id)
        cluster.save()

    def delete_cluster(self, cluster):
        if cluster.backend_id:
            self.client.delete_cluster(cluster.backend_id)

        cluster.delete()

    def delete_node(self, node):
        if node.backend_id:
            self.client.delete_node(node.backend_id)

        node.delete()

    def update_cluster(self, cluster):
        backend_cluster = self._cluster_to_backend_cluster(cluster)
        self.client.update_cluster(cluster.backend_id, backend_cluster)

    def _backend_cluster_to_cluster(self, backend_cluster, cluster):
        cluster.backend_id = backend_cluster['id']
        cluster.name = backend_cluster['name']
        cluster.runtime_state = backend_cluster['state']

    def _cluster_to_backend_cluster(self, cluster):
        return {'name': cluster.name}

    def _backend_node_to_node(self, backend_node):
        return {'backend_id': backend_node['nodeId'], 'name': backend_node['hostnameOverride']}

    def get_clusters_for_import(self):
        cur_clusters = set(models.Cluster.objects.filter(service_project_link__service__settings=self.settings)
                           .values_list('backend_id', flat=True))
        backend_clusters = self.client.list_clusters()
        return filter(lambda c: c['id'] not in cur_clusters, backend_clusters)

    def import_cluster(self, backend_id, service_project_link):
        backend_cluster = self.client.get_cluster(backend_id)
        cluster = models.Cluster(
            backend_id=backend_id,
            service_project_link=service_project_link,
            state=models.Cluster.States.OK,
            runtime_state=backend_cluster['state'])
        self.pull_cluster(cluster, backend_cluster)
        return cluster

    def pull_cluster(self, cluster, backend_cluster=None):
        backend_cluster = backend_cluster or self.client.get_cluster(cluster.backend_id)
        self._backend_cluster_to_cluster(backend_cluster, cluster)
        cluster.save()
        backend_nodes = backend_cluster.get('appliedSpec', {}).get('rancherKubernetesEngineConfig', {}).get('nodes', [])

        for backend_node in backend_nodes:
            roles = backend_node.get('role', [])

            # If the node has not been requested from Waldur, so it will be created
            node, created = models.Node.objects.get_or_create(
                name=backend_node.get('hostnameOverride'),
                cluster=cluster,
                defaults=dict(
                    state=models.Node.States.OK,
                    backend_id=backend_node.get('nodeId'),
                    controlplane_role='controlplane' in roles,
                    etcd_role='etcd' in roles,
                    worker_role='worker' in roles
                )
            )

            if not node.backend_id:
                # If the node has been requested from Waldur, but it has not been synchronized
                node.state = models.Node.States.OK
                node.backend_id = backend_node.get('nodeId')
                node.controlplane_role = 'controlplane' in roles
                node.etcd_role = 'etcd' in roles
                node.worker_role = 'worker' in roles
                node.save()

            # Update details in all cases.
            self.update_node_details(node)

    def pull_cluster_runtime_state(self, cluster):
        backend_cluster = self.client.get_cluster(cluster.backend_id)
        self._backend_cluster_to_cluster(backend_cluster, cluster)
        cluster.save()

    def check_cluster_creating(self, cluster):
        self.pull_cluster_runtime_state(cluster)

        if cluster.runtime_state == conf_settings.WALDUR_RANCHER['ACTIVE_CLUSTER_STATE']:
            cluster.state = models.Cluster.States.OK
            cluster.save()
            return

        for node in cluster.node_set.filter(Q(controlplane_role=True) | Q(etcd_role=True)):
            controlplane_role = etcd_role = False
            if node.instance.state not in [core_models.StateMixin.States.ERRED,
                                           core_models.StateMixin.States.DELETING,
                                           core_models.StateMixin.States.DELETION_SCHEDULED]:
                if node.controlplane_role:
                    controlplane_role = True
                if node.etcd_role:
                    etcd_role = True
                if controlplane_role and etcd_role:
                    # We make a return if one or more VMs with 'controlplane' and 'etcd' roles exist
                    # and they haven't a state 'error' or 'delete'.
                    # Here 'return' means that cluster state checking must be retry later.
                    return

        cluster.error_message = 'The cluster is not connected with any ' \
                                'non-failed VM\'s with \'controlplane\' or \'etcd\' roles.'
        cluster.runtime_state = 'error'
        cluster.state = core_models.StateMixin.States.ERRED
        cluster.save()

    def get_cluster_nodes(self, backend_id):
        backend_cluster = self.client.get_cluster(backend_id)
        nodes = backend_cluster.get('appliedSpec', {}).get('rancherKubernetesEngineConfig', {}).get('nodes', [])
        return [self._backend_node_to_node(node) for node in nodes]

    def node_is_active(self, backend_id):
        backend_node = self.client.get_node(backend_id)
        return backend_node['state'] == conf_settings.WALDUR_RANCHER['ACTIVE_NODE_STATE']

    def update_node_details(self, node):
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

        update_node_field('labels', field='labels')
        update_node_field('annotations', field='annotations')
        update_node_field('info', 'os', 'dockerVersion', field='docker_version')
        update_node_field('info', 'kubernetes', 'kubeletVersion', field='k8s_version')
        cpu_allocated = get_backend_node_field('requested', 'cpu')

        if cpu_allocated:
            node.cpu_allocated = \
                core_utils.parse_int(cpu_allocated) / 1000  # convert data from 380m to 0.38

        ram_allocated = get_backend_node_field('requested', 'memory')
        update_node_field('allocatable', 'cpu', field='cpu_total')

        if ram_allocated:
            node.ram_allocated = \
                int(core_utils.parse_int(ram_allocated) / 2 ** 20)  # convert data to Mi

        ram_total = get_backend_node_field('allocatable', 'memory')

        if ram_total:
            node.ram_total = \
                int(core_utils.parse_int(ram_total) / 2 ** 20)  # convert data to Mi

        update_node_field('requested', 'pods', field='pods_allocated')
        update_node_field('allocatable', 'pods', field='pods_total')
        update_node_field('state', field='runtime_state')

        if node.runtime_state == conf_settings.WALDUR_RANCHER['ACTIVE_NODE_STATE']:
            node.state = models.Node.States.OK
        else:
            node.state = models.Node.States.ERRED
        return node.save()

    def create_user(self, user):
        if user.backend_id:
            return

        password = models.RancherUser.make_random_password()
        response = self.client.create_user(
            name=user.user.username,
            username=user.user.username,
            password=password
        )
        user_id = response['id']
        user.backend_id = user_id
        user.save()
        self.client.create_global_role(user.backend_id, client.GlobalRoleId.user_base)
        signals.rancher_user_created.send(
            sender=models.RancherUser,
            instance=user,
            password=password,
        )

    def delete_user(self, user):
        if user.backend_id:
            self.client.delete_user(user_id=user.backend_id)

        user.delete()

    def block_user(self, user):
        if user.is_active:
            self.client.disable_user(user.backend_id)
            user.is_active = False
            user.save()

    def activate_user(self, user):
        if not user.is_active:
            self.client.enable_user(user.backend_id)
            user.is_active = True
            user.save()

    def create_cluster_role(self, link):
        role = None

        if link.role == models.ClusterRole.CLUSTER_OWNER:
            role = client.ClusterRoleId.cluster_owner

        if link.role == models.ClusterRole.CLUSTER_MEMBER:
            role = client.ClusterRoleId.cluster_member

        response = self.client.create_cluster_role(
            link.user.backend_id,
            link.cluster.backend_id,
            role)
        link_id = response['id']
        link.backend_id = link_id
        link.save()

    def delete_cluster_role(self, link):
        if link.backend_id:
            self.client.delete_cluster_role(cluster_role_id=link.backend_id)

        link.delete()

    def pull_catalogs(self):
        self.pull_global_catalogs()
        self.pull_cluster_catalogs()
        # TODO: Implement Rancher project catalogs synchronization

    def pull_global_catalogs(self):
        remote_catalogs = self.client.list_global_catalogs()
        self.pull_catalogs_for_scope(remote_catalogs, self.settings)

    def pull_cluster_catalogs(self):
        remote_catalogs = self.client.list_cluster_catalogs()
        for cluster in models.Cluster.objects.filter(service_project_link__service__settings=self.settings):
            self.pull_catalogs_for_scope(remote_catalogs, cluster)

    def pull_catalogs_for_scope(self, remote_catalogs, scope):
        content_type = ContentType.objects.get_for_model(scope)
        local_catalogs = models.Catalog.objects.filter(
            content_type=content_type,
            object_id=scope.id,
        )

        remote_catalog_map = {
            catalog['id']: self.remote_catalog_to_local(catalog, content_type, scope.id)
            for catalog in remote_catalogs
        }
        local_catalog_map = {
            catalog.id: catalog
            for catalog in local_catalogs
        }
        remote_catalog_ids = set(remote_catalog_map.keys())
        local_catalog_ids = set(local_catalog_map.keys())

        stale_catalogs = local_catalog_ids - remote_catalog_ids

        new_catalogs = [
            remote_catalog_map[catalog_id]
            for catalog_id in remote_catalog_ids - local_catalog_ids
        ]

        existing_catalogs = remote_catalog_ids & local_catalog_ids
        pulled_fields = {
            'name', 'description', 'catalog_url', 'branch',
            'commit', 'username', 'password', 'runtime_state',
        }
        for catalog_id in existing_catalogs:
            local_catalog = local_catalog_map[catalog_id]
            remote_catalog = remote_catalog_map[catalog_id]
            update_pulled_fields(local_catalog, remote_catalog, pulled_fields)

        models.Catalog.objects.bulk_create(new_catalogs)
        local_catalogs.filter(id__in=stale_catalogs).delete()

    def remote_catalog_to_local(self, remote_catalog, content_type, object_id):
        return models.Catalog(
            content_type=content_type,
            object_id=object_id,
            backend_id=remote_catalog['id'],
            name=remote_catalog['name'],
            description=remote_catalog['description'],
            created=parse_datetime(remote_catalog['created']),
            catalog_url=remote_catalog['url'],
            branch=remote_catalog['branch'],
            commit=remote_catalog.get('commit', ''),
            username=remote_catalog.get('username', ''),
            password=remote_catalog.get('password', ''),
            runtime_state=remote_catalog['state'],
        )

    def refresh_catalog(self, catalog):
        if isinstance(catalog.scope, ServiceSettings):
            return self.client.refresh_global_catalog(catalog.backend_id)
        elif isinstance(catalog.scope, models.Cluster):
            return self.client.refresh_cluster_catalog(catalog.backend_id)
        else:
            return self.client.refresh_project_catalog(catalog.backend_id)

    def delete_catalog(self, catalog):
        if isinstance(catalog.scope, ServiceSettings):
            return self.client.delete_global_catalog(catalog.backend_id)
        elif isinstance(catalog.scope, models.Cluster):
            return self.client.delete_cluster_catalog(catalog.backend_id)
        else:
            return self.client.delete_project_catalog(catalog.backend_id)

    def get_catalog_spec(self, catalog):
        spec = {
            'name': catalog.name,
            'description': catalog.description,
            'url': catalog.catalog_url,
            'branch': catalog.branch,
        }
        if catalog.username:
            spec['username'] = catalog.username
        if catalog.password:
            spec['password'] = catalog.password
        return spec

    def create_catalog(self, catalog):
        spec = self.get_catalog_spec(catalog)

        if isinstance(catalog.scope, ServiceSettings):
            remote_catalog = self.client.create_global_catalog(spec)
        elif isinstance(catalog.scope, models.Cluster):
            spec['clusterId'] = catalog.scope.backend_id
            remote_catalog = self.client.create_cluster_catalog(spec)
        else:
            spec['projectId'] = catalog.scope.backend_id
            remote_catalog = self.client.create_project_catalog(spec)

        catalog.backend_id = remote_catalog['id']
        catalog.runtime_state = remote_catalog['state']
        catalog.save()

    def update_catalog(self, catalog):
        spec = self.get_catalog_spec(catalog)
        if isinstance(catalog.scope, ServiceSettings):
            return self.client.update_global_catalog(catalog.backend_id, spec)
        elif isinstance(catalog.scope, models.Cluster):
            return self.client.update_cluster_catalog(catalog.backend_id, spec)
        else:
            return self.client.update_project_catalog(catalog.backend_id, spec)
