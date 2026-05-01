"""Backend enforcement of `resource_projects_limit_policy` plugin option.

Three policies are supported:
  - ``"none"`` (default): limits are accepted as-is, no comparison with parent
    Resource limits.
  - ``"per_project"``: each ``ResourceProject.limits[c] <= Resource.limits[c]``.
  - ``"aggregate"``: the SUM of all sibling ``ResourceProject.limits[c]`` plus
    this project's value must stay within ``Resource.limits[c]``.

Per-component bounds (``OfferingComponent.min_value`` / ``max_value``) apply
regardless of the policy.
"""

from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


def _list_url():
    return "http://testserver" + reverse("marketplace-resource-project-list")


def _detail_url(rp):
    return "http://testserver" + reverse(
        "marketplace-resource-project-detail", kwargs={"uuid": rp.uuid.hex}
    )


class _Base(test.APITestCase):
    """Common fixture: one offering with two limit components, one resource
    with parent-side limits, one staff caller (avoids permission noise)."""

    POLICY: str = "none"
    RESOURCE_LIMITS = {"cpu": 100, "ram": 1000}

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.plugin_options = {
            "enable_resource_projects": True,
            "resource_projects_limit_policy": self.POLICY,
        }
        self.offering.save(update_fields=["plugin_options"])
        # The fixture may already have components — wipe them so we control
        # the component set exactly.
        self.offering.components.all().delete()
        for ctype, name, unit in (
            ("cpu", "CPU", "cores"),
            ("ram", "Memory", "GB"),
        ):
            models.OfferingComponent.objects.create(
                offering=self.offering,
                type=ctype,
                name=name,
                measured_unit=unit,
                billing_type=BillingTypes.LIMIT,
            )
        self.resource = self.fixture.resource
        self.resource.limits = dict(self.RESOURCE_LIMITS)
        self.resource.save(update_fields=["limits"])

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def _create(self, name, limits):
        return self.client.post(
            _list_url(),
            {
                "resource": self.resource.uuid.hex,
                "name": name,
                "limits": limits,
            },
            format="json",
        )

    def _patch(self, rp, limits):
        return self.client.patch(_detail_url(rp), {"limits": limits}, format="json")


class PolicyNoneTest(_Base):
    """Default behaviour — limits accepted regardless of parent caps."""

    POLICY = "none"

    def test_create_above_parent_is_accepted(self):
        response = self._create("p1", {"cpu": 1000})
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.content
        )

    def test_aggregate_above_parent_is_accepted(self):
        for name in ("a", "b", "c"):
            self._create(name, {"cpu": 100})  # 3 * 100 = 300 > 100
        self.assertEqual(
            models.ResourceProject.objects.filter(resource=self.resource).count(), 3
        )


class PolicyPerProjectTest(_Base):
    """Each project's value <= parent resource value, per component."""

    POLICY = "per_project"

    def test_create_at_parent_cap_is_accepted(self):
        response = self._create("p1", {"cpu": 100, "ram": 1000})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_above_parent_cap_is_rejected(self):
        response = self._create("p1", {"cpu": 101})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cpu", response.data["limits"])

    def test_component_without_parent_cap_is_accepted(self):
        # Drop cpu from parent caps — the policy then doesn't constrain cpu.
        self.resource.limits = {"ram": 1000}
        self.resource.save(update_fields=["limits"])
        response = self._create("p1", {"cpu": 9999})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_patch_above_parent_cap_is_rejected(self):
        rp = models.ResourceProject.objects.create(
            resource=self.resource, name="p1", limits={"cpu": 50}
        )
        response = self._patch(rp, {"cpu": 200})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PolicyAggregateTest(_Base):
    """Sum of all sibling ResourceProject limits stays within parent."""

    POLICY = "aggregate"

    def test_first_two_within_cap_are_accepted(self):
        self.assertEqual(self._create("a", {"cpu": 60}).status_code, 201)
        self.assertEqual(self._create("b", {"cpu": 40}).status_code, 201)  # total 100

    def test_third_pushing_over_cap_is_rejected(self):
        self._create("a", {"cpu": 60})
        self._create("b", {"cpu": 40})
        response = self._create("c", {"cpu": 1})  # total would be 101
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cpu", response.data["limits"])

    def test_patch_excludes_self_from_aggregate(self):
        a = models.ResourceProject.objects.create(
            resource=self.resource, name="a", limits={"cpu": 60}
        )
        models.ResourceProject.objects.create(
            resource=self.resource, name="b", limits={"cpu": 40}
        )
        # Updating `a` to 60 again must not double-count its own existing value.
        response = self._patch(a, {"cpu": 60})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)


class PolicyComponentBoundsAlwaysApplyTest(_Base):
    """OfferingComponent min_value / max_value are honoured under any policy.
    Tested with policy=none to prove the bound check is policy-orthogonal."""

    POLICY = "none"

    def test_above_component_max_is_rejected(self):
        cpu = self.offering.components.get(type="cpu")
        cpu.max_value = 200
        cpu.save(update_fields=["max_value"])
        response = self._create("p1", {"cpu": 999})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PolicyUnknownIsRejectedTest(_Base):
    """A typo / unknown policy value yields a clear 400 instead of being
    silently treated as 'none'."""

    POLICY = "tyop_here"

    def test_unknown_policy_rejected(self):
        response = self._create("p1", {"cpu": 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
