"""What a personal access token bound to an organization or a project reaches
of the organization's credit: its allocations and the ledger of both."""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.tests import factories

# What an allocation shows of its organization's credit.
ORGANIZATION_FIELDS = {"customer_credit", "allocated_customer_credit", "offerings"}


def _bindings(*scopes):
    return [
        {
            "content_type_id": ContentType.objects.get_for_model(scope).id,
            "object_id": scope.id,
        }
        for scope in scopes
    ]


@override_config(PAT_ENABLED=True)
class CreditTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.owner = structure_factories.UserFactory(
            can_use_personal_access_tokens=True
        )
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.project_a = structure_factories.ProjectFactory(customer=self.customer)
        self.project_b = structure_factories.ProjectFactory(customer=self.customer)
        self.credit = factories.CustomerCreditFactory(
            customer=self.customer, value=1000
        )
        self.allocation_a = factories.ProjectCreditFactory(
            project=self.project_a, value=200
        )
        self.allocation_b = factories.ProjectCreditFactory(
            project=self.project_b, value=300
        )

        # The same owner holds another organization, which no binding below
        # names.
        self.other_customer = structure_factories.CustomerFactory()
        self.other_customer.add_user(self.owner, CustomerRole.OWNER)
        self.other_credit = factories.CustomerCreditFactory(
            customer=self.other_customer, value=500
        )

        # Creating credits writes ledger rows of its own; only these are
        # looked for.
        models.CreditTransaction.objects.all().delete()
        self.organization_row = self._row(credit=self.credit, comment="organization")
        self.row_a = self._row(project_credit=self.allocation_a, comment="a")
        self.row_b = self._row(project_credit=self.allocation_b, comment="b")
        self.other_row = self._row(credit=self.other_credit, comment="other")

    def _row(self, comment, **balance):
        return models.CreditTransaction.objects.create(
            amount=-10,
            transaction_type=models.CreditTransaction.Types.COMPENSATION,
            comment=comment,
            **balance,
        )

    def _authenticate(self, *bound_to, user=None, scopes=None):
        _, token = create_pat(
            user or self.owner,
            scopes=scopes or [PermissionEnum.LIST_PROJECTS.value],
            allowed_scopes=_bindings(*bound_to),
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _ledger(self):
        response = self.client.get("/api/credit-transactions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {row["comment"] for row in response.data}

    def _allocations(self):
        response = self.client.get(factories.ProjectCreditFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {row["uuid"] for row in response.data}

    def test_project_binding_reads_only_its_allocation_rows(self):
        self._authenticate(self.project_a)

        self.assertEqual(self._ledger(), {"a"})

    def test_organization_binding_reads_its_whole_ledger(self):
        self._authenticate(self.customer)

        self.assertEqual(self._ledger(), {"organization", "a", "b"})

    def test_unbound_token_reads_what_its_user_reads(self):
        self._authenticate()

        self.assertEqual(self._ledger(), {"organization", "a", "b", "other"})

    def test_ledger_row_of_a_deleted_allocation_is_reached_from_no_binding(self):
        self.allocation_b.delete()
        self._authenticate(self.customer)

        self.assertEqual(self._ledger(), {"organization", "a"})

    def test_project_binding_reads_only_its_allocation(self):
        self._authenticate(self.project_a)

        self.assertEqual(self._allocations(), {self.allocation_a.uuid.hex})

    def test_organization_binding_reads_its_allocations(self):
        self._authenticate(self.customer)

        self.assertEqual(
            self._allocations(),
            {self.allocation_a.uuid.hex, self.allocation_b.uuid.hex},
        )

    def _organization_fields_shown(self):
        response = self.client.get(
            factories.ProjectCreditFactory.get_url(self.allocation_a)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return ORGANIZATION_FIELDS & set(response.data)

    def test_project_binding_does_not_read_the_organizations_credit(self):
        self._authenticate(self.project_a)

        self.assertEqual(self._organization_fields_shown(), set())

    def test_organization_binding_reads_the_organizations_credit(self):
        self._authenticate(self.customer)

        self.assertEqual(self._organization_fields_shown(), ORGANIZATION_FIELDS)

    def test_project_binding_cannot_change_another_projects_allocation(self):
        self._authenticate(self.project_a)
        url = factories.ProjectCreditFactory.get_url(self.allocation_b)

        patched = self.client.patch(url, {"value": 1})
        deleted = self.client.delete(url)

        self.assertEqual(patched.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(deleted.status_code, status.HTTP_404_NOT_FOUND)
        self.allocation_b.refresh_from_db()
        self.assertEqual(self.allocation_b.value, 300)

    def test_project_binding_changes_its_own_allocation(self):
        self._authenticate(self.project_a)

        response = self.client.patch(
            factories.ProjectCreditFactory.get_url(self.allocation_a), {"value": 150}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.allocation_a.refresh_from_db()
        self.assertEqual(self.allocation_a.value, 150)

    def test_project_binding_cannot_create_an_allocation_for_another_project(self):
        project_c = structure_factories.ProjectFactory(customer=self.customer)
        self._authenticate(self.project_a)

        response = self.client.post(
            factories.ProjectCreditFactory.get_list_url(),
            {
                "project": structure_factories.ProjectFactory.get_url(project_c),
                "value": 10,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.ProjectCredit.objects.filter(project=project_c).exists()
        )

    def test_organization_binding_creates_an_allocation(self):
        project_c = structure_factories.ProjectFactory(customer=self.customer)
        self._authenticate(self.customer)

        response = self.client.post(
            factories.ProjectCreditFactory.get_list_url(),
            {
                "project": structure_factories.ProjectFactory.get_url(project_c),
                "value": 10,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


@override_config(PAT_ENABLED=True)
class StaffTokenCreatesAllocationTest(test.APITestCase):
    """Creating an allocation takes the owner check of updating one, whose
    staff bypass needs the token's staff scope."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        factories.CustomerCreditFactory(customer=self.customer)

    def _authenticate(self, scopes):
        _, token = create_pat(self.staff, scopes=scopes)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _create(self):
        return self.client.post(
            factories.ProjectCreditFactory.get_list_url(),
            {
                "project": structure_factories.ProjectFactory.get_url(self.project),
                "value": 10,
            },
        )

    def test_cannot_create_an_allocation(self):
        self._authenticate([PermissionEnum.LIST_PROJECTS.value])

        self.assertEqual(self._create().status_code, status.HTTP_403_FORBIDDEN)

    def test_creates_one_with_the_staff_scope(self):
        self._authenticate([PermissionEnum.STAFF_ACCESS.value])

        response = self._create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
