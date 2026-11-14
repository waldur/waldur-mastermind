"""An awarding portal follows the receiving portal to a corrected project identifier.

A receiving portal that minted its projects' identifiers with the awarding
portal's name ("proj.awarding" for "proj.receiving") and then corrected them
broke every update for those awards: the awarding portal stores the identifier
twice, on the allocation and as the award record's key, and update_mapping
logged that it was changing the stored one but called a setter that refuses
any change, so every update failed at its last step and the award stayed
"Pending".
"""

import openportal
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import exceptions, models

DESTINATION = "awarding.receiving.cluster"


class AwardFixture:
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.settings = structure_factories.ServiceSettingsFactory(
            customer=self.fixture.customer
        )
        self.number = 0

    def award(self, identifier, destination=DESTINATION, with_allocation=True):
        self.number += 1
        allocation = None
        if with_allocation:
            allocation = models.RemoteAllocation.objects.create(
                name=f"award {self.number}",
                project=self.fixture.project,
                service_settings=self.settings,
                backend_id=f"award_{self.number}.awarding",
                remote_project_identifier=identifier,
            )
        return models.RemoteProject.objects.create(
            destination=destination,
            identifier=identifier,
            state=models.RemoteProjectState.PENDING,
            remote_allocation=allocation,
            current_project=structure_factories.ProjectFactory(
                customer=self.fixture.customer
            ),
        )


class UpdateMappingFollowsTheReceivingPortalTest(AwardFixture, TestCase):
    def mapping(self, allocation, identifier):
        return openportal.ProjectMapping(f"{allocation.backend_id}:{identifier}")

    def test_a_corrected_identifier_moves_the_allocation_and_the_award(self):
        award = self.award("proj.awarding")
        allocation = award.remote_allocation

        allocation.update_mapping(self.mapping(allocation, "proj.receiving"))

        allocation.refresh_from_db()
        award.refresh_from_db()
        self.assertEqual(allocation.remote_project_identifier, "proj.receiving")
        self.assertEqual(award.identifier, "proj.receiving")

    def test_an_unchanged_identifier_changes_nothing(self):
        award = self.award("proj.receiving")
        allocation = award.remote_allocation

        allocation.update_mapping(self.mapping(allocation, "proj.receiving"))

        award.refresh_from_db()
        self.assertEqual(award.identifier, "proj.receiving")

    def test_a_clash_with_another_award_changes_nothing(self):
        award = self.award("proj.awarding")
        self.award("proj.receiving", with_allocation=False)
        allocation = award.remote_allocation

        with self.assertRaises(exceptions.RemoteIdentifierConflictError):
            allocation.update_mapping(self.mapping(allocation, "proj.receiving"))

        allocation.refresh_from_db()
        award.refresh_from_db()
        self.assertEqual(allocation.remote_project_identifier, "proj.awarding")
        self.assertEqual(award.identifier, "proj.awarding")

    def test_an_identifier_naming_a_portal_off_the_route_changes_nothing(self):
        """
        The new identifier must name a portal on the award's destination: a
        peer cannot move the award to "proj.elsewhere".
        """
        award = self.award("proj.awarding")
        allocation = award.remote_allocation

        with self.assertRaises(exceptions.RemoteIdentifierConflictError):
            allocation.update_mapping(self.mapping(allocation, "proj.elsewhere"))

        allocation.refresh_from_db()
        award.refresh_from_db()
        self.assertEqual(allocation.remote_project_identifier, "proj.awarding")
        self.assertEqual(award.identifier, "proj.awarding")

    def test_the_award_moved_onto_another_awards_project_is_stranded_visibly(self):
        """
        Award Y's record still holds "bee.receiving"; award X now reports it
        too. X cannot adopt it, and says so instead of retrying for ever.
        """
        self.award("bee.receiving")
        x = self.award("ex.receiving")
        allocation = x.remote_allocation

        with self.assertRaises(exceptions.RemoteIdentifierConflictError) as cm:
            allocation.update_mapping(self.mapping(allocation, "bee.receiving"))

        self.assertIn("already has it", str(cm.exception))
        x.refresh_from_db()
        self.assertEqual(x.identifier, "ex.receiving")
