from django.db import migrations
from django.utils import timezone

# Literal values, as a migration must not follow later renames.
ACCEPTED = "accepted"
PENDING = "pending"
EXPIRED = "expired"
DRAFT = "draft"
SENT = "sent"
RESPONDED = "responded"
CANCELLED = "cancelled"
REASON = "The reviewer had left the call's reviewer pool."


def close_assignments_of_departed_reviewers(apps, schema_editor):
    """Close the pending assignments of pool entries that are no longer
    accepted, as leaving the pool now does: entries declined when the call's
    reviewer role was revoked used to keep them, and re-accepting such an
    entry would have reopened them."""
    AssignmentItem = apps.get_model("proposal", "AssignmentItem")
    AssignmentBatch = apps.get_model("proposal", "AssignmentBatch")
    items = AssignmentItem.objects.filter(status=PENDING).exclude(
        batch__reviewer_pool_entry__invitation_status=ACCEPTED
    )
    items = items.exclude(batch__status=CANCELLED)
    batch_ids = set(items.values_list("batch_id", flat=True))
    if not batch_ids:
        return
    items.update(status=EXPIRED, decline_reason=REASON)
    done = AssignmentBatch.objects.filter(pk__in=batch_ids).exclude(
        items__status=PENDING
    )
    done.filter(status=SENT).update(status=RESPONDED, responded_at=timezone.now())
    done.filter(status=DRAFT).update(status=CANCELLED)


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0101_callworkflowstep_automatic_decision_guard"),
    ]

    operations = [
        migrations.RunPython(
            close_assignments_of_departed_reviewers,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
