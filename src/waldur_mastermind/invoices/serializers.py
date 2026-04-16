import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import F, Sum
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions, serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core import utils as core_utils
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.common.mixins import PRICE_DECIMAL_PLACES, PRICE_MAX_DIGITS
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes

from . import log, models, utils


class ResourceLimitPeriod(serializers.Serializer):
    start = serializers.CharField(help_text="Start date of the resource limit period")
    end = serializers.CharField(help_text="End date of the resource limit period")
    quantity = serializers.IntegerField(
        help_text="Quantity of resources consumed during this period"
    )
    billing_periods = serializers.IntegerField(help_text="Number of billing periods")
    total = serializers.CharField(help_text="Total amount for this period")


class InvoiceItemDetailsSerializer(serializers.Serializer):
    resource_name = serializers.CharField(help_text="Name of the marketplace resource")
    resource_uuid = serializers.UUIDField(help_text="UUID of the marketplace resource")
    plan_name = serializers.CharField(help_text="Name of the pricing plan")
    plan_uuid = serializers.UUIDField(help_text="UUID of the pricing plan")
    offering_type = serializers.CharField(help_text="Type of the offering")
    offering_name = serializers.CharField(help_text="Name of the offering")
    offering_uuid = serializers.UUIDField(help_text="UUID of the offering")
    service_provider_name = serializers.CharField(
        help_text="Name of the service provider"
    )
    service_provider_uuid = serializers.UUIDField(
        help_text="UUID of the service provider"
    )
    plan_component_id = serializers.IntegerField(help_text="ID of the plan component")
    offering_component_type = serializers.CharField(
        help_text="Type of the offering component"
    )
    offering_component_name = serializers.CharField(
        help_text="Name of the offering component"
    )
    resource_limit_periods = ResourceLimitPeriod(
        many=True, help_text="List of resource limit periods for this invoice item"
    )


@extend_schema_field(InvoiceItemDetailsSerializer)
class InvoiceItemDetailsField(serializers.JSONField):
    pass


class InvoiceItemSerializer(serializers.HyperlinkedModelSerializer):
    tax = serializers.DecimalField(max_digits=PRICE_DECIMAL_PLACES, decimal_places=2)
    total = serializers.DecimalField(max_digits=PRICE_MAX_DIGITS, decimal_places=2)
    factor = serializers.IntegerField(read_only=True, source="get_factor")
    measured_unit = serializers.CharField(read_only=True, source="get_measured_unit")
    resource_uuid = serializers.UUIDField(read_only=True, source="resource.uuid")
    resource_name = serializers.CharField(read_only=True, source="resource.name")
    project_uuid = serializers.UUIDField(
        read_only=True, allow_null=True, source="get_project_uuid"
    )
    project_name = serializers.CharField(read_only=True, source="get_project_name")
    details = InvoiceItemDetailsField()
    billing_type = serializers.SerializerMethodField()
    credit = serializers.SerializerMethodField()

    def get_billing_type(self, item: models.InvoiceItem) -> str:
        plan_component = item.get_plan_component()
        if plan_component:
            return plan_component.component.billing_type

    def get_credit(self, item: models.InvoiceItem) -> bool:
        return item.credit is not None

    class Meta:
        model = models.InvoiceItem
        fields = (
            "uuid",
            "url",
            "name",
            "price",
            "tax",
            "total",
            "unit_price",
            "unit",
            "factor",
            "measured_unit",
            "start",
            "end",
            "article_code",
            "project_name",
            "project_uuid",
            "quantity",
            "details",
            "resource",
            "resource_uuid",
            "resource_name",
            "billing_type",
            "backend_uuid",
            "credit",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "invoice-item-detail"},
            "resource": {
                "lookup_field": "uuid",
                "view_name": "marketplace-resource-detail",
            },
            "backend_uuid": {"read_only": True},
        }


class InvoiceItemDetailSerializer(serializers.HyperlinkedModelSerializer):
    offering_uuid = serializers.UUIDField(
        read_only=True, source="resource.offering.uuid"
    )
    offering_name = serializers.SerializerMethodField()
    offering_component_type = serializers.SerializerMethodField()
    project_uuid = serializers.UUIDField(
        read_only=True, allow_null=True, source="get_project_uuid"
    )
    project_name = serializers.CharField(read_only=True, source="get_project_name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="invoice.customer.uuid"
    )
    customer_name = serializers.CharField(
        read_only=True, source="invoice.customer.name"
    )

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_offering_name(self, obj: models.InvoiceItem) -> str | None:
        if obj.resource and obj.resource.offering:
            return obj.resource.offering.name

        if obj.details and "offering_name" in obj.details:
            return obj.details["offering_name"]

        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_offering_component_type(self, obj: models.InvoiceItem) -> str | None:
        # Use direct relationship first
        if obj.plan_component and obj.plan_component.component:
            return obj.plan_component.component.type

        # Fallback to details field for backward compatibility
        if obj.details and "offering_component_type" in obj.details:
            return obj.details["offering_component_type"]

        return None

    class Meta:
        model = models.InvoiceItem
        fields = (
            "invoice",
            "resource",
            "uuid",
            "article_code",
            "unit_price",
            "unit",
            "quantity",
            "measured_unit",
            "name",
            "start",
            "end",
            "price",
            "details",
            "offering_uuid",
            "offering_name",
            "offering_component_type",
            "project_uuid",
            "project_name",
            "customer_uuid",
            "customer_name",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "invoice-item-detail"},
            "invoice": {"lookup_field": "uuid", "view_name": "invoice-detail"},
            "resource": {
                "lookup_field": "uuid",
                "view_name": "marketplace-resource-detail",
            },
        }

    def create(self, validated_data):
        resource = validated_data.get("resource")
        if resource:
            validated_data["project"] = resource.project
        return super().create(validated_data)


