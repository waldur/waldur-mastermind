import logging
from datetime import timedelta

import requests
from requests import exceptions as requests_exceptions
from rest_framework import status

logger = logging.getLogger(__name__)


class HeappeBackend:
    def __init__(self, heappe_config):
        self.heappe_config = heappe_config

    def get_heappe_session_code(self):
        response = requests.post(
            f"{self.heappe_config.heappe_url}/heappe/UserAndLimitationManagement/AuthenticateUserPassword",
            json={
                "Credentials": {
                    "Username": self.heappe_config.heappe_username,
                    "Password": self.heappe_config.heappe_password,
                }
            },
        )

        if response.status_code != status.HTTP_200_OK:
            logger.error("Unable to receive HEAppE session code.")
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

        return response.json()

    def create_heappe_project(self, lexis_link):
        heappe_session_code = self.get_heappe_session_code()
        resource = lexis_link.robot_account.resource
        payload = {
            "SessionCode": heappe_session_code,
            "Name": resource.name,
            "Description": resource.description,
            "AccountingString": resource.backend_id,
            "StartDate": resource.created.isoformat(),
            # TODO: come up with a better solution for the EndDate field
            "EndDate": resource.end_date.isoformat()
            if resource.end_date
            else (resource.created + timedelta(weeks=60)).isoformat(),
            "UsageType": 1,
        }
        response = requests.post(
            f"{self.heappe_config.heappe_url}/heappe/Management/Project",
            json=payload,
        )
        if response.status_code != status.HTTP_200_OK:
            logger.error("Unable to create a project in HEAppE")
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

        return response.json()

    def get_heappe_project(self, lexis_link):
        heappe_session_code = self.get_heappe_session_code()
        resource = lexis_link.robot_account.resource
        response = requests.get(
            f"{self.heappe_config.heappe_url}/heappe/UserAndLimitationManagement/ProjectsForCurrentUser",
            params={"sessionCode": heappe_session_code},
        )

        if response.status_code != status.HTTP_200_OK:
            logger.error("Unable to get project list from HEAppE")
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

        map_project_names = {
            (item["Project"]["Name"], item["Project"]["AccountingString"]): item[
                "Project"
            ]
            for item in response.json()
        }

        return map_project_names.get((resource.name, resource.backend_id))

    def get_or_create_heappe_project(self, lexis_link):
        project = self.get_heappe_project(lexis_link)
        if project is None:
            project = self.create_heappe_project(lexis_link)
        lexis_link.heappe_project_id = project["Id"]
        lexis_link.save(update_fields=["heappe_project_id"])

    def delete_heappe_project(self, lexis_link):
        heappe_session_code = self.get_heappe_session_code()
        response = requests.delete(
            f"{self.heappe_config.heappe_url}/heappe/Management/Project",
            json={
                "SessionCode": heappe_session_code,
                "Id": lexis_link.heappe_project_id,
            },
        )
        if response.status_code != status.HTTP_200_OK:
            logger.error(
                "Unable to delete a project [id=%s] from HEAppE",
                lexis_link.heappe_project_id,
            )
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

    def create_ssh_key(self, lexis_link):
        heappe_session_code = self.get_heappe_session_code()
        username = lexis_link.robot_account.username
        response = requests.post(
            url=f"{self.heappe_config.heappe_url}/heappe/Management/SecureShellKey",
            json={
                "SessionCode": heappe_session_code,
                "Username": username,
                "Password": self.heappe_config.heappe_cluster_password,
                "ProjectId": lexis_link.heappe_project_id,
            },
        )
        if response.status_code != status.HTTP_200_OK:
            logger.error("Unable to create SSH key in HEAppE")
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

        ssh_key = response.json()
        ssh_key_rsa = ssh_key["PublicKeyOpenSSH"]
        lexis_link.robot_account.keys = [ssh_key_rsa]
        lexis_link.robot_account.save(update_fields=["keys"])

    def delete_ssh_key(self, lexis_link):
        heappe_session_code = self.get_heappe_session_code()
        key = lexis_link.robot_account.keys[0]
        response = requests.delete(
            url=f"{self.heappe_config.heappe_url}/heappe/Management/SecureShellKey",
            json={
                "SessionCode": heappe_session_code,
                "ProjectId": lexis_link.heappe_project_id,
                "PublicKey": key,
            },
        )
        if response.status_code != status.HTTP_200_OK:
            logger.error("Unable to delete SSH key from HEAppE")
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

    def list_available_clusters(self):
        response = requests.get(
            f"{self.heappe_config.heappe_url}/heappe/ClusterInformation/ListAvailableClusters"
        )

        if response.status_code != status.HTTP_200_OK:
            logger.error("Unable to fetch available clusters from HEAppE")
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

        return response.json()

    def connect_heappe_project_to_cluster(self, lexis_link):
        heappe_project_id = lexis_link.heappe_project_id
        clusters = self.list_available_clusters()
        cluster_project_list_map = {
            cluster["Id"]: [
                project["Id"] for project in cluster["NodeTypes"][0]["Projects"]
            ]
            for cluster in clusters
            if cluster.get("NodeTypes")
        }
        for cluster_id, project_ids in cluster_project_list_map.items():
            if heappe_project_id in project_ids:
                logger.info(
                    "The project [id=%s] is already connected to cluster [id=%s]",
                    heappe_project_id,
                    cluster_id,
                )
                return
        heappe_session_code = self.get_heappe_session_code()
        local_base_path = f"{self.heappe_config.heappe_local_base_path}/{lexis_link.robot_account.resource.backend_id}"
        response = requests.post(
            url=f"{self.heappe_config.heappe_url}/heappe/Management/ProjectAssignmentToCluster",
            json={
                "SessionCode": heappe_session_code,
                "ProjectId": heappe_project_id,
                "ClusterId": self.heappe_config.heappe_cluster_id,
                "LocalBasepath": local_base_path,
            },
        )

        if response.status_code != status.HTTP_200_OK:
            logger.error(
                "Unable to connect project [id=%s] to cluster [id=%s]",
                heappe_project_id,
                self.heappe_config.heappe_cluster_id,
            )
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

    def init_cluster_script_directory(self, lexis_link):
        heappe_session_code = self.get_heappe_session_code()
        heappe_project_id = lexis_link.heappe_project_id
        key = lexis_link.robot_account.keys[0]
        response = requests.post(
            f"{self.heappe_config.heappe_url}/heappe/Management/InitializeClusterScriptDirectory",
            json={
                "SessionCode": heappe_session_code,
                "ProjectId": heappe_project_id,
                "PublicKey": key,
                "ClusterProjectRootDirectory": self.heappe_config.heappe_local_base_path,
            },
        )

        if response.status_code != status.HTTP_200_OK:
            logger.error(
                "Unable to init cluster script directory for project [id=%s]",
                heappe_project_id,
            )
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )
