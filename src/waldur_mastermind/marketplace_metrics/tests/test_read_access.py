"""Who reads metric data, role by role.

The scenario's project is the consumer under test. A second organization
consumes the same offering, so that a consumer's view (its own resources) and
the provider's (every resource) differ.
"""

import datetime
import uuid

from ddt import data, ddt
from rest_framework import status

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.tests.pat_utils import STAFF, SUPPORT, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_metrics import models, rollups

from . import factories
from .test_read_and_goals import GOALS, PROJECT_METRICS, SERIES, Scenario

BREAKDOWN = "/api/marketplace-metric-breakdown/"
RESOURCE_METRICS = "/api/marketplace-resource-metrics/"

# What a caller sees.
EVERYTHING = "everything"  # staff and support: any project, any resource
PROVIDER = "provider"  # every resource of the offering, but no consumer's project
CONSUMER = "consumer"  # the scenario's project and its resources only
NOTHING = "nothing"

SIGHT = {
    "staff": EVERYTHING,
    "global_support": EVERYTHING,
    "provider_owner": PROVIDER,
    "provider_support": PROVIDER,
    "service_provider_manager_reporting_usage": PROVIDER,
    "service_provider_manager_not_reporting_usage": NOTHING,
    "offering_manager_reporting_usage": PROVIDER,
    "offering_manager_not_reporting_usage": NOTHING,
    "consumer_owner": CONSUMER,
    "project_manager": CONSUMER,
    "project_admin": CONSUMER,
    "project_member": CONSUMER,
    "outsider": NOTHING,
    # A token without the staff or support scope leaves its user the roles
    # they hold: here, membership of the scenario's project.
    "staff_token_without_staff_scope": CONSUMER,
    "support_token_without_support_scope": CONSUMER,
    "staff_token_with_staff_scope": EVERYTHING,
    "support_token_with_support_scope": EVERYTHING,
}
ROLES = tuple(SIGHT)

OWN_TOTAL = 9
FOREIGN_TOTAL = 100


