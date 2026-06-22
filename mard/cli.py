from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich import box

from .db.database import build_session_factory
from .db.repository import DeviceRepo, DuplicateGroupRepo, FileInstanceRepo
from .hasher import HASH_ALGO
from .scanner import scan_device

app = typer.Typer(
    name="mard",
    help="Media Archive & Dedupe — Phase 1",
    add_completion=False,
)
console = Console()

_DEFAULT_DB = Path.home() / ".mard" / "index.db"


def _get_factory(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return build_session_factory(db_path)


# ── scan ──────────────────────────────────────────────────────────────────────

@app.command()
def scan(
    device: Path = typer.Option(..., help="Mount path of the drive to scan"),
    db: Path = typer.Option(_DEFAULT_DB, help="Path to the index database"),
):
    """Scan a device: index files, compute hashes, detect exact duplicates."""
    if not device.exists() or not device.is_dir():
        console.print(f"[red]Error:[/red] {device} is not a valid directory.")
        raise typer.Exit(1)

    factory = _get_factory(db)

    console.print(f"[bold]mard scan[/bold]  device=[cyan]{device}[/cyan]  db=[cyan]{db}[/cyan]")
    console.print(f"Hash algorithm: [yellow]{HASH_ALGO}[/yellow]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[cyan]{task.completed}[/cyan] files"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Phase 1: walking + quick hash…", total=None)
        counter = {"n": 0}

        def on_progress(processed: int, total: int, path: str) -> None:
            counter["n"] = processed
            progress.update(task, completed=processed, description=f"[dim]{path[-60:]}[/dim]")

        try:
            result = scan_device(device, factory, progress_cb=on_progress)
        except KeyboardInterrupt:
            console.print("\n[yellow]Scan interrupted.[/yellow] Progress saved — re-run to resume.")
            raise typer.Exit(130)

    # ── summary ──────────────────────────────────────────────────────────────
    console.print("\n[bold green]Scan complete.[/bold green]\n")

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column(justify="right", style="bold")
    t.add_row("Total media files seen", str(result.total))
    t.add_row("New files indexed", str(result.new))
    t.add_row("Updated files", str(result.updated))
    t.add_row("Unchanged (skipped)", str(result.skipped))
    t.add_row("Disappeared since last scan", str(result.gone))
    t.add_row("Quick-hash collision candidates", str(result.quick_hash_candidates))
    t.add_row("Content-hashed (full)", str(result.content_hashed))
    t.add_row("[yellow]Exact duplicate groups[/yellow]", f"[yellow]{result.exact_dup_groups}[/yellow]")
    t.add_row("[yellow]Exact duplicate files[/yellow]", f"[yellow]{result.exact_dup_files}[/yellow]")
    console.print(t)

    if result.exact_dup_groups:
        console.print(
            f"\nRun [bold]mard duplicates --exact[/bold] to review duplicate groups."
        )


# ── duplicates ────────────────────────────────────────────────────────────────

@app.command()
def duplicates(
    exact: bool = typer.Option(False, "--exact", help="Show exact (content-hash) duplicates"),
    db: Path = typer.Option(_DEFAULT_DB, help="Path to the index database"),
    limit: int = typer.Option(50, help="Max number of groups to display"),
):
    """Report duplicate file groups detected in the index."""
    if not exact:
        console.print("[yellow]Tip:[/yellow] Use --exact to show content-hash duplicates.")
        raise typer.Exit(0)

    factory = _get_factory(db)

    from .db.database import open_session
    with open_session(factory) as s:
        dup_repo = DuplicateGroupRepo(s)
        fi_repo = FileInstanceRepo(s)
        dev_repo = DeviceRepo(s)

        groups = dup_repo.get_all_exact()

        if not groups:
            console.print("[green]No exact duplicates found.[/green]  Run [bold]mard scan[/bold] first.")
            raise typer.Exit(0)

        total_groups = len(groups)
        groups = groups[:limit]

        _print_exact_duplicates(groups, dup_repo, fi_repo, dev_repo)

        if total_groups > limit:
            console.print(
                f"\n[dim]Showing {limit} of {total_groups} groups. "
                f"Use --limit to see more.[/dim]"
            )
        else:
            console.print(f"\n[bold]{total_groups}[/bold] exact duplicate group(s) total.")

        # Wasted space estimate
        with open_session(factory) as s2:
            fi_repo2 = FileInstanceRepo(s2)
            all_groups = fi_repo2.get_exact_duplicate_groups()
            wasted = sum(
                fi.size * (len(g) - 1)
                for g in all_groups
                for fi in g[:1]
            )
        console.print(f"Estimated wasted space: [red]{_fmt_bytes(wasted)}[/red]")


def _print_exact_duplicates(groups, dup_repo, fi_repo, dev_repo) -> None:
    from .db.database import open_session

    for i, group in enumerate(groups, 1):
        members = dup_repo.get_members(group.id)
        if not members:
            continue

        keep_id = group.recommended_keep_instance_id
        chash = members[0].content_hash or "?"

        console.print(
            f"\n[bold]Group {i}[/bold]  "
            f"hash=[cyan]{chash[:16]}…[/cyan]  "
            f"size=[yellow]{_fmt_bytes(members[0].size)}[/yellow]  "
            f"copies=[red]{len(members)}[/red]"
        )

        t = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 1))
        t.add_column("", width=3)
        t.add_column("Device", style="dim")
        t.add_column("Path")
        t.add_column("Modified", style="dim")

        for fi in members:
            marker = "[green]keep[/green]" if fi.id == keep_id else ""
            device_label = fi.device_id[:8]
            mtime_str = fi.mtime.strftime("%Y-%m-%d %H:%M") if fi.mtime else "?"
            t.add_row(marker, device_label, fi.path, mtime_str)

        console.print(t)


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


if __name__ == "__main__":
    app()
