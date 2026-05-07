from django.utils.translation import gettext_lazy as _


class ChecklistTypes:
    PROJECT_COMPLIANCE = "project_compliance"
    PROPOSAL_COMPLIANCE = "proposal_compliance"
    OFFERING_COMPLIANCE = "offering_compliance"
    PROJECT_METADATA = "project_metadata"
    ONBOARDING_CUSTOMER_DATA = "onboarding_customer"
    ONBOARDING_INTENT_DATA = "onboarding_intent"
    WORKFLOW_STEP = "workflow_step"

    CHOICES = [
        (PROJECT_COMPLIANCE, _("Project compliance")),
        (PROPOSAL_COMPLIANCE, _("Proposal compliance")),
        (OFFERING_COMPLIANCE, _("Offering compliance")),
        (PROJECT_METADATA, _("Project metadata")),
        (ONBOARDING_CUSTOMER_DATA, _("Onboarding customer data")),
        (ONBOARDING_INTENT_DATA, _("Onboarding intent/purpose data")),
        (WORKFLOW_STEP, _("Workflow step evaluation")),
    ]


class QuestionTypes:
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    TEXT_INPUT = "text_input"
    TEXT_AREA = "text_area"
    NUMBER = "number"
    DATE = "date"
    FILE = "file"
    MULTIPLE_FILES = "multiple_files"
    PHONE_NUMBER = "phone_number"
    YEAR = "year"
    EMAIL = "email"
    URL = "url"
    COUNTRY = "country"
    RATING = "rating"
    DATETIME = "datetime"

    CHOICES = [
        (BOOLEAN, _("Yes/No/N/A")),
        (SINGLE_SELECT, _("Single selection")),
        (MULTI_SELECT, _("Multiple selection")),
        (TEXT_INPUT, _("Text input")),
        (TEXT_AREA, _("Text area")),
        (NUMBER, _("Number input")),
        (DATE, _("Date input")),
        (FILE, _("File input")),
        (MULTIPLE_FILES, _("Multiple files input")),
        (PHONE_NUMBER, _("Phone number")),
        (YEAR, _("Year")),
        (EMAIL, _("Email address")),
        (URL, _("URL")),
        (COUNTRY, _("Country")),
        (RATING, _("Rating")),
        (DATETIME, _("Date and time")),
    ]


OPERATORS = [
    ("equals", _("Equals")),
    ("not_equals", _("Not equals")),
    ("contains", _("Contains")),
    ("in", _("In list")),
    ("not_in", _("Not in list")),
]


class DependencyLogicOperators:
    AND = "and"
    OR = "or"

    CHOICES = [
        (AND, _("All conditions must be true (AND)")),
        (OR, _("At least one condition must be true (OR)")),
    ]