@ddt
@override_config(PAT_ENABLED=True)
class ReadAccessTest(Scenario):
    def setUp(self):
        super().setUp()
        self.foreign = structure_fixtures.ProjectFixture()
        self.foreign_resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.foreign.project,
            state=ResourceStates.OK,
        )
        self.add(
            self.completions,
            self.foreign_resource,
            {"course": "linux"},
            FOREIGN_TOTAL,
            minutes=5,
        )
        rollups.roll_up()
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.provider.customer
        )
        self.start = (self.now - datetime.timedelta(days=1)).isoformat()

    # Callers

    def login(self, role):
        getattr(self, f"as_{role}")()

    def as_staff(self):
        self.client.force_authenticate(self.fixture.staff)

    def as_global_support(self):
        self.client.force_authenticate(self.fixture.global_support)

    def as_provider_owner(self):
        self.client.force_authenticate(self.provider.owner)

    def as_provider_support(self):
        self.client.force_authenticate(self.provider.customer_support)

    def service_provider_manager(self):
        # The role sits on the ServiceProvider, not on its organization.
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = structure_factories.UserFactory()
        self.service_provider.add_user(user, ServiceProviderRole.MANAGER)
        self.client.force_authenticate(user)

    def as_service_provider_manager_reporting_usage(self):
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.service_provider_manager()

    def as_service_provider_manager_not_reporting_usage(self):
        self.service_provider_manager()

    def offering_manager(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = structure_factories.UserFactory()
        self.offering.add_user(user, OfferingRole.MANAGER)
        self.client.force_authenticate(user)

    def as_offering_manager_reporting_usage(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.offering_manager()

    def as_offering_manager_not_reporting_usage(self):
        self.offering_manager()

    def as_consumer_owner(self):
        self.client.force_authenticate(self.fixture.owner)

    def as_project_manager(self):
        self.client.force_authenticate(self.fixture.manager)

    def as_project_admin(self):
        self.client.force_authenticate(self.fixture.admin)

    def as_project_member(self):
        self.client.force_authenticate(self.fixture.member)

    def as_outsider(self):
        self.client.force_authenticate(structure_factories.UserFactory())

    def with_token(self, user, *scopes):
        self.fixture.project.add_user(user, ProjectRole.MEMBER)
        use_pat(self.client, user, *scopes)

    def as_staff_token_without_staff_scope(self):
        self.with_token(self.fixture.staff)

    def as_support_token_without_support_scope(self):
        self.with_token(self.fixture.global_support)

    def as_staff_token_with_staff_scope(self):
        self.with_token(self.fixture.staff, STAFF)

    def as_support_token_with_support_scope(self):
        self.with_token(self.fixture.global_support, SUPPORT)

    # Requests

    def series(self, **params):
        return self.client.get(
            SERIES,
            {
                "offering_metric_uuid": self.completions.uuid.hex,
                "start": self.start,
                "granularity": "hour",
                **params,
            },
        )

    def breakdown(self, **params):
        return self.client.get(
            BREAKDOWN,
            {
                "offering_metric_uuid": self.completions.uuid.hex,
                "start": self.start,
                **params,
            },
        )

    def assert_total(self, response, expected):
        """The series add up to ``expected``; None stands for a 404."""
        if expected is None:
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
            return
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        total = sum(
            point["value"]
            for group in response.data["series"]
            for point in group["points"]
        )
        self.assertEqual(total, expected)

    def assert_metrics(self, response, visible):
        """A summary lists both of the offering's metrics, or the route answers 404."""
        if not visible:
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
            return
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {uuid.UUID(str(item["offering_metric"]["uuid"])) for item in response.data},
            {self.completions.uuid, self.response_time.uuid},
        )

    # Offering metrics

    @data(*ROLES)
    def test_offering_metrics_list(self, role):
        self.login(role)

        response = self.client.get(factories.OfferingMetricFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        listed = {uuid.UUID(str(item["uuid"])) for item in response.data}
        expected = (
            set()
            if SIGHT[role] == NOTHING
            else {self.completions.uuid, self.response_time.uuid}
        )
        self.assertEqual(listed, expected)

    @data(*ROLES)
    def test_offering_metric_retrieve(self, role):
        self.login(role)

        response = self.client.get(
            factories.OfferingMetricFactory.get_url(self.completions)
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND if SIGHT[role] == NOTHING else status.HTTP_200_OK,
        )

    # Series

    @data(*ROLES)
    def test_series_of_the_metric(self, role):
        self.login(role)

        self.assert_total(
            self.series(),
            {
                EVERYTHING: OWN_TOTAL + FOREIGN_TOTAL,
                PROVIDER: OWN_TOTAL + FOREIGN_TOTAL,
                CONSUMER: OWN_TOTAL,
                NOTHING: None,
            }[SIGHT[role]],
        )

    @data(*ROLES)
    def test_series_of_another_consumers_resource(self, role):
        self.login(role)

        self.assert_total(
            self.series(resource_uuid=self.foreign_resource.uuid.hex),
            FOREIGN_TOTAL if SIGHT[role] in (EVERYTHING, PROVIDER) else None,
        )

    @data(*ROLES)
    def test_series_of_the_project(self, role):
        self.login(role)

        self.assert_total(
            self.series(project_uuid=self.fixture.project.uuid.hex),
            OWN_TOTAL if SIGHT[role] in (EVERYTHING, CONSUMER) else None,
        )

    # Breakdown

    @data(*ROLES)
    def test_breakdown_of_another_consumers_resource(self, role):
        self.login(role)

        response = self.breakdown(
            resource_uuid=self.foreign_resource.uuid.hex, group_by="course"
        )

        if SIGHT[role] in (EVERYTHING, PROVIDER):
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual([item["value"] for item in response.data], ["linux"])
        else:
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(*ROLES)
    def test_breakdown_of_the_project(self, role):
        self.login(role)

        response = self.breakdown(
            project_uuid=self.fixture.project.uuid.hex, group_by="resource"
        )

        if SIGHT[role] in (EVERYTHING, CONSUMER):
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(
                {uuid.UUID(str(item["resource_uuid"])) for item in response.data},
                {resource.uuid for resource in self.resources},
            )
        else:
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Summaries

    @data(*ROLES)
    def test_project_metrics(self, role):
        self.login(role)

        response = self.client.get(
            PROJECT_METRICS, {"project_uuid": self.fixture.project.uuid.hex}
        )

        self.assert_metrics(response, SIGHT[role] in (EVERYTHING, CONSUMER))

    @data(*ROLES)
    def test_resource_metrics(self, role):
        self.login(role)

        response = self.client.get(
            RESOURCE_METRICS, {"resource_uuid": self.resources[0].uuid.hex}
        )

        self.assert_metrics(response, SIGHT[role] != NOTHING)

    # Goals

    @data(*ROLES)
    def test_goals(self, role):
        default, own, foreign = (
            models.MetricGoal.objects.create(
                offering_metric=self.completions, project=project, value=10
            )
            for project in (None, self.fixture.project, self.foreign.project)
        )
        self.login(role)

        response = self.client.get(GOALS)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        listed = {uuid.UUID(str(item["uuid"])) for item in response.data}
        expected = {
            EVERYTHING: {default, own, foreign},
            PROVIDER: {default, own, foreign},
            CONSUMER: {default, own},
            NOTHING: set(),
        }[SIGHT[role]]
        self.assertEqual(listed, {goal.uuid for goal in expected})

    @data(*ROLES)
    def test_resource_metrics_of_another_consumer(self, role):
        self.login(role)

        response = self.client.get(
            RESOURCE_METRICS, {"resource_uuid": self.foreign_resource.uuid.hex}
        )

        self.assert_metrics(response, SIGHT[role] in (EVERYTHING, PROVIDER))


class AnonymousTest(Scenario):
    def test_every_read_needs_a_signed_in_caller(self):
        for url in (
            SERIES,
            BREAKDOWN,
            PROJECT_METRICS,
            RESOURCE_METRICS,
            factories.OfferingMetricFactory.get_list_url(),
            factories.OfferingMetricFactory.get_url(self.completions),
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED
                )


class HiddenAndMissingLookAlikeTest(Scenario):
    """A resource or project the caller cannot see answers as a missing one does."""

    def setUp(self):
        super().setUp()
        foreign = structure_fixtures.ProjectFixture()
        self.hidden_project = foreign.project
        self.hidden_resource = marketplace_factories.ResourceFactory(
            offering=self.offering, project=foreign.project, state=ResourceStates.OK
        )
        self.params = {
            "offering_metric_uuid": self.completions.uuid.hex,
            "start": (self.now - datetime.timedelta(days=1)).isoformat(),
            "group_by": "course",
        }
        self.client.force_authenticate(self.fixture.member)

    def assert_alike(self, url, name, hidden, **params):
        answers = [
            self.client.get(url, {**params, name: value})
            for value in (hidden.uuid.hex, uuid.uuid4().hex)
        ]
        for response in answers:
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(answers[0].data, answers[1].data)

    def test_resource(self):
        for url, params in (
            (SERIES, self.params),
            (BREAKDOWN, self.params),
            (RESOURCE_METRICS, {}),
        ):
            with self.subTest(url=url):
                self.assert_alike(url, "resource_uuid", self.hidden_resource, **params)

    def test_project(self):
        for url, params in (
            (SERIES, self.params),
            (BREAKDOWN, self.params),
            (PROJECT_METRICS, {}),
        ):
            with self.subTest(url=url):
                self.assert_alike(url, "project_uuid", self.hidden_project, **params)


class ConsumerWithAServiceProviderRoleTest(Scenario):
    """A consumer who also holds a service provider role that reports no usage."""

    def setUp(self):
        super().setUp()
        foreign = structure_fixtures.ProjectFixture()
        self.own_goal = models.MetricGoal.objects.create(
            offering_metric=self.completions, project=self.fixture.project, value=1
        )
        self.foreign_goal = models.MetricGoal.objects.create(
            offering_metric=self.completions, project=foreign.project, value=1
        )
        marketplace_factories.ServiceProviderFactory(
            customer=self.provider.customer
        ).add_user(self.fixture.member, ServiceProviderRole.MANAGER)
        self.client.force_authenticate(self.fixture.member)

    def test_sees_only_its_own_projects_goal(self):
        response = self.client.get(GOALS)

        self.assertEqual(
            {uuid.UUID(str(item["uuid"])) for item in response.data},
            {self.own_goal.uuid},
        )


class ResourceOfAnotherOfferingTest(Scenario):
    """A resource the caller sees, named with a metric of a different offering."""

    def setUp(self):
        super().setUp()
        self.other = marketplace_factories.ResourceFactory(
            project=self.fixture.project, state=ResourceStates.OK
        )
        self.client.force_authenticate(self.fixture.member)

    def test_series_and_breakdown_answer_alike(self):
        params = {
            "offering_metric_uuid": self.completions.uuid.hex,
            "resource_uuid": self.other.uuid.hex,
            "start": (self.now - datetime.timedelta(days=1)).isoformat(),
            "group_by": "course",
        }
        for url in (SERIES, BREAKDOWN):
            with self.subTest(url=url):
                self.assertEqual(
                    self.client.get(url, params).status_code,
                    status.HTTP_404_NOT_FOUND,
                )


class GoalOfAHiddenMetricTest(Scenario):
    """A goal can be set only on a metric the caller sees."""

    def create(self, user, **payload):
        self.client.force_authenticate(user)
        return self.client.post(GOALS, payload, format="json")

    def test_a_hidden_metric_answers_as_a_missing_one(self):
        hidden = factories.OfferingMetricFactory()

        answers = [
            self.create(
                self.fixture.manager,
                offering_metric=metric_uuid,
                project=self.fixture.project.uuid.hex,
                value=1,
            )
            for metric_uuid in (hidden.uuid.hex, uuid.uuid4().hex)
        ]

        for response in answers:
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(answers[0].data, answers[1].data)
        self.assertFalse(models.MetricGoal.objects.exists())

    def service_provider_manager(self, *permissions):
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        for permission in permissions:
            ServiceProviderRole.MANAGER.add_permission(permission)
        user = structure_factories.UserFactory()
        marketplace_factories.ServiceProviderFactory(
            customer=self.provider.customer
        ).add_user(user, ServiceProviderRole.MANAGER)
        return user

    def test_a_manager_who_cannot_read_the_metric_cannot_set_its_default(self):
        response = self.create(
            self.service_provider_manager(),
            offering_metric=self.completions.uuid.hex,
            value=10,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_manager_who_reads_the_metric_sets_its_default(self):
        response = self.create(
            self.service_provider_manager(PermissionEnum.SET_RESOURCE_USAGE),
            offering_metric=self.completions.uuid.hex,
            value=10,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class PrivateDefinitionTest(Scenario):
    """Who sees a definition private to the provider: as who may manage it."""

    def setUp(self):
        super().setUp()
        self.definition = factories.MetricDefinitionFactory(
            owner_customer=self.provider.customer
        )
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.provider.customer
        )

    def sees(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.MetricDefinitionFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return self.definition.uuid in {
            uuid.UUID(str(item["uuid"])) for item in response.data
        }

    def service_provider_manager(self, *permissions):
        for permission in permissions:
            ServiceProviderRole.MANAGER.add_permission(permission)
        user = structure_factories.UserFactory()
        self.service_provider.add_user(user, ServiceProviderRole.MANAGER)
        return user

    def test_a_service_provider_manager_who_creates_offerings_sees_it(self):
        self.assertTrue(
            self.sees(self.service_provider_manager(PermissionEnum.CREATE_OFFERING))
        )

    def test_a_service_provider_manager_who_cannot_create_offerings_does_not(self):
        self.assertFalse(
            self.sees(self.service_provider_manager(PermissionEnum.UPDATE_OFFERING))
        )

    def test_any_role_in_the_providers_organization_sees_it(self):
        self.assertTrue(self.sees(self.provider.customer_support))

    def test_a_consumer_does_not(self):
        self.assertFalse(self.sees(self.fixture.owner))
