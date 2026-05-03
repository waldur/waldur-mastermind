from django.db import migrations, models


class Migration(migrations.Migration):
    """Drop choices= from llm_intent_category.

    Django annotation only — no DB-level CHECK constraint exists for
    this column, so the migration is a no-op at the SQL level. Existing
    rows with values like 'compute' / 'storage' / 'software' /
    'consultancy' / 'unclear' stay valid; new judge runs write
    deployment-derived slugs (see judge.build_intent_rubric).
    """

    dependencies = [
        ("chat", "0011_anonymouschatclick_anonymouschatinteraction_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="anonymouschatfeedback",
            name="llm_intent_category",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
