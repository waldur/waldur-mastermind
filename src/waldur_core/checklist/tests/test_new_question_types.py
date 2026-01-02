"""Tests for new question types: PHONE_NUMBER, YEAR, EMAIL, URL, COUNTRY, RATING, DATETIME."""

from rest_framework import status, test

from waldur_core.checklist import enums, models
from waldur_core.checklist.tests import factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class PhoneNumberQuestionTest(test.APITransactionTestCase):
    """Test PHONE_NUMBER question type validation."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def test_phone_number_accepts_valid_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.PHONE_NUMBER,
        )

        valid_phones = [
            "+1-555-555-5555",
            "+44 20 7946 0958",
            "555-555-5555",
            "(555) 555-5555",
            "+1234567890",
        ]
        for phone in valid_phones:
            with self.subTest(phone=phone):
                self.assertTrue(question.is_valid_answer(phone))

    def test_phone_number_rejects_non_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.PHONE_NUMBER,
        )

        self.assertFalse(question.is_valid_answer(123456789))
        self.assertFalse(question.is_valid_answer(["+1-555-555-5555"]))
        self.assertFalse(question.is_valid_answer({"phone": "+1-555-555-5555"}))

    def test_required_phone_number_rejects_none(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.PHONE_NUMBER,
            required=True,
        )

        self.assertFalse(question.is_valid_answer(None))


class YearQuestionTest(test.APITransactionTestCase):
    """Test YEAR question type validation with min/max support."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def test_year_accepts_valid_integers(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.YEAR,
        )

        valid_years = [1900, 2000, 2024, 2025, 2100]
        for year in valid_years:
            with self.subTest(year=year):
                self.assertTrue(question.is_valid_answer(year))

    def test_year_accepts_string_integers(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.YEAR,
        )

        self.assertTrue(question.is_valid_answer("2024"))
        self.assertTrue(question.is_valid_answer("1990"))

    def test_year_with_min_max_validation(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.YEAR,
            min_value=2000,
            max_value=2030,
        )

        # Valid years within range
        self.assertTrue(question.is_valid_answer(2000))
        self.assertTrue(question.is_valid_answer(2024))
        self.assertTrue(question.is_valid_answer(2030))

        # Invalid years outside range
        self.assertFalse(question.is_valid_answer(1999))
        self.assertFalse(question.is_valid_answer(2031))

    def test_year_rejects_non_integers(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.YEAR,
        )

        self.assertFalse(question.is_valid_answer("not a year"))
        self.assertFalse(question.is_valid_answer(2024.5))
        self.assertFalse(question.is_valid_answer([2024]))


class EmailQuestionTest(test.APITransactionTestCase):
    """Test EMAIL question type validation."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def test_email_accepts_valid_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.EMAIL,
        )

        valid_emails = [
            "user@example.com",
            "test.user@domain.org",
            "admin+tag@company.co.uk",
        ]
        for email in valid_emails:
            with self.subTest(email=email):
                self.assertTrue(question.is_valid_answer(email))

    def test_email_rejects_non_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.EMAIL,
        )

        self.assertFalse(question.is_valid_answer(123))
        self.assertFalse(question.is_valid_answer(["user@example.com"]))


class UrlQuestionTest(test.APITransactionTestCase):
    """Test URL question type validation."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def test_url_accepts_valid_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.URL,
        )

        valid_urls = [
            "https://example.com",
            "http://localhost:8000/path",
            "https://sub.domain.org/page?query=value",
        ]
        for url in valid_urls:
            with self.subTest(url=url):
                self.assertTrue(question.is_valid_answer(url))

    def test_url_rejects_non_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.URL,
        )

        self.assertFalse(question.is_valid_answer(123))
        self.assertFalse(question.is_valid_answer(["https://example.com"]))


