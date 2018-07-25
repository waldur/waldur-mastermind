TIME_SUFFIXES = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
    'w': 604800,
}


def parse_time(value):
    """
    Convert 1d to 86400 seconds.
    """
    if value.isdigit():
        return int(value)

    if not value[:-1].isdigit():
        raise ValueError('Invalid time value %s' % value)

    for suffix, factor in TIME_SUFFIXES.items():
        if not value.endswith(suffix):
            continue

        stripped = value[:-1]
        if stripped.isdigit():
            return int(stripped) * factor

    raise ValueError('Invalid time value %s' % value)
