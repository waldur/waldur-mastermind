def volume_type_name_to_quota_name(volume_type_name):
    return f"gigabytes_{volume_type_name}"


def is_valid_volume_type_name(name):
    return name.startswith("gigabytes_")
