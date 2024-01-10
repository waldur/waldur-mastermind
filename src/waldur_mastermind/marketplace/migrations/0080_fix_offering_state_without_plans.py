from django.db import migrations


class States:
    DRAFT = 1
    ACTIVE = 2
    PAUSED = 3
    ARCHIVED = 4


def offering_has_plans(offering):
    return offering.plans.count() or (offering.parent and offering.parent.plans.count())


def fix_offering_state_without_plans(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    for offering in Offering.objects.filter(state=States.ACTIVE, shared=True):
        if offering_has_plans(offering):
            continue
        print(
            f"Switching state of offering with ID {offering.id} from active to paused."
        )
        offering.state = States.PAUSED
        offering.save()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0079_componentusage_modified_by"),
    ]

    operations = [
        migrations.RunPython(fix_offering_state_without_plans),
    ]
