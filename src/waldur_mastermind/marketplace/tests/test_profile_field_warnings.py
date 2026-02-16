from constance.test.unittest import override_config
from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OfferingUserStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories


class ProfileFieldWarningsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True,
            customer=self.fixture.customer,
            name="GPU VM - NVIDIA A100",
        )
        self.user = UserFactory(
            first_name="John",
            last_name="Doe",
            email="john@example.com",
            job_title="Engineer",
        )
        self.fixture.project.add_user(self.user, ProjectRole.MEMBER)
        self.url = (
            factories.OfferingUserFactory.get_list_url() + "profile_field_warnings/"
        )

    def _get_warnings(self, user=None):
        self.client.force_authenticate(user=user or self.user)
        return self.client.get(self.url)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_no_offering_users_returns_empty(self):
        response = self._get_warnings()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {})

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_offering_user_with_active_resource_and_exposed_field(self):
        models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="johndoe",
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_job_title=True,
        )
        factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        response = self._get_warnings()
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_title", response.data)
        self.assertEqual(len(response.data["job_title"]), 1)
        self.assertEqual(
            response.data["job_title"][0]["offering_name"], "GPU VM - NVIDIA A100"
        )
        self.assertEqual(
            response.data["job_title"][0]["offering_uuid"],
            str(self.offering.uuid),
        )

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_all_resources_terminated_returns_empty(self):
        models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="johndoe",
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_job_title=True,
        )
        factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.TERMINATED,
        )

        response = self._get_warnings()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {})

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_deleted_offering_user_is_excluded(self):
        models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="johndoe",
            state=OfferingUserStates.DELETED,
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_job_title=True,
        )
        factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        response = self._get_warnings()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {})

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=False)
    def test_setting_disabled_returns_empty(self):
        models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="johndoe",
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_job_title=True,
        )
        factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        response = self._get_warnings()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {})

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_full_name_maps_to_first_and_last_name_fields(self):
        models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="johndoe",
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_full_name=True,
            expose_email=False,
            expose_username=False,
            expose_registration_method=False,
        )
        factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        response = self._get_warnings()
        self.assertEqual(response.status_code, 200)
        self.assertIn("first_name", response.data)
        self.assertIn("last_name", response.data)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_multiple_offerings_aggregate(self):
        offering2 = factories.OfferingFactory(
            shared=True,
            customer=self.fixture.customer,
            name="Storage Service",
        )
        for offering in [self.offering, offering2]:
            models.OfferingUser.objects.create(
                offering=offering,
                user=self.user,
                username="johndoe",
            )
            models.OfferingUserAttributeConfig.objects.create(
                offering=offering,
                expose_job_title=True,
            )
            factories.ResourceFactory(
                offering=offering,
                project=self.fixture.project,
                state=ResourceStates.OK,
            )

        response = self._get_warnings()
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_title", response.data)
        self.assertEqual(len(response.data["job_title"]), 2)
        offering_names = {o["offering_name"] for o in response.data["job_title"]}
        self.assertEqual(offering_names, {"GPU VM - NVIDIA A100", "Storage Service"})
