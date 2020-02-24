import logging

import requests

from .exceptions import RancherException

logger = logging.getLogger(__name__)


class GlobalRoleId:
    admin = 'admin'
    authn_manage = 'authn-manage'
    catalogs_manage = 'catalogs-manage'
    catalogs_use = 'catalogs-use'
    clusters_create = 'clusters-create'
    kontainerdrivers_manage = 'kontainerdrivers-manage'
    nodedrivers_manage = 'nodedrivers-manage'
    podsecuritypolicytemplates_manage = 'podsecuritypolicytemplates-manage'
    roles_manage = 'roles-manage'
    settings_manage = 'settings-manage'
    user = 'user'
    user_base = 'user-base'
    users_manage = 'users-manage'


class ClusterRoleId:
    admin = 'admin'
    backups_manage = 'backups-manage'
    cluster_admin = 'cluster-admin'
    cluster_member = 'cluster-member'
    cluster_owner = 'cluster-owner'
    clustercatalogs_manage = 'clustercatalogs-manage'
    clustercatalogs_view = 'clustercatalogs-view'
    clusterroletemplatebindings_manage = 'clusterroletemplatebindings-manage'
    clusterroletemplatebindings_view = 'clusterroletemplatebindings-view'
    configmaps_manage = 'configmaps-manage'
    configmaps_view = 'configmaps-view'
    create_ns = 'create-ns'
    edit = 'edit'
    ingress_manage = 'ingress-manage'
    ingress_view = 'ingress-view'
    nodes_manage = 'nodes-manage'
    nodes_view = 'nodes-view'
    persistentvolumeclaims_manage = 'persistentvolumeclaims-manage'
    persistentvolumeclaims_view = 'persistentvolumeclaims-view'
    project_member = 'project-member'
    project_monitoring_readonly = 'project-monitoring-readonly'
    project_owner = 'project-owner'
    projectcatalogs_manage = 'projectcatalogs-manage'
    projectcatalogs_view = 'projectcatalogs-view'
    projectroletemplatebindings_manage = 'projectroletemplatebindings-manage'
    projectroletemplatebindings_view = 'projectroletemplatebindings-view'
    projects_create = 'projects-create'
    projects_view = 'projects-view'
    read_only = 'read-only'
    secrets_manage = 'secrets-manage'
    secrets_view = 'secrets-view'
    serviceaccounts_manage = 'serviceaccounts-manage'
    serviceaccounts_view = 'serviceaccounts-view'
    services_manage = 'services-manage'
    services_view = 'services-view'
    storage_manage = 'storage-manage'
    view = 'view'
    workloads_manage = 'workloads-manage'
    workloads_view = 'workloads-view'


