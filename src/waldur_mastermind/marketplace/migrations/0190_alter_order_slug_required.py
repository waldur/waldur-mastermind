from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0189_populate_order_slug"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="slug",
            field=models.SlugField(),
        ),
    ]