class InvoiceItemTotalPriceSerializer(serializers.Serializer):
    total_price = serializers.DecimalField(
        max_digits=PRICE_MAX_DIGITS,
        decimal_places=PRICE_DECIMAL_PLACES,
        help_text="Total price for the invoice item",
    )


class InvoiceItemUpdateSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.InvoiceItem
        fields = ("article_code", "quantity", "unit_price", "start", "end")
        extra_kwargs = {"quantity": {"required": False}}

    def get_fields(self):
        fields = super().get_fields()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if self.instance:
            plan_component = self.instance.get_plan_component()
            if plan_component:
                if plan_component.component.billing_type == BillingTypes.FIXED:
                    del fields["quantity"]
                else:
                    del fields["start"]
                    del fields["end"]
        return fields

    def update(self, instance, validated_data):
        """
        Update related ComponentUsage when quantity is being updated.
        """
        invoice_item = instance
        plan_component = invoice_item.get_plan_component()
        if plan_component:
            offering_component = plan_component.component
            if offering_component.billing_type == BillingTypes.USAGE:
                resource = invoice_item.resource
                if not resource:
                    raise ValidationError(
                        _("Marketplace resource is not defined in invoice item.")
                    )
                component_usage = (
                    marketplace_models.ComponentUsage.objects.filter(
                        resource=resource,
                        component=offering_component,
                        billing_period__year=invoice_item.invoice.year,
                        billing_period__month=invoice_item.invoice.month,
                    )
                    .order_by("date")
                    .last()
                )
                if not component_usage:
                    raise ValidationError(_("Component usage is not found."))
                quantity = validated_data.get("quantity")
                component_usage.usage = quantity
                component_usage.save(update_fields=["usage"])
            elif offering_component.billing_type == BillingTypes.FIXED:
                invoice_item = super().update(invoice_item, validated_data)
                invoice_item._update_quantity()
                return invoice_item

        return super().update(invoice_item, validated_data)


class InvoiceItemCompensationSerializer(serializers.Serializer):
    offering_component_name = serializers.CharField(
        help_text="Name of the offering component for compensation"
    )


class InvoiceItemMigrateToSerializer(serializers.HyperlinkedModelSerializer):
    invoice = serializers.HyperlinkedRelatedField(
        view_name="invoice-detail",
        lookup_field="uuid",
        queryset=models.Invoice.objects.all(),
    )

    class Meta:
        model = models.InvoiceItem
        fields = ("invoice",)


class CustomerDetailsSerializer(serializers.ModelSerializer):
    country_name = serializers.ReadOnlyField(source="get_country_display")

    class Meta:
        model = structure_models.Customer
        fields = (
            "name",
            "address",
            "country",
            "country_name",
            "email",
            "postal",
            "phone_number",
            "bank_name",
            "bank_account",
            "vat_code",
        )


class InvoiceSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    price = serializers.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
    )
    tax = serializers.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
    )
    total = serializers.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
    )
    items = serializers.SerializerMethodField()
    issuer_details = serializers.SerializerMethodField()
    customer_details = CustomerDetailsSerializer(source="customer")
    due_date = serializers.DateField()
    compensations = serializers.SerializerMethodField()
    incurred_costs = serializers.SerializerMethodField()

    class Meta:
        model = models.Invoice
        fields = (
            "url",
            "uuid",
            "number",
            "customer",
            "price",
            "tax",
            "total",
            "state",
            "year",
            "month",
            "issuer_details",
            "invoice_date",
            "due_date",
            "customer",
            "customer_details",
            "items",
            "backend_id",
            "payment_url",
            "reference_number",
            "compensations",
            "incurred_costs",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "customer": {"lookup_field": "uuid"},
        }

    @extend_schema_field(CustomerDetailsSerializer)
    def get_issuer_details(self, invoice):
        return settings.WALDUR_INVOICES["ISSUER_DETAILS"]

    @extend_schema_field(InvoiceItemSerializer(many=True))
    def get_items(self, invoice: models.Invoice):
        qs = invoice.items.all().order_by("project_name", "name")
        items = utils.filter_invoice_items(qs)
        serializer = InvoiceItemSerializer(items, many=True, context=self.context)
        return serializer.data

    @extend_schema_field(Decimal)
    def get_compensations(self, invoice: models.Invoice):
        items = invoice.items.filter(credit__isnull=False)
        compensations = items.aggregate(
            compensations=Sum(F("quantity") * F("unit_price"), default=0)
        ).get("compensations", 0)
        return Decimal(compensations)

    @extend_schema_field(Decimal)
    def get_incurred_costs(self, invoice: models.Invoice):
        items = invoice.items.filter(credit__isnull=True)
        incurred_costs = items.aggregate(
            incurred=Sum(F("quantity") * F("unit_price"), default=0)
        ).get("incurred", 0)
        return Decimal(incurred_costs)


class InvoiceItemCostsForPeriodSerializer(serializers.Serializer):
    period = serializers.ChoiceField(
        choices=models.PeriodMixin.Periods.CHOICES,
        default=models.PeriodMixin.Periods.TOTAL,
        help_text="Period for which statistics should be calculated.",
    )


