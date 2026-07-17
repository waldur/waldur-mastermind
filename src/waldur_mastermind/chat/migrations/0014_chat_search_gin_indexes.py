import django.contrib.postgres.indexes
from django.db import migrations


class Migration(migrations.Migration):
    # Builds the GIN indexes over the search_vector columns added in 0013. Kept
    # in a separate migration so CREATE INDEX runs in its own transaction, after
    # the 0013 backfill has committed. Postgres refuses "CREATE INDEX ... because
    # it has pending trigger events" if the index is built in the same
    # transaction as the backfill's bulk UPDATEs.
    dependencies = [
        ("chat", "0013_anonymouschatinteraction_search_text_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="anonymouschatinteraction",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["search_vector"], name="anon_chat_search_gin"
            ),
        ),
        migrations.AddIndex(
            model_name="message",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["search_vector"], name="chat_msg_search_gin"
            ),
        ),
    ]
