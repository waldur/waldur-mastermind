# The dancing with the function and its deletion is done
# to keep the namespace clean: only __version__ is going to be exposed.

from six import add_move, MovedModule

add_move(MovedModule('mock', 'mock', 'unittest.mock'))


def _get_version(package_name='waldur_mastermind'):
    import pkg_resources

    # Based on http://stackoverflow.com/a/17638236/175349
    # and https://github.com/pwaller/__autoversion__/blob/master/__autoversion__.py

    try:
        return pkg_resources.get_distribution(package_name).version
    except pkg_resources.DistributionNotFound:
        import os.path
        import re
        import subprocess  # nosec

        repo_dir = os.path.join(os.path.dirname(__file__), os.path.pardir)

        try:
            with open(os.devnull, 'w') as DEV_NULL:
                description = subprocess.check_output(   # nosec
                    ['git', 'describe', '--tags', '--dirty=.dirty'],
                    cwd=repo_dir, stderr=DEV_NULL
                ).strip()

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


__version__ = _get_version()