class InvoiceItemProjectCostsForPeriodSerializer(InvoiceItemCostsForPeriodSerializer):
    project_uuid = serializers.UUIDField(
        help_text="UUID of the project for which statistics should be calculated."
    )

    def validate(self, attrs):
        user = self.context["request"].user
        project_uuid = attrs["project_uuid"]
        queryset = structure_models.Project.objects.filter(uuid=project_uuid)
        queryset = filter_queryset_for_user(queryset, user)
        if not queryset.exists():
            raise exceptions.NotFound("Project with UUID %s not found." % project_uuid)
        return attrs


class InvoiceItemCustomerCostsForPeriodSerializer(InvoiceItemCostsForPeriodSerializer):
    customer_uuid = serializers.UUIDField(
        help_text="UUID of the customer for which statistics should be calculated."
    )

    def validate(self, attrs):
        user = self.context["request"].user
        customer_uuid = attrs["customer_uuid"]
        queryset = structure_models.Customer.objects.filter(uuid=customer_uuid)
        queryset = filter_queryset_for_user(queryset, user)
        if not queryset.exists():
            raise exceptions.NotFound(
                "Customer with UUID %s not found." % customer_uuid
            )
        return attrs


class InvoiceItemReportSerializer(serializers.ModelSerializer):
    invoice_number = serializers.ReadOnlyField(source="invoice.number")
    invoice_uuid = serializers.UUIDField(read_only=True, source="invoice.uuid")
    invoice_year = serializers.ReadOnlyField(source="invoice.year")
    invoice_month = serializers.ReadOnlyField(source="invoice.month")
    invoice_date = serializers.ReadOnlyField(source="invoice.invoice_date")
    due_date = serializers.ReadOnlyField(source="invoice.due_date")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="invoice.customer.uuid"
    )
    customer_name = serializers.ReadOnlyField(source="invoice.customer.name")

    class Meta:
        model = models.InvoiceItem
        fields = (
            "customer_uuid",
            "customer_name",
            "project_uuid",
            "project_name",
            "invoice_uuid",
            "invoice_number",
            "invoice_year",
            "invoice_month",
            "invoice_date",
            "due_date",
            "invoice_price",
            "invoice_tax",
            "invoice_total",
            "name",
            "article_code",
            "price",
            "tax",
            "total",
            "unit_price",
            "unit",
            "start",
            "end",
        )
        decimal_fields = (
            "price",
            "tax",
            "total",
            "unit_price",
            "invoice_price",
            "invoice_tax",
            "invoice_total",
        )
        decimal_fields_extra_kwargs = {
            "invoice_price": {
                "source": "invoice.price",
            },
            "invoice_tax": {
                "source": "invoice.tax",
            },
            "invoice_total": {
                "source": "invoice.total",
            },
        }

    def build_field(self, field_name, info, model_class, nested_depth):
        if field_name in self.Meta.decimal_fields:
            field_class = serializers.DecimalField
            field_kwargs = dict(
                max_digits=20,
                decimal_places=2,
                coerce_to_string=True,
            )
            default_kwargs = self.Meta.decimal_fields_extra_kwargs.get(field_name)
            if default_kwargs:
                field_kwargs.update(default_kwargs)
            return field_class, field_kwargs

        return super().build_field(field_name, info, model_class, nested_depth)

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()
        extra_kwargs.update(
            settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SERIALIZER_EXTRA_KWARGS"]
        )
        return extra_kwargs


