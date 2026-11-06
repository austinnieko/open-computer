import click
import uvicorn


@click.group()
def cli():
    """Your computer, from anywhere."""
    pass


@cli.command()
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host to bind to. Use 0.0.0.0 to allow access from other devices.",
)
@click.option("--port", default=8000, type=int, help="Port to bind to.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload.")
@click.option("--headless", is_flag=True, default=False, help="Don't open browser.")
def run(host: str, port: int, reload: bool, headless: bool):
    """Start the cptr server."""
    import os
    import secrets

    display_host = "localhost" if host == "0.0.0.0" else host

    token = secrets.token_hex(32)
    os.environ["CPTR_STARTUP_TOKEN"] = token
    os.environ["CPTR_PORT"] = str(port)
    url = f"http://{display_host}:{port}/?token={token}"

    print(f"\n  ➜  {url}\n")
    if not headless:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        "cptr.app:application",
        host=host,
        port=port,
        reload=reload,
    )


@cli.command("migrate-paths")
@click.option(
    "--map",
    "maps",
    multiple=True,
    metavar="OLD=NEW",
    help="Path prefix to move, e.g. /home/admin=/home/austin/spark-repos/worktree-backup/home/admin. "
    "Repeatable; the longest matching prefix wins.",
)
@click.option("--apply", "do_apply", is_flag=True, help="Write the changes (after a backup).")
@click.option(
    "--allow-missing",
    is_flag=True,
    help="Also rewrite values whose new path does not exist on this machine.",
)
@click.option("--db", "db_path", default=None, help="Database file (default: the app's).")
@click.option("--report", "report_path", default=None, help="Also write the report as JSON.")
def migrate_paths(maps, do_apply, allow_missing, db_path, report_path):
    """Move stored workspace and worktree paths to a new location.

    Dry run unless --apply. Without --map, lists stored paths that no longer
    exist. Never moves, deletes or prunes files or worktrees on disk.
    """
    import json as _json

    from cptr.env import DB_FILE
    from cptr.utils import path_migration

    try:
        mappings = [path_migration.Mapping.parse(m) for m in maps]
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--map")
    db = db_path or str(DB_FILE)
    if do_apply and not mappings:
        raise click.UsageError("--apply needs at least one --map")
    if do_apply:
        report = path_migration.apply(db, mappings, allow_missing=allow_missing)
    else:
        report = path_migration.plan(db, mappings)
    click.echo(report.summary(allow_missing=allow_missing))
    if report_path:
        with open(report_path, "w") as fh:
            _json.dump(report.to_dict(), fh, indent=2)
        click.echo(f"\nReport: {report_path}")


def main():
    cli()


if __name__ == "__main__":
    main()
