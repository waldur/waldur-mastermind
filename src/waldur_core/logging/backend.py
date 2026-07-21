import logging
from datetime import UTC

import requests
from django.conf import settings
from requests.auth import HTTPBasicAuth
from rest_framework import status

from waldur_core.logging.exceptions import RabbitMQError

logger = logging.getLogger(__name__)

# Standard queue arguments for subscription queues.
# These arguments must be set when creating queues to avoid precondition_failed
# errors when publishers send messages with these headers.
SUBSCRIPTION_QUEUE_ARGUMENTS = {
    "x-message-ttl": 60 * 60 * 1000,  # one hour in milliseconds
    "x-max-length": 10000,
    "x-overflow": "reject-publish-dlx",
    "x-dead-letter-exchange": "",
    "x-dead-letter-routing-key": "waldur.dlq.messages",
}


class RabbitMQManagementBackend:
    def __init__(self) -> None:
        self.rmq_management_url = f"http://{settings.RABBITMQ['HOST']}:{settings.RABBITMQ['MANAGEMENT_PORT']}/api"
        self.rmq_auth = HTTPBasicAuth(
            settings.RABBITMQ["USER"], settings.RABBITMQ["PASSWORD"]
        )

    def create_rabbitmq_virtual_host(self, vhost_name: str) -> bool:
        """Create a new virtual host in RabbitMQ.

        Args:
            vhost_name (str): Name of the virtual host to create

        Returns:
            bool: True if virtual host was created successfully or already exists,
                 False if creation failed or an error occurred

        In terms of Waldur a virtual host corresponds to a user.
        """
        vhost_name_encoded = requests.utils.quote(vhost_name, safe="")
        vhost_url = f"{self.rmq_management_url}/vhosts/{vhost_name_encoded}"
        try:
            logger.info("Creating a virtual host '%s' in RabbitMQ", vhost_name)
            response = requests.put(vhost_url, auth=self.rmq_auth, timeout=10)
        except requests.RequestException as exc:
            logger.error(
                "Failed to create virtual host '%s' in RabbitMQ: %s",
                vhost_name,
                exc,
            )
            return False

        if response.status_code == status.HTTP_201_CREATED:
            logger.info("Virtual host '%s' created successfully", vhost_name)
            return True
        elif response.status_code == status.HTTP_204_NO_CONTENT:
            logger.warning("Virtual host '%s' already exists", vhost_name)
            return True
        else:
            logger.error(
                "Failed to create virtual host '%s', status code: %s, response: %s",
                vhost_name,
                response.status_code,
                response.text,
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
        except requests.RequestException as exc:
            logger.error(
                "Unable to delete vhost %s from RabbitMQ, error: %s", vhost_name, exc
            )
            return False

        if response.status_code != status.HTTP_204_NO_CONTENT:
            logger.error(
                "Failed to delete virtual host %s, status code: %s, response: %s",
                vhost_name,
                response.status_code,
                response.text,
            )
            return False

        logger.info("Virtual host %s deleted successfully.", vhost_name)
        return True

    def list_rabbitmq_virtual_hosts(self) -> list[str]:
        """List all virtual hosts in RabbitMQ.

        Returns:
            List[str]: List of virtual host names
        """
        url = f"{self.rmq_management_url}/vhosts"

        try:
            response = requests.get(url, auth=self.rmq_auth, timeout=10)
        except requests.RequestException as exc:
            logger.error("Unable to list virtual hosts in RabbitMQ, error: %s", exc)
            return []

        if response.status_code != status.HTTP_200_OK:
            logger.error(
                "Failed to list virtual hosts, status code: %s, response: %s",
                response.status_code,
                response.text,
            )
            return []

        try:
            vhosts = response.json()
            return [vhost["name"] for vhost in vhosts]
        except (requests.JSONDecodeError, TypeError, KeyError):
            logger.error(
                "Failed to parse JSON response while listing virtual hosts, response: %s",
                response.text,
            )
            return []

    def list_rabbitmq_vhost_permissions(self, vhost_name: str) -> list[str]:
        """List all permissions for a virtual host in RabbitMQ.

        Args:
            vhost_name (str): The name of the virtual host

        Returns:
            List[str]: List of user with access to he vhost
        """
        vhost_name_encoded = requests.utils.quote(vhost_name, safe="")
        url = f"{self.rmq_management_url}/vhosts/{vhost_name_encoded}/permissions/"

        try:
            response = requests.get(url, auth=self.rmq_auth, timeout=10)
        except requests.RequestException as exc:
            logger.error(
                "Unable to list permissions for vhost %s in RabbitMQ, error: %s",
                vhost_name,
                exc,
            )
            return []

        if response.status_code != status.HTTP_200_OK:
            logger.error(
                "Failed to list permissions for vhost %s, status code: %s, response: %s",
                vhost_name,
                response.status_code,
                response.text,
            )
            return []

        try:
            permissions = response.json()
            return [permission["user"] for permission in permissions]
        except (requests.JSONDecodeError, TypeError, KeyError):
            logger.error("Failed to parse permissions: %s", permissions)
            return []

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

        In terms of Waldur an RMQ user corresponds to an event subscription (i.e. user session).
        """
        url = f"{self.rmq_management_url}/users/{username}"
        payload = {"password": password, "tags": []}

        try:
            logger.info("Creating a user '%s' in RabbitMQ", username)
            response = requests.put(url, json=payload, auth=self.rmq_auth, timeout=10)

            if response.status_code == status.HTTP_201_CREATED:
                logger.info("User '%s' created successfully in RabbitMQ", username)
                return True
            elif response.status_code == status.HTTP_204_NO_CONTENT:
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
        self, username: str, vhost_name: str, permissions: dict
    ) -> bool:
        """Assign permissions for a user on a specific virtual host.

        Args:
            username (str): The RabbitMQ username
            vhost_name (str): The virtual host name
            permissions (dict): Dictionary containing configure, write, and read permissions

        Returns:
            bool: True if permissions were set successfully, False otherwise
        """

        vhost_name_encoded = requests.utils.quote(vhost_name, safe="")
        url = f"{self.rmq_management_url}/permissions/{vhost_name_encoded}/{username}"

        try:
            logger.info(
                "Assigning user %s permissions for vhost %s in RabbitMQ",
                username,
                vhost_name,
            )
            response = requests.put(
                url, json=permissions, auth=self.rmq_auth, timeout=10
            )

            if response.status_code in (
                status.HTTP_201_CREATED,
                status.HTTP_204_NO_CONTENT,
            ):
                logger.info(
                    "Permissions for user '%s' on vhost '%s' set successfully.",
                    username,
                    vhost_name,
                )
                return True
            else:
                logger.error(
                    "Failed to set permissions for user %s on vhost %s, status code: %s, response: %s",
                    username,
                    vhost_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.ConnectionError as exc:
            logger.error(
                "Unable to assign permissions for user %s and vhost %s in RabbitMQ, error: %s",
                username,
                vhost_name,
                exc,
            )
            return False
        except Exception as exc:
            logger.error(
                "An unexpected error occurred while assigning permissions for user %s and vhost %s: %s",
                username,
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

            if response.status_code == status.HTTP_204_NO_CONTENT:
                logger.info("User %s deleted successfully from RabbitMQ", username)
                return True
            elif response.status_code == status.HTTP_404_NOT_FOUND:
                logger.info("The user %s does not exist in RabbitMQ")
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

    def list_rabbitmq_users(self) -> list[str]:
        """List all users in RabbitMQ."""
        url = f"{self.rmq_management_url}/users"

        try:
            response = requests.get(url, auth=self.rmq_auth, timeout=10)
        except requests.RequestException as exc:
            logger.error("Unable to list users in RabbitMQ, error: %s", exc)
            return []

        if response.status_code != status.HTTP_200_OK:
            logger.error(
                "Failed to list users, status code: %s, response: %s",
                response.status_code,
                response.text,
            )
            return []

        try:
            users = response.json()
            return [user["name"] for user in users]
        except (requests.JSONDecodeError, TypeError, KeyError):
            logger.error("Failed to parse users: %s", users)
            return []

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
        except requests.RequestException as exc:
            logger.error(
                "Unable to get user %s info from RabbitMQ, error: %s", username, exc
            )
            raise

        if response.status_code == status.HTTP_404_NOT_FOUND:
            logger.debug("User %s not found in RabbitMQ", username)
            return None
        elif response.status_code == status.HTTP_200_OK:
            try:
                return response.json()
            except requests.JSONDecodeError:
                logger.error(
                    "Failed to decode JSON response while getting user %s info: %s",
                    username,
                    response,
                )
                raise RabbitMQError(f"Failed to get user {username} info")
        else:
            logger.error(
                "Failed to get user %s info, status code: %s, response: %s",
                username,
                response.status_code,
                response.text,
            )
            raise RabbitMQError(f"Failed to get user {username} info")

    def get_user_connections(self, username: str) -> list[dict]:
        url = f"{self.rmq_management_url}/connections/username/{username}/"
        try:
            response = requests.get(url, auth=self.rmq_auth, timeout=10)
        except requests.RequestException as exc:
            logger.error(
                "Unable to get user %s connections from RabbitMQ, error: %s",
                username,
                exc,
            )
            raise

        if response.status_code == status.HTTP_404_NOT_FOUND:
            logger.debug("User %s connections not found in RabbitMQ", username)
            return []
        elif response.status_code == status.HTTP_200_OK:
            try:
                return response.json()
            except requests.JSONDecodeError:
                logger.error(
                    "Failed to decode JSON response while getting user %s connections: %s",
                    username,
                    response,
                )
                raise RabbitMQError(f"Failed to get user {username} connections")
        else:
            logger.error(
                "Failed to get user %s connections, status code: %s, response: %s",
                username,
                response.status_code,
                response.text,
            )
            raise RabbitMQError(f"Failed to get user {username} connections")

    def get_user_connections_enriched(self, username: str) -> list[dict]:
        """Get enriched connection data for a RabbitMQ user.

        Returns detailed connection information including client properties,
        traffic statistics, and connection state.

        Args:
            username: The RabbitMQ username

        Returns:
            list[dict]: List of enriched connection dictionaries with keys:
                - source_ip: Client IP address
                - vhost: Virtual host name
                - connected_at: Connection timestamp (ISO format)
                - state: Connection state (running, blocked, blocking)
                - recv_oct: Bytes received
                - send_oct: Bytes sent
                - channels: Number of channels
                - timeout: Heartbeat timeout in seconds
                - client_properties: Dict with product, version, platform
        """
        from datetime import datetime

        raw_connections = self.get_user_connections(username)
        enriched = []

        for conn in raw_connections:
            # Parse source IP from connection name (format: "ip:port -> ip:port")
            source_ip = conn.get("name", "").split(" ->")[0].rsplit(":", 1)[0]

            # Convert connected_at from milliseconds timestamp
            connected_at = None
            if conn.get("connected_at"):
                try:
                    connected_at = datetime.fromtimestamp(
                        conn["connected_at"] / 1000, tz=UTC
                    ).isoformat()
                except (ValueError, TypeError, OSError):
                    pass

            # Extract client properties
            client_props = conn.get("client_properties", {})
            client_properties = {
                "product": client_props.get("product"),
                "version": client_props.get("version"),
                "platform": client_props.get("platform"),
            }

            enriched.append(
                {
                    "source_ip": source_ip,
                    "vhost": conn.get("vhost", ""),
                    "connected_at": connected_at,
                    "state": conn.get("state", "unknown"),
                    "recv_oct": conn.get("recv_oct", 0),
                    "send_oct": conn.get("send_oct", 0),
                    "channels": conn.get("channels", 0),
                    "timeout": conn.get("timeout"),
                    "client_properties": client_properties,
                }
            )

        return enriched

    def get_overview(self) -> dict:
        """Get RabbitMQ cluster overview statistics.

        Returns:
            dict: Overview statistics including:
                - cluster_name: Name of the RabbitMQ cluster
                - rabbitmq_version: RabbitMQ version
                - erlang_version: Erlang/OTP version
                - message_stats: Message throughput statistics
                - queue_totals: Total queue message counts
                - object_totals: Counts of connections, channels, queues, etc.
                - node: Current node name
                - listeners: List of protocol listeners
        """
        url = f"{self.rmq_management_url}/overview"

        try:
            response = requests.get(url, auth=self.rmq_auth, timeout=10)
        except requests.RequestException as exc:
            logger.error("Unable to get RabbitMQ overview, error: %s", exc)
            raise RabbitMQError("Failed to get RabbitMQ overview")

        if response.status_code != status.HTTP_200_OK:
            logger.error(
                "Failed to get RabbitMQ overview, status code: %s, response: %s",
                response.status_code,
                response.text,
            )
            raise RabbitMQError("Failed to get RabbitMQ overview")

        try:
            data = response.json()
        except requests.JSONDecodeError:
            logger.error("Failed to decode RabbitMQ overview response")
            raise RabbitMQError("Failed to decode RabbitMQ overview response")

        # Extract message stats with rates
        message_stats = data.get("message_stats", {})
        extracted_message_stats = {
            "publish": message_stats.get("publish", 0),
            "publish_rate": message_stats.get("publish_details", {}).get("rate", 0.0),
            "deliver": message_stats.get("deliver", 0),
            "deliver_rate": message_stats.get("deliver_details", {}).get("rate", 0.0),
            "confirm": message_stats.get("confirm", 0),
            "confirm_rate": message_stats.get("confirm_details", {}).get("rate", 0.0),
            "ack": message_stats.get("ack", 0),
            "ack_rate": message_stats.get("ack_details", {}).get("rate", 0.0),
        }

        # Extract queue totals
        queue_totals = data.get("queue_totals", {})
        extracted_queue_totals = {
            "messages": queue_totals.get("messages", 0),
            "messages_ready": queue_totals.get("messages_ready", 0),
            "messages_unacknowledged": queue_totals.get("messages_unacknowledged", 0),
        }

        # Extract object totals
        object_totals = data.get("object_totals", {})
        extracted_object_totals = {
            "connections": object_totals.get("connections", 0),
            "channels": object_totals.get("channels", 0),
            "exchanges": object_totals.get("exchanges", 0),
            "queues": object_totals.get("queues", 0),
            "consumers": object_totals.get("consumers", 0),
        }

        # Extract listeners
        listeners = []
        for listener in data.get("listeners", []):
            listeners.append(
                {
                    "protocol": listener.get("protocol", "unknown"),
                    "port": listener.get("port", 0),
                }
            )

        return {
            "cluster_name": data.get("cluster_name", ""),
            "rabbitmq_version": data.get("rabbitmq_version", ""),
            "erlang_version": data.get("erlang_version", ""),
            "message_stats": extracted_message_stats,
            "queue_totals": extracted_queue_totals,
            "object_totals": extracted_object_totals,
            "node": data.get("node", ""),
            "listeners": listeners,
        }

    def list_queues(self, vhost: str) -> list[dict]:
        """List all queues in a virtual host.

        Args:
            vhost (str): The virtual host name

        Returns:
            list[dict]: List of queue information dictionaries with keys:
                - name: Queue name
                - messages: Total message count
                - messages_ready: Ready message count
                - messages_unacknowledged: Unacknowledged message count
                - consumers: Number of consumers
                - message_ttl: Message TTL in milliseconds (if set)
                - max_length: Maximum number of messages (if set)
                - max_length_bytes: Maximum total size in bytes (if set)
                - expires: Queue TTL - auto-delete after idle in ms (if set)
                - overflow: Behavior when full (if set)
                - dead_letter_exchange: DLX exchange name (if set)
                - dead_letter_routing_key: DLX routing key (if set)
                - max_priority: Max priority level 1-255 (if set)
                - queue_mode: 'default' or 'lazy' (if set)
                - queue_type: 'classic', 'quorum', or 'stream' (if set)
        """
        vhost_encoded = requests.utils.quote(vhost, safe="")
        url = f"{self.rmq_management_url}/queues/{vhost_encoded}"

        try:
            response = requests.get(url, auth=self.rmq_auth, timeout=30)
        except requests.RequestException as exc:
            logger.error(
                "Unable to list queues for vhost %s from RabbitMQ, error: %s",
                vhost,
                exc,
            )
            raise RabbitMQError(f"Failed to list queues for vhost {vhost}")

        if response.status_code == status.HTTP_404_NOT_FOUND:
            logger.debug("Vhost %s not found in RabbitMQ", vhost)
            return []
        elif response.status_code == status.HTTP_200_OK:
            try:
                queues = response.json()
                return [
                    {
                        "name": q.get("name", ""),
                        "messages": q.get("messages", 0),
                        "messages_ready": q.get("messages_ready", 0),
                        "messages_unacknowledged": q.get("messages_unacknowledged", 0),
                        "consumers": q.get("consumers", 0),
                        "message_ttl": q.get("arguments", {}).get("x-message-ttl"),
                        "max_length": q.get("arguments", {}).get("x-max-length"),
                        "max_length_bytes": q.get("arguments", {}).get(
                            "x-max-length-bytes"
                        ),
                        "expires": q.get("arguments", {}).get("x-expires"),
                        "overflow": q.get("arguments", {}).get("x-overflow"),
                        "dead_letter_exchange": q.get("arguments", {}).get(
                            "x-dead-letter-exchange"
                        ),
                        "dead_letter_routing_key": q.get("arguments", {}).get(
                            "x-dead-letter-routing-key"
                        ),
                        "max_priority": q.get("arguments", {}).get("x-max-priority"),
                        "queue_mode": q.get("arguments", {}).get("x-queue-mode"),
                        "queue_type": q.get("arguments", {}).get("x-queue-type"),
                    }
                    for q in queues
                ]
            except requests.JSONDecodeError:
                logger.error(
                    "Failed to decode JSON response while listing queues for vhost %s",
                    vhost,
                )
                raise RabbitMQError(f"Failed to list queues for vhost {vhost}")
        else:
            logger.error(
                "Failed to list queues for vhost %s, status code: %s, response: %s",
                vhost,
                response.status_code,
                response.text,
            )
            raise RabbitMQError(f"Failed to list queues for vhost {vhost}")

    def purge_queue(self, vhost: str, queue: str) -> int:
        """Purge all messages from a queue.

        Args:
            vhost (str): The virtual host name
            queue (str): The queue name

        Returns:
            int: Number of messages purged
        """
        vhost_encoded = requests.utils.quote(vhost, safe="")
        queue_encoded = requests.utils.quote(queue, safe="")
        url = (
            f"{self.rmq_management_url}/queues/{vhost_encoded}/{queue_encoded}/contents"
        )

        try:
            response = requests.delete(url, auth=self.rmq_auth, timeout=60)
        except requests.RequestException as exc:
            logger.error(
                "Unable to purge queue %s in vhost %s from RabbitMQ, error: %s",
                queue,
                vhost,
                exc,
            )
            raise RabbitMQError(f"Failed to purge queue {queue} in vhost {vhost}")

        if response.status_code == status.HTTP_204_NO_CONTENT:
            logger.info("Queue %s in vhost %s purged successfully", queue, vhost)
            return 0  # RabbitMQ doesn't return count on purge via API
        elif response.status_code == status.HTTP_404_NOT_FOUND:
            logger.warning("Queue %s in vhost %s not found", queue, vhost)
            return 0
        else:
            logger.error(
                "Failed to purge queue %s in vhost %s, status code: %s, response: %s",
                queue,
                vhost,
                response.status_code,
                response.text,
            )
            raise RabbitMQError(f"Failed to purge queue {queue} in vhost {vhost}")

    def delete_queue(self, vhost: str, queue: str) -> bool:
        """Delete a queue entirely.

        Args:
            vhost (str): The virtual host name
            queue (str): The queue name

        Returns:
            bool: True if queue was deleted, False if not found
        """
        vhost_encoded = requests.utils.quote(vhost, safe="")
        queue_encoded = requests.utils.quote(queue, safe="")
        url = f"{self.rmq_management_url}/queues/{vhost_encoded}/{queue_encoded}"

        try:
            response = requests.delete(url, auth=self.rmq_auth, timeout=60)
        except requests.RequestException as exc:
            logger.error(
                "Unable to delete queue %s in vhost %s from RabbitMQ, error: %s",
                queue,
                vhost,
                exc,
            )
            raise RabbitMQError(f"Failed to delete queue {queue} in vhost {vhost}")

        if response.status_code == status.HTTP_204_NO_CONTENT:
            logger.info("Queue %s in vhost %s deleted successfully", queue, vhost)
            return True
        elif response.status_code == status.HTTP_404_NOT_FOUND:
            logger.warning("Queue %s in vhost %s not found", queue, vhost)
            return False
        else:
            logger.error(
                "Failed to delete queue %s in vhost %s, status code: %s, response: %s",
                queue,
                vhost,
                response.status_code,
                response.text,
            )
            raise RabbitMQError(f"Failed to delete queue {queue} in vhost {vhost}")

    def create_queue(
        self,
        vhost: str,
        queue_name: str,
        durable: bool = True,
        auto_delete: bool = False,
        arguments: dict | None = None,
    ) -> bool:
        """Create a queue in RabbitMQ with specified arguments.

        This method creates a queue via the RabbitMQ Management HTTP API.
        It should be used to pre-create queues before STOMP subscribers connect,
        ensuring queues have the correct arguments (DLX, max-length, etc.).

        Args:
            vhost: The virtual host name
            queue_name: The queue name to create
            durable: Whether the queue survives broker restart (default: True)
            auto_delete: Whether queue is deleted when last consumer disconnects (default: False)
            arguments: Additional queue arguments (x-max-length, x-dead-letter-exchange, etc.)

        Returns:
            bool: True if queue was created successfully or already exists with matching args,
                  False if creation failed
        """
        vhost_encoded = requests.utils.quote(vhost, safe="")
        queue_encoded = requests.utils.quote(queue_name, safe="")
        url = f"{self.rmq_management_url}/queues/{vhost_encoded}/{queue_encoded}"

        payload = {
            "durable": durable,
            "auto_delete": auto_delete,
        }
        if arguments:
            payload["arguments"] = arguments

        try:
            logger.info(
                "Creating queue '%s' in vhost '%s' with arguments %s",
                queue_name,
                vhost,
                arguments,
            )
            response = requests.put(url, json=payload, auth=self.rmq_auth, timeout=10)
        except requests.RequestException as exc:
            logger.error(
                "Failed to create queue '%s' in vhost '%s': %s",
                queue_name,
                vhost,
                exc,
            )
            return False

        if response.status_code == status.HTTP_201_CREATED:
            logger.info(
                "Queue '%s' created successfully in vhost '%s'", queue_name, vhost
            )
            return True
        elif response.status_code == status.HTTP_204_NO_CONTENT:
            logger.info("Queue '%s' already exists in vhost '%s'", queue_name, vhost)
            return True
        else:
            logger.error(
                "Failed to create queue '%s' in vhost '%s', status code: %s, response: %s",
                queue_name,
                vhost,
                response.status_code,
                response.text,
            )
            return False

    def list_all_subscription_queues(self) -> list[dict]:
        """List all subscription queues across all vhosts.

        Returns:
            list[dict]: List of vhost information with their queues:
                - vhost: Virtual host name
                - queues: List of queue dictionaries
                - total_messages: Total messages in vhost
        """
        result = []
        vhosts = self.list_rabbitmq_virtual_hosts()

        for vhost in vhosts:
            # Skip default vhost
            if vhost == "/":
                continue

            try:
                queues = self.list_queues(vhost)
                # Filter to subscription and consumer queues
                subscription_queues = [
                    q
                    for q in queues
                    if q["name"].startswith("subscription_")
                    or q["name"].startswith("consumer_")
                ]
                if subscription_queues:
                    total_messages = sum(q["messages"] for q in subscription_queues)
                    result.append(
                        {
                            "vhost": vhost,
                            "queues": subscription_queues,
                            "total_messages": total_messages,
                        }
                    )
            except RabbitMQError:
                logger.warning("Failed to list queues for vhost %s, skipping", vhost)
                continue

        return result