class SAPReportSerializer(serializers.Serializer):
    registrikood = serializers.ReadOnlyField(
        source="invoice.customer.registration_code"
    )
    country = serializers.ReadOnlyField(source="invoice.customer.country")
    doc_type = serializers.SerializerMethodField(method_name="get_doc_type_field")
    purch_no_c = serializers.SerializerMethodField(method_name="get_purch_no_c")
    bill_date = serializers.SerializerMethodField(method_name="get_last_day_of_month")
    material = serializers.SerializerMethodField(method_name="get_material_code")
    req_qty = serializers.SerializerMethodField(method_name="get_quantity")
    sales_unit = serializers.SerializerMethodField(method_name="get_sales_unit")
    cond_value = serializers.SerializerMethodField(method_name="get_cond_value")
    sales_org = serializers.SerializerMethodField(method_name="get_sales_org_field")
    sales_off = serializers.SerializerMethodField(method_name="get_empty_field")
    eeluks = serializers.SerializerMethodField(method_name="get_eeluks_field")
    fund = serializers.SerializerMethodField(method_name="get_fund_field")
    funka = serializers.SerializerMethodField(method_name="get_funka_field")
    makset = serializers.SerializerMethodField(method_name="get_makset_field")
    maksjareg = serializers.SerializerMethodField(method_name="get_maksjareg_field")
    tekst_1 = serializers.SerializerMethodField(method_name="get_vali_field")
    projektielement = serializers.SerializerMethodField(method_name="get_empty_field")
    ressurss_kulukont = serializers.SerializerMethodField(
        method_name="get_ressurss_kulukont_field"
    )
    tekst_2 = serializers.SerializerMethodField(method_name="get_tekst_2_field")
    maarang = serializers.ReadOnlyField(source="get_empty_field")
    km_kood = serializers.SerializerMethodField(method_name="get_vat")
    kuluuksus = serializers.SerializerMethodField(method_name="get_kuluuksus_field")
    saaja_info = serializers.SerializerMethodField(method_name="get_saaja_info")
    divisjon = serializers.SerializerMethodField(method_name="get_empty_field")
    toetus = serializers.SerializerMethodField(method_name="get_empty_field")
    partii = serializers.SerializerMethodField(method_name="get_empty_field")
    ladu = serializers.SerializerMethodField(method_name="get_empty_field")
    grupitunnus = serializers.ReadOnlyField(source="get_project_name")

    class Meta:
        fields = (
            "registrikood",
            "country",
            "doc_type",
            "purch_no_c",
            "bill_date",
            "material",
            "req_qty",
            "sales_unit",
            "cond_value",
            "sales_org",
            "sales_off",
            "eeluks",
            "fund",
            "funka",
            "makset",
            "maksjareg",
            "tekst_1",
            "projektielement",
            "ressurss_kulukont",
            "tekst_2",
            "maarang",
            "km_kood",
            "kuluuksus",
            "saaja_info",
            "divisjon",
            "toetus",
            "partii",
            "ladu",
            "grupitunnus",
        )

    def format_date(self, date):
        if date:
            return date.strftime("%d.%m.%Y")
        return ""

    def get_quantity(self, invoice_item):
        return quantize_price(invoice_item.quantity)

    def get_doc_type_field(self, invoice_item):
        return "ZETA"

    def get_sales_org_field(self, invoice_item):
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAP_PARAMS"]["ORG_CODE"]

    def get_kuluuksus_field(self, invoice_item):
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAP_PARAMS"]["KULUUKSUS"]

    def get_sales_unit(self, invoice_item):
        try:
            return invoice_item.article_code.split("_")[2]
        except IndexError:
            return (
                "Article code is in unexpected format. Cannot extract sales unit! %s"
                % invoice_item.article_code
            )

    def get_cond_value(self, invoice_item):
        import locale

        locale.setlocale(locale.LC_ALL, "de_DE.UTF-8")
        return locale.format_string("%.4f", invoice_item.unit_price)

    def get_material_code(self, invoice_item):
        try:
            return invoice_item.article_code.split("_")[0]
        except IndexError:
            return (
                "Article code is in unexpected format. Cannot extract material code! %s"
                % invoice_item.article_code
            )

    def get_invoice_date(self, invoice_item):
        date = invoice_item.invoice.invoice_date
        return self.format_date(date)

    def get_sales_off_field(self, invoice_item):
        return ""

    def get_purch_no_c(self, invoice_item: models.InvoiceItem):
        customer = invoice_item.invoice.customer
        return customer.agreement_number

    def get_empty_field(self, invoice_item):
        return ""

    def get_fund_field(self, invoice_item):
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAP_PARAMS"]["FUND"]

    def get_eeluks_field(self, invoice_item):
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAP_PARAMS"]["EELUKS"]

    def get_funka_field(self, invoice_item):
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAP_PARAMS"]["FUNKA"]

    def get_makset_field(self, invoice_item):
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAP_PARAMS"]["MAKSET"]

    def get_maksjareg_field(self, invoice_item):
        customer = invoice_item.invoice.customer
        if customer.sponsor_number:
            return customer.sponsor_number
        else:
            return ""

    def get_vali_field(self, invoice_item):
        return f"Record no {invoice_item.invoice.number}. Periood: {invoice_item.invoice.month}/{invoice_item.invoice.year}"

    def get_tekst_2_field(self, invoice_item):
        # If a single plan for an offering exists, skip it from display
        if invoice_item.resource and invoice_item.resource.offering.plans.count() == 1:
            if "offering_component_name" in invoice_item.details:
                return (
                    f"{invoice_item.resource.name} ({invoice_item.resource.offering.name}) / "
                    f"{invoice_item.details['offering_component_name']}"
                )
            return invoice_item.name
        if "plan_name" in invoice_item.details.keys():
            return f"{invoice_item.name} / {invoice_item.details['plan_name']}"
        else:
            return invoice_item.name

    def get_vat(self, invoice_item):
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAP_PARAMS"]["KM_KOOD"]

    def get_ressurss_kulukont_field(self, invoice_item):
        try:
            return invoice_item.article_code.split("_")[1]
        except IndexError:
            return (
                "Article code is in unexpected format. Cannot extract kulukont! %s"
                % invoice_item.article_code
            )

    def get_lisainfo_field(self, invoice_item):
        return ""

    def get_saaja_info(self, invoice_item):
        return invoice_item.invoice.customer.contact_details

    def get_measured_unit(self, invoice_item):
        return invoice_item.measured_unit

    def get_first_day(self, invoice_item):
        year = invoice_item.invoice.year
        month = invoice_item.invoice.month
        return datetime.date(year=year, month=month, day=1)

    def get_last_day_of_month(self, invoice_item):
        first_day = self.get_first_day(invoice_item)
        last_day = core_utils.month_end(first_day)
        return self.format_date(last_day)


