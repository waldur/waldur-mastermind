import io
import logging
import time
from urllib.parse import parse_qs, urlparse

import requests
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.functional import cached_property

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.media.utils import guess_image_extension
from waldur_core.structure import ServiceBackend
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.utils import update_pulled_fields
from waldur_mastermind.common.utils import parse_datetime
from waldur_rancher.enums import ClusterRoles, GlobalRoles
from waldur_rancher.exceptions import NotFound, RancherException

from . import client, models, signals, utils

logger = logging.getLogger(__name__)


class RancherBackend(ServiceBackend):

    DEFAULTS = {
        'cloud_init_template': '#cloud-config\n'
        'packages: \n'
        '  - curl\n'
        'runcmd:\n'
        '  - curl -fsSL https://get.docker.com -o get-docker.sh; sh get-docker.sh\n'
        '  - sudo systemctl start docker\n'
        '  - sudo systemctl enable docker\n'
        '  - [ sh, -c, "{command}" ]\n',
        'default_mtu': 1440,
        'private_registry_url': None,
        'private_registry_user': None,
        'private_registry_password': None,
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
        self.pull_projects()
        self.pull_namespaces()
        self.pull_catalogs()
        self.pull_templates()
        self.pull_template_icons()
        self.pull_workloads()
        self.pull_hpas()

    def get_kubeconfig_file(self, cluster):
        return self.client.get_kubeconfig_file(cluster.backend_id)

    def create_cluster(self, cluster):
        mtu = self.settings.get_option('default_mtu')
        private_registry = None
        private_registry_url = self.settings.get_option('private_registry_url')
        private_registry_user = self.settings.get_option('private_registry_user')
        private_registry_password = self.settings.get_option(
            'private_registry_password'
        )
        if private_registry_url and private_registry_user and private_registry_password:
            private_registry = {
                'url': private_registry_url,
                'user': private_registry_user,
                'password': private_registry_password,
            }

        backend_cluster = self.client.create_cluster(
            cluster.name, mtu=mtu, private_registry=private_registry
        )
        self._backend_cluster_to_cluster(backend_cluster, cluster)
        # as rancher API is not transactional, give it 2s to write cluster state to etcd
        time.sleep(2)
        self.client.create_cluster_registration_token(cluster.backend_id)
        cluster.node_command = self.client.get_node_command(cluster.backend_id)
        cluster.save()

    def delete_cluster(self, cluster):
        if cluster.backend_id:
            try:
                self.client.delete_cluster(cluster.backend_id)
            except NotFound:
                logger.debug(
                    'Cluster %s is not present in the backend ' % cluster.backend_id
                )

        cluster.delete()

    def delete_node(self, node):
        if node.backend_id:
            try:
                self.client.delete_node(node.backend_id)
            except NotFound:
                logger.debug('Node %s is not present in the backend ' % node.backend_id)
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
        return {
            'backend_id': backend_node['id'],
            'name': backend_node['requestedHostname'],
            'controlplane_role': backend_node.get('controlPlane', False),
            'etcd_role': backend_node.get('etcd', False),
            'worker_role': backend_node.get('worker', False),
            'runtime_state': backend_node.get('state', ''),
        }

    def get_clusters_for_import(self):
        cur_clusters = set(
            models.Cluster.objects.filter(settings=self.settings).values_list(
                'backend_id', flat=True
            )
        )
        backend_clusters = self.client.list_clusters()
        return filter(lambda c: c['id'] not in cur_clusters, backend_clusters)

    def import_cluster(self, backend_id, service_project_link):
        backend_cluster = self.client.get_cluster(backend_id)

        if not backend_cluster.get('state', '') == models.Cluster.RuntimeStates.ACTIVE:
            raise RancherException('Cannot import K8s cluster in non-active state.')

        cluster = models.Cluster(
            backend_id=backend_id,
            service_project_link=service_project_link,
            state=models.Cluster.States.OK,
            runtime_state=backend_cluster['state'],
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

    def pull_cluster_details(self, cluster, backend_cluster=None):
        backend_cluster = backend_cluster or self.client.get_cluster(cluster.backend_id)
        self._backend_cluster_to_cluster(backend_cluster, cluster)
        cluster.save()

    def pull_cluster_nodes(self, cluster: models.Cluster):
        backend_nodes = self.get_cluster_nodes(cluster.backend_id)

        for backend_node in backend_nodes:
            # If the node has not been requested from Waldur, so it will be created
            node, created = models.Node.objects.get_or_create(
                name=backend_node['name'],
                cluster=cluster,
                defaults=dict(
                    backend_id=backend_node['backend_id'],
                    controlplane_role=backend_node['controlplane_role'],
                    etcd_role=backend_node['etcd_role'],
                    worker_role=backend_node['worker_role'],
                ),
            )

            if not node.backend_id:
                # If the node has been requested from Waldur, but it has not been synchronized
                node.backend_id = backend_node['backend_id']
                node.controlplane_role = backend_node['controlplane_role']
                node.etcd_role = backend_node['etcd_role']
                node.worker_role = backend_node['worker_role']
                node.save()

            # Update details in all cases.
            self.pull_node(node)

        # Update nodes states.
        utils.update_cluster_nodes_states(cluster.id)

    def check_cluster_nodes(self, cluster):
        self.pull_cluster_details(cluster)

        if cluster.runtime_state == models.Cluster.RuntimeStates.ACTIVE:
            # We don't need change cluster state here, because it will make in an executor.
            return

        for node in cluster.node_set.filter(
            Q(controlplane_role=True) | Q(etcd_role=True)
        ):
            controlplane_role = etcd_role = False
            if node.instance.state not in [
                core_models.StateMixin.States.ERRED,
                core_models.StateMixin.States.DELETING,
                core_models.StateMixin.States.DELETION_SCHEDULED,
            ]:
                if node.controlplane_role:
                    controlplane_role = True
                if node.etcd_role:
                    etcd_role = True
                if controlplane_role and etcd_role:
                    # We make a return if one or more VMs with 'controlplane' and 'etcd' roles exist
                    # and they haven't a state 'error' or 'delete'.
                    # Here 'return' means that cluster state checking must be retry later.
                    return

        cluster.error_message = (
            'The cluster is not connected with any '
            'non-failed VM\'s with \'controlplane\' or \'etcd\' roles.'
        )
        cluster.runtime_state = 'error'
        cluster.save()

    def get_cluster_nodes(self, backend_id):
        nodes = self.client.get_cluster_nodes(backend_id)
        return [self._backend_node_to_node(node) for node in nodes]

    def node_is_active(self, backend_id):
        backend_node = self.client.get_node(backend_id)
        return backend_node['state'] == models.Node.RuntimeStates.ACTIVE

    def pull_node(self, node):
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
            node.cpu_allocated = (
                core_utils.parse_int(cpu_allocated) / 1000
            )  # convert data from 380m to 0.38

        ram_allocated = get_backend_node_field('requested', 'memory')
        update_node_field('allocatable', 'cpu', field='cpu_total')

        if ram_allocated:
            node.ram_allocated = int(
                core_utils.parse_int(ram_allocated) / 2 ** 20
            )  # convert data to Mi

        ram_total = get_backend_node_field('allocatable', 'memory')

        if ram_total:
            node.ram_total = int(
                core_utils.parse_int(ram_total) / 2 ** 20
            )  # convert data to Mi

        update_node_field('requested', 'pods', field='pods_allocated')
        update_node_field('allocatable', 'pods', field='pods_total')
        update_node_field('state', field='runtime_state')

        return node.save()

    def create_user(self, user):
        if user.backend_id:
            return

        password = models.RancherUser.make_random_password()
        response = self.client.create_user(
            name=user.user.username, username=user.user.username, password=password
        )
        user_id = response['id']
        user.backend_id = user_id
        user.save()
        self.client.create_global_role(user.backend_id, GlobalRoles.user_base)
        signals.rancher_user_created.send(
            sender=models.RancherUser, instance=user, password=password,
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
            role = ClusterRoles.cluster_owner

        if link.role == models.ClusterRole.CLUSTER_MEMBER:
            role = ClusterRoles.cluster_member

        response = self.client.create_cluster_role(
            link.user.backend_id, link.cluster.backend_id, role
        )
        link_id = response['id']
        link.backend_id = link_id
        link.save()

    def delete_cluster_role(self, link):
        if link.backend_id:
            try:
                self.client.delete_cluster_role(cluster_role_id=link.backend_id)
            except NotFound:
                logger.debug(
                    'Cluster role %s is not present in the backend ' % link.backend_id
                )

        link.delete()

    def pull_catalogs_for_cluster(self, cluster: models.Cluster):
        self.pull_cluster_catalogs_for_cluster(cluster)
        self.pull_project_catalogs_for_cluster(cluster)

    def pull_cluster_catalogs_for_cluster(self, cluster):
        remote_catalogs = self.client.list_cluster_catalogs(cluster.backend_id)
        self.pull_catalogs_for_scope(remote_catalogs, cluster)

    def pull_project_catalogs_for_cluster(self, cluster):
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
            content_type=content_type, object_id=scope.id,
        )

        remote_catalog_map = {
            catalog['id']: self.remote_catalog_to_local(catalog, content_type, scope.id)
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
            'name',
            'description',
            'catalog_url',
            'branch',
            'commit',
            'username',
            'password',
            'runtime_state',
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
            settings=self.settings,
        )

    def refresh_catalog(self, catalog):
        if isinstance(catalog.scope, ServiceSettings):
            return self.client.refresh_global_catalog(catalog.backend_id)
        elif isinstance(catalog.scope, models.Cluster):
            return self.client.refresh_cluster_catalog(catalog.backend_id)
        else:
            return self.client.refresh_project_catalog(catalog.backend_id)

    def delete_catalog(self, catalog):
        try:
            if isinstance(catalog.scope, ServiceSettings):
                return self.client.delete_global_catalog(catalog.backend_id)
            elif isinstance(catalog.scope, models.Cluster):
                return self.client.delete_cluster_catalog(catalog.backend_id)
            else:
                return self.client.delete_project_catalog(catalog.backend_id)
        except NotFound:
            logger.debug(
                'Catalog %s is not present in the backend ', catalog.backend_id
            )

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
            project['id']: self.remote_project_to_local(project, local_cluster_map)
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
            'name',
            'description',
            'runtime_state',
            'cluster',
        }
        for project_id in existing_projects:
            local_project = local_project_map[project_id]
            remote_project = remote_project_map[project_id]
            update_pulled_fields(local_project, remote_project, pulled_fields)

        models.Project.objects.bulk_create(new_projects)
        local_projects.filter(backend_id__in=stale_projects).delete()

    def remote_project_to_local(self, remote_project, local_cluster_map):
        return models.Project(
            backend_id=remote_project['id'],
            name=remote_project['name'],
            description=remote_project['description'],
            created=parse_datetime(remote_project['created']),
            runtime_state=remote_project['state'],
            cluster=local_cluster_map.get(remote_project['clusterId']),
            settings=self.settings,
        )

    def pull_namespaces(self):
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
        for cluster in local_clusters:
            if cluster.state == models.Cluster.States.OK:
                self.pull_namespaces_for_cluster(cluster)
            else:
                logger.debug(
                    'Skipping namespace pulling for cluster with backend ID %s'
                    'because otherwise one failed cluster leads to provider failure',
                    cluster.backend_id,
                )

    def pull_namespaces_for_cluster(self, cluster: models.Cluster):
        remote_namespaces = self.client.list_namespaces(cluster.backend_id)
        local_namespaces = models.Namespace.objects.filter(project__cluster=cluster)
        local_projects = models.Project.objects.filter(cluster=cluster)

        local_project_map = {project.backend_id: project for project in local_projects}
        remote_namespace_map = {
            namespace['id']: self.remote_namespace_to_local(
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
            'name',
            'runtime_state',
            'project',
        }
        for namespace_id in existing_namespaces:
            local_namespace = local_namespace_map[namespace_id]
            remote_namespace = remote_namespace_map[namespace_id]
            update_pulled_fields(local_namespace, remote_namespace, pulled_fields)

        models.Namespace.objects.bulk_create(new_namespaces)
        local_namespaces.filter(backend_id__in=stale_namespaces).delete()

    def remote_namespace_to_local(self, remote_namespace, local_project_map):
        return models.Namespace(
            backend_id=remote_namespace['id'],
            name=remote_namespace['name'],
            created=parse_datetime(remote_namespace['created']),
            runtime_state=remote_namespace['state'],
            project=local_project_map.get(remote_namespace['projectId']),
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
        local_clusters = models.Cluster.objects.filter(settings=self.settings)
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
        local_templates,
        local_catalogs,
        local_clusters,
        local_projects,
        remote_templates,
    ):
        local_catalog_map = {catalog.backend_id: catalog for catalog in local_catalogs}
        local_cluster_map = {cluster.backend_id: cluster for cluster in local_clusters}
        local_project_map = {project.backend_id: project for project in local_projects}
        local_template_map = {
            template.backend_id: template for template in local_templates
        }
        remote_template_map = {
            template['id']: self.remote_template_to_local(
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
            'name',
            'description',
            'runtime_state',
            'project_url',
            'icon_url',
            'default_version',
            'versions',
            'catalog',
            'cluster',
            'project',
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
        catalog_id = remote_template['catalogId'] or remote_template['clusterCatalogId']
        return models.Template(
            backend_id=remote_template['id'],
            name=remote_template['name'],
            description=remote_template['description'],
            created=parse_datetime(remote_template['created']),
            runtime_state=remote_template['state'],
            icon_url=remote_template['links']['icon'],
            project_url=remote_template.get('projectURL', ''),
            default_version=remote_template['defaultVersion'],
            versions=list(remote_template['versionLinks'].keys()),
            catalog=local_catalog_map.get(catalog_id),
            cluster=local_cluster_map.get(remote_template['clusterId']),
            project=local_project_map.get(remote_template['projectId']),
            settings=self.settings,
        )

    def _get_external_template_icon(self, icon_url):
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
                    'Rancher did not return icon for a Template, trying with external URL'
                )
                content = self._get_external_template_icon(template.icon_url)
            if not content:
                # Clear icon field so that default icon would be rendered
                template.icon = None
                template.save()
                continue
            extension = guess_image_extension(content)
            if not extension:
                continue
            # Overwrite existing file
            if template.icon:
                template.icon.delete()
            template.icon.save(f'{template.uuid}.{extension}', io.BytesIO(content))

    def list_project_secrets(self, project):
        return self.client.list_project_secrets(project.backend_id)

    def list_cluster_applications(self, cluster):
        applications = []
        for project in models.Project.objects.filter(cluster=cluster):
            for app in self.client.get_project_applications(project.backend_id):
                parts = urlparse(app['externalId'])
                params = parse_qs(parts.query)
                project_id = app['projectId']
                app_id = app['id']
                applications.append(
                    {
                        'name': app['name'],
                        'state': app['state'],
                        'created': app['created'],
                        'id': app_id,
                        'answers': app.get('answers'),
                        'project': project.name,
                        'catalog': params['catalog'][0],
                        'template': params['template'][0],
                        'version': params['version'][0],
                        'url': f'{self.host}/p/{project_id}/apps/{app_id}',
                        'project_uuid': project.uuid.hex,
                    }
                )
        return applications

    def pull_cluster_workloads(self, cluster):
        for project in models.Project.objects.filter(cluster=cluster):
            self.pull_project_workloads(project)

    def pull_workloads(self):
        for project in models.Project.objects.filter(settings=self.settings):
            self.pull_project_workloads(project)

    def pull_project_workloads(self, project):
        remote_workloads = self.client.list_workloads(project.backend_id)
        local_workloads = models.Workload.objects.filter(project=project)
        local_namespaces = models.Namespace.objects.filter(project=project)

        local_namespaces_map = {
            namespace.backend_id: namespace for namespace in local_namespaces
        }
        remote_workload_map = {
            workload['id']: self.remote_workload_to_local(
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
            'name',
            'runtime_state',
            'scale',
        }
        for workload_id in existing_workloads:
            local_workload = local_workload_map[workload_id]
            remote_workload = remote_workload_map[workload_id]
            update_pulled_fields(local_workload, remote_workload, pulled_fields)

        models.Workload.objects.bulk_create(new_workloads)
        local_workloads.filter(backend_id__in=stale_workloads).delete()

    def remote_workload_to_local(self, remote_workload, project, local_namespaces_map):
        return models.Workload(
            backend_id=remote_workload['id'],
            name=remote_workload['name'],
            created=parse_datetime(remote_workload['created']),
            runtime_state=remote_workload['state'],
            project=project,
            cluster=project.cluster,
            settings=self.settings,
            namespace=local_namespaces_map.get(remote_workload['namespaceId']),
            scale=remote_workload.get('scale', 0),
        )

    def pull_cluster_hpas(self, cluster):
        for project in models.Project.objects.filter(cluster=cluster):
            self.pull_project_hpas(project)

    def pull_hpas(self):
        for project in models.Project.objects.filter(settings=self.settings):
            self.pull_project_hpas(project)

    def pull_project_hpas(self, project):
        local_workloads = models.Workload.objects.filter(project=project)
        local_workloads_map = {
            workload.backend_id: workload for workload in local_workloads
        }

        local_hpas = models.HPA.objects.filter(project=project)
        local_hpa_map = {hpa.backend_id: hpa for hpa in local_hpas}

        remote_hpas = self.client.list_hpas(project.backend_id)
        remote_hpa_map = {
            hpa['id']: self.remote_hpa_to_local(hpa, local_workloads_map)
            for hpa in remote_hpas
        }

        remote_hpa_ids = set(remote_hpa_map.keys())
        local_hpa_ids = set(local_hpa_map.keys())

        stale_hpas = local_hpa_ids - remote_hpa_ids

        new_hpas = [remote_hpa_map[hpa_id] for hpa_id in remote_hpa_ids - local_hpa_ids]

        existing_hpas = remote_hpa_ids & local_hpa_ids
        pulled_fields = {
            'name',
            'runtime_state',
            'current_replicas',
            'desired_replicas',
            'min_replicas',
            'max_replicas',
            'metrics',
        }
        for hpa_id in existing_hpas:
            local_hpa = local_hpa_map[hpa_id]
            remote_hpa = remote_hpa_map[hpa_id]
            update_pulled_fields(local_hpa, remote_hpa, pulled_fields)

        models.HPA.objects.bulk_create(new_hpas)
        local_hpas.filter(backend_id__in=stale_hpas).delete()

    def remote_hpa_to_local(self, remote_hpa, local_workloads_map):
        workload = local_workloads_map[remote_hpa['workloadId']]
        return models.HPA(
            backend_id=remote_hpa['id'],
            name=remote_hpa['name'],
            created=parse_datetime(remote_hpa['created']),
            runtime_state=remote_hpa['state'],
            project=workload.project,
            cluster=workload.cluster,
            settings=self.settings,
            namespace=workload.namespace,
            current_replicas=remote_hpa['currentReplicas'],
            desired_replicas=remote_hpa['desiredReplicas'],
            min_replicas=remote_hpa['minReplicas'],
            max_replicas=remote_hpa['maxReplicas'],
            metrics=remote_hpa['metrics'],
            state=models.HPA.States.OK,
        )

    def create_hpa(self, hpa):
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
        hpa.backend_id = remote_hpa['id']
        hpa.runtime_state = remote_hpa['state']
        hpa.save(update_fields=['backend_id', 'runtime_state'])

    def update_hpa(self, hpa):
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
            logger.debug('HPA %s is not present in the backend ' % hpa.backend_id)

    def install_longhorn_to_cluster(self, cluster):
        longhorn_name = 'longhorn'
        longhorn_namespace = 'longhorn-system'
        catalog_name = 'library'

        system_project = models.Project.objects.filter(
            cluster=cluster, name='System'
        ).first()
        if not system_project:
            raise RancherException(
                "There is no system project in cluster %s" % cluster.backend_id
            )

        available_templates = models.Template.objects.filter(
            name=longhorn_name, catalog__name=catalog_name
        )
        available_templates_count = len(available_templates)
        if available_templates_count != 1:
            if available_templates_count == 0:
                message = "There are no templates with name=%s, catalog.name=%s" % (
                    longhorn_name,
                    catalog_name,
                )
            else:
                message = (
                    "There are more than one template for name=%s, catalog.name=%s"
                    % (longhorn_name, catalog_name)
                )
            logger.info(message)
            raise RancherException(message)

        logger.info(
            'Starting longhorn installation for cluster %s (name=%s, backend_id=%s)',
            cluster,
            cluster.name,
            cluster.backend_id,
        )
        template = available_templates.first()

        try:
            namespace = models.Namespace.objects.get(
                name=longhorn_namespace, project=system_project
            )
        except models.Namespace.DoesNotExist:
            logger.info(
                'Creating namespace %s for cluster %s (name=%s, backend_id=%s)',
                longhorn_namespace,
                cluster,
                cluster.name,
                cluster.backend_id,
            )
            namespace_response = self.client.create_namespace(
                cluster.backend_id, system_project.backend_id, longhorn_namespace
            )
            namespace = models.Namespace.objects.create(
                name=longhorn_namespace,
                backend_id=namespace_response['id'],
                settings=system_project.settings,
                project=system_project,
            )

        logger.info(
            'Creating application %s for cluster %s (name=%s, backend_id=%s) in namespace %s (backend_id=%s)',
            longhorn_namespace,
            cluster,
            cluster.name,
            cluster.backend_id,
            namespace.name,
            namespace.backend_id,
        )
        worker_node_count = cluster.node_set.filter(worker_role=True).count()
        replica_count = min(3, worker_node_count)
        application = self.client.create_application(
            catalog_id=template.catalog.backend_id,
            template_id=template.name,
            version=template.default_version,
            project_id=system_project.backend_id,
            namespace_id=namespace.backend_id,
            name=longhorn_name,
            answers={'persistence.defaultClassReplicaCount': replica_count,},
        )

        logger.info(
            'Application %s for cluster %s (name=%s, backend_id=%s) was created',
            application,
            cluster,
            cluster.name,
            cluster.backend_id,
        )
