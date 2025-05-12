from django.db import migrations


def replace_code_block(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    for offering in Offering.objects.all():
        if offering.getting_started:
            offering.getting_started = offering.getting_started.replace(
                "<CodeBlock>", "```"
            ).replace("</CodeBlock>", "```")
            offering.save(update_fields=["getting_started"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0161_robotaccount_description_serviceaccount"),
    ]

    operations = [
        migrations.RunPython(
            replace_code_block,
        ),
    ]
