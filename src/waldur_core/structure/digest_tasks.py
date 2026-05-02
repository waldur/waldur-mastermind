import logging
from collections import defaultdict
from datetime import timedelta

from celery import shared_task
from constance import config as constance_config
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from waldur_core.core.utils import chunked_queryset

logger = logging.getLogger(__name__)


def _get_enabled_providers(config):
    from waldur_core.structure.digest_providers import (
        get_available_providers,
        get_provider,
    )

    if config.enabled_sections:
        providers = []
        for key in config.enabled_sections:
            p = get_provider(key)
            if p and p.is_available():
                providers.append(p)
        return sorted(providers, key=lambda p: p.order)
    return get_available_providers()


def _get_period(config):
    now = timezone.now()
    if config.frequency == "weekly":
        period_start = (now - timedelta(days=7)).date()
    elif config.frequency == "biweekly":
        period_start = (now - timedelta(days=14)).date()
    else:
        period_start = (now - timedelta(days=30)).date()
    period_end = now.date()
    return period_start, period_end


def _format_period(period_start, period_end):
    return f"{period_start.strftime('%Y-%m-%d')} - {period_end.strftime('%Y-%m-%d')}"


def _is_due_to_send(config):
    today = timezone.now().date()
    if config.last_sent_at and config.last_sent_at.date() == today:
        return False

    if config.frequency == "weekly":
        # Python weekday: 0=Monday..6=Sunday, but our field: 0=Sunday..6=Saturday
        py_weekday = today.weekday()
        config_weekday = (config.day_of_week - 1) % 7  # convert to Python weekday
        return py_weekday == config_weekday
    elif config.frequency == "biweekly":
        py_weekday = today.weekday()
        config_weekday = (config.day_of_week - 1) % 7
        if py_weekday != config_weekday:
            return False
        if config.last_sent_at is None:
            return True
        return (today - config.last_sent_at.date()).days >= 13
    elif config.frequency == "monthly":
        return today.day == config.day_of_month
    return False


def _gather_project_sections(project, providers, period_start, period_end):
    """Gather section data for a single project from all providers."""
    sections = []
    for provider in providers:
        try:
            section_data = provider.get_section_data(project, period_start, period_end)
            if section_data:
                sections.append(section_data)
        except Exception:
            logger.exception(
                "Error gathering digest data from provider %s for project %s",
                provider.section_key,
                project.uuid,
            )
    return sections


def _render_sections_for_language(sections, lang):
    """Render section templates in the given language."""
    rendered = []
    with translation.override(lang):
        for section_data in sections:
            rendered.append(
                {
                    "title": _(section_data.title_key),
                    "text_content": render_to_string(
                        section_data.text_template, section_data.context
                    ),
                    "html_content": render_to_string(
                        section_data.html_template, section_data.context
                    ),
                }
            )
    return rendered


def _render_and_send_digest(
    user, customer, projects_with_sections, period_start, period_end
):
    """Render and send digest email for a single user."""
    lang = getattr(user, "preferred_language", None) or settings.LANGUAGE_CODE

    with translation.override(lang):
        rendered_projects = []
        for entry in projects_with_sections:
            project = entry["project"]
            rendered_sections = _render_sections_for_language(entry["sections"], lang)
            rendered_projects.append(
                {
                    "name": project.name,
                    "sections": rendered_sections,
                }
            )

        context = {
            "user": user,
            "organization_name": customer.name,
            "period_label": _format_period(period_start, period_end),
            "projects": rendered_projects,
        }
        subject = render_to_string(
            "structure/project_digest_subject.txt", context
        ).strip()
        text_body = render_to_string("structure/project_digest_message.txt", context)
        html_body = render_to_string("structure/project_digest_message.html", context)

    from waldur_core.core.utils import send_mail

    send_mail(subject, text_body, [user.email], html_message=html_body)


