"""
TODO: drop patch when django-fsm package is updated.

If model with FSM state field has other fields that access their field value
via a property or a virtual Field, then creation of instances will fail.

There is pending patch in upstream project:
https://github.com/kmmbvnr/django-fsm/pull/171
"""

__all__ = ['monkey_patch_fields']


def subfield_get(self, obj, type=None):
    """
    Verbatim copy from:
    https://github.com/django/django/blob/1.9.13/django/db/models/fields/subclassing.py#L38
    """
    if obj is None:
        return self
    return obj.__dict__[self.field.name]


def get_field_name(self):
    return self.field.name


def patch_field_descriptor(cls):
    cls.__get__ = subfield_get
    setattr(cls, 'field_name', property(get_field_name))


def monkey_patch_fields():
    from django_fsm import FSMFieldDescriptor
    patch_field_descriptor(FSMFieldDescriptor)
