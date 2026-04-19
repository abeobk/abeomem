# abeomem

MCP server that stores and retrieves tips, tricks, and bug fixes Claude Code already paid for once, so it doesn't pay again.

See [design.md](design.md) for the spec (v2.4.2) and [plan.md](plan.md) for the build plan.

## Install (dev)

```bash
pip install -e '.[dev]'
abeomem --help
```

## Stage 1 goal

CC searches before it debugs, saves what it learns, and surfaces saved lessons across repos and sessions.
