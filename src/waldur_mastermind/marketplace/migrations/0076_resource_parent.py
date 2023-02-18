import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0075_categoryhelparticle'),
    ]

    operations = [
        migrations.AddField(
            model_name='resource',
            name='parent',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='children',
                to='marketplace.resource',
            ),
        ),
    ]