@shared_task(name="waldur_core.structure.send_project_digest_notifications")
def send_project_digest_notifications():
    """
    Daily task. For each org with an enabled digest config,
    check if it's time to send based on frequency + last_sent_at,
    then dispatch per-customer subtasks.
    """
    from waldur_core.structure.models import ProjectDigestConfiguration

    if not constance_config.ENABLE_PROJECT_DIGEST:
        logger.info("Project digest notifications are disabled.")
        return 0

    configs = ProjectDigestConfiguration.objects.filter(
        is_enabled=True,
        customer__archived=False,
        customer__blocked=False,
    ).select_related("customer")

    count = 0
    for config in chunked_queryset(configs, chunk_size=50, max_records=10_000):
        if _is_due_to_send(config):
            send_digest_for_customer.delay(config.pk)
            count += 1

    logger.info("Dispatched project digest for %d organizations", count)
    return count


@shared_task
def send_digest_for_customer(config_pk):
    from waldur_core.core.models import User
    from waldur_core.structure.models import ProjectDigestConfiguration

    config = ProjectDigestConfiguration.objects.select_related("customer").get(
        pk=config_pk
    )
    customer = config.customer
    providers = _get_enabled_providers(config)
    period_start, period_end = _get_period(config)

    if not providers:
        logger.info("No available providers for customer %s", customer.uuid)
        return

    # Phase 1: Gather structured data (language-independent)
    projects = customer.projects.filter(is_removed=False)
    project_data = []
    for project in chunked_queryset(projects, chunk_size=50, max_records=10_000):
        sections = _gather_project_sections(
            project, providers, period_start, period_end
        )
        if sections:
            project_data.append({"project": project, "sections": sections})

    if not project_data:
        logger.info("No digest data for customer %s", customer.uuid)
        return

    # Phase 2: Map users to their projects
    user_projects = defaultdict(list)
    for entry in project_data:
        project = entry["project"]
        members = project.get_users()
        for user in members:
            if user.email and user.notifications_enabled:
                user_projects[user.pk].append(entry)

    # Phase 3: Send per user
    for user_pk, user_proj_data in user_projects.items():
        try:
            user = User.objects.get(pk=user_pk)
            _render_and_send_digest(
                user, customer, user_proj_data, period_start, period_end
            )
        except Exception:
            logger.exception(
                "Error sending digest to user %s for customer %s",
                user_pk,
                customer.uuid,
            )

    config.last_sent_at = timezone.now()
    config.save(update_fields=["last_sent_at"])
    logger.info("Sent project digest for customer %s", customer.uuid)


def send_digest_for_customer_preview(customer, user):
    """Send a test digest to a specific user (for the send-test action)."""
    from waldur_core.structure.digest_providers import get_available_providers
    from waldur_core.structure.models import ProjectDigestConfiguration

    try:
        config = customer.project_digest_config
    except ProjectDigestConfiguration.DoesNotExist:
        config = None

    if config:
        providers = _get_enabled_providers(config)
    else:
        providers = get_available_providers()

    period_start, period_end = _get_period(
        config
        or type(
            "FakeConfig",
            (),
            {"frequency": "monthly", "enabled_sections": []},
        )()
    )

    projects = customer.projects.filter(is_removed=False)
    project_data = []
    for project in projects:
        sections = _gather_project_sections(
            project, providers, period_start, period_end
        )
        if sections:
            project_data.append({"project": project, "sections": sections})

    if project_data:
        _render_and_send_digest(user, customer, project_data, period_start, period_end)


def render_project_preview(project, customer):
    """Render preview of digest for a single project. Returns dict with subject, html_body, text_body."""
    from waldur_core.structure.digest_providers import get_available_providers
    from waldur_core.structure.models import ProjectDigestConfiguration

    try:
        config = customer.project_digest_config
    except ProjectDigestConfiguration.DoesNotExist:
        config = None

    if config:
        providers = _get_enabled_providers(config)
    else:
        providers = get_available_providers()

    period_end = timezone.now().date()
    period_start = period_end - timedelta(days=30)

    sections = _gather_project_sections(project, providers, period_start, period_end)

    lang = settings.LANGUAGE_CODE
    rendered_sections = _render_sections_for_language(sections, lang)

    context = {
        "organization_name": customer.name,
        "period_label": _format_period(period_start, period_end),
        "projects": [
            {
                "name": project.name,
                "sections": rendered_sections,
            }
        ],
    }

    with translation.override(lang):
        subject = render_to_string(
            "structure/project_digest_subject.txt", context
        ).strip()
        text_body = render_to_string("structure/project_digest_message.txt", context)
        html_body = render_to_string("structure/project_digest_message.html", context)

    return {
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
    }