class CountryQuestionTest(test.APITransactionTestCase):
    """Test COUNTRY question type validation."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def test_country_accepts_valid_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.COUNTRY,
        )

        valid_countries = ["US", "GB", "DE", "FR", "Estonia", "United States"]
        for country in valid_countries:
            with self.subTest(country=country):
                self.assertTrue(question.is_valid_answer(country))

    def test_country_rejects_non_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.COUNTRY,
        )

        self.assertFalse(question.is_valid_answer(1))
        self.assertFalse(question.is_valid_answer(["US"]))

    def test_country_in_operator_with_list(self):
        """Test that 'in' operator works with a list of countries for triggers."""
        from waldur_core.checklist import utils

        # Simulates: trigger if country is in ["US", "GB", "DE"]
        eu_countries = ["DE", "FR", "IT", "ES", "NL"]

        # User answer is in the list
        self.assertTrue(utils.apply_operator(["DE"], eu_countries, "in"))

        # User answer is not in the list
        self.assertFalse(utils.apply_operator(["US"], eu_countries, "in"))

        # Test not_in operator
        self.assertTrue(utils.apply_operator(["US"], eu_countries, "not_in"))
        self.assertFalse(utils.apply_operator(["DE"], eu_countries, "not_in"))


class RatingQuestionTest(test.APITransactionTestCase):
    """Test RATING question type validation with min/max support."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def test_rating_accepts_valid_integers(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.RATING,
        )

        valid_ratings = [1, 2, 3, 4, 5]
        for rating in valid_ratings:
            with self.subTest(rating=rating):
                self.assertTrue(question.is_valid_answer(rating))

    def test_rating_with_1_to_5_scale(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.RATING,
            min_value=1,
            max_value=5,
        )

        # Valid ratings
        for rating in [1, 2, 3, 4, 5]:
            self.assertTrue(question.is_valid_answer(rating))

        # Invalid ratings
        self.assertFalse(question.is_valid_answer(0))
        self.assertFalse(question.is_valid_answer(6))

    def test_rating_with_1_to_10_scale(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.RATING,
            min_value=1,
            max_value=10,
        )

        # Valid ratings
        self.assertTrue(question.is_valid_answer(1))
        self.assertTrue(question.is_valid_answer(5))
        self.assertTrue(question.is_valid_answer(10))

        # Invalid ratings
        self.assertFalse(question.is_valid_answer(0))
        self.assertFalse(question.is_valid_answer(11))

    def test_rating_accepts_string_integers(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.RATING,
            min_value=1,
            max_value=5,
        )

        self.assertTrue(question.is_valid_answer("3"))
        self.assertFalse(question.is_valid_answer("0"))
        self.assertFalse(question.is_valid_answer("6"))


class DatetimeQuestionTest(test.APITransactionTestCase):
    """Test DATETIME question type validation."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def test_datetime_accepts_valid_iso_strings(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.DATETIME,
        )

        valid_datetimes = [
            "2024-01-15T10:30:00",
            "2024-12-31T23:59:59",
            "2024-06-15T14:30:00+00:00",
            "2024-06-15T14:30:00.123456",
        ]
        for dt in valid_datetimes:
            with self.subTest(datetime=dt):
                self.assertTrue(question.is_valid_answer(dt))

    def test_datetime_rejects_invalid_formats(self):
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.DATETIME,
        )

        invalid_datetimes = [
            "not a datetime",
            "15/01/2024 10:30",
            123456789,
        ]
        for dt in invalid_datetimes:
            with self.subTest(datetime=dt):
                self.assertFalse(question.is_valid_answer(dt))

    def test_datetime_also_accepts_date_only(self):
        """Python 3.11+ datetime.fromisoformat accepts date-only strings."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.DATETIME,
        )

        # Date-only format is accepted by Python 3.11+
        self.assertTrue(question.is_valid_answer("2024-01-15"))


