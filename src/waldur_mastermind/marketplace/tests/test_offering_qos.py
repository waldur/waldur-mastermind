"""Tests for OfferingQoS / PartitionQoS models, API actions and backfill."""

from django.apps import apps as live_apps
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.migrations._backfill_offering_qos import backfill_qos
from waldur_mastermind.marketplace.serializers import AgentPluginOptionsSerializer
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class EnforceQoSPluginOptionTest(test.APITestCase):
    """The per-offering enforce_qos flag lives in plugin_options."""

    def test_defaults_to_false_informational(self):
        serializer = AgentPluginOptionsSerializer(data={})
        serializer.is_valid(raise_exception=True)
        self.assertFalse(serializer.validated_data["enforce_qos"])

    def test_accepts_true(self):
        serializer = AgentPluginOptionsSerializer(data={"enforce_qos": True})
        serializer.is_valid(raise_exception=True)
        self.assertTrue(serializer.validated_data["enforce_qos"])


class OfferingQoSModelTest(test.APITestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory()
        self.qos = factories.SlurmOfferingQoSFactory(
            offering=self.offering, name="boost"
        )

    def test_qos_has_uuid(self):
        self.assertEqual(len(str(self.qos.uuid)), 32)

    def test_qos_belongs_to_offering(self):
        self.assertIn(self.qos, self.offering.qos_profiles.all())

    def test_str(self):
        self.assertEqual(str(self.qos), f"{self.offering.name} - boost")

    def test_name_unique_per_offering(self):
        with self.assertRaises(Exception):
            factories.SlurmOfferingQoSFactory(offering=self.offering, name="boost")

    def test_different_offerings_share_qos_name(self):
        other = factories.OfferingFactory()
        factories.SlurmOfferingQoSFactory(offering=other, name="boost")
        self.assertEqual(
            models.SlurmOfferingQoS.objects.filter(name="boost").count(), 2
        )

    def test_all_numeric_name_rejected(self):
        qos = models.SlurmOfferingQoS(offering=self.offering, name="12345")
        with self.assertRaises(ValidationError):
            qos.full_clean()

    def test_leading_digit_name_allowed(self):
        qos = models.SlurmOfferingQoS(offering=self.offering, name="2go")
        qos.full_clean()  # should not raise


class PartitionQoSModelTest(test.APITestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory()
        self.partition = factories.OfferingPartitionFactory(offering=self.offering)
        self.qos = factories.SlurmOfferingQoSFactory(offering=self.offering)

    def test_single_default_per_partition(self):
        factories.SlurmPartitionQoSFactory(
            partition=self.partition, qos=self.qos, is_default=True
        )
        other_qos = factories.SlurmOfferingQoSFactory(offering=self.offering)
        with self.assertRaises(IntegrityError), transaction.atomic():
            factories.SlurmPartitionQoSFactory(
                partition=self.partition, qos=other_qos, is_default=True
            )

    def test_qos_unique_per_partition(self):
        factories.SlurmPartitionQoSFactory(partition=self.partition, qos=self.qos)
        with self.assertRaises(IntegrityError), transaction.atomic():
            factories.SlurmPartitionQoSFactory(partition=self.partition, qos=self.qos)

    @staticmethod
    def _offering_check_trigger_exists():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname = %s",
                ["marketplace_slurm_partition_qos_offering_check"],
            )
            return cursor.fetchone() is not None

    def test_cross_offering_link_rejected_at_db(self):
        # A QoS from another offering must not be linkable to this partition,
        # even bypassing the serializer — the DB trigger rejects it. The trigger
        # lives in a migration (RunSQL), so it is absent when the schema is
        # built from models directly (CI's ``pytest --no-migrations``); skip
        # there since there is nothing to exercise. It is present in real
        # migration-based environments (production, local --create-db).
        if not self._offering_check_trigger_exists():
            self.skipTest(
                "offering-check trigger absent (schema built without migrations)"
            )
        foreign_qos = factories.SlurmOfferingQoSFactory()
        with self.assertRaises(DatabaseError), transaction.atomic():
            factories.SlurmPartitionQoSFactory(
                partition=self.partition, qos=foreign_qos
            )

    def test_clean_rejects_cross_offering_link(self):
        foreign_qos = factories.SlurmOfferingQoSFactory()
        link = models.SlurmPartitionQoS(partition=self.partition, qos=foreign_qos)
        with self.assertRaises(ValidationError):
            link.clean()


class OfferingQoSApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

    def _url(self, action):
        return factories.OfferingFactory.get_url(self.offering, action)

    def test_owner_can_add_qos(self):
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(
            self._url("add_qos"), {"name": "boost", "max_nodes": 256}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        qos = models.SlurmOfferingQoS.objects.get(offering=self.offering, name="boost")
        self.assertEqual(qos.max_nodes, 256)

    def test_add_qos_rejects_all_numeric_name(self):
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(self._url("add_qos"), {"name": "123"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_owner_cannot_add_qos(self):
        self.client.force_login(self.fixture.offering_admin)
        response = self.client.post(self._url("add_qos"), {"name": "boost"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_update_qos(self):
        qos = factories.SlurmOfferingQoSFactory(offering=self.offering, name="boost")
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.patch(
            self._url("update_qos"),
            {"qos_uuid": qos.uuid.hex, "max_time": 999},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        qos.refresh_from_db()
        self.assertEqual(qos.max_time, 999)

    def test_owner_can_remove_qos(self):
        qos = factories.SlurmOfferingQoSFactory(offering=self.offering)
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(self._url("remove_qos"), {"qos_uuid": qos.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.SlurmOfferingQoS.objects.filter(pk=qos.pk).exists())

    def test_offering_payload_exposes_qos_profiles(self):
        factories.SlurmOfferingQoSFactory(offering=self.offering, name="boost")
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(factories.OfferingFactory.get_url(self.offering))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [q["name"] for q in response.data["qos_profiles"]]
        self.assertIn("boost", names)


class SetPartitionQoSApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.partition = factories.OfferingPartitionFactory(offering=self.offering)
        self.qos1 = factories.SlurmOfferingQoSFactory(
            offering=self.offering, name="normal"
        )
        self.qos2 = factories.SlurmOfferingQoSFactory(
            offering=self.offering, name="boost"
        )
        self.client.force_login(self.fixture.offering_owner)

    def _payload(self, options):
        return {"partition_uuid": self.partition.uuid.hex, "qos_options": options}

    def test_set_allow_list_with_default(self):
        response = self.client.post(
            factories.OfferingFactory.get_url(self.offering, "set_partition_qos"),
            self._payload(
                [
                    {"qos_uuid": self.qos1.uuid.hex, "is_default": True},
                    {"qos_uuid": self.qos2.uuid.hex},
                ]
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.partition.qos_options.count(), 2)
        self.assertEqual(self.partition.qos_options.get(is_default=True).qos, self.qos1)

    def test_two_defaults_rejected(self):
        response = self.client.post(
            factories.OfferingFactory.get_url(self.offering, "set_partition_qos"),
            self._payload(
                [
                    {"qos_uuid": self.qos1.uuid.hex, "is_default": True},
                    {"qos_uuid": self.qos2.uuid.hex, "is_default": True},
                ]
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_qos_from_other_offering_rejected(self):
        foreign = factories.SlurmOfferingQoSFactory()
        response = self.client.post(
            factories.OfferingFactory.get_url(self.offering, "set_partition_qos"),
            self._payload([{"qos_uuid": foreign.uuid.hex}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_set_replaces_existing(self):
        factories.SlurmPartitionQoSFactory(partition=self.partition, qos=self.qos1)
        response = self.client.post(
            factories.OfferingFactory.get_url(self.offering, "set_partition_qos"),
            self._payload([{"qos_uuid": self.qos2.uuid.hex, "is_default": True}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            list(self.partition.qos_options.values_list("qos__name", flat=True)),
            ["boost"],
        )


class BackfillOfferingQoSTest(test.APITestCase):
    def test_backfill_creates_qos_and_default_link(self):
        offering = factories.OfferingFactory()
        partition = factories.OfferingPartitionFactory(offering=offering, qos="normal")
        backfill_qos(live_apps, None)
        qos = models.SlurmOfferingQoS.objects.get(offering=offering, name="normal")
        link = models.SlurmPartitionQoS.objects.get(partition=partition, qos=qos)
        self.assertTrue(link.is_default)

    def test_backfill_skips_empty_qos(self):
        offering = factories.OfferingFactory()
        factories.OfferingPartitionFactory(offering=offering, qos="")
        backfill_qos(live_apps, None)
        self.assertEqual(
            models.SlurmOfferingQoS.objects.filter(offering=offering).count(), 0
        )

    def test_backfill_is_idempotent(self):
        offering = factories.OfferingFactory()
        factories.OfferingPartitionFactory(offering=offering, qos="normal")
        backfill_qos(live_apps, None)
        backfill_qos(live_apps, None)
        self.assertEqual(
            models.SlurmOfferingQoS.objects.filter(offering=offering).count(), 1
        )


class OfferingQoSQueryCountTest(test.APITestCase):
    """The nested qos_profiles / partitions.qos_options must not cause N+1.

    Serializing the offering detail nests, per partition, a qos_options ->
    SlurmOfferingQoS chain. Without prefetching this is O(partitions * qos).
    The provider-offering viewset prefetches those relations on detail, so the
    query count must be invariant to the number of partitions/QoS.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()

    def _build_offering(self, num_partitions):
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        for _ in range(num_partitions):
            partition = factories.OfferingPartitionFactory(offering=offering)
            q1 = factories.SlurmOfferingQoSFactory(offering=offering)
            q2 = factories.SlurmOfferingQoSFactory(offering=offering)
            factories.SlurmPartitionQoSFactory(
                partition=partition, qos=q1, is_default=True
            )
            factories.SlurmPartitionQoSFactory(partition=partition, qos=q2)
        return offering

    def _count_retrieve_queries(self, offering):
        self.client.force_login(self.fixture.staff)
        url = factories.OfferingFactory.get_url(offering)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # sanity: the nesting was actually serialized
        self.assertIn("qos_profiles", response.data)
        self.assertIn("partitions", response.data)
        return len(ctx.captured_queries)

    def test_qos_relations_do_not_cause_n_plus_1(self):
        small_offering = self._build_offering(1)
        large_offering = self._build_offering(4)
        # Warm process-global caches (content types, permission roles) so the
        # first measured request doesn't over-count and mask the comparison.
        self._count_retrieve_queries(small_offering)
        small = self._count_retrieve_queries(small_offering)
        large = self._count_retrieve_queries(large_offering)
        self.assertEqual(
            small,
            large,
            f"query count changed {small} -> {large} with more partitions/QoS (N+1)",
        )
