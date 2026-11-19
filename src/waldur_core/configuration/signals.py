from django.dispatch import Signal

# Sent after a setting is written, with ``key``, ``old_value`` and ``new_value``
# as stored: secrets arrive encrypted, so no receiver holds one in clear. A
# write also sends ``changed``, ``source``, ``source_detail`` and ``user``;
# ``override_config`` in tests sends none of them.
setting_updated = Signal()
