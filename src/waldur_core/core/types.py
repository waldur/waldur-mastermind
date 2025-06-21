from waldur_core.permissions.models import Role


class classproperty:
    def __init__(self, func):
        self.fget = func

    def __get__(self, instance, owner) -> Role:
        return self.fget(owner)
