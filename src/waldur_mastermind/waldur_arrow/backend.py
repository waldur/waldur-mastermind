"""
Arrow API Client

This module provides a client for interacting with the Arrow (ArrowSphere) API.
It handles authentication, pagination, and provides methods for:
- Credential validation (whoami)
- Customer management
- Billing export and sync
- Subscription listing
"""

import logging
from dataclasses import dataclass

import requests

from waldur_core.structure.exceptions import ServiceBackendError

logger = logging.getLogger(__name__)


class ArrowBackendError(ServiceBackendError):
    """Exception for Arrow API-related errors."""

    pass


@dataclass
class ArrowCredentials:
    """Container for Arrow API credentials."""

    api_url: str
    api_key: str

    def get_base_url(self) -> str:
        """Get base URL with trailing slash."""
        url = self.api_url
        if not url.endswith("/"):
            url += "/"
        return url


class ArrowClient:
    """
    Client for Arrow (ArrowSphere) API.

    All methods communicate with the Arrow API using the provided credentials.
    """

    DEFAULT_PER_PAGE = 100
    BILLING_EXPORT_PER_PAGE = 1000  # Arrow's max for billing exports

    def __init__(self, credentials: ArrowCredentials):
        self.credentials = credentials
        self._session = None

    @property
    def session(self) -> requests.Session:
        """Lazily create a session with authentication headers."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "apiKey": self.credentials.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )
        return self._session

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """Make a GET request to the Arrow API."""
        url = f"{self.credentials.get_base_url()}{endpoint}"
        try:
            response = self.session.get(url, params=params)
        except requests.exceptions.RequestException as e:
            logger.error("Arrow API GET %s connection failed: %s", endpoint, e)
            raise ArrowBackendError(f"Arrow API request to {endpoint} failed: {e}")

        if not response.ok:
            body = response.text[:500] if response.text else "(empty)"
            logger.error(
                "Arrow API GET %s failed with status %s. Response body: %s",
                endpoint,
                response.status_code,
                body,
            )
            raise ArrowBackendError(
                f"Arrow API {endpoint} returned HTTP {response.status_code}: {body}"
            )

        try:
            return response.json()
        except ValueError:
            body = response.text[:500] if response.text else "(empty)"
            logger.error(
                "Arrow API GET %s returned invalid JSON (status %s). Response body: %s",
                endpoint,
                response.status_code,
                body,
            )
            raise ArrowBackendError(
                f"Arrow API {endpoint} returned invalid JSON (status {response.status_code}): {body}"
            )

    def _post(self, endpoint: str, data: dict | None = None) -> dict:
        """Make a POST request to the Arrow API."""
        url = f"{self.credentials.get_base_url()}{endpoint}"
        try:
            response = self.session.post(url, json=data)
        except requests.exceptions.RequestException as e:
            logger.error("Arrow API POST %s connection failed: %s", endpoint, e)
            raise ArrowBackendError(f"Arrow API request to {endpoint} failed: {e}")

        if not response.ok:
            body = response.text[:500] if response.text else "(empty)"
            logger.error(
                "Arrow API POST %s failed with status %s. Response body: %s",
                endpoint,
                response.status_code,
                body,
            )
            raise ArrowBackendError(
                f"Arrow API {endpoint} returned HTTP {response.status_code}: {body}"
            )

        try:
            return response.json()
        except ValueError:
            body = response.text[:500] if response.text else "(empty)"
            logger.error(
                "Arrow API POST %s returned invalid JSON (status %s). Response body: %s",
                endpoint,
                response.status_code,
                body,
            )
            raise ArrowBackendError(
                f"Arrow API {endpoint} returned invalid JSON (status {response.status_code}): {body}"
            )

    # -------------------- Validation --------------------

    def ping(self) -> dict:
        """
        Validate credentials by calling the whoami endpoint.

        Returns:
            dict with company info and user details.

        Raises:
            ArrowBackendError: If credentials are invalid or API is unreachable.
        """
        try:
            response = self._get("whoami")
            if response.get("status") == 200:
                return {
                    "valid": True,
                    "data": response.get("data", {}),
                }
            return {
                "valid": False,
                "error": "Unexpected response status",
            }
        except ArrowBackendError as e:
            return {
                "valid": False,
                "error": str(e),
            }

    # -------------------- Customers --------------------

    def list_customers(
        self,
        page: int = 1,
        per_page: int | None = None,
    ) -> dict:
        """
        List all active customers.

        Args:
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            dict with 'data' (list of customers) and 'pagination' info
        """
        params = {
            "page": page,
            "per_page": per_page or self.DEFAULT_PER_PAGE,
        }
        return self._get("customers", params=params)

    def get_customer(self, reference: str) -> dict:
        """
        Get a specific customer by reference.

        Args:
            reference: Customer reference (e.g., 'XSP661245')

        Returns:
            dict with customer details
        """
        return self._get(f"customers/{reference}")

    def list_all_customers(self) -> list[dict]:
        """
        List all customers with pagination handling.

        Returns:
            List of all customer records
        """
        all_customers = []
        page = 1

        while True:
            response = self.list_customers(page=page)
            logger.debug(
                "Arrow customers response keys: %s, data keys: %s",
                list(response.keys()),
                list(response.get("data", {}).keys())
                if isinstance(response.get("data"), dict)
                else type(response.get("data")).__name__,
            )
            customers = response.get("data", {}).get("customers", [])
            if not customers:
                break
            all_customers.extend(customers)

            # Check pagination
            pagination = response.get("pagination", {})
            total_pages = pagination.get("totalPages", 1)
            if page >= total_pages:
                break
            page += 1

        return all_customers

    # -------------------- Billing Export --------------------

    def list_export_types(self) -> dict:
        """
        List available billing export types.

        Returns:
            dict with 'exportTypes' list
        """
        return self._get("billing/erp/exports/types")

    def list_export_columns(self) -> dict:
        """
        List available columns for billing exports.

        Returns:
            dict with available column definitions
        """
        return self._get("billing/erp/exports/columns")

    def export_billing_sync(
        self,
        export_type_reference: str,
        period_from: str,
        period_to: str,
        page: int = 1,
    ) -> dict:
        """
        Export billing data synchronously (paginated JSON).

        Args:
            export_type_reference: Export type reference (e.g., 'E1-2-b2f08942207fbd6838ba332a98387c69')
            period_from: Start period in YYYY-MM format
            period_to: End period in YYYY-MM format
            page: Page number (1-indexed)

        Returns:
            dict with 'headers', 'values', and 'pagination'
        """
        payload = {
            "exportTypeReference": export_type_reference,
            "filters": {
                "reportPeriod": {
                    "from": period_from,
                    "to": period_to,
                },
            },
            "page": page,
        }

        return self._post("billing/erp/exports/sync", data=payload)

    def export_billing_all_pages(
        self,
        export_type_reference: str,
        period_from: str,
        period_to: str,
    ) -> dict:
        """
        Export all billing data with automatic pagination.

        Args:
            export_type_reference: Export type reference
            period_from: Start period in YYYY-MM format
            period_to: End period in YYYY-MM format

        Returns:
            dict with 'headers' and combined 'values' from all pages
        """
        all_values = []
        headers = []
        page = 1

        while True:
            response = self.export_billing_sync(
                export_type_reference=export_type_reference,
                period_from=period_from,
                period_to=period_to,
                page=page,
            )

            data = response.get("data", {})
            if not headers:
                headers = data.get("headers", [])

            values = data.get("values", [])
            if not values:
                break

            all_values.extend(values)

            # Check pagination
            pagination = response.get("pagination", {})
            per_page = pagination.get("perPage", self.BILLING_EXPORT_PER_PAGE)
            if len(values) < per_page:
                # No more pages
                break
            page += 1

        return {
            "headers": headers,
            "values": all_values,
        }

    # -------------------- Subscriptions --------------------

    def list_subscriptions(
        self,
        page: int = 1,
        per_page: int | None = None,
    ) -> dict:
        """
        List subscriptions.

        Args:
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            dict with subscription data
        """
        params = {
            "page": page,
            "per_page": per_page or self.DEFAULT_PER_PAGE,
        }
        return self._get("subscriptions", params=params)

    def get_subscription(self, reference: str) -> dict:
        """
        Get a specific subscription by reference.

        Args:
            reference: Subscription reference

        Returns:
            dict with subscription details
        """
        return self._get(f"subscriptions/{reference}")

    # -------------------- Licenses --------------------

    def get_license(self, license_reference: str) -> dict:
        """
        Get detailed license information.

        Args:
            license_reference: License reference (e.g., 'XSP12345')

        Returns:
            dict with license details including:
            - name, friendlyName, state, seats, activeSeats
            - activation_datetime, expiry_datetime, nextRenewalDate
            - category, program, periodicity, term
            - price (unit/total buy/sell), currency
            - vendor_license_id, sku, service_ref
        """
        response = self._get(f"licenses/{license_reference}")
        return response.get("data", {}).get("license", {})

    # -------------------- Consumption --------------------

    def get_monthly_consumption(
        self,
        license_reference: str,
        period_from: str,
        period_to: str | None = None,
    ) -> dict:
        """
        Get monthly consumption for a license.

        Args:
            license_reference: License reference (e.g., 'XSP12345')
            period_from: Start period in YYYY-MM format
            period_to: End period in YYYY-MM format (defaults to period_from)

        Returns:
            dict with 'headers' and 'lines' arrays
        """
        params = {
            "reportPeriodStart": period_from,
            "reportPeriodEnd": period_to or period_from,
        }
        response = self._get(
            f"consumption/monthly/license/{license_reference}",
            params=params,
        )
        return response.get("data", {})

    def parse_consumption_to_dicts(self, consumption_data: dict) -> list[dict]:
        """
        Convert consumption data to list of dictionaries.

        Args:
            consumption_data: dict with 'headers' and 'lines' keys

        Returns:
            List of dictionaries, one per consumption line
        """
        headers = consumption_data.get("headers", [])
        lines = consumption_data.get("lines", [])

        result = []
        for row in lines:
            row_dict = {}
            for i, header in enumerate(headers):
                if i < len(row):
                    row_dict[header] = row[i]
            result.append(row_dict)

        return result

    def get_consumption_prediction(
        self,
        license_reference: str,
        granularity: str = "monthly",
    ) -> dict:
        """
        Get estimated prediction of future consumption for current billing period.

        This is useful for the ongoing month to see:
        - consumed: Actual consumption so far (buy/sell/list prices)
        - estimatedMin/estimatedMax: Predicted range for rest of the month

        Args:
            license_reference: License reference (e.g., 'XSP12345')
            granularity: 'monthly' or 'daily'

        Returns:
            dict with currency, reportPeriod, billingStartDate, and values array
        """
        params = {"granularity": granularity}
        response = self._get(
            f"consumption/license/{license_reference}/prediction",
            params=params,
        )
        return response.get("data", {})

    # -------------------- Utility Methods --------------------

    def parse_billing_export_to_dicts(self, export_data: dict) -> list[dict]:
        """
        Convert billing export data to list of dictionaries.

        Arrow returns billing data with separate headers and values arrays.
        This method combines them into a list of dictionaries for easier processing.

        Args:
            export_data: dict with 'headers' and 'values' keys

        Returns:
            List of dictionaries, one per billing line
        """
        headers = export_data.get("headers", [])
        values = export_data.get("values", [])

        result = []
        for row in values:
            row_dict = {}
            for i, header in enumerate(headers):
                if i < len(row):
                    row_dict[header] = row[i]
            result.append(row_dict)

        return result

    # -------------------- Catalog --------------------

    def list_categories(self) -> list[dict]:
        """
        List all catalog categories.

        Returns:
            List of category objects with 'name' and other details.
        """
        response = self._get("catalog/categories")
        return response.get("data", [])

    def list_category_programs(self, category: str) -> list[dict]:
        """
        List programs (vendors) for a specific category.

        Args:
            category: Category name (e.g., 'IAAS', 'SAAS')

        Returns:
            List of program objects with vendor information.
        """
        response = self._get(f"catalog/categories/{category}/programs")
        return response.get("data", [])

    def list_vendors(self, category: str = "IAAS") -> list[str]:
        """
        Get list of vendor names for a category.

        Args:
            category: Category name (default: 'IAAS')

        Returns:
            List of unique vendor names.
        """
        try:
            programs = self.list_category_programs(category)
            # Extract vendor names - the exact field depends on API response structure
            vendors = set()
            for program in programs:
                # Try common field names for vendor/program name
                vendor = (
                    program.get("vendorName")
                    or program.get("vendor")
                    or program.get("name")
                    or program.get("programName")
                )
                if vendor:
                    vendors.add(vendor)
            return sorted(vendors)
        except ArrowBackendError:
            return []


def get_arrow_client() -> ArrowClient | None:
    """
    Get Arrow client using the active ArrowSettings.

    Returns:
        ArrowClient instance or None if no active settings exist.
    """
    from .models import ArrowSettings

    settings = ArrowSettings.get_active()
    if not settings:
        return None

    credentials = ArrowCredentials(
        api_url=settings.api_url,
        api_key=settings.api_key,
    )
    return ArrowClient(credentials)
