from django.utils.functional import cached_property

from waldur_core.structure import ServiceBackend

from .client import RancherClient


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

    def _cluster_to_backend_cluster(self, cluster):
        return {'name': cluster.name}
