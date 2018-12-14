from django.contrib.contenttypes.models import ContentType

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models


def get_match_states():
    return {
        support_models.Offering.States.REQUESTED: marketplace_models.Resource.States.CREATING,
        support_models.Offering.States.OK: marketplace_models.Resource.States.OK,
        support_models.Offering.States.TERMINATED: marketplace_models.Resource.States.TERMINATED,
    }


def init_offerings_and_resources(category, customer):
    ct = ContentType.objects.get_for_model(support_models.Offering)
    exist_ids = [i[0] for i in marketplace_models.Resource.objects.filter(content_type=ct).values_list('object_id')]

    for support_offering in support_models.Offering.objects.exclude(id__in=exist_ids):
        # get offering
        offering, _ = marketplace_models.Offering.objects.get_or_create(
            scope=support_offering.template,
            type=PLUGIN_NAME,
            defaults={
                'name': support_offering.template.name,
                'customer': customer,
                'category': category,
            },
        )

        # get plan
        for offering_plan in support_offering.template.plans.all():
            marketplace_models.Plan.objects.get_or_create(
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

        # create resource
        marketplace_models.Resource.objects.create(
            project=support_offering.project,
            offering=offering,
            plan=marketplace_plan,
            scope=support_offering,
            state=get_match_states()[support_offering.state],
            attributes={'summary': support_offering.issue.summary,
                        'description': support_offering.issue.description,
                        'name': support_offering.name}
        )
