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


def patch_fsm_field_mixin(cls):
    from django_fsm import TransitionNotAllowed, pre_transition, post_transition

    def change_state(self, instance, method, *args, **kwargs):
        meta = method._django_fsm
        method_name = method.__name__
        current_state = self.get_state(instance)
        try:
            current_state_name = filter(lambda x: x[0] == current_state, meta.field.choices)[0][1]
        except Exception:
            current_state_name = current_state

        if not meta.has_transition(current_state):
            raise TransitionNotAllowed(
                "Can't switch from state '{0}' using method '{1}'".format(current_state_name, method_name),
                object=instance, method=method)
        if not meta.conditions_met(instance, current_state):
            raise TransitionNotAllowed(
                "Transition conditions have not been met for method '{0}'".format(method_name),
                object=instance, method=method)

        next_state = meta.next_state(current_state)

        signal_kwargs = {
            'sender': instance.__class__,
            'instance': instance,
            'name': method_name,
            'source': current_state,
            'target': next_state
        }

        pre_transition.send(**signal_kwargs)

        try:
            result = method(instance, *args, **kwargs)
            if next_state is not None:
                self.set_proxy(instance, next_state)
                self.set_state(instance, next_state)
        except Exception as exc:
            exception_state = meta.exception_state(current_state)
            if exception_state:
                self.set_proxy(instance, exception_state)
                self.set_state(instance, exception_state)
                signal_kwargs['target'] = exception_state
                signal_kwargs['exception'] = exc
                post_transition.send(**signal_kwargs)
            raise
        else:
            post_transition.send(**signal_kwargs)

        return result

    cls.change_state = change_state


def monkey_patch_fields():
    from django_fsm import FSMFieldDescriptor, FSMFieldMixin
    patch_field_descriptor(FSMFieldDescriptor)
    patch_fsm_field_mixin(FSMFieldMixin)
