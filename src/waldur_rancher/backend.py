from django.utils.functional import cached_property

from waldur_core.structure import ServiceBackend

from .client import RancherClient
from . import models


class RancherBackend(ServiceBackend):
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
        client = RancherClient(self.host, verify_ssl=False)
        client.login(self.settings.username, self.settings.password)
        return client

    @cached_property
    def host(self):
        return self.settings.backend_url.strip('/')

    def create_cluster(self, cluster):
        backend_cluster = self.client.create_cluster(cluster.name)
        self._backend_cluster_to_cluster(backend_cluster, cluster)
        self.client.create_cluster_registration_token(cluster.backend_id)
        cluster.node_command = self.client.get_node_command(cluster.backend_id)
        cluster.save()

    def delete_cluster(self, cluster):
        self.client.delete_cluster(cluster.backend_id)

    def update_cluster(self, cluster):
        backend_cluster = self._cluster_to_backend_cluster(cluster)
        self.client.update_cluster(cluster.backend_id, backend_cluster)

    def _backend_cluster_to_cluster(self, backend_cluster, cluster):
        cluster.backend_id = backend_cluster['id']
        cluster.name = backend_cluster['name']

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
            state=models.Cluster.States.OK)
        self._backend_cluster_to_cluster(backend_cluster, cluster)
        cluster.save()
        backend_nodes = backend_cluster.get('appliedSpec', {}).get('rancherKubernetesEngineConfig', {}).get('nodes', [])
        for node in backend_nodes:
            models.Node.objects.create(
                cluster=cluster
            )
        return cluster

    def get_cluster_nodes(self, backend_id):
        backend_cluster = self.client.get_cluster(backend_id)
        nodes = backend_cluster.get('appliedSpec', {}).get('rancherKubernetesEngineConfig', {}).get('nodes', [])
        return [self._backend_node_to_node(node) for node in nodes]

    def node_is_active(self, backend_id):
        backend_node = self.client.get_node(backend_id)
        return backend_node['state'] == 'active'
