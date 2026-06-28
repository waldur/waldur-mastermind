from rest_framework import test

from waldur_core.structure.tests import factories, fixtures


class CurrentUserHasRoleFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        # Materialize role assignments.
        self.manager = self.fixture.manager
        self.member = self.fixture.member
        self.owner = self.fixture.owner

    def project_names(self, user, role):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProjectFactory.get_list_url(),
            {"current_user_has_role": role},
        )
        return {project["name"] for project in response.data}

    def customer_names(self, user, role):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.CustomerFactory.get_list_url(),
            {"current_user_has_role": role},
        )
        return {customer["name"] for customer in response.data}

    def test_project_role_matches_project(self):
        self.assertIn(
            self.project.name, self.project_names(self.manager, "PROJECT.MANAGER")
        )

    def test_lesser_role_is_excluded(self):
        self.assertNotIn(
            self.project.name, self.project_names(self.member, "PROJECT.MANAGER")
        )

    def test_user_matches_own_role(self):
        self.assertIn(
            self.project.name, self.project_names(self.member, "PROJECT.MEMBER")
        )

    def test_customer_scope_role_matches_project(self):
        # A customer-scoped role grants access to the customer's projects.
        self.assertIn(
            self.project.name, self.project_names(self.owner, "CUSTOMER.OWNER")
        )

    def test_csv_role_list(self):
        self.assertIn(
            self.project.name,
            self.project_names(self.manager, "PROJECT.MANAGER,PROJECT.ADMIN"),
        )

    def test_customer_filter_matches_via_project_role(self):
        # A project-scoped role makes the project's organization selectable.
        self.assertIn(
            self.customer.name, self.customer_names(self.manager, "PROJECT.MANAGER")
        )

    def test_customer_filter_excludes_lesser_role(self):
        self.assertNotIn(
            self.customer.name, self.customer_names(self.member, "PROJECT.MANAGER")
        )
