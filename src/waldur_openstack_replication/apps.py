from django.apps import AppConfig
from django.db.models.signals import post_save


class OpenStackReplicationConfig(AppConfig):
    name = "waldur_openstack_replication"

    def ready(self):
        from .handlers import handle_migration_post_save

        Migration = self.get_model("Migration")
        post_save.connect(handle_migration_post_save, sender=Migration)
