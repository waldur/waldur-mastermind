import django.dispatch

# This signal allows to implement custom quota recalculation
# without introducing circular dependency between core quotas application and plugins.
# It is called when recalculatequotas management command is called.
recalculate_quotas = django.dispatch.Signal()
