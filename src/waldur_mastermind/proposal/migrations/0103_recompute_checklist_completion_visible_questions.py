"""Recompute checklist completions once more after ``0094``.

``checklist.0018`` and ``0094`` both follow ``checklist.0017``, so an upgrade
can run ``0094`` second, and it sets ``is_completed`` with the rule that
counts hidden questions. Running the recompute again is harmless.
"""

from importlib import import_module

from django.db import migrations

recompute = import_module(
    "waldur_core.checklist.migrations.0018_recompute_completion_visible_questions"
).recompute_completion


class Migration(migrations.Migration):
    dependencies = [
        ("checklist", "0018_recompute_completion_visible_questions"),
        ("proposal", "0102_close_assignments_of_departed_reviewers"),
    ]

    operations = [
        migrations.RunPython(recompute, migrations.RunPython.noop),
    ]
