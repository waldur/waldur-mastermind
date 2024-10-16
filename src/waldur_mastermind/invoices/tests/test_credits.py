import datetime

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models, tasks
from waldur_mastermind.invoices.tests import factories, fixtures


@ddt
class CustomerCreditRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.url = factories.CustomerCreditFactory.get_url(self.fixture.customer_credit)

    @data("staff", "global_support", "owner")
    def test_user_with_access_can_retrieve_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_retrieve_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class CustomerCreditCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def create_credit(self, user):
        payload = {
            "customer": structure_factories.CustomerFactory.get_url(
                self.fixture.customer
            ),
            "value": 1000,
        }
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerCreditFactory.get_list_url()
        return self.client.post(url, payload)

    @data("staff")
    def test_user_with_access_can_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="create_of_credit_by_staff"
            ).exists()
        )

    @data("global_support", "owner", "manager", "admin", "user")
    def test_user_cannot_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class CustomerCreditUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.fixture.customer_credit

    def update_credit(self, user):
        payload = {"value": 500}
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerCreditFactory.get_url(self.fixture.customer_credit)
        return self.client.patch(url, payload)

    @data("staff")
    def test_user_with_access_can_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="update_of_credit_by_staff"
            ).exists()
        )

    @data("global_support", "owner", "manager", "admin", "user")
    def test_user_cannot_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class CustomerCreditDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()

    def delete_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerCreditFactory.get_url(self.fixture.customer_credit)
        return self.client.delete(url)

    @data("staff")
    def test_user_with_access_can_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("global_support", "owner", "manager", "admin", "user")
    def test_user_cannot_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProjectCreditRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.url = factories.ProjectCreditFactory.get_url(self.fixture.project_credit)

    @data("staff", "global_support", "owner")
    def test_user_with_access_can_retrieve_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_retrieve_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ProjectCreditCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.fixture.customer_credit

    def create_credit(self, user):
        payload = {
            "project": structure_factories.ProjectFactory.get_url(self.fixture.project),
            "value": 10,
        }
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectCreditFactory.get_list_url()
        return self.client.post(url, payload)

    @data("staff", "owner")
    def test_user_with_access_can_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("global_support", "manager", "admin", "user")
    def test_user_cannot_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProjectCreditUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()

    def update_credit(self, user):
        payload = {"value": 7}
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectCreditFactory.get_url(self.fixture.project_credit)
        return self.client.patch(url, payload)

    @data("staff", "owner")
    def test_user_with_access_can_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "global_support",
    )
    def test_global_support_user_cannot_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProjectCreditDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()

    def delete_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectCreditFactory.get_url(self.fixture.project_credit)
        return self.client.delete(url)

    @data("staff", "owner")
    def test_user_with_access_can_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("manager", "admin", "user")
    def test_user_cannot_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "global_support",
    )
    def test_global_support_user_cannot_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CreditTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.invoice_item = self.fixture.invoice_item

    def test_compensate_cost(self):
        credit_value = self.invoice.total // 2
        credit = factories.CustomerCreditFactory(
            customer=self.invoice.customer, value=credit_value
        )
        old_total = self.invoice.total
        self.invoice.set_created()
        self.assertTrue(models.InvoiceItem.objects.filter(credit=credit).exists())
        credit_item = models.InvoiceItem.objects.filter(credit=credit).get()
        self.assertEqual(credit_value * -1, credit_item.total)
        self.assertEqual(self.invoice.total, old_total - credit.value)
        credit.refresh_from_db()
        self.assertEqual(credit.value, 0)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="reduction_of_credit"
            ).exists()
        )

    def test_compensate_cost_if_credit_greater_than_item_cost(self):
        credit_value = self.invoice.total * 2
        credit = factories.CustomerCreditFactory(
            customer=self.invoice.customer, value=credit_value
        )
        old_total = self.invoice.total
        self.invoice.set_created()
        self.assertTrue(models.InvoiceItem.objects.filter(credit=credit).exists())
        credit_item = models.InvoiceItem.objects.filter(credit=credit).get()
        self.assertEqual(old_total * -1, credit_item.total)
        self.assertEqual(self.invoice.total, 0)
        credit.refresh_from_db()
        self.assertEqual(credit.value, credit_value - old_total)

    def test_minimal_consumption(self):
        old_total = self.invoice.total
        credit_value = self.invoice.total * 3
        minimal_consumption = self.invoice.total * 2
        credit = factories.CustomerCreditFactory(
            customer=self.invoice.customer,
            value=credit_value,
            minimal_consumption=minimal_consumption,
        )
        self.invoice.set_created()
        self.assertTrue(models.InvoiceItem.objects.filter(credit=credit).exists())
        self.assertEqual(old_total * -1, old_total - minimal_consumption)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="reduction_of_credit_due_to_minimal_consumption"
            ).exists()
        )

    def test_task_set_to_zero_overdue_credits(self):
        credit_1 = factories.CustomerCreditFactory()
        credit_2 = factories.CustomerCreditFactory(
            end_date=datetime.date.today() + datetime.timedelta(days=5)
        )
        credit_3 = factories.CustomerCreditFactory(
            end_date=datetime.date.today() - datetime.timedelta(days=5)
        )
        tasks.set_to_zero_overdue_credits()
        credit_1.refresh_from_db()
        credit_2.refresh_from_db()
        credit_3.refresh_from_db()
        self.assertTrue(credit_1.value)
        self.assertTrue(credit_2.value)
        self.assertFalse(credit_3.value)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="set_to_zero_overdue_credit"
            ).exists()
        )


class ProjectCreditTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit
        self.project_credit = self.fixture.project_credit
        self.invoice = self.fixture.invoice
        self.invoice_item = self.fixture.invoice_item

    def test_project_credits_reduced(self):
        old_project_credit_value = self.project_credit.value
        self.invoice.set_created()
        self.project_credit.refresh_from_db()
        self.assertTrue(self.project_credit.value < old_project_credit_value)

    def test_use_organisation_credit_enabled(self):
        self.project_credit.use_organisation_credit = False
        self.project_credit.save()
        old_customer_credit_value = self.customer_credit.value
        self.invoice.set_created()
        self.customer_credit.refresh_from_db()
        self.assertEqual(
            self.customer_credit.value,
            old_customer_credit_value - self.project_credit.value,
        )

    def test_use_organisation_credit_disable(self):
        self.project_credit.use_organisation_credit = True
        self.project_credit.save()
        self.invoice.set_created()
        self.customer_credit.refresh_from_db()
        self.assertEqual(self.customer_credit.value, 0)
