# Development guidelines

1. Follow [PEP8](https://python.org/dev/peps/pep-0008/)
2. Use [git flow](https://github.com/nvie/gitflow)
3. Write docstrings

## Flow for feature tasks

- Create a new branch from develop

```bash
  git checkout develop
  git pull origin develop
  git checkout -b feature/task-id
```

- Perform brilliant work (don't forget about tests!)
- Verify that tests are passing.
- Push all changes to origin (https://code.opennodecloud.com)
- Create a Merge Request and assign it to a reviewer. Make sure that MR can be merged automatically. If not, resolve
   the conflicts by merging develop branch into yours:

```bash
  git checkout feature/task-id
  git pull origin develop
```

- Resolve ticket in JIRA.
