import logging
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)


class ScimError(Exception):
    """Base exception for SCIM-related errors."""

    pass


class ScimClient:
    BASE_PATH = "/scim/v2"
    PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
    DEFAULT_TIMEOUT = 10

    def __init__(
        self,
        api_url: str,
        *,
        api_key: str | None = None,
        timeout: int | None = None,
        extra_headers: dict | None = None,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self.extra_headers = extra_headers or {}
        self.session = requests.Session()

    def ping(self) -> None:
        self._request("GET", "ServiceProviderConfig")

    def get_user(self, user_id: str) -> dict:
        return self._request("GET", f"Users/{user_id}")

    def add_entitlement(
        self, user_id: str, urn_namespace: str, ssh_login_node: str, ssh_username: str
    ) -> dict:
        entitlement = self.build_entitlement(
            urn_namespace, ssh_login_node, ssh_username
        )
        payload = {
            "schemas": [self.PATCH_SCHEMA],
            "Operations": [
                {
                    "op": "add",
                    "path": "entitlements",
                    "value": [{"value": entitlement}],
                }
            ],
        }
        return self._request("PATCH", f"Users/{user_id}", json=payload)

    def add_entitlements(self, user_id: str, entitlements: list[str]) -> dict:
        """Add multiple entitlements in a single PATCH operation."""
        payload = {
            "schemas": [self.PATCH_SCHEMA],
            "Operations": [
                {
                    "op": "add",
                    "path": "entitlements",
                    "value": [{"value": entitlement} for entitlement in entitlements],
                }
            ],
        }
        return self._request("PATCH", f"Users/{user_id}", json=payload)

    def remove_entitlement(
        self, user_id: str, urn_namespace: str, ssh_login_node: str, ssh_username: str
    ) -> dict:
        entitlement = self.build_entitlement(
            urn_namespace, ssh_login_node, ssh_username
        )
        payload = {
            "schemas": [self.PATCH_SCHEMA],
            "Operations": [
                {
                    "op": "remove",
                    "path": f'entitlements[value eq "{entitlement}"]',
                }
            ],
        }
        return self._request("PATCH", f"Users/{user_id}", json=payload)

    def remove_entitlements(self, user_id: str, entitlements: list[str]) -> dict:
        """Remove multiple entitlements in a single PATCH operation."""
        operations = [
            {
                "op": "remove",
                "path": f'entitlements[value eq "{entitlement}"]',
            }
            for entitlement in entitlements
        ]
        payload = {
            "schemas": [self.PATCH_SCHEMA],
            "Operations": operations,
        }
        return self._request("PATCH", f"Users/{user_id}", json=payload)

    def clear_all_entitlements(self, user_id: str) -> dict:
        payload = {
            "schemas": [self.PATCH_SCHEMA],
            "Operations": [{"op": "remove", "path": "entitlements"}],
        }
        return self._request("PATCH", f"Users/{user_id}", json=payload)

    @staticmethod
    def build_entitlement(
        urn_namespace: str, ssh_login_node: str, ssh_username: str
    ) -> str:
        return f"{urn_namespace}:res:{ssh_login_node}:{ssh_username}:act:ssh"

    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        url = self._build_url(path)
        headers = self._get_headers()
        timeout = int(self.timeout)
        logger.debug("SCIM request %s %s", method, url)
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise ScimError(f"SCIM request failed: {exc}")

        if not response.ok:
            raise ScimError(
                f"SCIM request failed [{response.status_code}] {response.text}"
            )

        if response.content:
            try:
                return response.json()
            except requests.JSONDecodeError:
                raise ScimError("Unable to parse SCIM response JSON.")
        return {}

    def _build_url(self, path: str) -> str:
        base = self.api_url.rstrip("/") + "/"
        full_path = f"{self.BASE_PATH.strip('/')}/{path.lstrip('/')}"
        return urljoin(base, full_path)

    def _get_headers(self) -> dict:
        headers = {
            "Accept": "application/scim+json",
            "Content-Type": "application/scim+json",
        }
        if not self.api_key:
            raise ScimError("SCIM API key is not configured.")
        headers["X-API-Key"] = self.api_key

        headers.update(self.extra_headers)
        return headers
