from io import StringIO

from django.core.management import call_command

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.tests.fixtures import OpenStackFixture

from .utils import BaseOpenStackTest


class DedupeTenantOfferingsTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant

    def _offering(self, type=OPENSTACK_INSTANCE_OFFERING, scope=None, **kwargs):
        return marketplace_factories.OfferingFactory(
            type=type,
            scope=scope or self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=kwargs.pop("state", OfferingStates.ACTIVE),
            **kwargs,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("dedupe_tenant_offerings", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_no_duplicates_reports_clean(self):
        self._offering()
        output = self._run()
        self.assertIn("No duplicate per-tenant offerings found", output)

    def test_dry_run_reports_but_keeps_both(self):
        self._offering()
        self._offering()

        output = self._run()

        self.assertIn("dry-run", output)
        self.assertIn("would delete empty duplicate", output)
        # Nothing removed in dry-run.
        self.assertEqual(
            marketplace_models.Offering.objects.filter(
                type=OPENSTACK_INSTANCE_OFFERING, scope=self.tenant
            ).count(),
            2,
        )

    def test_apply_deletes_empty_duplicate(self):
        keeper = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.instance,
            project=self.fixture.project,
            offering=keeper,
        )
        self._offering()  # empty duplicate

        self._run("--apply")

        remaining = list(
            marketplace_models.Offering.objects.filter(
                type=OPENSTACK_INSTANCE_OFFERING, scope=self.tenant
            )
        )
        self.assertEqual([o.id for o in remaining], [keeper.id])

    def test_keeper_is_offering_with_active_resources(self):
        empty = self._offering()
        in_use = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.instance,
            project=self.fixture.project,
            offering=in_use,
        )

        self._run("--apply")

        self.assertTrue(
            marketplace_models.Offering.objects.filter(id=in_use.id).exists()
        )
        self.assertFalse(
            marketplace_models.Offering.objects.filter(id=empty.id).exists()
        )

    def test_non_empty_duplicate_skipped_without_merge(self):
        keeper = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.instance, project=self.fixture.project, offering=keeper
        )
        duplicate = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.volume, project=self.fixture.project, offering=duplicate
        )

        output = self._run("--apply")

        self.assertIn("SKIP duplicate", output)
        # Both survive — nothing is auto-merged.
        self.assertTrue(
            marketplace_models.Offering.objects.filter(id=duplicate.id).exists()
        )

    def test_merge_repoints_resources_then_deletes(self):
        keeper = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.instance, project=self.fixture.project, offering=keeper
        )
        duplicate = self._offering()
        moved = marketplace_factories.ResourceFactory(
            scope=self.fixture.volume, project=self.fixture.project, offering=duplicate
        )

        self._run("--apply", "--merge")

        moved.refresh_from_db()
        self.assertEqual(moved.offering_id, keeper.id)
        self.assertFalse(
            marketplace_models.Offering.objects.filter(id=duplicate.id).exists()
        )

    def test_merge_requires_apply(self):
        self._offering()
        self._offering()

        output = self._run("--merge")

        self.assertIn("--merge has no effect without --apply", output)

    def test_tenant_scoping(self):
        other_tenant = OpenStackFixture().tenant
        self._offering()
        self._offering()
        self._offering(scope=other_tenant)
        self._offering(scope=other_tenant)

        output = self._run("--tenant", str(self.tenant.id))

        # Only the requested tenant's group is reported.
        self.assertIn(f"Tenant {self.tenant.id}", output)
        self.assertNotIn(f"Tenant {other_tenant.id}", output)

    def test_terminated_resource_does_not_block_keeper_choice(self):
        # A duplicate whose only resource is terminated has 0 active resources,
        # so it should not be chosen as keeper over an in-use offering.
        in_use = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.instance, project=self.fixture.project, offering=in_use
        )
        stale = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.volume,
            project=self.fixture.project,
            offering=stale,
            state=ResourceStates.TERMINATED,
        )

        output = self._run()

        self.assertIn(f"keeping id={in_use.id}", output)
