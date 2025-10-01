from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import factories


@ddt
class ServiceProviderChecklistsSummaryTest(test.APITransactionTestCase):
    """Test service provider checklists summary endpoint."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )

        # Set up permissions for the compliance endpoint
        CustomerRole.OWNER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS
        )

        # Create categories for checklists
        self.category1 = checklist_factories.CategoryFactory(name="Security")
        self.category2 = checklist_factories.CategoryFactory(name="Privacy")

        # Create compliance checklists with questions
        self.checklist1 = checklist_factories.ChecklistFactory(
            name="Security Compliance Checklist",
            category=self.category1,
        )
        self.checklist2 = checklist_factories.ChecklistFactory(
            name="Data Protection Checklist",
            category=self.category2,
        )
        self.checklist3 = checklist_factories.ChecklistFactory(
            name="Unused Checklist",
            category=self.category1,
        )

        # Create questions for checklists to test questions_count
        for i in range(5):  # 5 questions for checklist1
            checklist_factories.QuestionFactory(
                checklist=self.checklist1,
                description=f"Security question {i + 1}",
                order=i,
            )

        for i in range(3):  # 3 questions for checklist2
            checklist_factories.QuestionFactory(
                checklist=self.checklist2,
                description=f"Privacy question {i + 1}",
                order=i,
            )

        for i in range(2):  # 2 questions for checklist3 (unused)
            checklist_factories.QuestionFactory(
                checklist=self.checklist3,
                description=f"Unused question {i + 1}",
                order=i,
            )

        # Create offerings with checklists
        self.offering1 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Offering 1",
            compliance_checklist=self.checklist1,
        )
        self.offering2 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Offering 2",
            compliance_checklist=self.checklist1,
        )
        self.offering3 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Offering 3",
            compliance_checklist=self.checklist2,
        )
        # Offering without checklist (should not appear in summary)
        self.offering4 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Offering 4",
            compliance_checklist=None,
        )

    def test_checklists_summary_success(self):
        """Test successful retrieval of checklists summary."""
        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)  # Only 2 checklists are used

        # Verify data structure and content
        checklist_data = {item["checklist_uuid"]: item for item in response.data}

        # Check checklist1 data (used by 2 offerings)
        checklist1_data = checklist_data[str(self.checklist1.uuid)]
        self.assertEqual(
            checklist1_data["checklist_name"], "Security Compliance Checklist"
        )
        self.assertEqual(checklist1_data["questions_count"], 5)
        self.assertEqual(checklist1_data["offerings_count"], 2)
        self.assertEqual(checklist1_data["category_name"], "Security")

        # Check checklist2 data (used by 1 offering)
        checklist2_data = checklist_data[str(self.checklist2.uuid)]
        self.assertEqual(checklist2_data["checklist_name"], "Data Protection Checklist")
        self.assertEqual(checklist2_data["questions_count"], 3)
        self.assertEqual(checklist2_data["offerings_count"], 1)
        self.assertEqual(checklist2_data["category_name"], "Privacy")

        # Unused checklist should not appear
        self.assertNotIn(str(self.checklist3.uuid), checklist_data)

    def test_checklists_summary_ordered_by_usage(self):
        """Test that results are ordered by usage count (descending)."""
        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # First should be checklist1 (2 offerings), then checklist2 (1 offering)
        self.assertEqual(response.data[0]["checklist_uuid"], str(self.checklist1.uuid))
        self.assertEqual(response.data[0]["offerings_count"], 2)
        self.assertEqual(response.data[1]["checklist_uuid"], str(self.checklist2.uuid))
        self.assertEqual(response.data[1]["offerings_count"], 1)

    def test_checklists_summary_no_checklists(self):
        """Test summary when no offerings have checklists."""
        # Remove checklists from all offerings
        for offering in [self.offering1, self.offering2, self.offering3]:
            offering.compliance_checklist = None
            offering.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_checklists_summary_checklist_without_category(self):
        """Test summary for checklist without category."""
        # Create checklist without category
        checklist_no_category = checklist_factories.ChecklistFactory(
            name="No Category Checklist",
            category=None,
        )
        checklist_factories.QuestionFactory(
            checklist=checklist_no_category,
            description="Question without category",
        )

        # Create offering with this checklist
        factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Offering No Category",
            compliance_checklist=checklist_no_category,
        )

        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find the checklist without category
        no_category_data = next(
            (
                item
                for item in response.data
                if item["checklist_uuid"] == str(checklist_no_category.uuid)
            ),
            None,
        )
        self.assertIsNotNone(no_category_data)
        self.assertIsNone(no_category_data["category_name"])
        self.assertEqual(no_category_data["questions_count"], 1)

    def test_checklists_summary_pagination(self):
        """Test pagination functionality for checklists summary."""
        # Create multiple checklists (more than default page size)
        checklists = []
        for i in range(15):  # Create 15 checklists
            checklist = checklist_factories.ChecklistFactory(
                name=f"Checklist {i:02d}",
                category=self.category1,
            )
            checklist_factories.QuestionFactory(
                checklist=checklist,
                description=f"Question for checklist {i}",
            )

            # Create 1-3 offerings for each checklist to vary usage
            for j in range((i % 3) + 1):
                factories.OfferingFactory(
                    customer=self.fixture.customer,
                    name=f"Offering {i}-{j}",
                    compliance_checklist=checklist,
                )
            checklists.append(checklist)

        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        # Test first page with page_size parameter
        response = self.client.get(url, {"page_size": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check pagination structure (Waldur uses LinkHeaderPagination)
        self.assertIn("X-Result-Count", response)
        self.assertIn("Link", response)

        # Check that we got the expected number of items
        self.assertEqual(len(response.data), 5)
        total_count = int(response["X-Result-Count"])
        self.assertGreaterEqual(
            total_count, 15
        )  # At least 15 checklists (plus any existing ones)

        # Check Link header contains next page info
        link_header = response["Link"]
        self.assertIn('rel="next"', link_header)

        # Test second page
        response = self.client.get(url, {"page": 2, "page_size": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 5)
        link_header = response["Link"]
        self.assertIn('rel="next"', link_header)  # Should have next page
        self.assertIn('rel="prev"', link_header)  # Should have previous page

        # Test getting to a page that should have fewer items
        total_pages = (total_count + 4) // 5  # Round up division
        last_page = total_pages
        response = self.client.get(url, {"page": last_page, "page_size": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertLessEqual(len(response.data), 5)  # Last page may have fewer items
        link_header = response["Link"]
        self.assertNotIn('rel="next"', link_header)  # Last page, no next
        self.assertIn('rel="prev"', link_header)  # Should have previous page

    def test_checklists_summary_pagination_ordering(self):
        """Test that pagination preserves correct ordering by usage count."""
        # Create checklists with different usage counts
        checklist_low = checklist_factories.ChecklistFactory(
            name="Low Usage Checklist",
            category=self.category1,
        )
        checklist_factories.QuestionFactory(checklist=checklist_low)
        factories.OfferingFactory(
            customer=self.fixture.customer,
            compliance_checklist=checklist_low,
        )  # 1 offering

        checklist_high = checklist_factories.ChecklistFactory(
            name="High Usage Checklist",
            category=self.category1,
        )
        checklist_factories.QuestionFactory(checklist=checklist_high)
        for i in range(5):  # 5 offerings
            factories.OfferingFactory(
                customer=self.fixture.customer,
                compliance_checklist=checklist_high,
            )

        checklist_medium = checklist_factories.ChecklistFactory(
            name="Medium Usage Checklist",
            category=self.category1,
        )
        checklist_factories.QuestionFactory(checklist=checklist_medium)
        for i in range(3):  # 3 offerings
            factories.OfferingFactory(
                customer=self.fixture.customer,
                compliance_checklist=checklist_medium,
            )

        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        # Get first page
        response = self.client.get(url, {"page_size": 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.data
        self.assertEqual(len(results), 2)

        # Check ordering: highest usage should be first
        self.assertEqual(results[0]["checklist_name"], "High Usage Checklist")
        self.assertEqual(results[0]["offerings_count"], 5)
        self.assertEqual(results[1]["checklist_name"], "Medium Usage Checklist")
        self.assertEqual(results[1]["offerings_count"], 3)

        # Check that the Low Usage Checklist appears somewhere in the results
        # (may not be on page 2 due to existing checklists)
        found_low_usage = False
        page = 2
        while not found_low_usage and page <= 10:  # Limit to avoid infinite loop
            response = self.client.get(url, {"page": page, "page_size": 2})
            if response.status_code != 200:
                break
            results = response.data
            for result in results:
                if result["checklist_name"] == "Low Usage Checklist":
                    self.assertEqual(result["offerings_count"], 1)
                    found_low_usage = True
                    break
            page += 1

        self.assertTrue(
            found_low_usage, "Low Usage Checklist should be found in paginated results"
        )

    @data("staff", "owner")
    def test_checklists_summary_authorized_users(self, user_role):
        """Test that authorized users can access checklists summary."""
        user = getattr(self.fixture, user_role)
        self.client.force_authenticate(user=user)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 0)

    def test_checklists_summary_unauthorized_user(self):
        """Test that unauthorized users cannot access checklists summary."""
        # Create user not related to service provider
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=unauthorized_user)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_checklists_summary_different_service_provider(self):
        """Test that users can only see their own service provider's data."""
        # Create another service provider with offerings
        other_fixture = structure_fixtures.ProjectFixture()
        other_service_provider = factories.ServiceProviderFactory(
            customer=other_fixture.customer
        )

        # Add permission for other customer owner
        other_fixture.customer.add_user(other_fixture.owner, CustomerRole.OWNER)

        other_checklist = checklist_factories.ChecklistFactory(name="Other Checklist")
        checklist_factories.QuestionFactory(
            checklist=other_checklist,
            description="Other question",
        )

        factories.OfferingFactory(
            customer=other_fixture.customer,
            name="Other Offering",
            compliance_checklist=other_checklist,
        )

        # Test that our service provider owner cannot see other provider's data
        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            other_service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_checklists_summary_response_structure(self):
        """Test that response has correct structure and field types."""
        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)

        # Check required fields and types
        for item in response.data:
            self.assertIn("checklist_uuid", item)
            self.assertIn("checklist_name", item)
            self.assertIn("questions_count", item)
            self.assertIn("offerings_count", item)
            self.assertIn("category_name", item)

            # Type validation
            self.assertIsInstance(item["checklist_uuid"], str)
            self.assertIsInstance(item["checklist_name"], str)
            self.assertIsInstance(item["questions_count"], int)
            self.assertIsInstance(item["offerings_count"], int)
            # category_name can be string or None
            self.assertTrue(
                item["category_name"] is None or isinstance(item["category_name"], str)
            )

            # Value validation
            self.assertGreater(item["questions_count"], 0)
            self.assertGreater(item["offerings_count"], 0)

    def test_checklists_summary_large_dataset_performance(self):
        """Test performance with larger dataset."""
        # Create additional checklists and offerings
        additional_checklists = []
        for i in range(5):
            checklist = checklist_factories.ChecklistFactory(
                name=f"Performance Test Checklist {i}",
                category=self.category1,
            )
            # Add varying number of questions
            for j in range(i + 1):
                checklist_factories.QuestionFactory(
                    checklist=checklist,
                    description=f"Performance question {j}",
                )
            additional_checklists.append(checklist)

        # Create offerings using these checklists
        for i, checklist in enumerate(additional_checklists):
            # Create multiple offerings per checklist
            for j in range(i + 1):
                factories.OfferingFactory(
                    customer=self.fixture.customer,
                    name=f"Performance Offering {i}-{j}",
                    compliance_checklist=checklist,
                )

        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ServiceProviderFactory.get_compliance_url(
            self.service_provider, "checklists-summary"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should have original 2 checklists + 5 new ones = 7 total
        self.assertEqual(len(response.data), 7)

        # Verify ordering (highest usage first)
        usage_counts = [item["offerings_count"] for item in response.data]
        self.assertEqual(usage_counts, sorted(usage_counts, reverse=True))