class ProjectRoleId:
    admin = 'admin'
    backups_manage = 'backups-manage'
    cluster_admin = 'cluster-admin'
    cluster_member = 'cluster-member'
    cluster_owner = 'cluster-owner'
    clustercatalogs_manage = 'clustercatalogs-manage'
    clustercatalogs_view = 'clustercatalogs-view'
    clusterroletemplatebindings_manage = 'clusterroletemplatebindings-manage'
    clusterroletemplatebindings_view = 'clusterroletemplatebindings-view'
    configmaps_manage = 'configmaps-manage'
    configmaps_view = 'configmaps-view'
    create_ns = 'create-ns'
    edit = 'edit'
    ingress_manage = 'ingress-manage'
    ingress_view = 'ingress-view'
    nodes_manage = 'nodes-manage'
    nodes_view = 'nodes-view'
    persistentvolumeclaims_manage = 'persistentvolumeclaims-manage'
    persistentvolumeclaims_view = 'persistentvolumeclaims-view'
    project_member = 'project-member'
    project_monitoring_readonly = 'project-monitoring-readonly'
    project_owner = 'project-owner'
    projectcatalogs_manage = 'projectcatalogs-manage'
    projectcatalogs_view = 'projectcatalogs-view'
    projectroletemplatebindings_manage = 'projectroletemplatebindings-manage'
    projectroletemplatebindings_view = 'projectroletemplatebindings-view'
    projects_create = 'projects-create'
    projects_view = 'projects-view'
    read_only = 'read-only'
    secrets_manage = 'secrets-manage'
    secrets_view = 'secrets-view'
    serviceaccounts_manage = 'serviceaccounts-manage'
    serviceaccounts_view = 'serviceaccounts-view'
    services_manage = 'services-manage'
    services_view = 'services-view'
    storage_manage = 'storage-manage'
    view = 'view'
    workloads_manage = 'workloads-manage'
    workloads_view = 'workloads-view'


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
        return self._post('clusters', json={'name': cluster_name, 'rancherKubernetesEngineConfig': {}})

    def delete_cluster(self, cluster_id):
        return self._delete('clusters/{0}'.format(cluster_id))

    def delete_node(self, node_id):
        return self._delete('nodes/{0}'.format(node_id))

    def list_cluster_registration_tokens(self):
        return self._get('clusterregistrationtokens', params={'limit': -1})['data']

    def create_cluster_registration_token(self, cluster_id):
        return self._post('clusterregistrationtoken',
                          json={'type': 'clusterRegistrationToken', 'clusterId': cluster_id})

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

    def list_users(self):
        return self._get('users')['data']

    def create_user(self, name, username, password, mustChangePassword=True):
        return self._post('users', json={
            'name': name,
            'mustChangePassword': mustChangePassword,
            'password': password,
            'username': username,
        })

    def enable_user(self, user_id):
        return self._put('users/{0}'.format(user_id), json={
            'enabled': True,
        })

    def disable_user(self, user_id):
        return self._put('users/{0}'.format(user_id), json={
            'enabled': False,
        })

    def create_global_role(self, user_id, role):
        return self._post('globalrolebindings', json={
            'globalRoleId': role,
            'userId': user_id,
        })

    def delete_global_role(self, role_id):
        return self._delete('globalrolebindings/{0}'.format(role_id))

    def create_cluster_role(self, user_id, cluster_id, role):
        return self._post('clusterroletemplatebindings', json={
            'roleTemplateId': role,
            'clusterId': cluster_id,
            'userId': user_id,
        })

    def create_project_role(self, user_id, project_id, role):
        return self._post('projectroletemplatebindings', json={
            'roleTemplateId': role,
            'projectId': project_id,
            'userId': user_id,
        })

    def delete_user(self, user_id):
        return self._delete('users/{0}'.format(user_id))

    def delete_cluster_role(self, cluster_role_id):
        return self._delete('clusterroletemplatebindings/{0}'.format(cluster_role_id))

    def list_global_catalogs(self):
        return self._get('catalogs', params={'limit': -1})['data']

    def list_cluster_catalogs(self):
        return self._get('clustercatalogs', params={'limit': -1})['data']

    def list_project_catalogs(self):
        return self._get('projectcatalogs', params={'limit': -1})['data']

    def refresh_global_catalog(self, catalog_id):
        return self._post('catalogs/{0}'.format(catalog_id), params={'action': 'refresh'})

    def refresh_cluster_catalog(self, catalog_id):
        return self._post('clustercatalogs/{0}'.format(catalog_id), params={'action': 'refresh'})

    def refresh_project_catalog(self, catalog_id):
        return self._post('projectcatalogs/{0}'.format(catalog_id), params={'action': 'refresh'})

    def delete_global_catalog(self, catalog_id):
        return self._delete('catalogs/{0}'.format(catalog_id))

    def delete_cluster_catalog(self, catalog_id):
        return self._delete('clustercatalogs/{0}'.format(catalog_id))

    def delete_project_catalog(self, catalog_id):
        return self._delete('projectcatalogs/{0}'.format(catalog_id))

    def create_global_catalog(self, spec):
        return self._post('catalogs', json=spec)

    def create_cluster_catalog(self, spec):
        return self._post('clustercatalogs', json=spec)

    def create_project_catalog(self, spec):
        return self._post('projectcatalogs', json=spec)

    def update_global_catalog(self, catalog_id, spec):
        return self._put('catalogs/{0}'.format(catalog_id), json=spec)

    def update_cluster_catalog(self, catalog_id, spec):
        return self._put('clustercatalogs/{0}'.format(catalog_id), json=spec)

    def update_project_catalog(self, catalog_id, spec):
        return self._put('projectcatalogs/{0}'.format(catalog_id), json=spec)
