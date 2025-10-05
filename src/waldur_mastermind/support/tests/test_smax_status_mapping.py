from unittest import mock

from waldur_mastermind.support.backend.smax_utils import SmaxBackend, SmaxBackendError
from waldur_mastermind.support.tests import smax_base


class StatusMappingTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.backend = SmaxBackend()

        # Sample enum data response based on real SMAX API
        self.sample_enum_response = {
            "entities": [
                {
                    "entity_type": "EnumData_c",
                    "properties": {
                        "DataValue_c": "RequestStatusPending",
                        "DisplayLabel": " Pending - user",
                        "Id": "10604123",
                    },
                },
                {
                    "entity_type": "EnumData_c",
                    "properties": {
                        "DataValue_c": "RequestStatusReady",
                        "DisplayLabel": "Ready",
                        "Id": "10604124",
                    },
                },
                {
                    "entity_type": "EnumData_c",
                    "properties": {
                        "DataValue_c": "RequestStatusComplete",
                        "DisplayLabel": "Complete",
                        "Id": "10604125",
                    },
                },
                {
                    "entity_type": "EnumData_c",
                    "properties": {
                        "DataValue_c": "RequestStatusRejected",
                        "DisplayLabel": "Rejected",
                        "Id": "10604131",
                    },
                },
                {
                    "entity_type": "EnumData_c",
                    "properties": {
                        "DataValue_c": "RequestStatusInProgress",
                        "DisplayLabel": "In progress",
                        "Id": "10604136",
                    },
                },
            ]
        }

    def test_successful_status_mapping(self):
        """Test that Status field is correctly mapped to user-friendly label."""
        # Mock the enum endpoint response
        mock_response = mock.Mock()
        mock_response.json.return_value = self.sample_enum_response

        with mock.patch.object(self.backend, "get", return_value=mock_response):
            # Test properties with Status field that should be mapped
            properties = {"Status": "RequestStatusRejected", "PhaseId": "phase123"}

            result = self.backend._get_mapped_status(properties)
            self.assertEqual(result, "Rejected")

            # Test another mapping
            properties = {"Status": "RequestStatusInProgress", "PhaseId": "phase456"}

            result = self.backend._get_mapped_status(properties)
            self.assertEqual(result, "In progress")

    def test_fallback_to_phase_id_when_status_missing(self):
        """Test fallback to PhaseId when Status field is missing."""
        # Mock the enum endpoint response
        mock_response = mock.Mock()
        mock_response.json.return_value = self.sample_enum_response

        with mock.patch.object(self.backend, "get", return_value=mock_response):
            # Properties without Status field
            properties = {"PhaseId": "phase789"}

            result = self.backend._get_mapped_status(properties)
            self.assertEqual(result, "phase789")

    def test_fallback_to_phase_id_when_status_unmapped(self):
        """Test fallback to PhaseId when Status field value has no mapping."""
        # Mock the enum endpoint response
        mock_response = mock.Mock()
        mock_response.json.return_value = self.sample_enum_response

        with mock.patch.object(self.backend, "get", return_value=mock_response):
            # Properties with Status field that doesn't exist in mappings
            properties = {"Status": "SomeUnknownStatus", "PhaseId": "phase999"}

            result = self.backend._get_mapped_status(properties)
            self.assertEqual(result, "phase999")

    def test_error_handling_when_enum_endpoint_fails(self):
        """Test graceful error handling when enum endpoint fails."""
        # Mock the get method to raise an exception
        with mock.patch.object(
            self.backend, "get", side_effect=SmaxBackendError("API Error")
        ):
            # Should not raise exception, should fall back to PhaseId
            properties = {"Status": "RequestStatusReady", "PhaseId": "phase_fallback"}

            result = self.backend._get_mapped_status(properties)
            self.assertEqual(result, "phase_fallback")

    def test_caching_behavior(self):
        """Test that status mappings are cached and not fetched repeatedly."""
        # Mock the enum endpoint response
        mock_response = mock.Mock()
        mock_response.json.return_value = self.sample_enum_response

        with mock.patch.object(
            self.backend, "get", return_value=mock_response
        ) as mock_get:
            # First call should fetch mappings
            properties1 = {"Status": "RequestStatusReady", "PhaseId": "phase1"}
            result1 = self.backend._get_mapped_status(properties1)
            self.assertEqual(result1, "Ready")

            # Second call should use cached mappings
            properties2 = {"Status": "RequestStatusComplete", "PhaseId": "phase2"}
            result2 = self.backend._get_mapped_status(properties2)
            self.assertEqual(result2, "Complete")

            # Enum endpoint should only be called once (for caching)
            mock_get.assert_called_once_with(
                "ems/EnumData_c?layout=DataValue_c,DisplayLabelET_c,DisplayLabel"
            )

    def test_empty_or_whitespace_display_labels_ignored(self):
        """Test that empty or whitespace-only DisplayLabels are ignored."""
        # Mock response with empty and whitespace-only labels
        enum_response_with_empty_labels = {
            "entities": [
                {
                    "entity_type": "EnumData_c",
                    "properties": {
                        "DataValue_c": "RequestStatusValid",
                        "DisplayLabel": "Valid Status",
                    },
                },
                {
                    "entity_type": "EnumData_c",
                    "properties": {
                        "DataValue_c": "RequestStatusEmpty",
                        "DisplayLabel": "",
                    },
                },
                {
                    "entity_type": "EnumData_c",
                    "properties": {
                        "DataValue_c": "RequestStatusWhitespace",
                        "DisplayLabel": "   ",
                    },
                },
            ]
        }

        mock_response = mock.Mock()
        mock_response.json.return_value = enum_response_with_empty_labels

        with mock.patch.object(self.backend, "get", return_value=mock_response):
            # Get mappings and verify only valid one is included
            mappings = self.backend._get_status_mappings()

            self.assertIn("RequestStatusValid", mappings)
            self.assertEqual(mappings["RequestStatusValid"], "Valid Status")

            # Empty and whitespace labels should not be in mappings
            self.assertNotIn("RequestStatusEmpty", mappings)
            self.assertNotIn("RequestStatusWhitespace", mappings)

    def test_status_mapping_in_issue_response_parsing(self):
        """Test status mapping integration in _smax_response_to_issue method."""
        # Sample issue response with Status field
        issue_response = {
            "entities": [
                {
                    "entity_type": "Request",
                    "properties": {
                        "Id": "12345",
                        "DisplayLabel": "Test Issue",
                        "Description": "Test description",
                        "PhaseId": "phase123",
                        "Status": "RequestStatusComplete",
                        "RequestAttachments": "{}",
                        "Comments": "{}",
                    },
                }
            ]
        }

        # Mock the enum endpoint response
        mock_enum_response = mock.Mock()
        mock_enum_response.json.return_value = self.sample_enum_response

        with mock.patch.object(self.backend, "get", return_value=mock_enum_response):
            # Parse the issue response
            issues = self.backend._smax_response_to_issue(
                mock.Mock(json=lambda: issue_response)
            )

            self.assertEqual(len(issues), 1)
            issue = issues[0]
            self.assertEqual(issue.id, "12345")
            self.assertEqual(
                issue.status, "Complete"
            )  # Should be mapped from RequestStatusComplete

    def test_status_mapping_fallback_in_issue_response_parsing(self):
        """Test status mapping fallback in _smax_response_to_issue method."""
        # Sample issue response without Status field
        issue_response = {
            "entities": [
                {
                    "entity_type": "Request",
                    "properties": {
                        "Id": "12346",
                        "DisplayLabel": "Test Issue 2",
                        "Description": "Test description 2",
                        "PhaseId": "phase456",
                        "RequestAttachments": "{}",
                        "Comments": "{}",
                    },
                }
            ]
        }

        # Mock the enum endpoint response
        mock_enum_response = mock.Mock()
        mock_enum_response.json.return_value = self.sample_enum_response

        with mock.patch.object(self.backend, "get", return_value=mock_enum_response):
            # Parse the issue response
            issues = self.backend._smax_response_to_issue(
                mock.Mock(json=lambda: issue_response)
            )

            self.assertEqual(len(issues), 1)
            issue = issues[0]
            self.assertEqual(issue.id, "12346")
            self.assertEqual(issue.status, "phase456")  # Should fall back to PhaseId
