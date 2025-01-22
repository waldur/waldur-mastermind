from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure import models
from waldur_core.structure.tests import factories, fixtures


@ddt
class OrganizationGroupListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.organization_group_1 = factories.OrganizationGroupFactory()
        self.organization_group_2 = factories.OrganizationGroupFactory()
        self.url = factories.OrganizationGroupFactory.get_list_url()

    @data("staff", "user", None)
    def test_user_can_list_organization_groups(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    def test_list_filters(self):
        """Test of organization_groups' list filter by name and parent UUID."""
        organization_group_parent = factories.OrganizationGroupFactory()
        self.organization_group_1.parent = organization_group_parent
        self.organization_group_1.save()
        rows = [
            {
                "name": "name",
                "valid": self.organization_group_1.name[2:],
                "invalid": "AAA",
            },
            {
                "name": "name_exact",
                "valid": self.organization_group_1.name,
                "invalid": self.organization_group_1.name[2:],
            },
            {
                "name": "parent",
                "valid": organization_group_parent.uuid.hex,
                "invalid": organization_group_parent.uuid.hex[2:],
            },
        ]
        self.client.force_authenticate(user=self.fixture.staff)

        for row in rows:
            response = self.client.get(self.url, data={row["name"]: row["valid"]})
            self.assertEqual(status.HTTP_200_OK, response.status_code)
            self.assertEqual(len(response.data), 1)

            response = self.client.get(self.url, data={row["name"]: row["invalid"]})
            if row["name"] == "parent":
                self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
            else:
                self.assertEqual(status.HTTP_200_OK, response.status_code)
                self.assertEqual(len(response.data), 0)


@ddt
class OrganizationGroupChangeTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.organization_group_1 = factories.OrganizationGroupFactory()
        self.organization_group_2 = factories.OrganizationGroupFactory()
        self.fixture.customer.organization_groups.add(self.organization_group_1)
        self.url = factories.CustomerFactory.get_url(
            self.fixture.customer, action="update_organization_groups"
        )

    @data(
        "staff",
    )
    def test_staff_can_change_customer_organization_group(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        new_organization_group_url = factories.OrganizationGroupFactory.get_url(
            self.organization_group_2
        )
        response = self.client.post(
            self.url, {"organization_groups": [new_organization_group_url]}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.customer.refresh_from_db()
        self.assertIn(
            self.organization_group_2, self.fixture.customer.organization_groups.all()
        )

    @data(
        "owner",
    )
    def test_other_can_not_change_customer_organization_group(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        new_organization_group_url = factories.OrganizationGroupFactory.get_url(
            self.organization_group_2
        )
        response = self.client.post(
            self.url, {"organization_groups": [new_organization_group_url]}
        )
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class OrganizationGroupCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def create_organization_group(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrganizationGroupFactory.get_list_url()
        payload = {
            "name": "testcrud",
        }
        response = self.client.post(url, payload)
        return response

    def update_organization_group(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        organization_group = factories.OrganizationGroupFactory(name="testcrud")
        payload = {
            "name": "updated_testcrud",
        }
        response = self.client.put(
            factories.OrganizationGroupFactory.get_url(organization_group), payload
        )
        return response

    @data(
        "staff",
    )
    def test_staff_user_can_create_organization_group(self, user):
        response = self.create_organization_group(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.OrganizationGroup.objects.filter(name="testcrud").exists()
        )

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_non_staff_user_can_not_create_organization_group(self, user):
        response = self.create_organization_group(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        "staff",
    )
    def test_staff_user_can_update_organization_group(self, user):
        response = self.update_organization_group(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.OrganizationGroup.objects.filter(name="updated_testcrud").exists()
        )

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_non_staff_user_can_not_update_organization_group(self, user):
        response = self.update_organization_group(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