# SAF is accounting soft from Estonia: www.sysdec.ee/safsaf.htm
class SAFReportSerializer(serializers.Serializer):
    DOKNR = serializers.ReadOnlyField(source="invoice.number")
    KUUPAEV = serializers.SerializerMethodField(method_name="get_last_day_of_month")
    VORMKUUP = serializers.SerializerMethodField(method_name="get_invoice_date")
    MAKSEAEG = serializers.SerializerMethodField(method_name="get_due_date")
    YKSUS = serializers.ReadOnlyField(source="invoice.customer.agreement_number")
    PARTNER = serializers.SerializerMethodField(method_name="get_partner")
    ARTIKKEL = serializers.ReadOnlyField(source="article_code")
    KOGUS = serializers.IntegerField(source="quantity")
    SUMMA = serializers.SerializerMethodField(method_name="get_total")
    RMAKSUSUM = serializers.SerializerMethodField(method_name="get_tax")
    RMAKSULIPP = serializers.SerializerMethodField(method_name="get_vat")
    ARTPROJEKT = serializers.SerializerMethodField(method_name="get_project")
    ARTNIMI = serializers.SerializerMethodField(method_name="get_artnimi_field")
    VALI = serializers.SerializerMethodField(method_name="get_vali_field")
    U_KONEDEARV = serializers.SerializerMethodField(method_name="get_empty_field")
    U_GRUPPITUNNUS = serializers.ReadOnlyField(source="get_project_name")
    H_PERIOOD = serializers.SerializerMethodField(method_name="get_covered_period")

    class Meta:
        fields = (
            "DOKNR",
            "KUUPAEV",
            "VORMKUUP",
            "MAKSEAEG",
            "YKSUS",
            "PARTNER",
            "ARTIKKEL",
            "KOGUS",
            "SUMMA",
            "RMAKSUSUM",
            "RMAKSULIPP",
            "ARTPROJEKT",
            "ARTNIMI",
            "VALI",
            "U_KONEDEARV",
            "U_GRUPPITUNNUS",
            "H_PERIOOD",
        )

    def format_date(self, date):
        if date:
            return date.strftime("%d.%m.%Y")
        return ""

    def get_partner(self, invoice_item: models.InvoiceItem):
        customer = invoice_item.invoice.customer
        if customer.sponsor_number:
            return customer.sponsor_number
        else:
            return customer.agreement_number

    def get_first_day(self, invoice_item: models.InvoiceItem):
        year = invoice_item.invoice.year
        month = invoice_item.invoice.month
        return datetime.date(year=year, month=month, day=1)

    def get_last_day_of_month(self, invoice_item: models.InvoiceItem) -> str:
        first_day = self.get_first_day(invoice_item)
        last_day = core_utils.month_end(first_day)
        return self.format_date(last_day)

    def get_invoice_date(self, invoice_item: models.InvoiceItem) -> str:
        date = invoice_item.invoice.invoice_date
        return self.format_date(date)

    def get_due_date(self, invoice_item: models.InvoiceItem) -> str:
        date = invoice_item.invoice.due_date
        return self.format_date(date)

    def get_total(self, invoice_item: models.InvoiceItem) -> Decimal:
        return quantize_price(invoice_item.price)

    def get_tax(self, invoice_item: models.InvoiceItem) -> Decimal:
        # SAF expects a specific handling of rounding for VAT
        return invoice_item.tax.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_project(self, invoice_item: models.InvoiceItem) -> str:
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAF_PARAMS"]["ARTPROJEKT"]

    def get_vat(self, invoice_item: models.InvoiceItem) -> float:
        return settings.WALDUR_INVOICES["INVOICE_REPORTING"]["SAF_PARAMS"]["RMAKSULIPP"]

    def get_vali_field(self, invoice_item: models.InvoiceItem) -> str:
        if invoice_item.invoice.customer.contact_details:
            return f"Record no {invoice_item.invoice.number}. {invoice_item.invoice.customer.contact_details}"
        else:
            return f"Record no {invoice_item.invoice.number}"

    def get_empty_field(self, invoice_item: models.InvoiceItem):
        return ""

    def get_artnimi_field(self, invoice_item: models.InvoiceItem) -> str:
        # If a single plan for an offering exists, skip it from display
        if invoice_item.resource and invoice_item.resource.offering.plans.count() == 1:
            return invoice_item.name
        if "plan_name" in invoice_item.details.keys():
            return f"{invoice_item.name} / {invoice_item.details['plan_name']}"
        else:
            return invoice_item.name

    def get_covered_period(self, invoice_item: models.InvoiceItem) -> str:
        first_day = self.get_first_day(invoice_item)
        last_day = core_utils.month_end(first_day)
        return f"{self.format_date(first_day)}-{self.format_date(last_day)}"


class PaymentProfileAttributesSerializer(serializers.Serializer):
    end_date = serializers.CharField(required=False)
    agreement_number = serializers.CharField(required=False)
    contract_sum = serializers.IntegerField(required=False)


@extend_schema_field(PaymentProfileAttributesSerializer)
class PaymentProfileAttributesField(serializers.JSONField):
    pass


class PaymentProfileSerializer(serializers.HyperlinkedModelSerializer):
    organization_uuid = serializers.UUIDField(
        read_only=True, source="organization.uuid"
    )
    payment_type_display = serializers.CharField(
        read_only=True, source="get_payment_type_display"
    )
    attributes = PaymentProfileAttributesField(required=False)

    class Meta:
        model = models.PaymentProfile
        fields = (
            "uuid",
            "url",
            "name",
            "organization_uuid",
            "organization",
            "attributes",
            "payment_type",
            "payment_type_display",
            "is_active",
        )
        extra_kwargs = {
            "url": {
                "view_name": "payment-profile-detail",
                "lookup_field": "uuid",
            },
            "organization": {
                "view_name": "customer-detail",
                "lookup_field": "uuid",
            },
        }


class PaymentSerializer(
    serializers.HyperlinkedModelSerializer,
):
    profile = serializers.HyperlinkedRelatedField(
        view_name="payment-profile-detail",
        lookup_field="uuid",
        queryset=models.PaymentProfile.objects.filter(is_active=True),
    )
    invoice = serializers.HyperlinkedRelatedField(
        view_name="invoice-detail",
        lookup_field="uuid",
        read_only=True,
    )
    invoice_uuid = serializers.UUIDField(read_only=True, source="invoice.uuid")
    invoice_period = serializers.SerializerMethodField(method_name="get_invoice_period")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="profile.organization.uuid"
    )

    def get_invoice_period(self, payment: models.Payment) -> str | None:
        if payment.invoice:
            return "%02d-%s" % (payment.invoice.month, payment.invoice.year)

    class Meta:
        model = models.Payment
        fields = (
            "uuid",
            "url",
            "profile",
            "date_of_payment",
            "sum",
            "proof",
            "invoice",
            "invoice_uuid",
            "invoice_period",
            "customer_uuid",
        )
        extra_kwargs = {
            "url": {"view_name": "payment-detail", "lookup_field": "uuid"},
        }


