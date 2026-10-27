import collections
import uuid

from django.contrib.contenttypes.models import ContentType
from django.db.models import F, Q
from django.utils import timezone

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource

from . import models


def get_user_customer_ids(user) -> set[int]:
    """Organisations the user holds a role in, directly or through a project."""
    project_customer_ids = structure_models.Project.available_objects.filter(
        id__in=get_connected_projects(user)
    ).values_list("customer_id", flat=True)
    return set(get_connected_customers(user)) | set(project_customer_ids)


def _audience_uuids(items) -> list[str]:
    """UUIDs of audience entries, tolerating plain strings from a hand-edited audience."""
    uuids = []
    for item in items or []:
        value = item.get("uuid") if isinstance(item, dict) else item
        try:
            uuids.append(uuid.UUID(str(value)).hex)
        except ValueError:
            continue
    return uuids


def get_audience_customers(user, audience, customer_ids=None):
    """The user's organisations that put them in the audience, or None if they are outside it.

    Same audience shape as broadcasts, but a narrower rule when both lists are
    set: broadcasts reach everyone in the listed organisations plus the
    offerings' consumers, while a survey reaches only the consumers of the
    listed offerings within the listed organisations.
    """
    if not isinstance(audience, dict):
        return None
    if customer_ids is None:
        customer_ids = get_user_customer_ids(user)
    user_customers = structure_models.Customer.objects.filter(id__in=customer_ids)
    if audience.get("all_users"):
        return user_customers

    customer_uuids = _audience_uuids(audience.get("customers"))
    offering_uuids = _audience_uuids(audience.get("offerings"))

    if offering_uuids:
        resources = Resource.objects.filter(
            Q(offering__uuid__in=offering_uuids)
            | Q(offering__parent__uuid__in=offering_uuids),
            project__customer_id__in=customer_ids,
        ).exclude(state=ResourceStates.TERMINATED)
        if customer_uuids:
            resources = resources.filter(project__customer__uuid__in=customer_uuids)
        matched_ids = set(resources.values_list("project__customer_id", flat=True))
    else:
        matched_ids = set(
            user_customers.filter(uuid__in=customer_uuids).values_list("id", flat=True)
        )

    if not matched_ids:
        return None
    return structure_models.Customer.objects.filter(id__in=matched_ids)


def can_be_prompted(request: models.FeedbackRequest, now=None) -> bool:
    now = now or timezone.now()
    return (
        request.state == models.FeedbackRequest.States.PENDING
        and request.prompt_count < request.survey.max_prompts
        and (request.snoozed_until is None or request.snoozed_until <= now)
    )


def get_or_create_request(survey, user, customers):
    request, created = models.FeedbackRequest.objects.get_or_create(
        survey=survey,
        user=user,
        defaults={
            "organization_names": sorted(customers.values_list("name", flat=True)),
            "organization_uuids": sorted(
                customer_uuid.hex
                for customer_uuid in customers.values_list("uuid", flat=True)
            ),
        },
    )
    if created:
        checklist_models.ChecklistCompletion.objects.create(
            checklist=survey.checklist,
            scope_content_type=ContentType.objects.get_for_model(request),
            scope_object_id=request.id,
        )
    return request


def is_answered(completion) -> bool:
    """Whether every required question the respondent can see has an answer.

    completion.is_completed also counts required questions hidden by their
    dependencies, which a respondent cannot answer.
    """
    answered_ids = set(completion.answers.values_list("question_id", flat=True))
    return bool(answered_ids) and all(
        question.id in answered_ids
        for question in completion.checklist.get_visible_questions(completion)
        if question.required
    )


def get_prompt(user):
    """Return the request to show the user now, counting it as a prompt, or None.

    Showing it also snoozes it, so other tabs and repeated calls don't use up
    the prompts. If several surveys are eligible, the one ending soonest wins.
    """
    if user.is_staff or user.is_support:
        return None

    now = timezone.now()
    surveys = models.FeedbackSurvey.objects.filter(
        is_active=True, starts_at__lte=now, ends_at__gte=now
    ).order_by("ends_at", "id")

    customer_ids = None
    for survey in surveys:
        existing = models.FeedbackRequest.objects.filter(
            survey=survey, user=user
        ).first()
        if existing and not can_be_prompted(existing, now):
            continue
        if customer_ids is None:
            customer_ids = get_user_customer_ids(user)
        customers = get_audience_customers(user, survey.audience, customer_ids)
        if customers is None:
            continue
        request = existing or get_or_create_request(survey, user, customers)
        # A conditional update, so concurrent calls can't both count the same prompt.
        updated = (
            models.FeedbackRequest.objects.filter(
                pk=request.pk,
                state=models.FeedbackRequest.States.PENDING,
                prompt_count__lt=survey.max_prompts,
            )
            .filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now))
            .update(
                prompt_count=F("prompt_count") + 1,
                snoozed_until=now + timezone.timedelta(days=survey.snooze_days),
                modified=now,
            )
        )
        if updated:
            request.refresh_from_db()
            return request
    return None


