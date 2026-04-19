import typer

app = typer.Typer(
    name="abeomem",
    help="MCP server for tips, tricks, and bug fixes Claude Code already paid for once.",
    no_args_is_help=True,
)


def _unimpl(name: str) -> None:
    raise typer.Exit(code=2) from SystemExit(f"abeomem {name}: unimplemented")


@app.command()
def init(
    global_: bool = typer.Option(False, "--global", help="Install globally under ~/.claude/CLAUDE.md"),
) -> None:
    """Setup (injects CLAUDE.md block with markers)."""
    _unimpl("init")


@app.command()
def serve(verbose: bool = typer.Option(False, "--verbose")) -> None:
    """Run MCP server (stdio); watchdog + reconciliation."""
    _unimpl("serve")


@app.command()
def sync(import_new: bool = typer.Option(False, "--import-new")) -> None:
    """Rescan memos dir; reimport changed files."""
    _unimpl("sync")


@app.command()
def backup(out: str = typer.Option(None, "--out")) -> None:
    """Checkpoint WAL + VACUUM INTO timestamped copy."""
    _unimpl("backup")


@app.command()
def ls(
    kind: str = typer.Option(None, "--kind"),
    topic: str = typer.Option(None, "--topic"),
    tag: str = typer.Option(None, "--tag"),
    scope: str = typer.Option(None, "--scope"),
    limit: int = typer.Option(50, "--limit"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    """List memos with filters."""
    _unimpl("ls")


@app.command()
def show(id: int) -> None:
    """Print full memo as markdown to stdout."""
    _unimpl("show")


@app.command()
def edit(id: int) -> None:
    """Open exported .md in $EDITOR (non-blocking)."""
    _unimpl("edit")


@app.command()
def chain(id: int) -> None:
    """Print supersede chain for id (root → tip)."""
    _unimpl("chain")


@app.command()
def archive(id: int, reason: str = typer.Option(None, "--reason")) -> None:
    """Soft-delete; excludes from search."""
    _unimpl("archive")


@app.command()
def topics(min_count: int = typer.Option(1, "--min-count")) -> None:
    """List topics by frequency."""
    _unimpl("topics")


@app.command()
def stats(json_: bool = typer.Option(False, "--json")) -> None:
    """Success metrics (30-day window)."""
    _unimpl("stats")


@app.command()
def scope(show_remote: bool = typer.Option(False, "--show-remote")) -> None:
    """Print current-directory scope."""
    _unimpl("scope")
