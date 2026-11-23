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
from urllib.parse import parse_qs, urlparse

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


def get_total_pages(pagination: dict | None) -> int | None:
    """
    Page count from an Arrow pagination block.

    List endpoints (customers, licenses) report it as snake_case
    ``total_page``. The billing export documents only perPage/currentPage,
    so it may report none.
    """
    if not pagination:
        return None
    for key in ("total_page", "totalPages", "total_pages", "TotalPages"):
        value = pagination.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


class ArrowClient:
    """
    Client for Arrow (ArrowSphere) API.

    All methods communicate with the Arrow API using the provided credentials.
    """

    DEFAULT_PER_PAGE = 100
    # (connect, read) seconds. Without a timeout a hung Arrow API holds a
    # Celery worker, or a web worker for the views that call it inline,
    # indefinitely. A billing export page of 1000 lines can take a while.
    REQUEST_TIMEOUT = (10, 120)

    BILLING_EXPORT_PER_PAGE = 1000  # Arrow's max for billing exports
    # Guard against a server that ignores the page and repeats it forever.
    BILLING_EXPORT_MAX_PAGES = 500

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
            response = self.session.get(
                url, params=params, timeout=self.REQUEST_TIMEOUT
            )
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

    def _post(
        self, endpoint: str, data: dict | None = None, params: dict | None = None
    ) -> dict:
        """Make a POST request to the Arrow API."""
        url = f"{self.credentials.get_base_url()}{endpoint}"
        try:
            response = self.session.post(
                url, json=data, params=params, timeout=self.REQUEST_TIMEOUT
            )
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
            total_pages = get_total_pages(response.get("pagination")) or 1
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

        # The API reference documents the page in the body; Arrow's PHP SDK
        # sends it as a query parameter, so send both.
        return self._post(
            "billing/erp/exports/sync", data=payload, params={"page": page}
        )

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
        previous_values = None
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
            if values == previous_values:
                raise ArrowBackendError(
                    f"Arrow billing export returned page {page - 1} again for "
                    f"page {page}; is the page parameter being ignored?"
                )
            previous_values = values

            all_values.extend(values)

            # Check pagination: trust the reported page count when there is
            # one, else a short page ends the export.
            pagination = response.get("pagination", {})
            total_pages = get_total_pages(pagination)
            if total_pages is not None:
                if page >= total_pages:
                    break
            else:
                per_page = pagination.get("perPage", self.BILLING_EXPORT_PER_PAGE)
                if len(values) < per_page:
                    break
            if page >= self.BILLING_EXPORT_MAX_PAGES:
                raise ArrowBackendError(
                    f"Arrow billing export for {period_from}..{period_to} did not "
                    f"end after {page} pages"
                )
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

    LICENSES_PER_PAGE = 1000  # the v2 maximum
    LICENSES_MAX_PAGES = 100

    def list_all_licenses(self) -> list[dict]:
        """
        List every license of the partner.

        Uses ``licenses/v2``: the v1 ``licenses`` list was removed by Arrow
        on 2026-09-30 (HTTP 410). v2 pages with a ``search_after`` cursor
        taken from ``pagination.next``; page numbers stop at 10,000 results.
        """
        licenses: list[dict] = []
        params: dict = {"per_page": self.LICENSES_PER_PAGE}
        for _ in range(self.LICENSES_MAX_PAGES):
            response = self._get("licenses/v2", params=params)
            page = (response.get("data") or {}).get("licenses") or []
            licenses.extend(page)
            next_link = (response.get("pagination") or {}).get("next") or ""
            search_after = parse_qs(urlparse(next_link).query).get("search_after")
            if not page or not search_after:
                return licenses
            params = {
                "per_page": self.LICENSES_PER_PAGE,
                "search_after": search_after[0],
            }
        raise ArrowBackendError(
            f"Arrow license list did not end after {self.LICENSES_MAX_PAGES} pages"
        )

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
