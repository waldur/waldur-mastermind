"""Test fixtures for Arrow integration tests."""

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.waldur_arrow import models


class ArrowFixture:
    """Fixture class for Arrow-related tests."""

    def __init__(self):
        self.customer = structure_factories.CustomerFactory()
        self.user = structure_factories.UserFactory(is_staff=True)

    @property
    def arrow_settings(self):
        if not hasattr(self, "_arrow_settings"):
            self._arrow_settings = models.ArrowSettings.objects.create(
                api_url="https://api.arrow.test/",
                api_key="test-api-key-12345",
                export_type_reference="TEST-EXPORT-TYPE",
                classification_filter="IAAS",
                is_active=True,
                sync_enabled=True,
                partner_reference="XSP12345",
                partner_name="Test Partner",
            )
        return self._arrow_settings

    @property
    def customer_mapping(self):
        if not hasattr(self, "_customer_mapping"):
            self._customer_mapping = models.ArrowCustomerMapping.objects.create(
                settings=self.arrow_settings,
                arrow_reference="XSP67890",
                arrow_company_name="Test Arrow Company",
                waldur_customer=self.customer,
                is_active=True,
            )
        return self._customer_mapping

    @property
    def billing_sync(self):
        if not hasattr(self, "_billing_sync"):
            from waldur_mastermind.invoices import models as invoice_models

            invoice, _ = invoice_models.Invoice.objects.get_or_create(
                customer=self.customer,
                year=2024,
                month=1,
            )
            self._billing_sync = models.ArrowBillingSync.objects.create(
                customer_mapping=self.customer_mapping,
                statement_reference="STMT-2024-01-001",
                report_period="2024-01",
                arrow_state="pending",
                invoice=invoice,
            )
        return self._billing_sync
