import logging

import requests

from .exceptions import RancherException

logger = logging.getLogger(__name__)


class RancherClient:
    """
    Rancher API client.
    See also: https://rancher.com/docs/rancher/v2.x/en/api/
    """
    def __init__(self, host, verify_ssl=True):
        """
        Initialize client with connection options.

        :param host: Rancher server IP address
        :type host: string
        :param verify_ssl: verify SSL certificates for HTTPS requests
        :type verify_ssl: bool
        """
        self._host = host
        self._base_url = '{0}/v3'.format(self._host)
        self._session = requests.Session()
        self._session.verify = verify_ssl

    def _request(self, method, endpoint, json=None, **kwargs):
        url = '%s/%s' % (self._base_url, endpoint)

        try:
            response = self._session.request(method, url, json=json, **kwargs)
        except requests.RequestException as e:
            raise RancherException(e)

        data = None
        if response.content:
            data = response.json()

        status_code = response.status_code
        if status_code in (requests.codes.ok,
                           requests.codes.created,
                           requests.codes.accepted,
                           requests.codes.no_content):
            if isinstance(data, dict) and 'value' in data:
                return data['value']
            return data
        else:
            raise RancherException(data)

    def _get(self, endpoint, **kwargs):
        return self._request('get', endpoint, **kwargs)

    def _post(self, endpoint, **kwargs):
        return self._request('post', endpoint, **kwargs)

    def _patch(self, endpoint, **kwargs):
        return self._request('patch', endpoint, **kwargs)

    def _delete(self, endpoint, **kwargs):
        return self._request('delete', endpoint, **kwargs)

    def _put(self, endpoint, **kwargs):
        return self._request('put', endpoint, **kwargs)

    def login(self, access_key, secret_key):
        """
        Login to Rancher server using access_key and secret_key.

        :param access_key: access_key to connect
        :type access_key: string
        :param secret_key: secret_key to connect
        :type secret_key: string
        :raises Unauthorized: raised if credentials are invalid.
        """
        self._post('', auth=(access_key, secret_key))
        self._session.auth = (access_key, secret_key)
        logger.debug('Successfully logged in as {0}'.format(access_key))

    def list_clusters(self):
        return self._get('clusters')['data']

    def get_cluster(self, cluster_id):
        return self._get('clusters/{0}'.format(cluster_id))

    def create_cluster(self, cluster_name):
        return self._post('clusters', json={"name": cluster_name, "rancherKubernetesEngineConfig": {}})

    def delete_cluster(self, cluster_id):
        return self._delete('clusters/{0}'.format(cluster_id))

    def list_cluster_registration_tokens(self):
        return self._get('clusterregistrationtokens', params={'limit': -1})['data']

    def create_cluster_registration_token(self, cluster_id):
        return self._post('clusterregistrationtoken',
                          json={"type": "clusterRegistrationToken", "clusterId": cluster_id})

    def get_node_command(self, cluster_id):
        cluster_list = self.list_cluster_registration_tokens()
        cluster = list(filter(lambda x: x['clusterId'] == cluster_id, cluster_list))
        if cluster:
            node_command = cluster[0]['nodeCommand']
            return node_command

    def update_cluster(self, cluster_id, new_params):
        return self._put('clusters/{0}'.format(cluster_id), json=new_params)

    def get_node(self, node_id):
        return self._get('nodes/{0}'.format(node_id))

    def get_kubeconfig_file(self, cluster_id):
        data = self._post('clusters/{0}'.format(cluster_id), params={'action': 'generateKubeconfig'})
        return data['config']
