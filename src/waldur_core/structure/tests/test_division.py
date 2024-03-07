from ddt import data, ddt
from rest_framework import status, test

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
        """Test of organization_groups' list filter by name, type and parent UUID."""
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
                "name": "type",
                "valid": self.organization_group_1.type.name,
                "invalid": self.organization_group_1.type.name[2:],
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
        self.fixture.customer.organization_group = self.organization_group_1
        self.fixture.customer.save()
        self.url = factories.CustomerFactory.get_url(self.fixture.customer)

    @data(
        "staff",
    )
    def test_staff_can_change_customer_organization_group(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        new_organization_group_url = factories.OrganizationGroupFactory.get_url(
            self.organization_group_2
        )
        response = self.client.patch(
            self.url, {"organization_group": new_organization_group_url}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(
            self.fixture.customer.organization_group, self.organization_group_2
        )

    @data(
        "owner",
    )
    def test_other_can_not_change_customer_organization_group(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        new_organization_group_url = factories.OrganizationGroupFactory.get_url(
            self.organization_group_2
        )
        response = self.client.patch(
            self.url, {"organization_group": new_organization_group_url}
        )
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class OrganizationGroupTypeListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.type_1 = factories.OrganizationGroupTypeFactory()
        self.type_2 = factories.OrganizationGroupTypeFactory()
        self.url = factories.OrganizationGroupTypeFactory.get_list_url()

    @data("staff", "user", None)
    def test_user_can_list_organization_group_types(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    def test_list_filters(self):
        rows = [
            {"name": "name", "valid": self.type_1.name[2:], "invalid": "AAA"},
            {
                "name": "name_exact",
                "valid": self.type_1.name,
                "invalid": self.type_1.name[2:],
            },
        ]
        self.client.force_authenticate(user=self.fixture.staff)

        for row in rows:
            response = self.client.get(self.url, data={row["name"]: row["valid"]})
            self.assertEqual(status.HTTP_200_OK, response.status_code)
            self.assertEqual(len(response.data), 1)

            response = self.client.get(self.url, data={row["name"]: row["invalid"]})
            self.assertEqual(status.HTTP_200_OK, response.status_code)
            self.assertEqual(len(response.data), 0)
