Development guidelines
======================

1. Follow `PEP8 <http://python.org/dev/peps/pep-0008/>`_, except:

  - Limit all lines to a maximum of 119 characters

2. Use `git flow <https://github.com/nvie/gitflow>`_
3. Write docstrings


Flow for feature tasks
----------------------

- Create a new branch from develop

.. code-block:: bash

    git checkout develop
    git pull origin develop
    git checkout -b feature/task-id

- Perform brilliant work (don't forget about tests!)
- Update CHANGELOG.rst
- Verify that tests are passing.
- Push all changes to origin (http://code.opennodecloud.com)
- Create a Merge Request and assign it to a reviewer. Make sure that MR can be merged automatically. If not, resolve
   the conflicts by merging develop branch into yours:

.. code-block:: bash

    git checkout feature/task-id
    git pull origin develop

- If MR consists of several commits, please squash them all to the single commit using
  `interactive rebase <https://git-scm.com/docs/git-rebase#_interactive_mode>`_.

- Resolve ticket in JIRA.


Flow for hot fixes
------------------

- TODO
