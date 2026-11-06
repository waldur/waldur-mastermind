"""Date the reviews submitted before their submission time was recorded.

Each takes the time its submission was logged, the first such event on its
feed, or else its last change, which for a submitted review is its
submission or later. Reviews that have a submission time keep it, so running
this again changes nothing.
"""

from django.db import migrations
from django.db.models import F, OuterRef, Subquery
from django.db.models.functions import Coalesce

BATCH_SIZE = 1000
REVIEW_SUBMITTED = "review_submitted"


def backfill_submitted_at(apps, schema_editor):
    Review = apps.get_model("proposal", "Review")
    Event = apps.get_model("logging", "Event")
    ContentType = apps.get_model("contenttypes", "ContentType")
    undated = Review.objects.filter(state="submitted", submitted_at__isnull=True)
    review_type = ContentType.objects.filter(
        app_label="proposal", model="review"
    ).first()
    if review_type is None:
        submitted_at = F("modified")
    else:
        logged = (
            Event.objects.filter(
                event_type=REVIEW_SUBMITTED,
                feed__content_type=review_type,
                feed__object_id=OuterRef("pk"),
            )
            .order_by("created")
            .values("created")[:1]
        )
        submitted_at = Coalesce(Subquery(logged), F("modified"))
    ids = list(undated.order_by("pk").values_list("pk", flat=True))
    for start in range(0, len(ids), BATCH_SIZE):
        Review.objects.filter(
            pk__in=ids[start : start + BATCH_SIZE], submitted_at__isnull=True
        ).update(submitted_at=submitted_at)


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0096_review_submitted_at"),
        ("logging", "0031_eventconsumer_ephemeral"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill_submitted_at, migrations.RunPython.noop),
    ]
