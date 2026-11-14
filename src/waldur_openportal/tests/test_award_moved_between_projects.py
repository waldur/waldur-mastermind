"""An award's local_identifier follows the project the award is attached to."""

from unittest import mock

from django.test import TestCase
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import models

PORTAL = "testportal"


class LocalIdentifierFollowsTheProjectTest(TestCase):
    """
    ManagedProject.local_identifier names the project the award is attached
    to. It was minted once, for the first project, and kept naming that one
    after the award moved - even once the first project had been deleted.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.first = self.fixture.project
        self.second = structure_factories.ProjectFactory(customer=self.fixture.customer)
        models.ProjectInfo.objects.create(project=self.first, shortname="first")
        models.ProjectInfo.objects.create(project=self.second, shortname="second")
        self.award = models.ManagedProject.objects.create(
            destination="awarding.receiving.cluster",
            identifier="award.awarding",
            local_identifier=f"first.{PORTAL}",
            project=self.first,
        )
        # set_project also rewrites the award's project link in its details,
        # which needs a real AwardDetails; that is not what is under test.
        for name, value in (
            ("get_details", mock.Mock()),
            ("set_details", None),
            ("_get_project_link", None),
        ):
            patcher = mock.patch.object(models.ManagedProject, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _openportal(self, loaded=True):
        return (
            mock.patch("openportal.get_portal", return_value=PORTAL),
            mock.patch(
                "waldur_openportal.config.ensure_config_loaded", return_value=loaded
            ),
        )

    def test_moving_the_award_renames_it_after_the_new_project(self):
        get_portal, loaded = self._openportal()
        with get_portal, loaded:
            self.award.set_project(None)
            self.award.set_project(self.second)

        self.award.refresh_from_db()
        self.assertEqual(self.award.local_identifier, f"second.{PORTAL}")

    def test_moving_straight_from_one_project_to_another(self):
        get_portal, loaded = self._openportal()
        with get_portal, loaded:
            self.award.set_project(self.second)

        self.award.refresh_from_db()
        self.assertEqual(self.award.local_identifier, f"second.{PORTAL}")

    def test_detaching_keeps_the_last_identifier(self):
        get_portal, loaded = self._openportal()
        with get_portal, loaded:
            self.award.set_project(None)

        self.award.refresh_from_db()
        self.assertEqual(self.award.local_identifier, f"first.{PORTAL}")

    def test_moving_to_a_project_with_no_shortname_clears_it(self):
        """
        Left alone it would name the project the award has left; cleared,
        the board mints the new project's when it next needs one.
        """
        no_shortname = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )

        get_portal, loaded = self._openportal()
        with get_portal, loaded:
            self.award.set_project(no_shortname)

        self.award.refresh_from_db()
        self.assertIsNone(self.award.local_identifier)

    def test_same_project_without_the_config_keeps_its_identifier(self):
        get_portal, loaded = self._openportal(loaded=False)
        with get_portal, loaded:
            self.award.set_project(self.first)

        self.award.refresh_from_db()
        self.assertEqual(self.award.local_identifier, f"first.{PORTAL}")

    def test_reattaching_the_same_project_corrects_a_wrong_portal(self):
        """
        An identifier minted with another portal's name is corrected when the
        award is next attached, even to the same project.
        """
        self.award.local_identifier = "first.awarding"
        self.award.save(update_fields=["local_identifier"])

        get_portal, loaded = self._openportal()
        with get_portal, loaded:
            self.award.set_project(self.first)

        self.award.refresh_from_db()
        self.assertEqual(self.award.local_identifier, f"first.{PORTAL}")


class AwardMovedOntoAnotherAwardsProjectTest(test.APITestCase):
    """
    An awarding portal keys each award by (destination, project identifier),
    so two awards over one destination cannot both name the same project.
    Award Y detached from project B keeps naming it - the awarding portal's
    record for Y still holds that identifier - so attaching award X to B is
    refused instead of handing X an identifier the awarding portal can never
    adopt (which would leave X "pending" there for ever).
    """

    DESTINATION = "awarding.receiving.cluster"

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.a = self.fixture.project
        self.b = structure_factories.ProjectFactory(customer=self.fixture.customer)
        models.ProjectInfo.objects.create(project=self.a, shortname="ay")
        models.ProjectInfo.objects.create(project=self.b, shortname="bee")
        template = models.ProjectTemplate.objects.create(
            name="template", portal="receiving"
        )
        self.y = models.ManagedProject.objects.create(
            destination=self.DESTINATION,
            identifier="y.awarding",
            local_identifier=f"bee.{PORTAL}",
            project=self.b,
            project_template=template,
            details={},
        )
        self.x = models.ManagedProject.objects.create(
            destination=self.DESTINATION,
            identifier="x.awarding",
            local_identifier=f"ay.{PORTAL}",
            project=self.a,
            project_template=template,
            details={},
        )
        for patcher in (
            mock.patch("openportal.get_portal", return_value=PORTAL),
            mock.patch(
                "waldur_openportal.config.ensure_config_loaded", return_value=True
            ),
            mock.patch.object(models.ManagedProject, "notify_changed"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_authenticate(self.fixture.staff)

    def post(self, award, action, data=None):
        url = (
            f"/api/openportal-managed-projects/{award.identifier}/"
            f"{award.destination}/{action}/"
        )
        return self.client.post(url, data or {}, format="json")

    def test_attaching_a_project_another_award_still_names_is_refused(self):
        self.assertEqual(self.post(self.y, "detach").status_code, 200)

        response = self.post(self.x, "attach", {"project_uuid": self.b.uuid.hex})

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("y.awarding", response.data["error"])
        self.x.refresh_from_db()
        self.assertEqual(self.x.project, self.a)
        self.assertEqual(self.x.local_identifier, f"ay.{PORTAL}")

    def test_attaching_is_allowed_once_the_other_award_is_gone(self):
        self.y.delete()

        response = self.post(self.x, "attach", {"project_uuid": self.b.uuid.hex})

        self.assertEqual(response.status_code, 200, response.data)
        self.x.refresh_from_db()
        self.assertEqual(self.x.project, self.b)
        self.assertEqual(self.x.local_identifier, f"bee.{PORTAL}")

    def test_an_award_over_another_destination_does_not_block(self):
        self.y.project = None
        self.y.destination = "other.receiving.cluster"
        self.y.save()

        response = self.post(self.x, "attach", {"project_uuid": self.b.uuid.hex})

        self.assertEqual(response.status_code, 200, response.data)
        self.x.refresh_from_db()
        self.assertEqual(self.x.local_identifier, f"bee.{PORTAL}")