class PaidSerializer(serializers.Serializer):
    date = serializers.DateField(required=True)
    proof = serializers.FileField(required=False)


class LinkToInvoiceSerializer(serializers.Serializer):
    invoice = serializers.HyperlinkedRelatedField(
        view_name="invoice-detail",
        lookup_field="uuid",
        queryset=models.Invoice.objects.filter(state=models.Invoice.States.PAID),
    )


class BackendIdSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Invoice
        fields = ("backend_id",)


class PaymentURLSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Invoice
        fields = ("payment_url",)


class ReferenceNumberSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Invoice
        fields = ("reference_number",)


@extend_schema_field(PaymentProfileSerializer(many=True))
def get_payment_profiles(serializer, customer: structure_models.Customer):
    user = serializer.context["request"].user

    # Use prefetched data if available to avoid N+1 queries
    if (
        hasattr(customer, "_prefetched_objects_cache")
        and "paymentprofile_set" in customer._prefetched_objects_cache
    ):
        profiles = list(customer._prefetched_objects_cache["paymentprofile_set"])

        # Filter in Python if needed
        if not (user.is_staff or user.is_support):
            if structure_permissions._has_owner_access(user, customer):
                profiles = [p for p in profiles if p.is_active]
            else:
                return None
    else:
        # Fallback to original query behavior
        if user.is_staff or user.is_support:
            profiles = customer.paymentprofile_set.all()
        elif structure_permissions._has_owner_access(user, customer):
            profiles = customer.paymentprofile_set.filter(is_active=True)
        else:
            return None

    return PaymentProfileSerializer(
        profiles,
        many=True,
        context={"request": serializer.context["request"]},
    ).data


def add_payment_profile(sender, fields, **kwargs):
    """Add a payment profile field to the serializer."""
    fields["payment_profiles"] = serializers.SerializerMethodField()
    setattr(sender, "get_payment_profiles", get_payment_profiles)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_payment_profile,
)


class FinancialReportEmailSerializer(serializers.Serializer):
    emails = serializers.ListField(child=serializers.EmailField())
    year = serializers.IntegerField()
    month = serializers.IntegerField()

    def validate_emails(self, value):
        if len(value) < 1:
            raise serializers.ValidationError("Provide at least one email address")
        return value


class NestedProviderOfferingSerializer(serializers.ModelSerializer):
    class Meta:
        model = marketplace_models.Offering
        fields = ("uuid", "url", "type", "name")
        extra_kwargs = {
            "url": {
                "view_name": "marketplace-provider-offering-detail",
                "lookup_field": "uuid",
            },
        }


class NestedPublicOfferingSerializer(serializers.ModelSerializer):
    class Meta:
        model = marketplace_models.Offering
        fields = ("uuid", "url", "type", "name")
        extra_kwargs = {
            "url": {
                "view_name": "marketplace-public-offering-detail",
                "lookup_field": "uuid",
            },
        }


class CustomerCreditSerializer(serializers.HyperlinkedModelSerializer):
    offerings = NestedProviderOfferingSerializer(
        read_only=True,
        many=True,
    )
    customer_name = serializers.ReadOnlyField(source="customer.name")
    customer_uuid = serializers.UUIDField(read_only=True, source="customer.uuid")
    customer_slug = serializers.ReadOnlyField(source="customer.slug")

    class Meta:
        model = models.CustomerCredit
        fields = (
            "uuid",
            "url",
            "value",
            "customer",
            "customer_name",
            "customer_uuid",
            "customer_slug",
            "offerings",
            "end_date",
            "expected_consumption",
            "minimal_consumption",
            "minimal_consumption_logic",
            "grace_coefficient",
            "apply_as_minimal_consumption",
            "allocated_to_projects",
            "consumption_last_month",
        )
        protected_fields = ("customer",)

        extra_kwargs = {
            "url": {
                "view_name": "customer-credit-detail",
                "lookup_field": "uuid",
            },
            "customer": {
                "view_name": "customer-detail",
                "lookup_field": "uuid",
            },
        }


class CreateCustomerCreditSerializer(CustomerCreditSerializer):
    def validate_end_date(self, end_date):
        if not end_date:
            return

        if end_date and end_date < datetime.date.today():
            raise exceptions.ValidationError(
                _("The end date must be greater than today's date.")
            )

        if end_date.day != 1:
            raise exceptions.ValidationError(
                _("End date must be the first day of the month.")
            )

        return end_date

    def get_from_attrs_or_instance(self, attrs, field_name, default=None):
        return attrs.get(field_name, getattr(self.instance, field_name, default))

    def validate(self, attrs):
        expected_consumption = self.get_from_attrs_or_instance(
            attrs, "expected_consumption"
        )
        minimal_consumption_logic = self.get_from_attrs_or_instance(
            attrs, "minimal_consumption_logic"
        )
        end_date = self.get_from_attrs_or_instance(attrs, "end_date")
        value = self.get_from_attrs_or_instance(attrs, "value")

        if expected_consumption and expected_consumption >= value:
            raise exceptions.ValidationError(
                _("Expected consumption must be smaller than the credit.")
            )

        if (
            minimal_consumption_logic
            == models.CustomerCredit.MinimalConsumptionLogic.LINEAR
        ):
            if not end_date:
                raise exceptions.ValidationError(
                    _("End date is required if minimal consumption logic is linear.")
                )
            elif (
                end_date.year == datetime.date.today().year
                and end_date.month == datetime.date.today().month
            ):
                raise exceptions.ValidationError(
                    _(
                        "End date must be greater if minimal consumption logic is linear."
                    )
                )
        return attrs

    offerings = serializers.HyperlinkedRelatedField(
        view_name="marketplace-provider-offering-detail",
        lookup_field="uuid",
        queryset=marketplace_models.Offering.objects.all(),
        required=False,
        allow_null=True,
        many=True,
    )

    def update(self, instance, validated_data):
        old_offerings = set(instance.offerings.all())
        new_offerings = set(validated_data.get("offerings", []))
        instance = super().update(instance, validated_data)

        if old_offerings != new_offerings:
            log.log_changing_of_offerings(
                instance.customer,
                old_offerings,
                new_offerings,
            )

        return instance


