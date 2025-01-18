import datetime
import decimal
import uuid

from django.core.exceptions import ObjectDoesNotExist


class LoggableMixin:
    """Mixin to serialize model in logs.
    Extends django model or custom class with fields extraction method.
    """

    def get_log_fields(self):
        return ("uuid", "name")

    def _get_log_context(self, entity_name=None):
        context = {}
        for field in self.get_log_fields():
            try:
                if not hasattr(self, field):
                    continue
                value = getattr(self, field)
            except ObjectDoesNotExist:
                # the related object has been deleted
                continue

            if entity_name:
                name = f"{entity_name}_{field}"
            else:
                name = field
            if isinstance(value, uuid.UUID):
                context[name] = value.hex
            elif isinstance(value, LoggableMixin):
                context.update(value._get_log_context(field))
            elif isinstance(value, datetime.date):
                context[name] = value.isoformat()
            elif isinstance(value, decimal.Decimal):
                context[name] = float(value)
            elif isinstance(value, dict):
                context[name] = value
            elif callable(value):
                context[name] = value()
            else:
                context[name] = str(value)

        return context

    @classmethod
    def get_permitted_objects(cls, user):
        return cls.objects.none()
