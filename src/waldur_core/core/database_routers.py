class ReadOnlyReplicaRouter:
    def db_for_read(self, model, **hints):
        # Router read queries to a read-only replica db for helm.
        return "read_db"

    def db_for_write(self, model, **hints):
        # Use the default waldur db for write queries.
        return "default"

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Make sure migrations not allowed for the replca database.
        return db == "default"
