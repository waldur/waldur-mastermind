import logging

from django.db import transaction
from django.template import Context, Template
from django.template.loader import get_template

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.marketplace_support import utils as marketplace_support_utils

from . import tasks

logger = logging.getLogger(__name__)


ItemTypes = marketplace_models.Order.Types


RESOURCE_CALLBACKS = {
    (ItemTypes.CREATE, True): callbacks.resource_creation_succeeded,
    (ItemTypes.CREATE, False): callbacks.resource_creation_canceled,
    (ItemTypes.UPDATE, True): callbacks.resource_update_succeeded,
    (ItemTypes.UPDATE, False): callbacks.resource_update_failed,
    (ItemTypes.TERMINATE, True): callbacks.resource_deletion_succeeded,
    (ItemTypes.TERMINATE, False): callbacks.resource_deletion_failed,
}


def update_order_if_issue_was_complete(sender, instance, created=False, **kwargs):
    if created:
        return

    issue = instance

    if not issue.tracker.has_changed("status"):
        return

    if not (
        issue.resource
        and isinstance(issue.resource, marketplace_models.Order)
        and issue.resource.offering.type == PLUGIN_NAME
        and issue.resolved is not None
    ):
        return

    callback = RESOURCE_CALLBACKS[(issue.resource.type, issue.resolved)]
    callback(issue.resource.resource)


def notify_about_request_based_item_creation(sender, instance, created=False, **kwargs):
    if created:
        return

    issue = instance

    if not issue.tracker.has_changed("backend_id"):
        return

    if not (
        issue.resource
        and isinstance(issue.resource, marketplace_models.Order)
        and issue.resource.offering.type == PLUGIN_NAME
        and issue.resource.type == ItemTypes.CREATE
    ):
        return

    order = issue.resource
    service_provider = getattr(order.offering.customer, "serviceprovider", None)

    if not service_provider:
        logger.warning(
            "Customer providing an Offering is not registered as a Service Provider."
        )
        return

    if not service_provider.lead_email:
        return

    attributes_with_display_names = {}

    for attribute_key, attribute_value in order.attributes.items():
        if attribute_key in order.offering.options["options"].keys():
            display_name = order.offering.options["options"][attribute_key]["label"]
            attributes_with_display_names[display_name] = attribute_value
            continue

        attributes_with_display_names[attribute_key] = attribute_value

    setattr(order, "attributes_with_display_names", attributes_with_display_names)

    context = Context({"order": order, "issue": issue}, autoescape=False)
    template = Template(service_provider.lead_body)
    message = template.render(context).strip()
    template = Template(service_provider.lead_subject)
    subject = template.render(context).strip()

    transaction.on_commit(
        lambda: tasks.send_mail_notification.delay(
            subject, message, service_provider.lead_email
        )
    )


def _create_issue_if_membership_changed(instance, summary):
    user_role = instance

    if not isinstance(user_role.scope, structure_models.Project):
        return

    project = user_role.scope
    resources = marketplace_models.Resource.objects.exclude(
        state=marketplace_models.Resource.States.TERMINATED
    ).filter(
        project=project,
        offering__type=PLUGIN_NAME,
        offering__plugin_options__enable_issues_for_membership_changes=True,
    )

    if resources.exists():
        offering_ids = resources.values_list("offering_id", flat=True).distinct()
        offerings = marketplace_models.Offering.objects.filter(id__in=offering_ids)

        for offering in offerings:
            resources = marketplace_models.Resource.objects.filter(
                offering=offering, project=project
            )
            setattr(offering, "resources", resources)
            offering_user = offering.offeringuser_set.filter(
                user=user_role.user
            ).first()
            setattr(offering, "offering_user", offering_user)

        template = get_template(
            "marketplace_support/create_project_membership_update_issue.txt"
        ).template
        description = template.render(
            Context(
                {
                    "offerings": offerings,
                    "project": project,
                    "user": user_role.user,
                    "project_url": core_utils.format_homeport_link(
                        "projects/{project_uuid}/", project_uuid=project.uuid.hex
                    ),
                },
                autoescape=False,
            )
        )
        marketplace_support_utils.create_issue_about_project_team_changes(
            project,
            created_by=user_role.user,
            summary=summary.format(
                user=user_role.user.full_name,
                project=project.name,
                organization=project.customer.get_display_name(),
            ),
            description=description,
        )


def create_issue_if_membership_changed(sender, instance, created=False, **kwargs):
    if created and not instance.is_active:
        return

    if not isinstance(instance.scope, structure_models.Project):
        return

    if not instance.tracker.has_changed("is_active"):
        return

    if instance.is_active:
        _create_issue_if_membership_changed(
            instance, "{organization}: User {user} has been added to project {project}."
        )
    else:
        _create_issue_if_membership_changed(
            instance,
            "{organization}: User {user} has been removed from project {project}.",
        )
