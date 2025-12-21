from django.db import models
from django.utils.translation import gettext_lazy as _


class ChecklistTypes(models.TextChoices):
    PROJECT_COMPLIANCE = "project_compliance", _("Project compliance")
    PROPOSAL_COMPLIANCE = "proposal_compliance", _("Proposal compliance")
    OFFERING_COMPLIANCE = "offering_compliance", _("Offering compliance")
    PROJECT_METADATA = "project_metadata", _("Project metadata")
    CUSTOMER_ONBOARDING = "customer_onboarding", _("Customer onboarding")


class QuestionTypes(models.TextChoices):
    BOOLEAN = "boolean", _("Yes/No/N/A")
    SINGLE_SELECT = "single_select", _("Single selection")
    MULTI_SELECT = "multi_select", _("Multiple selection")
    TEXT_INPUT = "text_input", _("Text input")
    TEXT_AREA = "text_area", _("Text area")
    NUMBER = "number", _("Number input")
    DATE = "date", _("Date input")
    FILE = "file", _("File input")
    MULTIPLE_FILES = "multiple_files", _("Multiple files input")


class Operators(models.TextChoices):
    """Comparison operators for conditional logic and review triggers."""

    EQUALS = "equals", _("Equals")
    NOT_EQUALS = "not_equals", _("Not equals")
    CONTAINS = "contains", _("Contains")
    IN = "in", _("In list")
    NOT_IN = "not_in", _("Not in list")


class DependencyLogicOperators(models.TextChoices):
    """Logic operators for combining multiple dependencies."""

    AND = "and", _("All conditions must be true (AND)")
    OR = "or", _("At least one condition must be true (OR)")
