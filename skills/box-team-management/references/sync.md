# Sync State

State for tasks is stored in 2 files:

1. TASKS.md
2. TEAM.md

## Workflow

- Check to see the modification times on Box
- Use the most recently modified file as the source of truth for the state of tasks
- If a task has been updated, follow the rules in `references/tasks.md` to update TASKS.md
- Ensure that TEAM.md is updated to reflect the state of active tasks.