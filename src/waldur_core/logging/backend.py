import logging

import requests
from django.conf import settings
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class RabbitMQManagementBackend:
    def __init__(self) -> None:
        self.rmq_management_url = f"http://{settings.RABBITMQ_MQTT['HOST']}:{settings.RABBITMQ_MQTT['MANAGEMENT_PORT']}/api"
        self.rmq_auth = HTTPBasicAuth(
            settings.RABBITMQ_MQTT["USER"], settings.RABBITMQ_MQTT["PASSWORD"]
        )

    def create_rabbitmq_virtual_host(self, vhost_name: str) -> bool:
        """Create a new virtual host in RabbitMQ.

        Args:
            vhost_name (str): Name of the virtual host to create

        Returns:
            bool: True if virtual host was created successfully or already exists,
                 False if creation failed or an error occurred
        """
        vhost_url = f"{self.rmq_management_url}/vhosts/{vhost_name}"
        try:
            logger.info("Creating a virtual host '%s' in RabbitMQ", vhost_name)
            response = requests.put(vhost_url, auth=self.rmq_auth, timeout=10)

            if response.status_code == 201:
                logger.info("Virtual host '%s' created successfully", vhost_name)
            elif response.status_code == 204:
                logger.warning("Virtual host '%s' already exists", vhost_name)
            else:
                logger.error(
                    "Failed to create virtual host '%s', status code: %s, response: %s",
                    vhost_name,
                    response.status_code,
                    response.text,
                )
                return False

            return True
        except requests.ConnectionError as exc:
            logger.error(
                "Connection error while creating virtual host '%s' in RabbitMQ: %s",
                vhost_name,
                exc,
            )
            return False
        except requests.Timeout as exc:
            logger.error(
                "Timeout occurred while creating virtual host '%s' in RabbitMQ: %s",
                vhost_name,
                exc,
            )
            return False
        except Exception as exc:
            logger.error(
                "An unexpected error occurred while creating virtual host '%s': %s",
                vhost_name,
                exc,
            )
            return False

    def create_rabbitmq_user(self, username: str, password: str) -> bool:
        """Create a new RabbitMQ user with the specified username and password.

        Args:
            username (str): The username for the new RabbitMQ user
            password (str): The password for the new RabbitMQ user

        Returns:
            bool: True if user creation was successful or user already exists,
                 False if creation failed or an error occurred

        Note:
            The user is created without any permissions by default.
            Use assign_rabbitmq_vhost_permissions() to grant specific permissions.
        """
        url = f"{self.rmq_management_url}/users/{username}"
        payload = {"password": password, "tags": []}

        try:
            logger.info("Creating a user '%s' in RabbitMQ", username)
            response = requests.put(url, json=payload, auth=self.rmq_auth, timeout=10)

            if response.status_code == 201:
                logger.info("User '%s' created successfully in RabbitMQ", username)
                return True
            elif response.status_code == 204:
                logger.warning("User '%s' already exists in RabbitMQ", username)
                return True
            else:
                logger.error(
                    "Failed to create user '%s', status code: %s, response: %s",
                    username,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.ConnectionError as exc:
            logger.error(
                "Unable to create user '%s' in RabbitMQ, error: %s", username, exc
            )
            return False
        except requests.Timeout as exc:
            logger.error(
                "Timeout occurred while creating user '%s' in RabbitMQ: %s",
                username,
                exc,
            )
            return False
        except Exception as exc:
            logger.error(
                "An unexpected error occurred while creating user '%s': %s",
                username,
                exc,
            )
            return False

    def assign_rabbitmq_vhost_permissions(
        self, username: str, vhost: str, permissions: dict
    ) -> bool:
        """Assign permissions for a user on a specific virtual host.

        Args:
            username (str): The RabbitMQ username
            vhost (str): The virtual host name
            permissions (dict): Dictionary containing configure, write, and read permissions

        Returns:
            bool: True if permissions were set successfully, False otherwise
        """
        url = f"{self.rmq_management_url}/permissions/{vhost}/{username}"

        try:
            logger.info(
                "Assigning user %s permissions for vhost %s in RabbitMQ",
                username,
                vhost,
            )
            response = requests.put(
                url, json=permissions, auth=self.rmq_auth, timeout=10
            )

            if response.status_code in (201, 204):
                logger.info(
                    "Permissions for user '%s' on vhost '%s' set successfully.",
                    username,
                    vhost,
                )
                return True
            else:
                logger.error(
                    "Failed to set permissions for user %s on vhost %s, status code: %s, response: %s",
                    username,
                    vhost,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.ConnectionError as exc:
            logger.error(
                "Unable to assign permissions for user %s and vhost %s in RabbitMQ, error: %s",
                username,
                vhost,
                exc,
            )
            return False
        except Exception as exc:
            logger.error(
                "An unexpected error occurred while assigning permissions for user %s and vhost %s: %s",
                username,
                vhost,
                exc,
            )
            return False

    def delete_rabbitmq_virtual_host(self, vhost_name: str) -> bool:
        """Delete a virtual host from RabbitMQ.

        Args:
            vhost_name (str): Name of the virtual host to delete

        Returns:
            bool: True if virtual host was deleted successfully, False otherwise
        """
        vhost_name_encoded = requests.utils.quote(vhost_name, safe="")

        url = f"{self.rmq_management_url}/vhosts/{vhost_name_encoded}"

        try:
            response = requests.delete(url, auth=self.rmq_auth, timeout=10)

            if response.status_code == 204:
                logger.info("Virtual host %s deleted successfully.", vhost_name)
                return True
            else:
                logger.error(
                    "Failed to delete virtual host %s, status code: %s, response: %s",
                    vhost_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.ConnectionError as exc:
            logger.error(
                "Unable to delete vhost %s from RabbitMQ, error: %s", vhost_name, exc
            )
            return False
        except requests.Timeout as exc:
            logger.error(
                "Timeout occurred while deleting vhost %s from RabbitMQ: %s",
                vhost_name,
                exc,
            )
            return False
        except Exception as exc:
            logger.error(
                "An unexpected error occurred while deleting vhost %s: %s",
                vhost_name,
                exc,
            )
            return False

    def delete_rabbitmq_user(self, username: str) -> bool:
        """Delete a RabbitMQ user.

        Args:
            username: The username of the RabbitMQ user to delete

        Returns:
            bool: True if deletion was successful, False otherwise
        """
        url = f"{self.rmq_management_url}/users/{username}"

        try:
            response = requests.delete(url, auth=self.rmq_auth, timeout=10)

            if response.status_code == 204:
                logger.info("User %s deleted successfully from RabbitMQ", username)
                return True
            else:
                logger.error(
                    "Failed to delete user %s, status code: %s, response: %s",
                    username,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.ConnectionError as exc:
            logger.error(
                "Unable to delete user %s from RabbitMQ, error: %s", username, exc
            )
            return False
        except requests.Timeout as exc:
            logger.error(
                "Timeout occurred while deleting user %s from RabbitMQ: %s",
                username,
                exc,
            )
            return False
        except Exception as exc:
            logger.error(
                "An unexpected error occurred while deleting user %s: %s", username, exc
            )
            return False

    def get_user(self, username: str) -> dict | None:
        """Get RabbitMQ user information.

        Args:
            username (str): The username of the RabbitMQ user to retrieve

        Returns:
            dict | None: Dictionary containing user information if found and request successful,
                        None if user doesn't exist or request failed
        """
        url = f"{self.rmq_management_url}/users/{username}"
        try:
            response = requests.get(url, auth=self.rmq_auth, timeout=10)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.debug("User %s not found in RabbitMQ", username)
                return None
            else:
                logger.error(
                    "Failed to get user %s info, status code: %s, response: %s",
                    username,
                    response.status_code,
                    response.text,
                )
                raise Exception(f"Failed to get user {username} info")
        except requests.ConnectionError as exc:
            logger.error(
                "Unable to get user %s info from RabbitMQ, error: %s", username, exc
            )
            raise
        except requests.Timeout as exc:
            logger.error(
                "Timeout occurred while getting user %s info from RabbitMQ: %s",
                username,
                exc,
            )
            raise
        except Exception as exc:
            logger.error(
                "An unexpected error occurred while getting user %s info: %s",
                username,
                exc,
            )
            raise

    def get_user_connections(self, username: str) -> list[dict]:
        url = f"{self.rmq_management_url}/connections/username/{username}/"
        try:
            response = requests.get(url, auth=self.rmq_auth, timeout=10)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.debug("User %s connections not found in RabbitMQ", username)
                return []
            else:
                logger.error(
                    "Failed to get user %s connections, status code: %s, response: %s",
                    username,
                    response.status_code,
                    response.text,
                )
                raise Exception(f"Failed to get user {username} connections")
        except requests.ConnectionError as exc:
            logger.error(
                "Unable to get user %s connections from RabbitMQ, error: %s",
                username,
                exc,
            )
            raise

    def delete_user(self, username: str) -> bool:
        url = f"{self.rmq_management_url}/users/{username}/"
        try:
            response = requests.delete(url, auth=self.rmq_auth, timeout=10)
            if response.status_code == 204:
                logger.info("User %s deleted successfully", username)
                return True
            else:
                logger.error(
                    "Failed to delete user %s, status code: %s",
                    username,
                    response.status_code,
                )
                return False
        except Exception as exc:
            logger.error(
                "An unexpected error occurred while deleting user %s: %s",
                username,
                exc,
            )
            return False
