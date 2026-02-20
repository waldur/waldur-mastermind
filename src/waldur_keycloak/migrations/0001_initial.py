"""Placeholder migration preserving compatibility with the old waldur_keycloak app.

The original waldur_keycloak app (Keycloak group sync, WAL-4214) had a
0001_initial migration that created a ProjectGroup model. That app was
removed in WAL-7397, but the django_migrations record remains in
production databases. This empty migration keeps the same number and
dependency so Django's consistency check passes.
"""

from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("structure", "0001_squashed_0036"),
    ]

    operations = []
