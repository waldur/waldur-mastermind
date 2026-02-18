import logging

from keycloak import KeycloakAdmin
from keycloak import exceptions as keycloak_exceptions

logger = logging.getLogger(__name__)


class KeycloakClient:
    """Generic Keycloak client for managing groups and user memberships.

    Accepts a config dict with keys:
    - keycloak_url
    - keycloak_realm
    - keycloak_user_realm (default: "master")
    - keycloak_username
    - keycloak_password
    - keycloak_ssl_verify (default: True)
    """

    def __init__(self, config: dict):
        keycloak_url = config["keycloak_url"]
        keycloak_realm = config["keycloak_realm"]
        keycloak_user_realm = config.get("keycloak_user_realm") or "master"
        keycloak_username = config["keycloak_username"]
        keycloak_password = config["keycloak_password"]
        keycloak_verify = config.get("keycloak_ssl_verify")
        keycloak_verify = keycloak_verify if keycloak_verify is not None else True

        self.keycloak = KeycloakAdmin(
            server_url=keycloak_url,
            user_realm_name=keycloak_user_realm,
            realm_name=keycloak_realm,
            username=keycloak_username,
            password=keycloak_password,
            verify=keycloak_verify,
        )

    def find_user_by_username(self, username) -> dict | None:
        """Find a user by their username in Keycloak."""
        users = self.keycloak.get_users({"username": username})
        return users[0] if users else None

    def get_group(self, group_id) -> dict | None:
        """Fetch group data from Keycloak."""
        try:
            return self.keycloak.get_group(group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to fetch the group %s in Keycloak: %s", group_id, e)
            raise

    def create_group(self, group_name: str, parent_id: str | None = None) -> dict:
        """Create a group in Keycloak. Returns existing group if name matches."""
        try:
            groups = self.keycloak.get_groups({"search": group_name})
            group = None
            for g in groups:
                if g["name"] == group_name:
                    group = g
                    break
                for sub_group in g.get("subGroups", []):
                    if sub_group["name"] == group_name:
                        group = sub_group
                        break
                if group:
                    break

            if group:
                logger.info(
                    "The group %s already exists, skipping creation", group_name
                )
            else:
                logger.info("Creating a group %s, parent %s", group_name, parent_id)
                payload = {"name": group_name}
                if parent_id is None:
                    group_id = self.keycloak.create_group(payload)
                else:
                    group_id = self.keycloak.create_group(payload, parent=parent_id)
                group = {"id": group_id}
            return group

        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to create the group %s in Keycloak: %s", group_name, e)
            raise

    def delete_group(self, group_id):
        """Delete a group from Keycloak."""
        try:
            group = self.get_group(group_id)
            if group:
                logger.info(
                    "Deleting group %s (%s) in Keycloak",
                    group["name"],
                    group_id,
                )
                self.keycloak.delete_group(group_id)
            else:
                logger.info("The group %s is already deleted in Keycloak", group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to delete the group %s in Keycloak: %s", group_id, e)
            raise

    def list_groups(self) -> list:
        """List all groups in Keycloak."""
        try:
            return self.keycloak.get_groups()
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to list groups in Keycloak: %s", e)
            raise

    def list_group_members(self, group_id: str) -> list:
        """Get members of a group in Keycloak."""
        try:
            return self.keycloak.get_group_members(group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to get group members in Keycloak: %s", e)
            raise

    def search_users(self, query: str, max_results: int = 25) -> list:
        """Search for users in Keycloak by username, email, first or last name."""
        try:
            return self.keycloak.get_users({"search": query, "max": max_results})
        except keycloak_exceptions.KeycloakError as e:
            logger.error("Failed to search users in Keycloak: %s", e)
            raise

    def add_user_to_group(self, user_id, group_id):
        """Add a user to a Keycloak group."""
        try:
            logger.info("Adding user %s to group %s", user_id, group_id)
            self.keycloak.group_user_add(user_id, group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error(
                "Failed to add user %s to group %s in Keycloak: %s",
                user_id,
                group_id,
                e,
            )
            raise

    def remove_user_from_group(self, user_id, group_id):
        """Remove a user from a Keycloak group."""
        try:
            logger.info("Removing user %s from group %s", user_id, group_id)
            self.keycloak.group_user_remove(user_id, group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.error(
                "Failed to revoke user %s role in Keycloak group %s: %s",
                user_id,
                group_id,
                e,
            )
            raise
