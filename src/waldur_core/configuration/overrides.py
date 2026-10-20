"""Values that ``override_config`` puts in front of the stored ones.

Layers stack: the innermost override wins. A write to an overridden key ends
the override of that key, so code under test that sets a setting reads back
what it stored, as it would without the override.
"""

MISSING = object()

_layers = []


def push(values):
    layer = dict(values)
    _layers.append(layer)
    return layer


def pop(layer):
    _layers.remove(layer)


def lookup(key):
    for layer in reversed(_layers):
        if key in layer:
            return layer[key]
    return MISSING


def release(key):
    for layer in _layers:
        layer.pop(key, None)
