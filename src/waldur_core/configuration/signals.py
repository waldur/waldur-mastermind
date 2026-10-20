from django.dispatch import Signal

# Sent after a setting is written, with ``key``, ``old_value`` and ``new_value``
# as stored: secrets arrive encrypted, so no receiver holds one in clear.
setting_updated = Signal()
