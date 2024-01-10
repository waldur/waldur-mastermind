from ddt import data, ddt
from django.urls import reverse
from rest_framework import test

from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm.tests import factories as slurm_factories


@ddt
class TestSetAllocationState(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.allocation = slurm_factories.AllocationFactory(
            project=self.fixture.project,
            state=slurm_models.Allocation.States.CREATION_SCHEDULED,
        )
        self.resource.scope = self.allocation
        self.resource.save()
        offering = self.resource.offering
        offering.type = PLUGIN_NAME
        offering.save()

        self.user = self.fixture.user
        self.url = (
            "http://testserver"
            + reverse(
                "marketplace-slurm-remote-detail",
                kwargs={"uuid": self.resource.uuid.hex},
            )
            + "set_state"
            + "/"
        )

    @data("staff", "offering_owner", "service_manager")
    def test_set_state_action_is_allowed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                "state": "ok",
            },
        )
        self.assertEqual(200, response.status_code)
        self.allocation.refresh_from_db()
        self.assertEqual(slurm_models.Allocation.States.OK, self.allocation.state)

    @data(
        "creating",
        "updating",
        "deletion_scheduled",
        "update_scheduled",
        "deleting",
        "ok",
        "erred",
    )
    def test_set_state_action_works_with_all_possible_states(self, state):
        self.client.force_login(self.fixture.staff)
        previous_state_map = {
            "creating": slurm_models.Allocation.States.CREATION_SCHEDULED,
            "updating": slurm_models.Allocation.States.UPDATE_SCHEDULED,
            "deletion_scheduled": slurm_models.Allocation.States.OK,
            "update_scheduled": slurm_models.Allocation.States.OK,
            "deleting": slurm_models.Allocation.States.DELETION_SCHEDULED,
            "ok": slurm_models.Allocation.States.CREATING,
            "erred": slurm_models.Allocation.States.OK,
        }
        self.allocation.state = previous_state_map[state]
        self.allocation.save()
        response = self.client.post(
            self.url,
            {
                "state": state,
            },
        )
        self.assertEqual(200, response.status_code)
        self.allocation.refresh_from_db()
        self.assertEqual(
            getattr(slurm_models.Allocation.States, state.upper()),
            self.allocation.state,
        )

    @data("owner", "admin", "manager", "member")
    def test_set_state_action_is_forbidden(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                "state": "ok",
            },
        )
        self.assertEqual(403, response.status_code)


@ddt
class TestSetAllocationBackendId(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.old_backend_id = "old_backend_id"
        self.allocation = slurm_factories.AllocationFactory(
            project=self.fixture.project, backend_id=self.old_backend_id
        )
        self.resource.scope = self.allocation
        self.resource.save()
        offering = self.resource.offering
        offering.type = PLUGIN_NAME
        offering.save()

        self.user = self.fixture.user
        self.url = (
            "http://testserver"
            + reverse(
                "marketplace-slurm-remote-detail",
                kwargs={"uuid": self.resource.uuid.hex},
            )
            + "set_backend_id"
            + "/"
        )
        self.new_backend_id = "new_backend_id"

    @data("staff", "offering_owner", "service_manager")
    def test_set_backend_id_action_is_allowed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                "backend_id": self.new_backend_id,
            },
        )
        self.assertEqual(200, response.status_code)
        self.allocation.refresh_from_db()
        self.assertEqual(self.new_backend_id, self.allocation.backend_id)

    @data("owner", "admin", "manager", "member")
    def test_set_backend_id_action_is_forbidden(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                "state": "ok",
            },
        )
        self.assertEqual(403, response.status_code)
        self.allocation.refresh_from_db()
        self.assertEqual(self.old_backend_id, self.allocation.backend_id)