def expire_requests():
    """Close pending requests that can no longer be answered."""
    now = timezone.now()
    return models.FeedbackRequest.objects.filter(
        Q(survey__ends_at__lt=now)
        | Q(prompt_count__gte=F("survey__max_prompts"), snoozed_until__lte=now),
        state=models.FeedbackRequest.States.PENDING,
    ).update(state=models.FeedbackRequest.States.EXPIRED, modified=now)


def _normalize_uuid(value) -> str:
    try:
        return uuid.UUID(str(value)).hex
    except ValueError:
        return str(value)


def _sort_key(value: str):
    try:
        return (0, float(value), value)
    except ValueError:
        return (1, 0, value)


def summarize_question(question, answer_values):
    """Per-question result: mean for ratings, a distribution for scales and selects."""
    result = {
        "question_uuid": question.uuid.hex,
        "question": question.description,
        "question_type": question.question_type,
        "answer_count": len(answer_values),
        "mean": None,
        "distribution": [],
    }
    question_type = question.question_type
    QuestionTypes = checklist_enums.QuestionTypes

    if question_type == QuestionTypes.RATING:
        # answer_data can be stored as 5 or "5".
        numbers = []
        for value in answer_values:
            try:
                numbers.append(float(value))
            except (TypeError, ValueError):
                continue
        if numbers:
            result["mean"] = round(sum(numbers) / len(numbers), 2)
        counts = collections.Counter(
            str(int(number)) if number.is_integer() else str(number)
            for number in numbers
        )
        result["distribution"] = [
            {"value": value, "label": value, "count": counts[value]}
            for value in sorted(counts, key=_sort_key)
        ]

    elif question_type == QuestionTypes.LIKERT:
        # Stored as a 0-based index; reported on a 1-based scale.
        scale_length = (
            question.likert_scale_length or checklist_enums.LikertScaleLengths.FIVE
        )
        counts = collections.Counter()
        for value in answer_values:
            if value == "na":
                counts["na"] += 1
            elif isinstance(value, int) and not isinstance(value, bool):
                counts[str(value + 1)] += 1
        distribution = []
        for point in range(1, scale_length + 1):
            label = str(point)
            if point == 1 and question.likert_low_label:
                label = f"{point} – {question.likert_low_label}"
            elif point == scale_length and question.likert_high_label:
                label = f"{point} – {question.likert_high_label}"
            distribution.append(
                {"value": str(point), "label": label, "count": counts[str(point)]}
            )
        if question.likert_allow_na:
            distribution.append({"value": "na", "label": "N/A", "count": counts["na"]})
        result["distribution"] = distribution

    elif question_type in (QuestionTypes.SINGLE_SELECT, QuestionTypes.MULTI_SELECT):
        counts = collections.Counter()
        for value in answer_values:
            values = value if isinstance(value, list) else [value]
            counts.update({_normalize_uuid(item) for item in values})
        result["distribution"] = [
            {
                "value": option.uuid.hex,
                "label": option.label,
                "count": counts[option.uuid.hex],
            }
            for option in question.question_options.all()
        ]

    return result


def get_survey_summary(survey):
    States = models.FeedbackRequest.States
    requests = survey.requests.all()
    counts = collections.Counter(requests.values_list("state", flat=True))

    answers = checklist_models.Answer.objects.filter(
        completion__scope_content_type=ContentType.objects.get_for_model(
            models.FeedbackRequest
        ),
        # Answered requests only, so the results add up to the "answered" count.
        completion__scope_object_id__in=requests.filter(state=States.ANSWERED).values(
            "id"
        ),
        completion__checklist=survey.checklist,
    ).values_list("question_id", "answer_data")
    values_by_question = collections.defaultdict(list)
    for question_id, answer_data in answers:
        values_by_question[question_id].append(answer_data)

    return {
        "prompted": requests.count(),
        "answered": counts[States.ANSWERED],
        "dismissed": counts[States.DISMISSED],
        "pending": counts[States.PENDING],
        "expired": counts[States.EXPIRED],
        "questions": [
            summarize_question(question, values_by_question[question.id])
            for question in survey.checklist.get_questions()
        ],
    }
