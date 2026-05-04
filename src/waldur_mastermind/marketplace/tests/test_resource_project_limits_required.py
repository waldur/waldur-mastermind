"""Backend enforcement of `resource_projects_limits_required` plugin option.

When True, every limit-billing component declared by the offering must
have a value in the request when creating or updating a ResourceProject.

Use case: backends that reject projects without quotas (e.g. the
rancher-keycloak-operator's project-level resourceQuota.limit cap).
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
    """Offering with two limit components and the limits-required flag set."""

    LIMITS_REQUIRED: bool = True

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.plugin_options = {
            "enable_resource_projects": True,
            "resource_projects_limits_required": self.LIMITS_REQUIRED,
        }
        self.offering.save(update_fields=["plugin_options"])
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
        self.resource.save()

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def _create(self, name, limits):
        body = {"resource": self.resource.uuid.hex, "name": name}
        if limits is not None:
            body["limits"] = limits
        return self.client.post(_list_url(), body, format="json")

    def _patch(self, rp, payload):
        return self.client.patch(_detail_url(rp), payload, format="json")


class LimitsRequiredEnforcedTest(_Base):
    LIMITS_REQUIRED = True

    def test_create_without_limits_field_is_rejected(self):
        """Omitting `limits` entirely on create must fail with a clear error."""
        resp = self._create(name="rp1", limits=None)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.data
        assert "limits" in resp.data
        assert "cpu" in str(resp.data["limits"])
        assert "ram" in str(resp.data["limits"])

    def test_create_with_empty_limits_dict_is_rejected(self):
        resp = self._create(name="rp1", limits={})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.data
        assert "limits" in resp.data

    def test_create_with_some_components_missing_is_rejected(self):
        resp = self._create(name="rp1", limits={"cpu": 4})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.data
        # Only `ram` is missing, not `cpu`.
        assert "ram" in str(resp.data["limits"])

    def test_create_with_zero_value_treated_as_missing(self):
        """Zero is falsy; the flag is about HAVING a meaningful quota,
        not about ``key in limits``. Mirrors how operators reject a zero
        cap as effectively no cap."""
        resp = self._create(name="rp1", limits={"cpu": 4, "ram": 0})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.data
        assert "ram" in str(resp.data["limits"])

    def test_create_with_all_components_set_succeeds(self):
        resp = self._create(name="rp1", limits={"cpu": 4, "ram": 8})
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        rp = models.ResourceProject.objects.get(uuid=resp.data["uuid"])
        assert rp.limits == {"cpu": 4, "ram": 8}

    def test_partial_update_without_touching_limits_is_allowed(self):
        """A user changing only the description must NOT trip the limits
        check — the existing record's limits are trusted."""
        rp = models.ResourceProject.objects.create(
            resource=self.resource,
            name="rp1",
            limits={"cpu": 4, "ram": 8},
        )
        resp = self._patch(rp, {"description": "renamed"})
        assert resp.status_code == status.HTTP_200_OK, resp.data
        rp.refresh_from_db()
        assert rp.description == "renamed"
        assert rp.limits == {"cpu": 4, "ram": 8}

    def test_partial_update_emptying_limits_is_rejected(self):
        rp = models.ResourceProject.objects.create(
            resource=self.resource,
            name="rp1",
            limits={"cpu": 4, "ram": 8},
        )
        resp = self._patch(rp, {"limits": {}})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.data
        # Original limits must be untouched after a rejected update.
        rp.refresh_from_db()
        assert rp.limits == {"cpu": 4, "ram": 8}

    def test_partial_update_dropping_one_component_is_rejected(self):
        rp = models.ResourceProject.objects.create(
            resource=self.resource,
            name="rp1",
            limits={"cpu": 4, "ram": 8},
        )
        resp = self._patch(rp, {"limits": {"cpu": 4}})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.data
        assert "ram" in str(resp.data["limits"])


class LimitsRequiredOffTest(_Base):
    """Default behaviour — limits remain optional."""

    LIMITS_REQUIRED = False

    def test_create_without_limits_succeeds(self):
        resp = self._create(name="rp1", limits=None)
        assert resp.status_code == status.HTTP_201_CREATED, resp.data

    def test_create_with_empty_limits_succeeds(self):
        resp = self._create(name="rp1", limits={})
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