class ProjectCreditSerializer(serializers.HyperlinkedModelSerializer):
    project_name = serializers.ReadOnlyField(source="project.name")
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_slug = serializers.ReadOnlyField(source="project.slug")
    customer_name = serializers.ReadOnlyField(source="project.customer.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    customer_slug = serializers.ReadOnlyField(source="project.customer.slug")
    customer_credit = serializers.ReadOnlyField(
        source="project.customer.customercredit.value"
    )
    allocated_customer_credit = serializers.SerializerMethodField(
        method_name="get_allocated_customer_credit"
    )
    offerings = NestedPublicOfferingSerializer(
        read_only=True, many=True, source="project.customer.customercredit.offerings"
    )

    def get_allocated_customer_credit(self, project_credit) -> float:
        return models.ProjectCredit.objects.filter(
            project__customer=project_credit.project.customer
        ).aggregate(sum=Sum("value"))["sum"]

    def validate_project(self, project):
        user = self.context["request"].user

        if user.is_staff or user in project.customer.get_users(CustomerRole.OWNER):
            return project

        raise exceptions.PermissionDenied()

    class Meta:
        model = models.ProjectCredit
        fields = (
            "uuid",
            "url",
            "value",
            "project",
            "project_name",
            "project_uuid",
            "project_slug",
            "customer_name",
            "customer_slug",
            "customer_uuid",
            "customer_credit",
            "allocated_customer_credit",
            "consumption_last_month",
            "offerings",
            "end_date",
            "expected_consumption",
            "minimal_consumption",
            "minimal_consumption_logic",
            "grace_coefficient",
            "apply_as_minimal_consumption",
            "mark_unused_credit_as_spent_on_project_termination",
        )

        extra_kwargs = {
            "url": {
                "view_name": "project-credit-detail",
                "lookup_field": "uuid",
            },
            "project": {
                "view_name": "project-detail",
                "lookup_field": "uuid",
            },
        }


def get_project_credit(serializer, project) -> float | None:
    try:
        return project.projectcredit.value
    except models.ProjectCredit.DoesNotExist:
        return None


def add_project_credit(sender, fields, **kwargs):
    """Add a project credit field to the serializer."""
    fields["project_credit"] = serializers.SerializerMethodField()
    setattr(sender, "get_project_credit", get_project_credit)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.ProjectSerializer,
    receiver=add_project_credit,
)


def get_customer_credit(serializer, customer) -> float | None:
    # Use prefetched data if available to avoid N+1 queries
    if (
        hasattr(customer, "_prefetched_objects_cache")
        and "customercredit" in customer._prefetched_objects_cache
    ):
        # For OneToOneField, Django stores the related object directly, not as a list
        credit = customer._prefetched_objects_cache.get("customercredit")
        if credit:
            return credit.value
        return None
    else:
        # Fallback to original query behavior
        try:
            return models.CustomerCredit.objects.get(customer=customer).value
        except models.CustomerCredit.DoesNotExist:
            return None


def add_customer_credit(sender, fields, **kwargs):
    """Add a customer credit field to the serializer."""
    fields["customer_credit"] = serializers.SerializerMethodField()
    setattr(sender, "get_customer_credit", get_customer_credit)

    # Also optimize eager loading for CustomerSerializer
    if sender.__name__ == "CustomerSerializer":
        _optimize_customer_serializer_eager_load_for_credit(sender)


def _optimize_customer_serializer_eager_load_for_credit(sender):
    """Optimize eager loading for CustomerSerializer to prefetch customer credits."""
    # Check if we already have an optimized eager_load method
    # The flag is on the underlying function if it's a staticmethod
    eager_load_func = getattr(sender.eager_load, "__func__", sender.eager_load)
    if hasattr(eager_load_func, "_credit_optimized"):
        return

    # If billing optimization is already applied, we need to combine them
    if hasattr(eager_load_func, "_billing_optimized"):
        # Get the true original method that was stored by the billing optimization
        original_eager_load = getattr(
            eager_load_func, "_original_eager_load", sender.eager_load
        )

        def combined_eager_load(queryset, request=None):
            # Call the true original method first
            queryset = original_eager_load(queryset, request)

            # Add billing optimizations
            if request:
                fields = request.query_params.getlist("field")
                if "billing_price_estimate" in fields:
                    queryset._billing_optimization_enabled = True

            # Add credit optimizations
            if request:
                fields = request.query_params.getlist("field")
                if "customer_credit" in fields:
                    queryset = queryset.select_related("customercredit")

            return queryset

        # Mark as optimized for both and store original
        combined_eager_load._billing_optimized = True
        combined_eager_load._credit_optimized = True
        combined_eager_load._original_eager_load = original_eager_load

        # Replace the eager_load method
        sender.eager_load = staticmethod(combined_eager_load)
        return

    # Store the original eager_load method
    original_eager_load = sender.eager_load

    def optimized_eager_load(queryset, request=None):
        # Call the original eager_load first
        queryset = original_eager_load(queryset, request)

        # Add optimizations for customer_credit if requested
        if request:
            fields = request.query_params.getlist("field")

            # Prefetch customer credit if requested
            if "customer_credit" in fields:
                queryset = queryset.select_related("customercredit")

        return queryset

    # Mark as optimized to avoid double optimization and store original
    optimized_eager_load._credit_optimized = True
    optimized_eager_load._original_eager_load = original_eager_load

    # Replace the eager_load method
    sender.eager_load = staticmethod(optimized_eager_load)


