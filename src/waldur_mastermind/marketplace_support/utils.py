import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from django.template import Context, Template

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models


logger = logging.getLogger(__name__)


def get_match_states():
    return {
        support_models.Offering.States.REQUESTED: marketplace_models.Resource.States.CREATING,
        support_models.Offering.States.OK: marketplace_models.Resource.States.OK,
        support_models.Offering.States.TERMINATED: marketplace_models.Resource.States.TERMINATED,
    }


def init_offerings_and_resources(category, customer):
    offerings_counter = 0
    plans_counter = 0
    resources_counter = 0

    # Import marketplace offerings
    ct = ContentType.objects.get_for_model(support_models.OfferingTemplate)
    exist_ids = marketplace_models.Offering.objects.filter(content_type=ct).values_list('object_id', flat=True)

    for template in support_models.OfferingTemplate.objects.exclude(id__in=exist_ids):
        marketplace_models.Offering.objects.create(
            scope=template,
            type=PLUGIN_NAME,
            name=template.name,
            customer=customer,
            category=category,
            state=marketplace_models.Offering.States.ACTIVE,
            options={
                'order': template.config.get('order', []),
                'options': template.config.get('options', {})
            },
            description=template.config.get('description', ''),
            full_description=template.config.get('summary', ''),
            terms_of_service=template.config.get('terms_of_service', ''),
        )
        offerings_counter += 1

    # Import marketplace resources
    ct = ContentType.objects.get_for_model(support_models.Offering)
    exist_ids = marketplace_models.Resource.objects.filter(content_type=ct).values_list('object_id', flat=True)

    for support_offering in support_models.Offering.objects.exclude(id__in=exist_ids):
        # get offering
        offering = marketplace_models.Offering.objects.get(scope=support_offering.template, type=PLUGIN_NAME)

        # get plan
        for offering_plan in support_offering.template.plans.all():
            new_plan, create = marketplace_models.Plan.objects.get_or_create(
                scope=offering_plan,
                defaults=dict(
                    offering=offering,
                    name=offering_plan.name,
                    unit_price=offering_plan.unit_price,
                    unit=offering_plan.unit,
                    product_code=offering_plan.product_code,
                    article_code=offering_plan.article_code
                )
            )
            if create:
                plans_counter += 1

        try:
            marketplace_plan = offering.plans.get(unit_price=support_offering.unit_price, unit=support_offering.unit)
        except marketplace_models.Plan.DoesNotExist:
            marketplace_plan = marketplace_models.Plan.objects.create(
                scope=support_offering,
                offering=offering,
                name=support_offering.name,
                unit_price=support_offering.unit_price,
                unit=support_offering.unit,
                product_code=support_offering.product_code,
                article_code=support_offering.article_code,
            )
            plans_counter += 1

        # create resource
        resource = marketplace_models.Resource.objects.create(
            name=support_offering.name,
            created=support_offering.created,
            project=support_offering.project,
            offering=offering,
            plan=marketplace_plan,
            scope=support_offering,
            state=get_match_states()[support_offering.state],
            attributes={'summary': support_offering.issue.summary,
                        'description': support_offering.issue.description,
                        'name': support_offering.name}
        )
        if marketplace_plan:
            marketplace_models.ResourcePlanPeriod.objects.create(
                resource=resource,
                plan=marketplace_plan,
                start=support_offering.created,
                end=support_offering.terminated_at,
            )
        resources_counter += 1

    return offerings_counter, plans_counter, resources_counter


def get_issue_resource(issue):
    """
    This utility method is called from support backend create_issue method
    to traverse from support issue to marketplace resource.

    1) When marketplace resource is created, we need to fetch
    related support offering and then marketplace resource:

    Support Issue <- Support Offering <- Marketplace Resource

    2) When marketplace resource plan is switched, we need to traverse
    from support issue to marketplace order item and then marketplace resource:

    Support issue -> Marketplace Order Item -> Marketplace resource
    """

    if isinstance(issue.resource, marketplace_models.OrderItem):
        return issue.resource.resource
    try:
        offering = support_models.Offering.objects.get(issue=issue)
        return marketplace_models.Resource.objects.get(scope=offering)
    except (ObjectDoesNotExist, MultipleObjectsReturned):
        logger.debug('Resource for issue is not found. Issue ID: %s', issue.id)
        return None


def get_request_link(request):
    link_template = settings.WALDUR_MARKETPLACE_SUPPORT['REQUEST_LINK_TEMPLATE']
    return link_template.format(request_uuid=request.uuid)


def format_description(template_name, context):
    template = Template(settings.WALDUR_MARKETPLACE_SUPPORT[template_name])
    return template.render(Context(context, autoescape=False))
