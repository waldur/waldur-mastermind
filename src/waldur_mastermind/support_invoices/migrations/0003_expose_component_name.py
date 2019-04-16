# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


def expose_component_name(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Offering = apps.get_model('support', 'Offering')
    GenericInvoiceItem = apps.get_model('invoices', 'GenericInvoiceItem')
    PlanComponent = apps.get_model('marketplace', 'PlanComponent')

    content_type = ContentType.objects.get_for_model(Offering)
    for item in GenericInvoiceItem.objects.filter(content_type=content_type):
        if item.details:
            plan_component_id = item.details.get('plan_component_id')
            if not plan_component_id:
                continue
            try:
                plan_component = PlanComponent.objects.get(id=plan_component_id)
            except PlanComponent.DoesNotExist:
                continue
            else:
                item.details['offering_component_name'] = plan_component.component.name
                item.details['plan_name'] = plan_component.plan.name
                item.save(update_fields=['details'])


class Migration(migrations.Migration):

    dependencies = [
        ('support_invoices', '0002_delete_requestbasedoffering'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('support', '0036_offering_ordering'),
        ('invoices', '0030_json_details_on_genericinvoiceitem'),
        ('marketplace', '0078_fix_plan_component_amount'),
    ]

    operations = [
        migrations.RunPython(expose_component_name),
    ]