def get_customer_unallocated_credit(serializer, customer) -> float | None:
    # Use prefetched data if available to avoid N+1 queries for customer credit
    if (
        hasattr(customer, "_prefetched_objects_cache")
        and "customercredit" in customer._prefetched_objects_cache
    ):
        # For OneToOneField, Django stores the related object directly, not as a list
        credit = customer._prefetched_objects_cache.get("customercredit")
        if not credit:
            return None
        customer_credit = credit.value
    else:
        # Fallback to original query behavior
        try:
            customer_credit = models.CustomerCredit.objects.get(customer=customer).value
        except models.CustomerCredit.DoesNotExist:
            return None

    # Calculate project credits sum
    # TODO: This could be further optimized with bulk prefetching
    project_credits_sum = (
        models.ProjectCredit.objects.filter(project__customer=customer).aggregate(
            sum=Sum("value")
        )["sum"]
        or 0
    )

    return customer_credit - project_credits_sum


def add_customer_unallocated_credit(sender, fields, **kwargs):
    """Add a customer unallocated credit field to the serializer."""
    fields["customer_unallocated_credit"] = serializers.SerializerMethodField()
    setattr(sender, "get_customer_unallocated_credit", get_customer_unallocated_credit)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_customer_credit,
)

core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_customer_unallocated_credit,
)


class InvoiceStatsOfferingSerializer(serializers.Serializer):
    offering_name = serializers.CharField(read_only=True)
    aggregated_price = serializers.FloatField(read_only=True)
    aggregated_tax = serializers.FloatField(read_only=True)
    aggregated_total = serializers.FloatField(read_only=True)
    service_category_title = serializers.CharField(read_only=True)
    service_provider_name = serializers.CharField(read_only=True)
    service_provider_uuid = serializers.UUIDField(read_only=True)


class InvoiceStatsSerializer(serializers.ListSerializer):
    child = InvoiceStatsOfferingSerializer(read_only=True)


class InvoiceGrowthCustomerPeriodSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True)
    periods = serializers.ListField(read_only=True, child=serializers.FloatField())


class InvoiceGrowthSerializer(serializers.Serializer):
    periods = serializers.ListField(read_only=True, child=serializers.CharField())
    total_periods = serializers.ListField(
        read_only=True, child=serializers.FloatField()
    )
    other_periods = serializers.ListField(
        read_only=True, child=serializers.FloatField()
    )
    customer_periods = InvoiceGrowthCustomerPeriodSerializer(many=True)


class InvoiceCostItemSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True)
    unit_price = serializers.DecimalField(
        read_only=True, max_digits=20, decimal_places=10
    )
    unit = serializers.CharField(read_only=True)
    quantity = serializers.DecimalField(
        read_only=True, max_digits=20, decimal_places=10
    )
    measured_unit = serializers.CharField(read_only=True)
    price = serializers.FloatField(read_only=True)


class InvoiceCostSerializer(serializers.Serializer):
    price = serializers.FloatField(read_only=True)
    year = serializers.IntegerField(read_only=True)
    month = serializers.IntegerField(read_only=True)
    items = InvoiceCostItemSerializer(many=True, required=False)


class CostsForPeriodSerializer(serializers.Serializer):
    total_price = serializers.CharField(read_only=True)
    start_date = serializers.DateField(read_only=True)
    end_date = serializers.DateField(read_only=True)


class CustomerCreditConsumptionSerializer(serializers.Serializer):
    date = serializers.DateField(read_only=True)
    price = serializers.DecimalField(
        read_only=True, max_digits=PRICE_MAX_DIGITS, decimal_places=2
    )


class ImportUsageItemSerializer(serializers.Serializer):
    customer_name = serializers.CharField(required=False, allow_blank=True)
    customer_uuid = serializers.UUIDField(required=False)
    name = serializers.CharField(required=True)
    unit_price = serializers.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
    )
    article_code = serializers.CharField(required=False, allow_blank=True)
    service_provider_name = serializers.CharField(required=False, allow_blank=True)
    offering_name = serializers.CharField(required=False, allow_blank=True)
    plan_name = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("customer_name") and not attrs.get("customer_uuid"):
            raise serializers.ValidationError(
                _("Either customer_name or customer_uuid must be provided.")
            )
        return attrs


class ImportUsageSerializer(serializers.Serializer):
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    items = ImportUsageItemSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError(_("At least one item is required."))
        return value


class ImportUsageResponseSerializer(serializers.Serializer):
    created = serializers.IntegerField()
    skipped = serializers.IntegerField()
    errors = serializers.ListField(
        child=serializers.DictField(child=serializers.CharField())
    )
