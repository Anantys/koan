# Hooks

Place `.py` files in this directory to extend Koan's lifecycle with custom logic.

## How it works

At startup, Koan discovers all `.py` files in `instance/hooks/` (files starting with `_` are skipped). Each module must define a `HOOKS` dict mapping event names to handler functions. Handlers receive a single `ctx` dict with event-specific context.

Hooks are **fire-and-forget**: errors are logged to stderr but never block the agent.

## Hook module format

```python
def on_post_mission(ctx):
    """Called after the post-mission pipeline completes."""
    project = ctx["project_name"]
    title = ctx["mission_title"]
    print(f"Mission done: {title} on {project}")

HOOKS = {
    "post_mission": on_post_mission,
}
```

## Available events

| Event | When | Context keys |
|-------|------|-------------|
| `session_start` | After startup completes | `instance_dir`, `koan_root` |
| `session_end` | On shutdown (finally block) | `instance_dir`, `total_runs` |
| `pre_mission` | Before Claude execution | `instance_dir`, `project_name`, `project_path`, `mission_title`, `autonomous_mode`, `run_num` |
| `post_mission` | After post-mission pipeline | `instance_dir`, `project_name`, `project_path`, `exit_code`, `mission_title`, `duration_minutes`, `result` |

## Tips

- Hooks must be fast. For slow operations (HTTP calls), use threading internally.
- Hooks are discovered once at startup. Restart to pick up new hooks.
- Use `.py.example` extension for template files to prevent auto-discovery.
- The `result` dict in `post_mission` is a snapshot copy — modifying it has no effect.
