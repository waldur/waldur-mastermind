import string

import requests

from . import models


def find_versions(queried_library_name, python_version):
    lib_info = requests.get("https://pypi.python.org/pypi/%s/json" % queried_library_name).json()

    wrong_os_versions = ["win", "mac"]
    python_versions = build_version_criteria(python_version)
    version_upload_date_pairs = []

    for release_version, release_info in lib_info['releases'].viewitems():
        for platform_version in release_info:
            if not contains_any_string(platform_version['filename'], wrong_os_versions) and\
                    (contains_any_string(platform_version['python_version'], python_versions) or
                     version_not_specified(platform_version['filename'])):
                version_upload_date_pairs.append(dict(version=release_version, upload_date=platform_version['upload_time']))
                break

    version_upload_date_pairs.sort(reverse=True, key=lambda pair: pair['upload_date'])
    return map(lambda pair: pair['version'], version_upload_date_pairs)


def build_version_criteria(python_version):
    if python_version != '3':
        required_version = python_version[0:find_nth(python_version, '.', 2)]
        required_version_without_dots = string.replace(required_version, '.', '').strip()
        return [required_version, required_version_without_dots]
    else:
        return [python_version]


def find_nth(target_string, substring, n):
    start = target_string.find(substring)
    while start >= 0 and n > 1:
        start = target_string.find(substring, start + len(substring))
        n -= 1
    return start


def version_not_specified(python_version):
    return 'cp' not in python_version and 'py' not in python_version


def contains_any_string(target_string, strings):
    for item in strings:
        if item in target_string:
            return True
    return False


def autocomplete_library_name(queried_library_name):
    return models.CachedRepositoryPythonLibrary.objects.filter(name__startswith=queried_library_name).order_by('name')[0:30]
