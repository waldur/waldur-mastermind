import datetime

from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import maintenance_reservations, models
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    ImpactLevel,
    MaintenanceState,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


def enable(offering, enabled=True):
    offering.plugin_options = {
        **(offering.plugin_options or {}),
        maintenance_reservations.OPTION: enabled,
    }
    offering.save(update_fields=["plugin_options"])


class DesiredReservationsTest(test.APITestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(type=SITE_AGENT_OFFERING, shared=True)
        enable(self.offering)
        self.start = timezone.now() + datetime.timedelta(days=3)

    def announce(self, impact, state=MaintenanceState.SCHEDULED, partitions=()):
        maintenance = factories.MaintenanceAnnouncementFactory(
            state=state,
            scheduled_start=self.start,
            scheduled_end=self.start + datetime.timedelta(hours=12),
            service_provider=factories.ServiceProviderFactory(
                customer=structure_factories.CustomerFactory()
            ),
        )
        factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=maintenance,
            offering=self.offering,
            impact_level=impact,
            affected_partitions=list(partitions),
        )
        return maintenance

    def test_full_outage_reserves_all_nodes(self):
        maintenance = self.announce(ImpactLevel.FULL_OUTAGE)
        [reservation] = maintenance_reservations.desired_reservations(self.offering)
        self.assertEqual(
            reservation["name"], f"waldur-maint-{maintenance.uuid.hex[:12]}"
        )
        self.assertEqual(reservation["partitions"], [])
        self.assertEqual(reservation["flags"], ["MAINT"])
        self.assertEqual(reservation["start"], self.start)

    def test_partial_outage_needs_named_partitions(self):
        self.announce(ImpactLevel.PARTIAL_OUTAGE)
        self.assertEqual(
            maintenance_reservations.desired_reservations(self.offering), []
        )
        self.announce(ImpactLevel.PARTIAL_OUTAGE, partitions=["gpu"])
        [reservation] = maintenance_reservations.desired_reservations(self.offering)
        self.assertEqual(reservation["partitions"], ["gpu"])

    def test_notices_drafts_and_finished_maintenance_are_not_reserved(self):
        self.announce(ImpactLevel.DEGRADED_PERFORMANCE)
        self.announce(ImpactLevel.NO_IMPACT)
        self.announce(ImpactLevel.FULL_OUTAGE, state=MaintenanceState.DRAFT)
        self.announce(ImpactLevel.FULL_OUTAGE, state=MaintenanceState.COMPLETED)
        self.announce(ImpactLevel.FULL_OUTAGE, state=MaintenanceState.CANCELLED)
        self.assertEqual(
            maintenance_reservations.desired_reservations(self.offering), []
        )

    def test_nothing_unless_opted_in(self):
        self.announce(ImpactLevel.FULL_OUTAGE)
        enable(self.offering, False)
        self.assertEqual(
            maintenance_reservations.desired_reservations(self.offering), []
        )


class MaintenanceReservationEndpointsTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.shared = True
        self.offering.save()
        enable(self.offering)
        self.fixture.maintenance_announcement.delete()
        # What the site agent's role holds in permissions.yaml.
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)

    def url(self, action):
        return reverse(
            f"marketplace-provider-offering-{action}",
            kwargs={"uuid": self.offering.uuid.hex},
        )

    def test_site_agent_reads_desired_reservations(self):
        maintenance = factories.MaintenanceAnnouncementFactory(
            state=MaintenanceState.SCHEDULED,
            scheduled_start=timezone.now() + datetime.timedelta(days=1),
            scheduled_end=timezone.now() + datetime.timedelta(days=1, hours=4),
            service_provider=self.fixture.service_provider,
        )
        factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=maintenance,
            offering=self.offering,
            impact_level=ImpactLevel.FULL_OUTAGE,
        )
        # The site agent's identity is an offering manager.
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(self.url("maintenance-reservations"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["enabled"])
        self.assertEqual(response.data["prefix"], "waldur-maint-")
        self.assertEqual(
            response.data["reservations"][0]["maintenance_uuid"], maintenance.uuid.hex
        )

    def test_consumer_cannot_read_reservations(self):
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url("maintenance-reservations"))
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def site_reservation(self, name="maint-oct", **extra):
        start = timezone.now() + datetime.timedelta(days=2)
        return {
            "name": name,
            "start": start.isoformat(),
            "end": (start + datetime.timedelta(hours=6)).isoformat(),
            "flags": ["MAINT"],
            **extra,
        }

    def test_site_reservations_become_drafts_once(self):
        self.client.force_authenticate(self.fixture.offering_manager)
        payload = {
            "reservations": [
                self.site_reservation(),
                self.site_reservation("gpu-maint", partitions=["gpu"]),
                self.site_reservation("course", flags=[]),
                self.site_reservation("waldur-maint-abc"),
            ]
        }
        response = self.client.post(
            self.url("import-maintenance-reservations"), payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["created"], ["maint-oct", "gpu-maint"])
        self.assertEqual(response.data["skipped"], ["course", "waldur-maint-abc"])
        draft = models.MaintenanceAnnouncement.objects.get(
            backend_id=maintenance_reservations.import_backend_id(
                self.offering, "gpu-maint"
            )
        )
        self.assertEqual(draft.state, MaintenanceState.DRAFT)
        row = draft.affected_offerings.get()
        self.assertEqual(row.impact_level, ImpactLevel.PARTIAL_OUTAGE)
        self.assertEqual(row.affected_partitions, ["gpu"])

        response = self.client.post(
            self.url("import-maintenance-reservations"), payload, format="json"
        )
        self.assertEqual(response.data["created"], [])
        self.assertEqual(response.data["unchanged"], ["maint-oct", "gpu-maint"])

    def test_published_import_is_not_overwritten(self):
        self.client.force_authenticate(self.fixture.offering_manager)
        url = self.url("import-maintenance-reservations")
        self.client.post(
            url, {"reservations": [self.site_reservation()]}, format="json"
        )
        draft = models.MaintenanceAnnouncement.objects.get(
            backend_id__endswith=":maint-oct"
        )
        draft.schedule()
        draft.save()
        moved = self.site_reservation()
        moved["end"] = (timezone.now() + datetime.timedelta(days=9)).isoformat()
        response = self.client.post(url, {"reservations": [moved]}, format="json")
        self.assertEqual(response.data["unchanged"], ["maint-oct"])

    def test_import_requires_opt_in(self):
        enable(self.offering, False)
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.post(
            self.url("import-maintenance-reservations"),
            {"reservations": [self.site_reservation()]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_option_is_declared_on_plugin_options(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url("update-integration"),
            {"plugin_options": {maintenance_reservations.OPTION: True}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertTrue(self.offering.plugin_options[maintenance_reservations.OPTION])

    def test_announcements_filter_by_offering(self):
        other = factories.OfferingFactory(customer=self.offering.customer, shared=True)
        for offering in (self.offering, other):
            factories.MaintenanceAnnouncementOfferingFactory(
                offering=offering,
                maintenance=factories.MaintenanceAnnouncementFactory(
                    service_provider=self.fixture.service_provider
                ),
            )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.MaintenanceAnnouncementFactory.get_list_url(),
            {"offering_uuid": self.offering.uuid.hex},
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["affected_offerings"][0]["offering_name"],
            self.offering.name,
        )

    def test_reservation_on_every_partition_is_a_full_outage(self):
        for name in ("batch", "gpu"):
            factories.OfferingPartitionFactory(
                offering=self.offering, partition_name=name
            )
        self.client.force_authenticate(self.fixture.offering_manager)
        self.client.post(
            self.url("import-maintenance-reservations"),
            {"reservations": [self.site_reservation(partitions=["batch", "gpu"])]},
            format="json",
        )
        row = models.MaintenanceAnnouncementOffering.objects.get(
            maintenance__backend_id__endswith=":maint-oct"
        )
        self.assertEqual(row.impact_level, ImpactLevel.FULL_OUTAGE)
        self.assertEqual(row.affected_partitions, [])
