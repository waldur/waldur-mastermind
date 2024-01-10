import logging
from typing import List

import requests

from waldur_core.core.utils import QuietSession

from .exceptions import NotFound, RancherException

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
        self._base_url = f"{self._host}/v3"
        self._session = requests.Session()
        if not verify_ssl:
            self._session = QuietSession()
        self._session.verify = verify_ssl

    def _request(self, method, endpoint, json=None, **kwargs):
        url = f"{self._base_url}/{endpoint}"

        try:
            response = self._session.request(method, url, json=json, **kwargs)
        except requests.RequestException as e:
            raise RancherException(e)

        data = response.content
        if data:
            content_type = response.headers["Content-Type"].lower()
            if content_type == "application/json":
                data = response.json()
            elif content_type == "text/plain":
                data = data.decode("utf-8")

        status_code = response.status_code
        if status_code in (
            requests.codes.ok,
            requests.codes.created,
            requests.codes.accepted,
            requests.codes.no_content,
        ):
            return data
        elif status_code == requests.codes.not_found:
            raise NotFound(data)
        else:
            raise RancherException(data)

    def _get(self, endpoint, **kwargs):
        return self._request("get", endpoint, **kwargs)

    def _post(self, endpoint, **kwargs):
        return self._request("post", endpoint, **kwargs)

    def _patch(self, endpoint, **kwargs):
        return self._request("patch", endpoint, **kwargs)

    def _delete(self, endpoint, **kwargs):
        return self._request("delete", endpoint, **kwargs)

    def _put(self, endpoint, **kwargs):
        return self._request("put", endpoint, **kwargs)

    def _get_yaml(self, endpoint: str):
        return self._get(
            endpoint,
            headers={"accept": "application/yaml"},
        )

    def _put_yaml(self, endpoint: str, yaml: str):
        return self._put(
            endpoint,
            data=yaml,
            headers={"content-type": "application/yaml"},
        )

    def login(self, access_key, secret_key):
        """
        Login to Rancher server using access_key and secret_key.

        :param access_key: access_key to connect
        :type access_key: string
        :param secret_key: secret_key to connect
        :type secret_key: string
        :raises Unauthorized: raised if credentials are invalid.
        """
        self._post("", auth=(access_key, secret_key))
        self._session.auth = (access_key, secret_key)
        logger.debug(f"Successfully logged in as {access_key}")

    def list_clusters(self):
        return self._get("clusters")["data"]

    def get_cluster(self, cluster_id):
        return self._get(f"clusters/{cluster_id}")

    def create_cluster(self, cluster_name, mtu=None, private_registry=None):
        rancher_config = {}

        if mtu:
            rancher_config["network"] = {"mtu": mtu}

        if private_registry:
            rancher_config["privateRegistries"] = [
                {
                    "url": private_registry["url"],
                    "user": private_registry["user"],
                    "password": private_registry["password"],
                }
            ]

        return self._post(
            "clusters",
            json={
                "name": cluster_name,
                "rancherKubernetesEngineConfig": rancher_config,
            },
        )

    def get_cluster_nodes(self, cluster_id):
        return self._get(f"clusters/{cluster_id}/nodes")["data"]

    def delete_cluster(self, cluster_id):
        return self._delete(f"clusters/{cluster_id}")

    def delete_node(self, node_id):
        return self._delete(f"nodes/{node_id}")

    def list_cluster_registration_tokens(self):
        return self._get("clusterregistrationtokens", params={"limit": -1})["data"]

    def create_cluster_registration_token(self, cluster_id):
        return self._post(
            "clusterregistrationtoken",
            json={"type": "clusterRegistrationToken", "clusterId": cluster_id},
        )

    def get_node_command(self, cluster_id):
        cluster_list = self.list_cluster_registration_tokens()
        cluster = list(filter(lambda x: x["clusterId"] == cluster_id, cluster_list))
        if cluster:
            node_command = cluster[0]["nodeCommand"]
            return node_command

    def update_cluster(self, cluster_id, new_params):
        return self._put(f"clusters/{cluster_id}", json=new_params)

    def get_node(self, node_id):
        return self._get(f"nodes/{node_id}")

    def get_kubeconfig_file(self, cluster_id):
        data = self._post(
            f"clusters/{cluster_id}", params={"action": "generateKubeconfig"}
        )
        return data["config"]

    def list_users(self):
        return self._get("users")["data"]

    def create_user(self, name, username, password, mustChangePassword=True):
        return self._post(
            "users",
            json={
                "name": name,
                "mustChangePassword": mustChangePassword,
                "password": password,
                "username": username,
                "type": "user",
                "enabled": True,
            },
        )

    def enable_user(self, user_id):
        return self._put(
            f"users/{user_id}",
            json={
                "enabled": True,
            },
        )

    def disable_user(self, user_id):
        return self._put(
            f"users/{user_id}",
            json={
                "enabled": False,
            },
        )

    def create_global_role(self, user_id, role):
        return self._post(
            "globalrolebindings",
            json={
                "globalRoleId": role,
                "userId": user_id,
            },
        )

    def delete_global_role(self, role_id):
        return self._delete(f"globalrolebindings/{role_id}")

    def create_cluster_user_role(self, user_id, cluster_id, role):
        return self._post(
            "clusterroletemplatebindings",
            json={
                "roleTemplateId": role,
                "clusterId": cluster_id,
                "userId": user_id,
            },
        )

    def create_cluster_group_role(self, group_id, cluster_id, role):
        return self._post(
            "clusterroletemplatebindings",
            json={
                "roleTemplateId": role,
                "clusterId": cluster_id,
                "groupPrincipalId": group_id,
            },
        )

    def get_cluster_group_role(self, group_id, cluster_id, role):
        return self._get(
            "clusterroletemplatebindings",
            params={
                "roleTemplateId": role,
                "clusterId": cluster_id,
                "groupPrincipalId": group_id,
            },
        )["data"]

    def create_project(self, cluster_id, project_name):
        return self._post(
            "project",
            json={
                "name": project_name,
                "clusterId": cluster_id,
                "type": "project",
                "enableProjectMonitoring": False,
            },
        )

    def create_project_user_role(self, user_id, project_id, role):
        return self._post(
            "projectroletemplatebindings",
            json={
                "roleTemplateId": role,
                "projectId": project_id,
                "userId": user_id,
            },
        )

    def get_projects_roles(self, projectId=None, roleTemplateId=None):
        params = {}
        if projectId:
            params["projectId"] = projectId
        if roleTemplateId:
            params["roleTemplateId"] = roleTemplateId
        return self._get(
            "projectroletemplatebindings",
            params=params,
        )["data"]

    def delete_user(self, user_id):
        return self._delete(f"users/{user_id}")

    def delete_cluster_role(self, cluster_role_id):
        return self._delete(f"clusterroletemplatebindings/{cluster_role_id}")

    def list_global_catalogs(self):
        return self._get("catalogs", params={"limit": -1})["data"]

    def list_cluster_catalogs(self, cluster_id=None):
        params = {"limit": -1}
        if cluster_id:
            params["cluster_id"] = cluster_id
        return self._get("clustercatalogs", params=params)["data"]

    def list_project_catalogs(self, project_id=None):
        params = {"limit": -1}
        if project_id:
            params["project_id"] = project_id
        return self._get("projectcatalogs", params=params)["data"]

    def refresh_global_catalog(self, catalog_id):
        return self._post(f"catalogs/{catalog_id}", params={"action": "refresh"})

    def refresh_cluster_catalog(self, catalog_id):
        return self._post(f"clustercatalogs/{catalog_id}", params={"action": "refresh"})

    def refresh_project_catalog(self, catalog_id):
        return self._post(f"projectcatalogs/{catalog_id}", params={"action": "refresh"})

    def delete_global_catalog(self, catalog_id):
        return self._delete(f"catalogs/{catalog_id}")

    def delete_cluster_catalog(self, catalog_id):
        return self._delete(f"clustercatalogs/{catalog_id}")

    def delete_project_catalog(self, catalog_id):
        return self._delete(f"projectcatalogs/{catalog_id}")

    def create_global_catalog(self, spec):
        return self._post("catalogs", json=spec)

    def create_cluster_catalog(self, spec):
        return self._post("clustercatalogs", json=spec)

    def create_project_catalog(self, spec):
        return self._post("projectcatalogs", json=spec)

    def update_global_catalog(self, catalog_id, spec):
        return self._put(f"catalogs/{catalog_id}", json=spec)

    def update_cluster_catalog(self, catalog_id, spec):
        return self._put(f"clustercatalogs/{catalog_id}", json=spec)

    def update_project_catalog(self, catalog_id, spec):
        return self._put(f"projectcatalogs/{catalog_id}", json=spec)

    def list_projects(self, cluster_id=None):
        params = {"limit": -1}
        if cluster_id:
            params["clusterId"] = cluster_id
        return self._get("projects", params=params)["data"]

    def list_namespaces(self, cluster_id):
        return self._get(f"cluster/{cluster_id}/namespaces", params={"limit": -1})[
            "data"
        ]

    def list_templates(self, cluster_id=None):
        params = {"limit": -1}
        if cluster_id:
            params["clusterId"] = cluster_id
        return self._get("templates", params=params)["data"]

    def get_template_icon(self, template_id):
        return self._get(f"templates/{template_id}/icon")

    def get_template_version_details(self, template_id, template_version):
        return self._get(f"templateVersions/{template_id}-{template_version}")

    def get_template_version_readme(self, template_id, template_version):
        return self._get(f"templateVersions/{template_id}-{template_version}/readme")

    def get_template_version_app_readme(self, template_id, template_version):
        return self._get(
            f"templateVersions/{template_id}-{template_version}/app-readme"
        )

    def create_application(
        self,
        catalog_id: str,
        template_id: str,
        version: str,
        project_id: str,
        namespace_id: str,
        name: str,
        answers: dict = None,
        wait: bool = False,
        timeout: int = 300,
    ):
        payload = {
            "prune": False,
            "timeout": timeout,
            "wait": wait,
            "type": "app",
            "name": name,
            "targetNamespace": namespace_id,
            "externalId": f"catalog://?catalog={catalog_id}&template={template_id}&version={version}",
            "projectId": project_id,
        }
        if answers:
            payload["answers"] = answers
        return self._post(f"projects/{project_id}/app", json=payload)

    def create_namespace(self, cluster_id: str, project_id: str, name: str):
        return self._post(
            f"clusters/{cluster_id}/namespace",
            json={
                "clusterId": cluster_id,
                "projectId": project_id,
                "name": name,
                "type": "namespace",
                "labels": {},
                "resourceQuota": None,
            },
        )

    def get_project_applications(self, project_id):
        return self._get(f"project/{project_id}/apps", params={"limit": -1})["data"]

    def list_project_secrets(self, project_id):
        return self._get(f"project/{project_id}/secrets", params={"limit": -1})["data"]

    def get_application(self, project_id, app_id):
        return self._get(f"/project/{project_id}/apps/{app_id}")

    def destroy_application(self, project_id, app_id):
        return self._delete(f"/project/{project_id}/apps/{app_id}")

    def list_workloads(self, project_id: str):
        return self._get(f"project/{project_id}/workloads", params={"limit": -1})[
            "data"
        ]

    def redeploy_workload(self, project_id: str, workload_id: str):
        return self._post(
            f"project/{project_id}/workloads/{workload_id}",
            params={"action": "redeploy"},
        )

    def delete_workload(self, project_id: str, workload_id: str):
        return self._delete(f"project/{project_id}/workloads/{workload_id}")

    def get_workload_yaml(self, project_id: str, workload_id: str):
        return self._get_yaml(
            f"project/{project_id}/workloads/{workload_id}/yaml",
        )

    def put_workload_yaml(self, project_id: str, workload_id: str, yaml: str):
        return self._put_yaml(
            f"project/{project_id}/workloads/{workload_id}/yaml",
            yaml,
        )

    def list_hpas(self, project_id: str):
        """
        List all horizontal pod autoscalers in project.
        """
        return self._get(
            f"project/{project_id}/horizontalpodautoscalers", params={"limit": -1}
        )["data"]

    def create_hpa(
        self,
        project_id: str,
        namespace_id: str,
        workload_id: str,
        name: str,
        description: str,
        min_replicas: int,
        max_replicas: int,
        metrics: List[dict],
    ):
        """
        Create horizontal pod autoscaler.
        """
        return self._post(
            f"projects/{project_id}/horizontalpodautoscalers",
            json={
                "name": name,
                "description": description,
                "namespaceId": namespace_id,
                "workloadId": workload_id,
                "minReplicas": min_replicas,
                "maxReplicas": max_replicas,
                "metrics": metrics,
            },
        )

    def update_hpa(
        self,
        project_id: str,
        hpa_id: str,
        namespace_id: str,
        workload_id: str,
        name: str,
        description: str,
        min_replicas: int,
        max_replicas: int,
        metrics: List[dict],
    ):
        """
        Update horizontal pod autoscaler.
        """
        return self._put(
            f"projects/{project_id}/horizontalpodautoscalers/{hpa_id}",
            json={
                "name": name,
                "description": description,
                "namespaceId": namespace_id,
                "workloadId": workload_id,
                "minReplicas": min_replicas,
                "maxReplicas": max_replicas,
                "metrics": metrics,
            },
        )

    def delete_hpa(self, project_id: str, hpa_id: str):
        """
        Delete horizontal pod autoscaler.
        """
        return self._delete(f"projects/{project_id}/horizontalpodautoscalers/{hpa_id}")

    def get_hpa_yaml(self, project_id: str, hpa_id: str):
        return self._get_yaml(
            f"projects/{project_id}/horizontalpodautoscalers/{hpa_id}/yaml",
        )

    def put_hpa_yaml(self, project_id: str, hpa_id: str, yaml: str):
        return self._put_yaml(
            f"projects/{project_id}/horizontalpodautoscalers/{hpa_id}/yaml",
            yaml,
        )

    def list_ingresses(self, project_id: str):
        return self._get(f"project/{project_id}/ingresses", params={"limit": -1})[
            "data"
        ]

    def get_ingress_yaml(self, project_id: str, ingress_id: str):
        return self._get_yaml(
            f"project/{project_id}/ingresses/{ingress_id}/yaml",
        )

    def put_ingress_yaml(self, project_id: str, ingress_id: str, yaml: str):
        return self._put_yaml(
            f"project/{project_id}/ingresses/{ingress_id}/yaml",
            yaml,
        )

    def delete_ingress(self, project_id: str, ingress_id: str):
        return self._delete(f"project/{project_id}/ingresses/{ingress_id}")

    def list_services(self, project_id: str):
        return self._get(f"project/{project_id}/services", params={"limit": -1})["data"]

    def get_service_yaml(self, project_id: str, service_id: str):
        return self._get_yaml(
            f"project/{project_id}/services/{service_id}/yaml",
        )

    def put_service_yaml(self, project_id: str, service_id: str, yaml: str):
        return self._put_yaml(
            f"project/{project_id}/services/{service_id}/yaml",
            yaml,
        )

    def delete_service(self, project_id: str, service_id: str):
        return self._delete(f"project/{project_id}/services/{service_id}")

    def import_yaml(
        self,
        cluster_id: str,
        yaml: str,
        default_namespace_id: str = None,
        namespace_id: str = None,
    ):
        payload = {"yaml": yaml}
        if default_namespace_id:
            payload["defaultNamespace"] = default_namespace_id
        if namespace_id:
            payload["namespace"] = namespace_id
        return self._post(f"clusters/{cluster_id}?action=importYaml", json=payload)
