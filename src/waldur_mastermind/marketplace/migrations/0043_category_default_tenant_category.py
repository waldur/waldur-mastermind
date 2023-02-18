from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0042_default_openstack_category_flags'),
    ]

    operations = [
        migrations.AddField(
            model_name='category',
            name='default_tenant_category',
            field=models.BooleanField(
                default=False,
                help_text='Set to true if this category is for OpenStack Tenant. Only one category can have "true" value.',
            ),
        ),
    ]
