def _get_version(package_name='waldur_mastermind'):
    from importlib.metadata import version

    package_version = version(package_name)
    if package_version == '0.0.0':
        import os.path
        import re
        import subprocess  # noqa: S404

        repo_dir = os.path.join(os.path.dirname(__file__), os.path.pardir)

        try:
            with open(os.devnull, 'w') as DEV_NULL:
                description = (
                    subprocess.check_output(  # noqa: S603, S607
                        ['git', 'describe', '--tags', '--dirty=.dirty'],
                        cwd=repo_dir,
                        stderr=DEV_NULL,
                    )
                    .strip()
                    .decode("utf-8")
                )

            v = re.search(r'-[0-9]+-', description)
            if v is not None:
                # Replace -n- with -branchname-n-
                # branch = r"-{0}-\1-".format(cls.get_branch(path))
                description, _ = re.subn('-([0-9]+)-', r'+\1.', description, 1)

            if description[0] == 'v':
                description = description[1:]

            return description
        except (OSError, subprocess.CalledProcessError):
            return 'unknown'
    else:
        return package_version


__version__ = _get_version()
