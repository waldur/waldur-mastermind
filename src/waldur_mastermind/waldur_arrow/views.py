import logging
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher

from constance import config
from django.db import transaction
from django.db.models import Sum
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import decorators, response, status

from waldur_core.core import views as core_views
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions

from . import filters, models, serializers, tasks
from .backend import ArrowBackendError, ArrowClient, ArrowCredentials, get_arrow_client

logger = logging.getLogger(__name__)


# -------------------- Arrow Settings ViewSet --------------------


class ArrowSettingsViewSet(core_views.ActionsViewSet):
    """
    ViewSet for Arrow settings management.

    Provides CRUD operations and discovery/wizard endpoints for configuring
    Arrow integration.
    """

    queryset = models.ArrowSettings.objects.all().order_by("-created")
    lookup_field = "uuid"
    serializer_class = serializers.ArrowSettingsSerializer
    create_serializer_class = serializers.ArrowSettingsCreateSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ArrowSettingsFilter

    # Permissions - staff only
    list_permissions = retrieve_permissions = [structure_permissions.is_staff]
    create_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_staff
    ]
    destroy_permissions = [structure_permissions.is_staff]

    def perform_create(self, serializer):
        """Create settings and validate credentials."""
        settings = serializer.save()
        self._update_partner_info(settings)

    def perform_update(self, serializer):
        """Update settings and refresh partner info."""
        settings = serializer.save()
        self._update_partner_info(settings)

    def _update_partner_info(self, settings: models.ArrowSettings):
        """Fetch and update partner info from Arrow API."""
        try:
            credentials = ArrowCredentials(
                api_url=settings.api_url,
                api_key=settings.api_key,
            )
            client = ArrowClient(credentials)
            result = client.ping()
            if result.get("valid"):
                data = result.get("data", {})
                settings.partner_reference = data.get("reference", "")
                settings.partner_name = data.get("companyName", "")
                settings.save(update_fields=["partner_reference", "partner_name"])
        except ArrowBackendError as e:
            logger.warning(f"Failed to fetch partner info: {e}")

    # -------------------- Discovery/Wizard Actions --------------------

    @extend_schema(
        request=serializers.ArrowCredentialsSerializer,
        responses={200: serializers.ArrowCredentialsValidationResponseSerializer},
        description="Validate Arrow API credentials without saving them.",
    )
    @decorators.action(detail=False, methods=["post"])
    def validate_credentials(self, request):
        """Validate Arrow credentials and return partner info."""
        serializer = serializers.ArrowCredentialsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        credentials = ArrowCredentials(
            api_url=serializer.validated_data["api_url"],
            api_key=serializer.validated_data["api_key"],
        )
        client = ArrowClient(credentials)

        try:
            result = client.ping()
            if result.get("valid"):
                data = result.get("data", {})
                # Also fetch export types
                export_types = []
                try:
                    types_response = client.list_export_types()
                    export_types = types_response.get("data", {}).get("exportTypes", [])
                except ArrowBackendError:
                    pass

                return response.Response(
                    {
                        "valid": True,
                        "message": "Credentials validated successfully",
                        "partner_info": {
                            "reference": data.get("reference", ""),
                            "company_name": data.get("companyName", ""),
                            "email": data.get("emailContact", ""),
                            "city": data.get("city", ""),
                            "country_code": data.get("countryCode", ""),
                        },
                        "export_types": export_types,
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                return response.Response(
                    {
                        "valid": False,
                        "error": result.get("error", "Invalid credentials"),
                    },
                    status=status.HTTP_200_OK,
                )
        except ArrowBackendError as e:
            return response.Response(
                {"valid": False, "error": str(e)},
                status=status.HTTP_200_OK,
            )

    validate_credentials_serializer_class = serializers.ArrowCredentialsSerializer
    validate_credentials_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.DiscoverCustomersRequestSerializer,
        responses={200: serializers.DiscoverCustomersResponseSerializer},
        description="Discover Arrow customers and suggest mappings to Waldur customers.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_customers(self, request):
        """
        Fetch Arrow customers, Waldur customers, and suggest mappings.

        Uses fuzzy matching on company names to suggest mappings.
        """
        serializer = serializers.DiscoverCustomersRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        credentials = ArrowCredentials(
            api_url=serializer.validated_data["api_url"],
            api_key=serializer.validated_data["api_key"],
        )
        client = ArrowClient(credentials)

        try:
            # Fetch Arrow customers
            arrow_customers_raw = client.list_all_customers()
            arrow_customers = [
                {
                    "reference": c.get("Reference", ""),
                    "companyName": c.get("CompanyName", ""),
                    "email": c.get("EmailContact", "")
                    or c.get("Contact", {}).get("Email", ""),
                    "city": c.get("City", ""),
                    "countryCode": c.get("CountryCode", ""),
                }
                for c in arrow_customers_raw
            ]
        except ArrowBackendError as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch available export types and validate compatibility
        export_types = []
        try:
            types_response = client.list_export_types()
            raw_types = types_response.get("data", {}).get("exportTypes", {})
            if isinstance(raw_types, dict):
                export_type_refs = [
                    {"reference": ref, "name": name} for ref, name in raw_types.items()
                ]
            elif isinstance(raw_types, list):
                export_type_refs = raw_types
            else:
                export_type_refs = []

            # Check each export type's headers for compatibility.
            # Required: billing sync breaks without these.
            required_fields = {
                "Classification",
                "Vendor Subscription ID",
                "End User Company Name",
                "Customer Total Price",
                "Total Wholesale Price",
            }
            # Important: used for resource sync, invoicing, and reporting.
            # Note: some fields have fallback chains in code:
            #   Line Reference -> Sequence -> Order Id
            #   License Reference -> ARS Subscription ID
            #   Customer Reference -> End User Company Name (matching)
            important_fields = {
                "Friendly Name",
                "Report Period",
                "Vendor Name",
                "Offer Name",
                "Qty",
                "Order Id",
                "Description",
                "Service Name",
                "Arrow SKU",
                "Vendor SKU",
                "Billing Cycle",
                "Customer Unit Price",
                "Bill From",
                "Bill To",
                "End User Company ID",
                "End User E-mail",
                "End User Country Code",
                "End User Address Line1",
                "End User City",
                "End User Post Code",
                "Sequence",
                "ARS Subscription ID",
            }

            for et in export_type_refs:
                try:
                    sample = client.export_billing_sync(
                        export_type_reference=et["reference"],
                        period_from="2025-01",
                        period_to="2025-01",
                        page=1,
                    )
                    headers = set(sample.get("data", {}).get("headers", []))
                    missing_required = sorted(required_fields - headers)
                    missing_important = sorted(important_fields - headers)
                    found_required = len(required_fields) - len(missing_required)
                    found_important = len(important_fields) - len(missing_important)
                    et["required_fields_total"] = len(required_fields)
                    et["required_fields_found"] = found_required
                    et["important_fields_total"] = len(important_fields)
                    et["important_fields_found"] = found_important
                    et["missing_required_fields"] = missing_required
                    et["missing_important_fields"] = missing_important
                    et["compatible"] = found_required == len(required_fields)
                    et["recommended"] = (
                        et["compatible"]
                        and found_important >= len(important_fields) // 2
                    )
                except ArrowBackendError:
                    et["required_fields_total"] = len(required_fields)
                    et["required_fields_found"] = 0
                    et["important_fields_total"] = len(important_fields)
                    et["important_fields_found"] = 0
                    et["missing_required_fields"] = sorted(required_fields)
                    et["missing_important_fields"] = sorted(important_fields)
                    et["compatible"] = False
                    et["recommended"] = False

            export_types = export_type_refs
        except ArrowBackendError:
            pass

        # Get Waldur customers
        waldur_customers = list(
            structure_models.Customer.objects.values("uuid", "name", "abbreviation")
        )

        # Generate suggestions using fuzzy matching
        suggestions = []
        existing_mappings = set(
            models.ArrowCustomerMapping.objects.values_list(
                "arrow_reference", flat=True
            )
        )

        for arrow_customer in arrow_customers:
            suggestion = {
                "arrow_customer": arrow_customer,
                "suggested_waldur_customer": None,
                "confidence": 0.0,
                "existing_mapping": arrow_customer["reference"] in existing_mappings,
            }

            if not suggestion["existing_mapping"]:
                # Find best matching Waldur customer
                arrow_name = arrow_customer["companyName"].lower()
                best_match = None
                best_score = 0.0

                for waldur_customer in waldur_customers:
                    waldur_name = waldur_customer["name"].lower()
                    score = SequenceMatcher(None, arrow_name, waldur_name).ratio()
                    if score > best_score and score > 0.5:
                        best_score = score
                        best_match = waldur_customer

                if best_match:
                    suggestion["suggested_waldur_customer"] = best_match
                    suggestion["confidence"] = best_score

            suggestions.append(suggestion)

        return response.Response(
            {
                "arrow_customers": arrow_customers,
                "waldur_customers": waldur_customers,
                "suggestions": suggestions,
                "export_types": export_types,
            },
            status=status.HTTP_200_OK,
        )

    discover_customers_serializer_class = serializers.DiscoverCustomersRequestSerializer
    discover_customers_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.PreviewSettingsRequestSerializer,
        responses={200: serializers.PreviewSettingsResponseSerializer},
        description="Preview settings configuration before saving.",
    )
    @decorators.action(detail=False, methods=["post"])
    def preview_settings(self, request):
        """Generate preview of settings to be saved."""
        serializer = serializers.PreviewSettingsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        credentials = ArrowCredentials(
            api_url=serializer.validated_data["api_url"],
            api_key=serializer.validated_data["api_key"],
        )
        client = ArrowClient(credentials)

        try:
            result = client.ping()
            if not result.get("valid"):
                return response.Response(
                    {"error": result.get("error", "Invalid credentials")},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            data = result.get("data", {})
            return response.Response(
                {
                    "api_url": serializer.validated_data["api_url"],
                    "partner_name": data.get("companyName", ""),
                    "partner_reference": data.get("reference", ""),
                    "export_type_reference": serializer.validated_data.get(
                        "export_type_reference", ""
                    ),
                    "classification_filter": serializer.validated_data.get(
                        "classification_filter", "IAAS"
                    ),
                    "sync_enabled": serializer.validated_data.get(
                        "sync_enabled", False
                    ),
                },
                status=status.HTTP_200_OK,
            )
        except ArrowBackendError as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    preview_settings_serializer_class = serializers.PreviewSettingsRequestSerializer
    preview_settings_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.SaveSettingsRequestSerializer,
        responses={201: serializers.SaveSettingsResponseSerializer},
        description="Save Arrow settings and customer mappings.",
    )
    @decorators.action(detail=False, methods=["post"])
    def save_settings(self, request):
        """
        Save Arrow settings and create customer mappings.

        This endpoint:
        1. Validates credentials
        2. Creates/updates ArrowSettings
        3. Creates customer mappings if provided
        """
        serializer = serializers.SaveSettingsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        credentials = ArrowCredentials(
            api_url=serializer.validated_data["api_url"],
            api_key=serializer.validated_data["api_key"],
        )
        client = ArrowClient(credentials)

        try:
            result = client.ping()
            if not result.get("valid"):
                return response.Response(
                    {"error": result.get("error", "Invalid credentials")},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            data = result.get("data", {})

            with transaction.atomic():
                # Deactivate any existing active settings
                models.ArrowSettings.objects.filter(is_active=True).update(
                    is_active=False
                )

                # Create new settings
                settings = models.ArrowSettings.objects.create(
                    api_url=serializer.validated_data["api_url"],
                    api_key=serializer.validated_data["api_key"],
                    export_type_reference=serializer.validated_data.get(
                        "export_type_reference", ""
                    ),
                    classification_filter=serializer.validated_data.get(
                        "classification_filter", "IAAS"
                    ),
                    sync_enabled=serializer.validated_data.get("sync_enabled", False),
                    partner_reference=data.get("reference", ""),
                    partner_name=data.get("companyName", ""),
                    is_active=True,
                )

                # Create customer mappings
                mappings_created = 0
                customer_mappings = serializer.validated_data.get(
                    "customer_mappings", []
                )

                for mapping in customer_mappings:
                    try:
                        waldur_customer = structure_models.Customer.objects.get(
                            uuid=mapping["waldur_customer_uuid"]
                        )
                        # Get Arrow customer info
                        try:
                            arrow_customer = client.get_customer(
                                mapping["arrow_reference"]
                            )
                            customers_list = arrow_customer.get("data", {}).get(
                                "customers", []
                            )
                            arrow_company_name = (
                                customers_list[0].get("CompanyName", "")
                                if customers_list
                                else ""
                            )
                        except ArrowBackendError:
                            arrow_company_name = ""

                        models.ArrowCustomerMapping.objects.create(
                            settings=settings,
                            arrow_reference=mapping["arrow_reference"],
                            arrow_company_name=arrow_company_name,
                            waldur_customer=waldur_customer,
                            is_active=True,
                        )
                        mappings_created += 1
                    except structure_models.Customer.DoesNotExist:
                        logger.warning(
                            f"Waldur customer not found: {mapping['waldur_customer_uuid']}"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to create mapping: {e}")

            return response.Response(
                {
                    "settings_uuid": settings.uuid,
                    "mappings_created": mappings_created,
                    "message": f"Settings saved with {mappings_created} customer mappings",
                },
                status=status.HTTP_201_CREATED,
            )

        except ArrowBackendError as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    save_settings_serializer_class = serializers.SaveSettingsRequestSerializer
    save_settings_permissions = [structure_permissions.is_staff]


# -------------------- Vendor Offering Mapping ViewSet --------------------


class ArrowVendorOfferingMappingViewSet(core_views.ActionsViewSet):
    """
    ViewSet for Arrow vendor-to-offering mapping management.

    Maps Arrow vendor names (e.g., 'Microsoft', 'Amazon Web Services')
    to Waldur marketplace offerings, enabling separate tracking of
    Arrow subscriptions by cloud provider.
    """

    queryset = models.ArrowVendorOfferingMapping.objects.all().order_by("-created")
    lookup_field = "uuid"
    serializer_class = serializers.ArrowVendorOfferingMappingSerializer
    create_serializer_class = serializers.ArrowVendorOfferingMappingCreateSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ArrowVendorOfferingMappingFilter

    # Permissions - staff only
    list_permissions = retrieve_permissions = [structure_permissions.is_staff]
    create_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_staff
    ]
    destroy_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={200: serializers.VendorNameChoiceSerializer(many=True)},
        description="Get vendor names from Arrow catalog API (IAAS category).",
    )
    @decorators.action(detail=False, methods=["get"])
    def vendor_choices(self, request):
        """
        Return vendor names from Arrow catalog API.

        Fetches vendors from the IAAS category in Arrow's catalog.
        Falls back to billing sync items if catalog fetch fails.
        """
        vendor_names = set()

        # Try to fetch from Arrow catalog API
        client = get_arrow_client()
        if client:
            try:
                # Get vendors from IAAS category
                iaas_vendors = client.list_vendors("IAAS")
                vendor_names.update(iaas_vendors)
            except Exception as e:
                logger.warning(f"Failed to fetch vendors from Arrow catalog: {e}")

        # Fall back to / supplement with billing sync items
        if not vendor_names:
            settings_uuid = request.query_params.get("settings_uuid")
            queryset = models.ArrowBillingSyncItem.objects.exclude(
                vendor_name=""
            ).exclude(vendor_name__isnull=True)

            if settings_uuid:
                queryset = queryset.filter(
                    billing_sync__customer_mapping__settings__uuid=settings_uuid
                )

            db_vendors = (
                queryset.order_by().values_list("vendor_name", flat=True).distinct()
            )
            vendor_names.update(db_vendors)

        # Return as list of objects with value/label for dropdown
        choices = [{"value": name, "label": name} for name in sorted(vendor_names)]
        return response.Response(choices)

    vendor_choices_permissions = [structure_permissions.is_staff]


# -------------------- Customer Mapping ViewSet --------------------


class ArrowCustomerMappingViewSet(core_views.ActionsViewSet):
    """
    ViewSet for Arrow customer mapping management.
    """

    queryset = models.ArrowCustomerMapping.objects.all().order_by("-created")
    lookup_field = "uuid"
    serializer_class = serializers.ArrowCustomerMappingSerializer
    create_serializer_class = serializers.ArrowCustomerMappingCreateSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ArrowCustomerMappingFilter

    # Permissions - staff only
    list_permissions = retrieve_permissions = [structure_permissions.is_staff]
    create_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_staff
    ]
    destroy_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.SyncFromArrowRequestSerializer,
        responses={200: None},
        description="Sync customer list from Arrow and update arrow_company_name.",
    )
    @decorators.action(detail=False, methods=["post"])
    def sync_from_arrow(self, request):
        """
        Sync customer information from Arrow.

        Updates arrow_company_name for all active mappings.
        """
        serializer = serializers.SyncFromArrowRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        settings_uuid = serializer.validated_data.get("settings_uuid")
        if settings_uuid:
            settings = models.ArrowSettings.objects.filter(uuid=settings_uuid).first()
        else:
            settings = models.ArrowSettings.get_active()

        if not settings:
            return response.Response(
                {"error": "No Arrow settings found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        credentials = ArrowCredentials(
            api_url=settings.api_url,
            api_key=settings.api_key,
        )
        client = ArrowClient(credentials)

        try:
            arrow_customers = client.list_all_customers()
            # Build map using .get() to handle missing keys gracefully
            arrow_customer_map = {
                c.get("Reference") or c.get("reference"): c
                for c in arrow_customers
                if c.get("Reference") or c.get("reference")
            }

            updated = 0
            for mapping in models.ArrowCustomerMapping.objects.filter(
                settings=settings, is_active=True
            ):
                arrow_customer = arrow_customer_map.get(mapping.arrow_reference)
                if arrow_customer:
                    new_name = arrow_customer.get(
                        "CompanyName", ""
                    ) or arrow_customer.get("companyName", "")
                    if mapping.arrow_company_name != new_name:
                        mapping.arrow_company_name = new_name
                        mapping.save(update_fields=["arrow_company_name"])
                        updated += 1

            return response.Response(
                {"updated": updated},
                status=status.HTTP_200_OK,
            )
        except ArrowBackendError as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    sync_from_arrow_serializer_class = serializers.SyncFromArrowRequestSerializer
    sync_from_arrow_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={200: serializers.CustomerBillingSummaryResponseSerializer},
        description="Get billing and consumption summary for this customer mapping.",
    )
    @decorators.action(detail=True, methods=["get"])
    def billing_summary(self, request, uuid=None):
        """
        Get billing and consumption summary for a specific customer mapping.

        Returns:
        - Summary statistics (totals, counts)
        - Recent consumption records
        - Recent billing syncs
        """
        mapping = self.get_object()

        # Get consumption records for this customer (via waldur_customer)
        consumption_records = (
            models.ArrowConsumptionRecord.objects.filter(
                resource__project__customer=mapping.waldur_customer
            )
            .select_related("resource")
            .order_by("-billing_period", "-created")
        )

        # Get billing syncs for this customer mapping
        billing_syncs = models.ArrowBillingSync.objects.filter(
            customer_mapping=mapping
        ).order_by("-report_period", "-created")

        # Calculate statistics
        total_consumption = consumption_records.count()
        pending = consumption_records.filter(finalized_at__isnull=True).count()
        finalized = consumption_records.filter(finalized_at__isnull=False).count()
        reconciled = consumption_records.filter(reconciled_at__isnull=False).count()

        total_consumed_sell = consumption_records.aggregate(total=Sum("consumed_sell"))[
            "total"
        ] or Decimal("0")
        total_final_sell = consumption_records.filter(
            final_sell__isnull=False
        ).aggregate(total=Sum("final_sell"))["total"]

        total_billing_syncs = billing_syncs.count()
        total_billing_sell = billing_syncs.aggregate(total=Sum("sell_total"))["total"]

        # Get recent records (limit to 20)
        recent_consumption = [
            {
                "uuid": r.uuid,
                "license_reference": r.license_reference,
                "resource_name": r.resource.name if r.resource else None,
                "billing_period": r.billing_period,
                "consumed_sell": r.consumed_sell,
                "final_sell": r.final_sell,
                "is_finalized": r.is_finalized,
                "is_reconciled": r.is_reconciled,
            }
            for r in consumption_records[:20]
        ]

        recent_syncs = [
            {
                "uuid": s.uuid,
                "report_period": s.report_period,
                "state": s.state,
                "sell_total": s.sell_total,
                "items_count": s.items.count() if hasattr(s, "items") else 0,
                "created": s.created,
            }
            for s in billing_syncs[:20]
        ]

        data = {
            "customer_mapping_uuid": mapping.uuid,
            "arrow_reference": mapping.arrow_reference,
            "arrow_company_name": mapping.arrow_company_name,
            "waldur_customer_uuid": mapping.waldur_customer.uuid,
            "waldur_customer_name": mapping.waldur_customer.name,
            "total_consumption_records": total_consumption,
            "total_consumed_sell": total_consumed_sell,
            "total_final_sell": total_final_sell,
            "pending_records": pending,
            "finalized_records": finalized,
            "reconciled_records": reconciled,
            "total_billing_syncs": total_billing_syncs,
            "total_billing_sell": total_billing_sell,
            "recent_consumption_records": recent_consumption,
            "recent_billing_syncs": recent_syncs,
        }

        response_serializer = serializers.CustomerBillingSummaryResponseSerializer(data)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    billing_summary_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={200: serializers.FetchCustomerArrowDataResponseSerializer},
        description="Fetch fresh consumption and billing data from Arrow API for this customer.",
    )
    @decorators.action(detail=True, methods=["get"])
    def fetch_arrow_data(self, request, uuid=None):
        """
        Fetch fresh consumption and billing data from Arrow API for a customer.

        Returns current month billing lines and consumption data directly from Arrow.

        Resource linking: Resources are linked to Arrow via their `backend_id` field
        which should contain the Arrow License Reference (e.g., XSP12345).
        Consumption is fetched by calling get_monthly_consumption(backend_id) directly.
        """
        from .backend import ArrowBackendError, ArrowClient, ArrowCredentials

        mapping = self.get_object()
        settings = mapping.settings

        if not settings:
            return response.Response(
                {"error": "No Arrow settings found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get current period
        today = date.today()
        period = f"{today.year:04d}-{today.month:02d}"

        credentials = ArrowCredentials(
            api_url=settings.api_url,
            api_key=settings.api_key,
        )
        client = ArrowClient(credentials)

        billing_lines = []
        billing_total_sell = Decimal("0")
        billing_total_buy = Decimal("0")
        billing_available = False
        consumption_lines = []
        consumption_total_sell = Decimal("0")
        consumption_total_buy = Decimal("0")
        error_msg = None

        # Fetch billing data for current month (for display, not for matching)
        if settings.export_type_reference:
            try:
                billing_data = client.export_billing_all_pages(
                    export_type_reference=settings.export_type_reference,
                    period_from=period,
                    period_to=period,
                )
                all_lines = client.parse_billing_export_to_dicts(billing_data)

                # Filter by customer company name
                for line in all_lines:
                    if (
                        line.get("End User Company Name", "")
                        != mapping.arrow_company_name
                    ):
                        continue
                    sell = Decimal(str(line.get("Customer Total Price", 0) or 0))
                    buy = Decimal(str(line.get("Total Wholesale Price", 0) or 0))
                    qty = line.get("Qty") or line.get("Quantity")
                    license_ref = line.get("ARS Subscription ID", "")

                    billing_lines.append(
                        {
                            "vendor_name": line.get("Vendor Name", ""),
                            "subscription_reference": line.get(
                                "Vendor Subscription ID", ""
                            ),
                            "license_reference": license_ref,
                            "offer_sku": line.get("Arrow SKU", ""),
                            "classification": line.get("Classification", ""),
                            "quantity": Decimal(str(qty)) if qty else None,
                            "sell_price": sell,
                            "buy_price": buy,
                        }
                    )
                    billing_total_sell += sell
                    billing_total_buy += buy

                billing_available = True
            except ArrowBackendError as e:
                # Billing export may not be available for current month yet
                logger.info(f"Billing export not available for {period}: {e}")
                billing_available = False
        else:
            logger.info("No export_type_reference configured, skipping billing fetch")

        total_resources = 0
        resources_with_backend_id = 0
        consumption_fetched = 0

        try:
            # Get resources for this customer
            from waldur_mastermind.marketplace import models as marketplace_models

            # Get all resources for this customer
            all_customer_resources = marketplace_models.Resource.objects.filter(
                project__customer=mapping.waldur_customer,
            )
            total_resources = all_customer_resources.count()

            # Find resources that have backend_id (which should be the Arrow License Reference)
            resources_with_license = all_customer_resources.exclude(
                backend_id__isnull=True
            ).exclude(backend_id="")
            resources_with_backend_id = resources_with_license.count()

            logger.info(
                f"Customer {mapping.waldur_customer.name}: "
                f"{total_resources} total resources, "
                f"{resources_with_backend_id} with backend_id (license reference)"
            )

            # For each resource with backend_id, use it as license_reference to fetch consumption
            # According to docs: "Resources are linked to Arrow via the backend_id field
            # containing the Arrow license reference (e.g., XSP12345)"
            for resource in resources_with_license:
                license_ref = resource.backend_id  # backend_id IS the license reference

                try:
                    # Fetch consumption from Arrow API using backend_id as license_reference
                    consumption_data = client.get_monthly_consumption(
                        license_reference=license_ref,
                        period_from=period,
                        period_to=period,
                    )
                    parsed = client.parse_consumption_to_dicts(consumption_data)

                    # Aggregate totals for this license
                    license_sell = Decimal("0")
                    license_buy = Decimal("0")
                    for row in parsed:
                        sell = Decimal(str(row.get("Total sell price", 0) or 0))
                        buy = Decimal(str(row.get("Total buy price", 0) or 0))
                        license_sell += sell
                        license_buy += buy

                    consumption_lines.append(
                        {
                            "license_reference": license_ref,
                            "resource_name": resource.name,
                            "resource_uuid": str(resource.uuid),
                            "period": period,
                            "sell_price": license_sell,
                            "buy_price": license_buy,
                        }
                    )
                    consumption_total_sell += license_sell
                    consumption_total_buy += license_buy
                    consumption_fetched += 1

                except ArrowBackendError as e:
                    logger.warning(
                        f"Failed to fetch consumption for license {license_ref}: {e}"
                    )
                    consumption_lines.append(
                        {
                            "license_reference": license_ref,
                            "resource_name": resource.name,
                            "resource_uuid": str(resource.uuid),
                            "period": period,
                            "sell_price": None,
                            "buy_price": None,
                            "error": str(e),
                        }
                    )

        except Exception as e:
            if not error_msg:
                error_msg = f"Failed to fetch consumption data: {e}"
            logger.warning(f"Error fetching consumption: {e}")

        data = {
            "customer_mapping_uuid": mapping.uuid,
            "arrow_reference": mapping.arrow_reference,
            "arrow_company_name": mapping.arrow_company_name,
            "waldur_customer_name": mapping.waldur_customer.name,
            "period": period,
            "billing_available": billing_available,
            "billing_lines": billing_lines,
            "billing_total_sell": billing_total_sell if billing_lines else None,
            "billing_total_buy": billing_total_buy if billing_lines else None,
            "consumption_lines": consumption_lines,
            "consumption_total_sell": consumption_total_sell
            if consumption_lines
            else None,
            "consumption_total_buy": consumption_total_buy
            if consumption_lines
            else None,
            "total_customer_resources": total_resources,
            "resources_with_backend_id": resources_with_backend_id,
            "matched_resources": consumption_fetched,
            "error": error_msg,
        }

        response_serializer = serializers.FetchCustomerArrowDataResponseSerializer(data)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    fetch_arrow_data_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={200: serializers.DiscoverLicensesResponseSerializer},
        description="Discover Arrow licenses for this customer and show linkable Waldur resources.",
    )
    @decorators.action(detail=True, methods=["get"])
    def discover_licenses(self, request, uuid=None):
        """
        Discover Arrow licenses for a customer and show Waldur resources that can be linked.

        Returns:
        - Arrow licenses from the /licenses API (with license_id as license_reference)
        - Waldur resources for this customer (with their current backend_id)
        - Suggested matches based on name similarity
        """
        from difflib import SequenceMatcher

        from .backend import ArrowBackendError, ArrowClient, ArrowCredentials

        mapping = self.get_object()
        settings = mapping.settings

        if not settings:
            return response.Response(
                {"error": "No Arrow settings found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        credentials = ArrowCredentials(
            api_url=settings.api_url,
            api_key=settings.api_key,
        )
        client = ArrowClient(credentials)

        arrow_licenses = []
        error_msg = None
        resolved_arrow_ref = None

        # First, resolve the actual Arrow customer reference
        # Our mapping might use a short code (RAEVALD) but Arrow uses XSP1535885
        try:
            # Try to find the customer by matching name or reference
            arrow_customers = client.list_all_customers()

            # Build a map for lookup - Arrow uses PascalCase keys
            for cust in arrow_customers:
                cust_ref = cust.get("Reference", "")
                cust_name = cust.get("CompanyName", "")
                internal_ref = cust.get("InternalReference", "")
                ref_numeric = cust.get("Ref", "")  # Numeric part without XSP

                # Check if our mapping reference matches any Arrow identifier
                if (
                    mapping.arrow_reference == cust_ref
                    or mapping.arrow_reference == ref_numeric
                    or f"XSP{mapping.arrow_reference}" == cust_ref
                    or mapping.arrow_reference.upper() == internal_ref.upper()
                    or mapping.waldur_customer.name.lower() == cust_name.lower()
                ):
                    resolved_arrow_ref = cust_ref
                    logger.info(
                        f"Resolved mapping {mapping.arrow_reference} to Arrow customer {cust_ref} ({cust_name})"
                    )
                    break

            if not resolved_arrow_ref:
                logger.warning(
                    f"Could not resolve Arrow customer reference for mapping {mapping.arrow_reference}. "
                    f"Waldur customer: {mapping.waldur_customer.name}"
                )
        except ArrowBackendError as e:
            error_msg = f"Failed to fetch Arrow customers: {e}"
            logger.warning(error_msg)

        # Fetch licenses from the /licenses API
        if resolved_arrow_ref:
            try:
                page = 1
                while page <= 50:  # Safety limit
                    response_data = client._get(
                        "licenses", params={"page": page, "per_page": 100}
                    )
                    licenses = response_data.get("data", {}).get("licenses", [])
                    if not licenses:
                        break

                    for lic in licenses:
                        if lic.get("customer_ref") == resolved_arrow_ref:
                            arrow_licenses.append(
                                {
                                    "license_reference": lic.get("license_id", ""),
                                    "vendor_name": lic.get("service_ref", ""),
                                    "offer_name": lic.get("name", ""),
                                    "offer_sku": lic.get("sku", ""),
                                    "friendly_name": lic.get("friendlyName", ""),
                                }
                            )

                    pagination = response_data.get("pagination", {})
                    if page >= pagination.get("totalPages", 1):
                        break
                    page += 1

                logger.info(
                    f"Found {len(arrow_licenses)} licenses for customer {resolved_arrow_ref}"
                )
            except ArrowBackendError as e:
                if not error_msg:
                    error_msg = f"Failed to fetch licenses: {e}"
                logger.warning(error_msg)

        # Get Waldur resources for this customer
        from waldur_mastermind.marketplace import models as marketplace_models

        waldur_resources = []
        resources = marketplace_models.Resource.objects.filter(
            project__customer=mapping.waldur_customer,
        ).select_related("project", "offering")

        for resource in resources:
            waldur_resources.append(
                {
                    "uuid": str(resource.uuid),
                    "name": resource.name,
                    "backend_id": resource.backend_id or "",
                    "project_name": resource.project.name if resource.project else "",
                    "offering_name": resource.offering.name
                    if resource.offering
                    else "",
                    "state": resource.state,
                }
            )

        # Generate suggestions using fuzzy matching
        suggestions = []
        linked_licenses = {r["backend_id"] for r in waldur_resources if r["backend_id"]}

        for resource in waldur_resources:
            if resource["backend_id"]:
                # Already linked
                continue

            resource_name = resource["name"].lower()
            best_match = None
            best_score = 0.0

            for license in arrow_licenses:
                if license["license_reference"] in linked_licenses:
                    # Already linked to another resource
                    continue

                # Try matching against friendly_name, offer_name
                for field in ["friendly_name", "offer_name"]:
                    license_name = (license.get(field) or "").lower()
                    if license_name:
                        score = SequenceMatcher(
                            None, resource_name, license_name
                        ).ratio()
                        if score > best_score and score > 0.4:
                            best_score = score
                            best_match = license

            if best_match:
                suggestions.append(
                    {
                        "resource_uuid": resource["uuid"],
                        "resource_name": resource["name"],
                        "license_reference": best_match["license_reference"],
                        "license_name": best_match.get("friendly_name")
                        or best_match.get("offer_name", ""),
                        "confidence": best_score,
                    }
                )

        data = {
            "customer_mapping_uuid": mapping.uuid,
            "arrow_reference": resolved_arrow_ref or mapping.arrow_reference,
            "waldur_customer_name": mapping.waldur_customer.name,
            "arrow_licenses": arrow_licenses,
            "waldur_resources": waldur_resources,
            "suggestions": suggestions,
            "error": error_msg,
        }

        response_serializer = serializers.DiscoverLicensesResponseSerializer(data)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_licenses_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.LinkResourceRequestSerializer,
        responses={200: serializers.LinkResourceResponseSerializer},
        description="Link a Waldur resource to an Arrow license by setting its backend_id.",
    )
    @decorators.action(detail=True, methods=["post"])
    def link_resource(self, request, uuid=None):
        """
        Link a Waldur resource to an Arrow license by setting its backend_id.

        This allows consumption tracking for the resource.
        """
        mapping = self.get_object()

        serializer = serializers.LinkResourceRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        resource_uuid = serializer.validated_data["resource_uuid"]
        license_reference = serializer.validated_data["license_reference"]

        from waldur_mastermind.marketplace import models as marketplace_models

        try:
            resource = marketplace_models.Resource.objects.get(
                uuid=resource_uuid,
                project__customer=mapping.waldur_customer,
            )
        except marketplace_models.Resource.DoesNotExist:
            return response.Response(
                {"error": f"Resource {resource_uuid} not found for this customer"},
                status=status.HTTP_404_NOT_FOUND,
            )

        old_backend_id = resource.backend_id
        resource.backend_id = license_reference
        resource.save(update_fields=["backend_id"])

        logger.info(
            f"Linked resource {resource.name} ({resource.uuid}) to Arrow license {license_reference}. "
            f"Previous backend_id: {old_backend_id or 'None'}"
        )

        data = {
            "resource_uuid": str(resource.uuid),
            "resource_name": resource.name,
            "license_reference": license_reference,
            "previous_backend_id": old_backend_id or "",
            "success": True,
        }

        response_serializer = serializers.LinkResourceResponseSerializer(data)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    link_resource_serializer_class = serializers.LinkResourceRequestSerializer
    link_resource_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.ImportLicenseRequestSerializer,
        responses={200: serializers.ImportLicenseResponseSerializer},
        description="Import an Arrow license as a new Waldur resource.",
    )
    @decorators.action(detail=True, methods=["post"])
    def import_license(self, request, uuid=None):
        """
        Import an Arrow license as a new Waldur marketplace resource.

        Creates a new resource in the specified project with the license reference
        as backend_id, linked to the specified offering.
        """
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.enums import OrderStates

        mapping = self.get_object()

        serializer = serializers.ImportLicenseRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        license_reference = serializer.validated_data["license_reference"]
        license_name = serializer.validated_data.get("license_name", license_reference)
        offering_uuid = serializer.validated_data["offering_uuid"]
        project_uuid = serializer.validated_data["project_uuid"]

        # Validate offering exists
        try:
            offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)
        except marketplace_models.Offering.DoesNotExist:
            return response.Response(
                {"error": f"Offering {offering_uuid} not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Validate project exists and belongs to the mapped customer
        try:
            project = structure_models.Project.objects.get(
                uuid=project_uuid,
                customer=mapping.waldur_customer,
            )
        except structure_models.Project.DoesNotExist:
            return response.Response(
                {"error": f"Project {project_uuid} not found for this customer"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check if already imported
        if marketplace_models.Resource.objects.filter(
            offering=offering,
            backend_id=license_reference,
        ).exists():
            return response.Response(
                {"error": f"License {license_reference} has already been imported"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create the marketplace resource directly
        resource = marketplace_models.Resource(
            project=project,
            offering=offering,
            backend_id=license_reference,
            state=marketplace_models.Resource.States.OK,
            name=license_name,
        )
        resource.init_cost()
        resource.save()

        # Create a "fake" order for audit trail
        order = marketplace_models.Order(
            created=resource.created,
            created_by=request.user,
            resource=resource,
            offering=offering,
            project=project,
            state=OrderStates.DONE,
            consumer_reviewed_by=request.user,
            provider_reviewed_by=request.user,
            consumer_reviewed_at=resource.created,
            provider_reviewed_at=resource.created,
        )
        order.save()

        logger.info(
            f"Imported Arrow license {license_reference} as resource {resource.uuid} "
            f"for customer {mapping.waldur_customer.name}"
        )

        data = {
            "resource_uuid": str(resource.uuid),
            "resource_name": resource.name,
            "license_reference": license_reference,
            "offering_name": offering.name,
            "project_name": project.name,
            "success": True,
        }

        response_serializer = serializers.ImportLicenseResponseSerializer(data)
        return response.Response(
            response_serializer.data, status=status.HTTP_201_CREATED
        )

    import_license_serializer_class = serializers.ImportLicenseRequestSerializer
    import_license_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={200: serializers.AvailableArrowCustomersResponseSerializer},
        description="Get available Arrow customers that are not yet mapped, with suggestions for Waldur organization matches.",
    )
    @decorators.action(detail=False, methods=["get"])
    def available_customers(self, request):
        """
        Fetch Arrow customers from the configured account and return unmapped ones.

        Returns:
        - Arrow customers not yet mapped (filtered out already-mapped references)
        - Waldur customers for selection
        - Suggestions with fuzzy matching confidence scores
        """
        settings = models.ArrowSettings.get_active()
        if not settings:
            return response.Response(
                {"detail": "No active Arrow settings configured"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            credentials = ArrowCredentials(
                api_url=settings.api_url,
                api_key=settings.api_key,
            )
            client = ArrowClient(credentials)
            arrow_customers_raw = client.list_all_customers()
            # Arrow API returns PascalCase keys (Reference, CompanyName, etc.)
            # but some endpoints may return camelCase, so we check both
            arrow_customers = []
            for c in arrow_customers_raw:
                contact = c.get("Contact", c.get("contact", {})) or {}
                arrow_customers.append(
                    {
                        "reference": c.get("Reference") or c.get("reference", ""),
                        "companyName": c.get("CompanyName") or c.get("companyName", ""),
                        "email": c.get("EmailContact", "")
                        or contact.get("Email", "")
                        or contact.get("email", ""),
                        "city": c.get("City") or c.get("city", ""),
                        "countryCode": c.get("CountryCode") or c.get("countryCode", ""),
                    }
                )
        except ArrowBackendError as e:
            return response.Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get already-mapped references for this settings
        mapped_refs = set(
            models.ArrowCustomerMapping.objects.filter(
                settings=settings, is_active=True
            ).values_list("arrow_reference", flat=True)
        )

        # Filter out already-mapped customers
        available_customers = [
            c for c in arrow_customers if c["reference"] not in mapped_refs
        ]

        # Get Waldur customers
        waldur_customers = list(
            structure_models.Customer.objects.values("uuid", "name", "abbreviation")
        )

        # Generate suggestions using fuzzy matching
        suggestions = []
        for arrow_customer in available_customers:
            suggestion = {
                "arrow_customer": arrow_customer,
                "suggested_waldur_customer": None,
                "confidence": 0.0,
                "existing_mapping": False,
            }

            # Find best matching Waldur customer
            arrow_name = arrow_customer["companyName"].lower()
            best_match = None
            best_score = 0.0

            for waldur_customer in waldur_customers:
                waldur_name = waldur_customer["name"].lower()
                score = SequenceMatcher(None, arrow_name, waldur_name).ratio()
                if score > best_score and score > 0.5:
                    best_score = score
                    best_match = waldur_customer

            if best_match:
                suggestion["suggested_waldur_customer"] = best_match
                suggestion["confidence"] = best_score

            suggestions.append(suggestion)

        return response.Response(
            {
                "settings_uuid": settings.uuid,
                "arrow_customers": available_customers,
                "waldur_customers": waldur_customers,
                "suggestions": suggestions,
            },
            status=status.HTTP_200_OK,
        )

    available_customers_permissions = [structure_permissions.is_staff]


# -------------------- Billing Sync ViewSet --------------------


class ArrowBillingSyncViewSet(core_views.ActionsViewSet):
    """
    ViewSet for Arrow billing sync management.

    Provides read-only access to sync records and manual sync triggers.
    """

    queryset = (
        models.ArrowBillingSync.objects.all()
        .select_related(
            "customer_mapping",
            "customer_mapping__waldur_customer",
            "invoice",
        )
        .order_by("-report_period", "-created")
    )
    lookup_field = "uuid"
    serializer_class = serializers.ArrowBillingSyncSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ArrowBillingSyncFilter

    # Disable create/update/delete - syncs are created automatically
    disabled_actions = ["create", "update", "partial_update", "destroy"]

    # Permissions - staff only for list/retrieve
    list_permissions = retrieve_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.TriggerSyncRequestSerializer,
        responses={202: None},
        description="Trigger billing sync for a specific period.",
    )
    @decorators.action(detail=False, methods=["post"])
    def trigger_sync(self, request):
        """
        Trigger manual billing sync for a specific period.
        """
        serializer = serializers.TriggerSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tasks.sync_arrow_billing.delay(
            year=serializer.validated_data["year"],
            month=serializer.validated_data["month"],
            settings_uuid=str(serializer.validated_data.get("settings_uuid", "")),
            resource_uuid=str(serializer.validated_data.get("resource_uuid", "")),
        )

        return response.Response(
            {"message": "Sync task scheduled"},
            status=status.HTTP_202_ACCEPTED,
        )

    trigger_sync_serializer_class = serializers.TriggerSyncRequestSerializer
    trigger_sync_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.ReconcileRequestSerializer,
        responses={202: None},
        description="Trigger reconciliation for a specific period.",
    )
    @decorators.action(detail=False, methods=["post"])
    def reconcile(self, request):
        """
        Trigger reconciliation for a specific period.

        Creates compensation invoice items for validated billing.
        """
        serializer = serializers.ReconcileRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tasks.reconcile_arrow_billing.delay(
            year=serializer.validated_data["year"],
            month=serializer.validated_data["month"],
            settings_uuid=str(serializer.validated_data.get("settings_uuid", "")),
            force=serializer.validated_data.get("force", False),
        )

        return response.Response(
            {"message": "Reconciliation task scheduled"},
            status=status.HTTP_202_ACCEPTED,
        )

    reconcile_serializer_class = serializers.ReconcileRequestSerializer
    reconcile_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.SyncResourcesRequestSerializer,
        responses={200: serializers.SyncResourcesResponseSerializer},
        description=(
            "Sync Arrow IAAS subscriptions to Waldur Resources. "
            "Matches subscriptions by Vendor Subscription ID to resource backend_id. "
            "Updates resource report and current_usages fields. "
            "With force_import=True, auto-creates Customers and Projects from Arrow data."
        ),
    )
    @decorators.action(detail=False, methods=["post"])
    def sync_resources(self, request):
        """
        Sync Arrow IAAS subscriptions to Waldur Resources.

        Matches Arrow subscriptions by Vendor Subscription ID to Waldur Resource backend_id.
        Updates resource report and current_usages fields with aggregated billing data.

        With force_import=True:
        - Creates Waldur Customers from Arrow customer data (name, address, etc.)
        - Creates Projects under each customer for Arrow resources
        - Creates the Arrow Azure offering if needed
        """
        serializer = serializers.SyncResourcesRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = tasks.sync_arrow_resources(
            period_from=serializer.validated_data["period_from"],
            period_to=serializer.validated_data["period_to"],
            settings_uuid=str(serializer.validated_data.get("settings_uuid", "")),
            offering_uuid=str(serializer.validated_data.get("offering_uuid", "")),
            project_uuid=str(serializer.validated_data.get("project_uuid", "")),
            force_import=serializer.validated_data.get("force_import", False),
        )

        return response.Response(result, status=status.HTTP_200_OK)

    sync_resources_serializer_class = serializers.SyncResourcesRequestSerializer
    sync_resources_permissions = [structure_permissions.is_staff]

    # -------------------- Staff Maintenance Actions --------------------

    @extend_schema(
        request=serializers.TriggerConsumptionSyncRequestSerializer,
        responses={202: None},
        description="Trigger consumption sync for a specific period.",
    )
    @decorators.action(detail=False, methods=["post"])
    def trigger_consumption_sync(self, request):
        """
        Trigger manual consumption sync for a specific period.

        Syncs real-time consumption data from Arrow for resources
        with arrow_license_reference attribute.
        """
        serializer = serializers.TriggerConsumptionSyncRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        tasks.sync_arrow_consumption.delay(
            year=serializer.validated_data["year"],
            month=serializer.validated_data["month"],
            settings_uuid=str(serializer.validated_data.get("settings_uuid", "")),
        )

        return response.Response(
            {"message": "Consumption sync task scheduled"},
            status=status.HTTP_202_ACCEPTED,
        )

    trigger_consumption_sync_serializer_class = (
        serializers.TriggerConsumptionSyncRequestSerializer
    )
    trigger_consumption_sync_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.SyncResourceHistoricalConsumptionRequestSerializer,
        responses={
            200: serializers.SyncResourceHistoricalConsumptionResponseSerializer
        },
        description="Sync historical consumption for a specific resource from Arrow.",
    )
    @decorators.action(detail=False, methods=["post"])
    def sync_resource_historical_consumption(self, request):
        """
        Sync historical consumption for a specific resource.

        Fetches consumption data from Arrow for multiple billing periods
        and creates/updates ArrowConsumptionRecord and ComponentUsage entries.

        This is useful for backporting historical usage data for newly imported
        resources or to refresh consumption data for a specific resource.
        """
        from waldur_mastermind.marketplace import models as marketplace_models

        serializer = serializers.SyncResourceHistoricalConsumptionRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        resource_uuid = serializer.validated_data["resource_uuid"]
        period_from = serializer.validated_data.get("period_from")
        period_to = serializer.validated_data.get("period_to")
        force = serializer.validated_data.get("force", False)
        dry_run = serializer.validated_data.get("dry_run", False)

        # Get the resource
        try:
            resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
        except marketplace_models.Resource.DoesNotExist:
            return response.Response(
                {"error": f"Resource {resource_uuid} not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check if resource has a backend_id (Arrow license reference)
        license_ref = resource.backend_id
        if not license_ref:
            return response.Response(
                {
                    "error": "Resource does not have a backend_id (Arrow license reference)"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get Arrow settings
        settings = models.ArrowSettings.get_active()
        if not settings:
            return response.Response(
                {"error": "No active Arrow settings found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Calculate periods
        from dateutil.relativedelta import relativedelta

        today = date.today()
        if period_to:
            year_to, month_to = map(int, period_to.split("-"))
            end_date = date(year_to, month_to, 1)
        else:
            end_date = date(today.year, today.month, 1)

        if period_from:
            year_from, month_from = map(int, period_from.split("-"))
            start_date = date(year_from, month_from, 1)
        else:
            # Default to 12 months ago
            start_date = end_date - relativedelta(months=11)

        # Build list of periods to sync
        periods = []
        current = start_date
        while current <= end_date:
            periods.append(current)
            current = current + relativedelta(months=1)

        # Initialize Arrow client
        credentials = ArrowCredentials(
            api_url=settings.api_url,
            api_key=settings.api_key,
        )
        client = ArrowClient(credentials)

        results = {
            "resource_uuid": str(resource.uuid),
            "resource_name": resource.name,
            "periods_synced": 0,
            "periods_skipped": 0,
            "periods_no_data": 0,
            "errors": [],
            "dry_run": dry_run,
            "preview_periods": [],
        }

        for billing_period in periods:
            period_str = billing_period.strftime("%Y-%m")

            # Check if record already finalized
            existing = models.ArrowConsumptionRecord.objects.filter(
                resource=resource,
                billing_period=billing_period,
                license_reference=license_ref,
            ).first()

            if existing and existing.is_finalized and not force:
                results["periods_skipped"] += 1
                continue

            if dry_run:
                # Preview mode: fetch data from Arrow but don't save
                try:
                    consumption_data = client.get_monthly_consumption(
                        license_reference=license_ref,
                        period_from=period_str,
                        period_to=period_str,
                    )
                    consumption_lines = client.parse_consumption_to_dicts(
                        consumption_data
                    )
                    total_sell = sum(
                        tasks._parse_decimal(line.get("Total sell price", 0))
                        for line in consumption_lines
                    )
                    total_buy = sum(
                        tasks._parse_decimal(line.get("Total buy price", 0))
                        for line in consumption_lines
                    )
                    preview = {
                        "period": period_str,
                        "consumed_sell": str(total_sell),
                        "consumed_buy": str(total_buy),
                        "has_existing": existing is not None,
                        "existing_consumed_sell": str(existing.consumed_sell)
                        if existing
                        else None,
                        "is_finalized": existing.is_finalized if existing else False,
                        "is_reconciled": existing.is_reconciled if existing else False,
                    }
                    results["preview_periods"].append(preview)
                except Exception as e:
                    results["errors"].append({"period": period_str, "error": str(e)})
            else:
                try:
                    synced = tasks._sync_resource_consumption(
                        client=client,
                        resource=resource,
                        license_ref=license_ref,
                        billing_period=billing_period,
                        period=period_str,
                        prefix=settings.invoice_item_prefix or "Arrow consumption",
                    )
                    if synced:
                        results["periods_synced"] += 1
                    else:
                        results["periods_no_data"] += 1
                except Exception as e:
                    results["errors"].append({"period": period_str, "error": str(e)})

        return response.Response(results, status=status.HTTP_200_OK)

    sync_resource_historical_consumption_serializer_class = (
        serializers.SyncResourceHistoricalConsumptionRequestSerializer
    )
    sync_resource_historical_consumption_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.ReconcileRequestSerializer,
        responses={202: None},
        description="Trigger reconciliation (check billing export and apply adjustments).",
    )
    @decorators.action(detail=False, methods=["post"])
    def trigger_reconciliation(self, request):
        """
        Trigger reconciliation for a specific period.

        Checks billing export and applies compensation for any
        discrepancies between consumed and finalized amounts.
        """
        serializer = serializers.ReconcileRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tasks.check_and_reconcile_billing.delay(
            year=serializer.validated_data["year"],
            month=serializer.validated_data["month"],
            settings_uuid=str(serializer.validated_data.get("settings_uuid", "")),
            force_reconcile=serializer.validated_data.get("force", False),
        )

        return response.Response(
            {"message": "Reconciliation task scheduled"},
            status=status.HTTP_202_ACCEPTED,
        )

    trigger_reconciliation_serializer_class = serializers.ReconcileRequestSerializer
    trigger_reconciliation_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.CleanupConsumptionRequestSerializer,
        responses={200: serializers.CleanupConsumptionResponseSerializer},
        description="Delete consumption records with optional dry-run preview.",
    )
    @decorators.action(detail=False, methods=["post"])
    def cleanup_consumption(self, request):
        """
        Clean up consumption records based on filters.

        By default runs in dry-run mode, returning a preview of records
        that would be deleted. Set dry_run=false to actually delete.
        """
        serializer = serializers.CleanupConsumptionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        queryset = models.ArrowConsumptionRecord.objects.all()

        # Apply filters
        period_from = serializer.validated_data.get("period_from")
        period_to = serializer.validated_data.get("period_to")
        resource_uuid = serializer.validated_data.get("resource_uuid")
        only_finalized = serializer.validated_data.get("only_finalized")
        only_unfinalized = serializer.validated_data.get("only_unfinalized")
        dry_run = serializer.validated_data.get("dry_run", True)

        if period_from:
            year, month = map(int, period_from.split("-"))
            queryset = queryset.filter(billing_period__gte=date(year, month, 1))

        if period_to:
            year, month = map(int, period_to.split("-"))
            queryset = queryset.filter(billing_period__lte=date(year, month, 1))

        if resource_uuid:
            queryset = queryset.filter(resource__uuid=resource_uuid)

        if only_finalized:
            queryset = queryset.filter(finalized_at__isnull=False)
        elif only_unfinalized:
            queryset = queryset.filter(finalized_at__isnull=True)

        # Count affected records
        records_count = queryset.count()
        compensation_items_count = queryset.filter(
            compensation_item__isnull=False
        ).count()
        invoice_items_count = queryset.filter(invoice_item__isnull=False).count()

        records_deleted = 0
        if not dry_run:
            with transaction.atomic():
                records_deleted = records_count
                queryset.delete()

        data = {
            "dry_run": dry_run,
            "records_to_delete": records_count,
            "records_deleted": records_deleted,
            "compensation_items_affected": compensation_items_count,
            "invoice_items_affected": invoice_items_count,
        }
        response_serializer = serializers.CleanupConsumptionResponseSerializer(data)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    cleanup_consumption_serializer_class = (
        serializers.CleanupConsumptionRequestSerializer
    )
    cleanup_consumption_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.SyncPauseRequestSerializer,
        responses={200: serializers.SyncPauseResponseSerializer},
        description="Pause consumption sync operations.",
    )
    @decorators.action(detail=False, methods=["post"])
    def pause_sync(self, request):
        """
        Pause sync operations.

        Can pause globally (via Constance config) or per-settings.
        """
        input_serializer = serializers.SyncPauseRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        settings_uuid = input_serializer.validated_data.get("settings_uuid")
        pause_global = input_serializer.validated_data.get("pause_global", False)

        result = {"paused": []}

        if pause_global:
            # Pause global sync via Constance
            config.ARROW_CONSUMPTION_SYNC_ENABLED = False
            result["paused"].append("global")

        if settings_uuid:
            settings = models.ArrowSettings.objects.filter(uuid=settings_uuid).first()
            if settings:
                settings.sync_enabled = False
                settings.save(update_fields=["sync_enabled"])
                result["paused"].append(str(settings_uuid))
            else:
                return response.Response(
                    {"error": f"Settings not found: {settings_uuid}"},
                    status=status.HTTP_404_NOT_FOUND,
                )

        if not pause_global and not settings_uuid:
            # Pause active settings
            settings = models.ArrowSettings.get_active()
            if settings:
                settings.sync_enabled = False
                settings.save(update_fields=["sync_enabled"])
                result["paused"].append(str(settings.uuid))

        response_serializer = serializers.SyncPauseResponseSerializer(result)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    pause_sync_serializer_class = serializers.SyncPauseRequestSerializer
    pause_sync_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.SyncPauseRequestSerializer,
        responses={200: serializers.SyncPauseResponseSerializer},
        description="Resume consumption sync operations.",
    )
    @decorators.action(detail=False, methods=["post"])
    def resume_sync(self, request):
        """
        Resume sync operations.

        Can resume globally (via Constance config) or per-settings.
        """
        input_serializer = serializers.SyncPauseRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        settings_uuid = input_serializer.validated_data.get("settings_uuid")
        pause_global = input_serializer.validated_data.get("pause_global", False)

        result = {"resumed": []}

        if pause_global:
            # Resume global sync via Constance
            config.ARROW_CONSUMPTION_SYNC_ENABLED = True
            result["resumed"].append("global")

        if settings_uuid:
            settings = models.ArrowSettings.objects.filter(uuid=settings_uuid).first()
            if settings:
                settings.sync_enabled = True
                settings.save(update_fields=["sync_enabled"])
                result["resumed"].append(str(settings_uuid))
            else:
                return response.Response(
                    {"error": f"Settings not found: {settings_uuid}"},
                    status=status.HTTP_404_NOT_FOUND,
                )

        if not pause_global and not settings_uuid:
            # Resume active settings
            settings = models.ArrowSettings.get_active()
            if settings:
                settings.sync_enabled = True
                settings.save(update_fields=["sync_enabled"])
                result["resumed"].append(str(settings.uuid))

        response_serializer = serializers.SyncPauseResponseSerializer(result)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    resume_sync_serializer_class = serializers.SyncPauseRequestSerializer
    resume_sync_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={200: serializers.ConsumptionStatusResponseSerializer},
        description="Get current consumption sync status.",
    )
    @decorators.action(detail=False, methods=["get"])
    def consumption_status(self, request):
        """
        Get current consumption sync status.

        Returns global and settings-level sync enabled flags and last sync time.
        """
        settings = models.ArrowSettings.get_active()

        # Get most recent sync time from consumption records
        last_sync = (
            models.ArrowConsumptionRecord.objects.filter(last_sync_at__isnull=False)
            .order_by("-last_sync_at")
            .values_list("last_sync_at", flat=True)
            .first()
        )

        data = {
            "global_sync_enabled": getattr(
                config, "ARROW_CONSUMPTION_SYNC_ENABLED", False
            ),
            "settings_sync_enabled": settings.sync_enabled if settings else False,
            "settings_uuid": settings.uuid if settings else None,
            "last_sync_run": last_sync,
        }
        response_serializer = serializers.ConsumptionStatusResponseSerializer(data)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    consumption_status_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={200: serializers.ConsumptionStatisticsResponseSerializer},
        description="Get consumption statistics.",
    )
    @decorators.action(detail=False, methods=["get"])
    def consumption_statistics(self, request):
        """
        Get aggregate statistics for consumption records.

        Returns counts, totals, and period breakdown.
        """
        records = models.ArrowConsumptionRecord.objects.all()

        total_records = records.count()
        pending_records = records.filter(finalized_at__isnull=True).count()
        finalized_records = records.filter(finalized_at__isnull=False).count()
        reconciled_records = records.filter(reconciled_at__isnull=False).count()

        total_consumed_sell = records.aggregate(total=Sum("consumed_sell"))[
            "total"
        ] or Decimal("0")

        # Calculate total adjustments (final_sell - consumed_sell for finalized records)
        finalized = records.filter(final_sell__isnull=False)
        total_adjustments = Decimal("0")
        for record in finalized:
            if record.final_sell is not None:
                total_adjustments += record.final_sell - record.consumed_sell

        # Period breakdown
        period_data = (
            records.values("billing_period")
            .annotate(
                count=Sum(1),
                consumed_sell=Sum("consumed_sell"),
            )
            .order_by("-billing_period")[:12]
        )

        period_breakdown = []
        for p in period_data:
            period_records = records.filter(billing_period=p["billing_period"])
            period_breakdown.append(
                {
                    "period": p["billing_period"].strftime("%Y-%m"),
                    "count": period_records.count(),
                    "consumed_sell": p["consumed_sell"] or Decimal("0"),
                    "finalized_count": period_records.filter(
                        finalized_at__isnull=False
                    ).count(),
                    "reconciled_count": period_records.filter(
                        reconciled_at__isnull=False
                    ).count(),
                }
            )

        data = {
            "total_records": total_records,
            "pending_records": pending_records,
            "finalized_records": finalized_records,
            "reconciled_records": reconciled_records,
            "total_consumed_sell": total_consumed_sell,
            "total_adjustments": total_adjustments,
            "period_breakdown": period_breakdown,
        }
        response_serializer = serializers.ConsumptionStatisticsResponseSerializer(data)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    consumption_statistics_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={200: serializers.PendingRecordSerializer(many=True)},
        description="List pending consumption records (not yet finalized).",
    )
    @decorators.action(detail=False, methods=["get"])
    def pending_records(self, request):
        """
        List consumption records that haven't been finalized yet.

        Limited to 100 most recent records.
        """
        records = (
            models.ArrowConsumptionRecord.objects.filter(finalized_at__isnull=True)
            .select_related("resource")
            .order_by("-modified")[:100]
        )

        data = [
            {
                "uuid": record.uuid,
                "resource_uuid": record.resource.uuid,
                "resource_name": record.resource.name,
                "license_reference": record.license_reference,
                "billing_period": record.billing_period,
                "consumed_sell": record.consumed_sell,
                "last_sync_at": record.last_sync_at,
            }
            for record in records
        ]

        response_serializer = serializers.PendingRecordSerializer(data, many=True)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    pending_records_permissions = [structure_permissions.is_staff]

    # -------------------- Raw Arrow API Fetch Actions --------------------

    @extend_schema(
        request=serializers.FetchConsumptionRequestSerializer,
        responses={200: serializers.FetchConsumptionResponseSerializer},
        description="Fetch raw consumption data from Arrow API.",
    )
    @decorators.action(detail=False, methods=["post"])
    def fetch_consumption(self, request):
        """
        Fetch raw consumption data from Arrow API.

        Returns consumption data for the specified license and period
        directly from the Arrow API without storing it.
        """
        input_serializer = serializers.FetchConsumptionRequestSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)

        client = get_arrow_client()
        if not client:
            return response.Response(
                {"error": "No active Arrow settings found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            consumption_data = client.get_monthly_consumption(
                license_reference=input_serializer.validated_data["license_reference"],
                period_from=input_serializer.validated_data["period"],
                period_to=input_serializer.validated_data["period"],
            )
            # Parse to dict format for easier reading
            parsed_data = client.parse_consumption_to_dicts(consumption_data)
            data = {
                "license_reference": input_serializer.validated_data[
                    "license_reference"
                ],
                "period": input_serializer.validated_data["period"],
                "row_count": len(parsed_data),
                "data": parsed_data[:100],  # Limit to first 100 rows
            }
            response_serializer = serializers.FetchConsumptionResponseSerializer(data)
            return response.Response(
                response_serializer.data, status=status.HTTP_200_OK
            )
        except ArrowBackendError as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    fetch_consumption_serializer_class = serializers.FetchConsumptionRequestSerializer
    fetch_consumption_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.FetchBillingExportRequestSerializer,
        responses={200: serializers.FetchBillingExportResponseSerializer},
        description="Fetch raw billing export from Arrow API.",
    )
    @decorators.action(detail=False, methods=["post"])
    def fetch_billing_export(self, request):
        """
        Fetch raw billing export data from Arrow API.

        Returns billing line items for the specified period
        directly from the Arrow API without storing them.
        """
        input_serializer = serializers.FetchBillingExportRequestSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)

        settings = models.ArrowSettings.get_active()
        if not settings:
            return response.Response(
                {"error": "No active Arrow settings found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client = get_arrow_client()
        if not client:
            return response.Response(
                {"error": "Failed to create Arrow client"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            classification = input_serializer.validated_data.get(
                "classification", settings.classification_filter
            )
            billing_data = client.export_billing_all_pages(
                export_type_reference=settings.export_type_reference,
                period_from=input_serializer.validated_data["period_from"],
                period_to=input_serializer.validated_data["period_to"],
                classification=classification,
            )
            data = {
                "period_from": input_serializer.validated_data["period_from"],
                "period_to": input_serializer.validated_data["period_to"],
                "classification": classification,
                "row_count": len(billing_data),
                "data": billing_data[:100],  # Limit to first 100 rows
            }
            response_serializer = serializers.FetchBillingExportResponseSerializer(data)
            return response.Response(
                response_serializer.data, status=status.HTTP_200_OK
            )
        except ArrowBackendError as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    fetch_billing_export_serializer_class = (
        serializers.FetchBillingExportRequestSerializer
    )
    fetch_billing_export_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.FetchLicenseInfoRequestSerializer,
        responses={200: serializers.FetchLicenseInfoResponseSerializer},
        description="Fetch license details from Arrow API.",
    )
    @decorators.action(detail=False, methods=["post"])
    def fetch_license_info(self, request):
        """
        Fetch license details from Arrow API.

        Returns license information for the specified license reference
        directly from the Arrow API.
        """
        input_serializer = serializers.FetchLicenseInfoRequestSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)

        client = get_arrow_client()
        if not client:
            return response.Response(
                {"error": "No active Arrow settings found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            license_data = client.get_license(
                input_serializer.validated_data["license_reference"]
            )
            response_serializer = serializers.FetchLicenseInfoResponseSerializer(
                {"data": license_data}
            )
            return response.Response(
                response_serializer.data, status=status.HTTP_200_OK
            )
        except ArrowBackendError as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    fetch_license_info_serializer_class = serializers.FetchLicenseInfoRequestSerializer
    fetch_license_info_permissions = [structure_permissions.is_staff]


# -------------------- Arrow Consumption Record ViewSet --------------------


class ArrowConsumptionRecordViewSet(core_views.ActionsViewSet):
    """
    ViewSet for browsing Arrow consumption records.

    Provides read-only access to consumption records with filtering support.
    Staff-only access.
    """

    queryset = (
        models.ArrowConsumptionRecord.objects.all()
        .select_related(
            "resource",
            "resource__project",
            "resource__project__customer",
            "invoice_item",
            "compensation_item",
        )
        .order_by("-billing_period", "-created")
    )
    lookup_field = "uuid"
    serializer_class = serializers.ArrowConsumptionRecordSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ArrowConsumptionRecordFilter

    # Disable create/update/delete - records are created automatically
    disabled_actions = ["create", "update", "partial_update", "destroy"]

    # Permissions - staff only
    list_permissions = retrieve_permissions = [structure_permissions.is_staff]


# -------------------- Arrow Billing Sync Item ViewSet --------------------


class ArrowBillingSyncItemViewSet(core_views.ActionsViewSet):
    """
    ViewSet for browsing Arrow billing sync items.

    Provides read-only access to billing line items with filtering support.
    Staff-only access.
    """

    queryset = (
        models.ArrowBillingSyncItem.objects.all()
        .select_related(
            "billing_sync",
            "invoice_item",
            "compensation_item",
        )
        .order_by("-created")
    )
    lookup_field = "uuid"
    serializer_class = serializers.ArrowBillingSyncItemDetailSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ArrowBillingSyncItemFilter

    # Disable create/update/delete - items are created automatically
    disabled_actions = ["create", "update", "partial_update", "destroy"]

    # Permissions - staff only
    list_permissions = retrieve_permissions = [structure_permissions.is_staff]
