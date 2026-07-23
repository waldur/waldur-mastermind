from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "onboarding",
            "0007_alter_onboardingverification_country_and_more",
        ),
    ]

    operations = [
        migrations.AlterField(
            model_name="onboardingverification",
            name="validation_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ariregister", "Estonian Business Register (ariregister)"),
                    (
                        "wirtschaftscompass",
                        "Austrian Business Register (WirtschaftsCompass)",
                    ),
                    ("bolagsverket", "Swedish Business Register (Bolagsverket)"),
                    ("breg", "Norwegian Business Register (Brreg)"),
                    ("dnb_se", "Dun & Bradstreet Sweden"),
                    ("dnb_no", "Dun & Bradstreet Norway"),
                    ("dnb_dk", "Dun & Bradstreet Denmark"),
                    ("dnb_fi", "Dun & Bradstreet Finland"),
                ],
                help_text="Method used for validation",
                max_length=50,
            ),
        ),
    ]
