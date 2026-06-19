from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import ChecklistTypes, QuestionTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class ServiceProviderProjectMetadataTest(test.APITestCase):
    """A provider can read a consumer project's metadata-checklist answers
    through the service-provider projects endpoint, which reuses ProjectSerializer.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        # Shared offering with a resource in the consumer project links the
        # provider to the project.
        self.fixture.offering.shared = True
        self.fixture.offering.save()
        self.resource = self.fixture.resource

        self.project = self.fixture.project
        self.consumer_customer = self.project.customer

        # Configure project-metadata checklist on the consumer customer.
        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROJECT_METADATA
        )
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            question_type=QuestionTypes.TEXT_INPUT,
            description="Call ID",
            order=1,
        )
        self.consumer_customer.project_metadata_checklist = self.checklist
        self.consumer_customer.save()

        completion, _ = checklist_models.ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=ContentType.objects.get_for_model(type(self.project)),
            scope_object_id=self.project.id,
        )
        checklist_factories.AnswerFactory(
            completion=completion,
            question=self.question,
            answer_data="EXT-2026-042",
            user=self.fixture.owner,
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_PROJECTS)

        self.url = reverse(
            "service-provider-projects-list",
            kwargs={"service_provider_uuid": self.fixture.service_provider.uuid.hex},
        )

    def test_provider_sees_project_metadata(self):
        self.client.force_authenticate(user=self.fixture.service_owner)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        projects = (
            response.data["results"]
            if isinstance(response.data, dict) and "results" in response.data
            else response.data
        )
        match = next(p for p in projects if p["uuid"] == self.project.uuid.hex)
        self.assertEqual(len(match["project_metadata"]), 1)
        self.assertEqual(match["project_metadata"][0]["question"], "Call ID")
        self.assertEqual(match["project_metadata"][0]["answer"], "EXT-2026-042")