class NewQuestionTypesApiTest(test.APITransactionTestCase):
    """Test API creation of new question types."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.checklist = factories.ChecklistFactory()
        self.url = factories.QuestionFactory.get_admin_list_url()

    def _get_base_payload(self, question_type, description):
        return {
            "description": description,
            "question_type": question_type,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "required": False,
            "order": 1,
        }

    def test_create_phone_number_question(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_base_payload(
            enums.QuestionTypes.PHONE_NUMBER, "Enter your phone number"
        )
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(description="Enter your phone number")
        self.assertEqual(question.question_type, enums.QuestionTypes.PHONE_NUMBER)

    def test_create_year_question_with_limits(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_base_payload(
            enums.QuestionTypes.YEAR, "Enter the year established"
        )
        payload.update({"min_value": "1900", "max_value": "2030"})

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(description="Enter the year established")
        self.assertEqual(float(question.min_value), 1900)
        self.assertEqual(float(question.max_value), 2030)

    def test_create_email_question(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_base_payload(
            enums.QuestionTypes.EMAIL, "Enter contact email"
        )
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(description="Enter contact email")
        self.assertEqual(question.question_type, enums.QuestionTypes.EMAIL)

    def test_create_url_question(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_base_payload(enums.QuestionTypes.URL, "Enter website URL")
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(description="Enter website URL")
        self.assertEqual(question.question_type, enums.QuestionTypes.URL)

    def test_create_country_question(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_base_payload(
            enums.QuestionTypes.COUNTRY, "Select your country"
        )
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(description="Select your country")
        self.assertEqual(question.question_type, enums.QuestionTypes.COUNTRY)

    def test_create_rating_question_with_scale(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_base_payload(
            enums.QuestionTypes.RATING, "Rate your satisfaction"
        )
        payload.update({"min_value": "1", "max_value": "5"})

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(description="Rate your satisfaction")
        self.assertEqual(question.question_type, enums.QuestionTypes.RATING)
        self.assertEqual(float(question.min_value), 1)
        self.assertEqual(float(question.max_value), 5)

    def test_create_datetime_question(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_base_payload(
            enums.QuestionTypes.DATETIME, "Select appointment date and time"
        )
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(
            description="Select appointment date and time"
        )
        self.assertEqual(question.question_type, enums.QuestionTypes.DATETIME)


class NewQuestionTypesOperatorTest(test.APITransactionTestCase):
    """Test operator support for new question types."""

    def test_phone_number_supports_equals_contains(self):
        from waldur_core.checklist import utils

        # equals
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.PHONE_NUMBER, "equals"
            )
        )
        # contains
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.PHONE_NUMBER, "contains"
            )
        )
        # not_equals
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.PHONE_NUMBER, "not_equals"
            )
        )

    def test_year_supports_equals_not_equals(self):
        from waldur_core.checklist import utils

        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.YEAR, "equals"
            )
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.YEAR, "not_equals"
            )
        )
        # Year should not support contains
        self.assertFalse(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.YEAR, "contains"
            )
        )

    def test_email_supports_equals_contains(self):
        from waldur_core.checklist import utils

        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.EMAIL, "equals"
            )
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.EMAIL, "contains"
            )
        )

    def test_url_supports_equals_contains(self):
        from waldur_core.checklist import utils

        self.assertTrue(
            utils.is_valid_operator_for_question_type(enums.QuestionTypes.URL, "equals")
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.URL, "contains"
            )
        )

    def test_country_supports_equals_not_equals_in_not_in(self):
        from waldur_core.checklist import utils

        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.COUNTRY, "equals"
            )
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.COUNTRY, "not_equals"
            )
        )
        # Country also supports 'in' and 'not_in' for checking against a set of countries
        self.assertTrue(
            utils.is_valid_operator_for_question_type(enums.QuestionTypes.COUNTRY, "in")
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.COUNTRY, "not_in"
            )
        )

    def test_rating_supports_equals_not_equals(self):
        from waldur_core.checklist import utils

        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.RATING, "equals"
            )
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.RATING, "not_equals"
            )
        )

    def test_datetime_supports_equals_not_equals(self):
        from waldur_core.checklist import utils

        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.DATETIME, "equals"
            )
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.DATETIME, "not_equals"
            )
        )
