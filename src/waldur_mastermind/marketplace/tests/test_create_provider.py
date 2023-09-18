from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace.models import ServiceProvider


class CreateProviderCommandTest(TestCase):
    def test_create_provider_command_success(self):
        customer_name = "Test Customer"
        categories = ["vm", "hpc", "storage"]

        out_create_provider = StringIO()

        call_command(
            "create_provider",
            "-n",
            customer_name,
            "-c",
            *categories,
            stdout=out_create_provider,
        )

        customer = Customer.objects.get(name=customer_name)
        service_provider = ServiceProvider.objects.get(customer=customer)

        expected_messages = [
            "Loaded category vm",
            "Loaded category hpc",
            "Loaded category storage",
            "Service Provider created",
        ]

        output_text = out_create_provider.getvalue()

        for message in expected_messages:
            self.assertIn(message, output_text)

        self.assertEqual(customer.name, customer_name)
        self.assertEqual(service_provider.customer.name, customer_name)

    def test_create_provider_command_fail_with_customer_already_exists(self):
        customer_name = "Customer Test"
        categories = ["vm", "hpc", "storage"]

        Customer.objects.create(name=customer_name)

        out_create_provider = StringIO()

        call_command(
            "create_provider",
            "-n",
            customer_name,
            "-c",
            *categories,
            stdout=out_create_provider,
        )

        expected_message = f"Customer {customer_name} already exists"
        output_text = out_create_provider.getvalue()

        self.assertIn(expected_message, output_text)
