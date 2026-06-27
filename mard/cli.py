from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich import box

from .db.database import build_session_factory, open_session
from .db.repository import DeviceRepo, DuplicateGroupRepo, FileInstanceRepo, MediaAssetRepo
from .hasher import HASH_ALGO
from .meta import run_meta
from .quarantine import QUARANTINE_DIR, run_quarantine
from .scanner import scan_device

app = typer.Typer(
    name="mard",
    help="Media Archive & Dedupe",
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
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write report to file"),
):
    """Report duplicate file groups detected in the index."""
    if not exact:
        console.print("[yellow]Tip:[/yellow] Use --exact to show content-hash duplicates.")
        raise typer.Exit(0)

    factory = _get_factory(db)

    with open_session(factory) as s:
        all_groups = DuplicateGroupRepo(s).get_all_exact()
        if not all_groups:
            console.print("[green]No exact duplicates found.[/green]  Run [bold]mard scan[/bold] first.")
            raise typer.Exit(0)

        dup_repo = DuplicateGroupRepo(s)
        fi_repo = FileInstanceRepo(s)
        dev_repo = DeviceRepo(s)

        # Build device_id → display name map
        device_names = {
            d.id: (d.volume_label or d.mount_hint or d.id[:8])
            for d in dev_repo.get_all()
        }

        # Collect full stats across all groups
        total_groups = len(all_groups)
        total_wasted = 0
        total_dup_files = 0
        group_data = []  # (group, members) for display

        for group in all_groups:
            members = dup_repo.get_members(group.id)
            if not members:
                continue
            copies = len(members)
            size = members[0].size or 0
            wasted = size * (copies - 1)
            total_wasted += wasted
            total_dup_files += copies
            group_data.append((group, members, wasted))

        # Sort by wasted space descending
        group_data.sort(key=lambda x: x[2], reverse=True)

        # ── header summary ────────────────────────────────────────────────────
        n_devices = len(device_names)
        header = (
            f"[bold]Exact Duplicates Report[/bold]  "
            f"[dim]{n_devices} device(s) · "
            f"{total_groups} group(s) · "
            f"{total_dup_files} duplicate files · "
            f"[red]{_fmt_bytes(total_wasted)}[/red][dim] can be freed[/dim]"
        )
        console.print(header)

        # ── per-group detail ──────────────────────────────────────────────────
        shown = group_data[:limit]
        for i, (group, members, wasted) in enumerate(shown, 1):
            keep_id = group.recommended_keep_instance_id
            chash = members[0].content_hash or "?"
            size = members[0].size or 0

            console.print(
                f"\n[bold]Group {i}[/bold]  "
                f"hash=[cyan]{chash[:16]}…[/cyan]  "
                f"size=[yellow]{_fmt_bytes(size)}[/yellow]  "
                f"copies=[red]{len(members)}[/red]  "
                f"frees=[magenta]{_fmt_bytes(wasted)}[/magenta]"
            )

            t = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 1))
            t.add_column("", width=4, no_wrap=True)
            t.add_column("Device", style="cyan", no_wrap=True)
            t.add_column("Path")
            t.add_column("Modified", style="dim", no_wrap=True)

            for fi in members:
                marker = "[green]keep[/green]" if fi.id == keep_id else ""
                dev_name = device_names.get(fi.device_id, fi.device_id[:8])
                mtime_str = fi.mtime.strftime("%Y-%m-%d %H:%M") if fi.mtime else "?"
                t.add_row(marker, dev_name, fi.path, mtime_str)

            console.print(t)

        # ── footer ────────────────────────────────────────────────────────────
        if total_groups > limit:
            console.print(
                f"\n[dim]Showing top {limit} of {total_groups} groups (sorted by wasted space). "
                f"Use --limit to see more.[/dim]"
            )

        console.print(
            f"\n[bold]Total:[/bold] {total_groups} group(s) · "
            f"{total_dup_files} duplicate files · "
            f"[red]{_fmt_bytes(total_wasted)}[/red] can be freed"
        )

        # ── optional file output ──────────────────────────────────────────────
        if output:
            _write_text_report(output, group_data[:limit], device_names, total_groups,
                               total_dup_files, total_wasted, limit)
            console.print(f"\nReport saved to [cyan]{output}[/cyan]")


def _write_text_report(
    path: Path,
    group_data: list,
    device_names: dict,
    total_groups: int,
    total_dup_files: int,
    total_wasted: int,
    limit: int,
) -> None:
    lines = [
        f"Exact Duplicates Report",
        f"Groups: {total_groups}  Duplicate files: {total_dup_files}  "
        f"Can free: {_fmt_bytes(total_wasted)}",
        "",
    ]
    for i, (group, members, wasted) in enumerate(group_data, 1):
        keep_id = group.recommended_keep_instance_id
        chash = members[0].content_hash or "?"
        size = members[0].size or 0
        lines.append(
            f"Group {i}  hash={chash[:16]}  "
            f"size={_fmt_bytes(size)}  copies={len(members)}  frees={_fmt_bytes(wasted)}"
        )
        for fi in members:
            tag = "[keep]" if fi.id == keep_id else "      "
            dev = device_names.get(fi.device_id, fi.device_id[:8])
            mtime = fi.mtime.strftime("%Y-%m-%d %H:%M") if fi.mtime else "?"
            lines.append(f"  {tag} {dev}  {fi.path}  ({mtime})")
        lines.append("")
    if total_groups > limit:
        lines.append(f"... {total_groups - limit} more groups not shown (use --limit)")
    path.write_text("\n".join(lines), encoding="utf-8")


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── quarantine ────────────────────────────────────────────────────────────────

@app.command()
def quarantine(
    device: Path = typer.Option(..., help="Mount path of the drive"),
    db: Path = typer.Option(_DEFAULT_DB, help="Path to the index database"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without moving files"),
):
    """Move duplicate copies (non-keep) to .media-archive-quarantine/ on the device."""
    if not device.exists() or not device.is_dir():
        console.print(f"[red]Error:[/red] {device} is not a valid directory.")
        raise typer.Exit(1)

    factory = _get_factory(db)

    from .device import MARKER_FILE
    import json as _json
    marker = device / MARKER_FILE
    if not marker.exists():
        console.print("[red]Error:[/red] Device not registered. Run [bold]mard scan[/bold] first.")
        raise typer.Exit(1)

    device_marker_id = _json.loads(marker.read_text())["device_id"]
    with open_session(factory) as s:
        dev = DeviceRepo(s).get_by_marker_id(device_marker_id)
    if dev is None:
        console.print("[red]Error:[/red] Device not found in database.")
        raise typer.Exit(1)

    device_id = dev.id
    quarantine_path = device / QUARANTINE_DIR

    if dry_run:
        console.print("[yellow]DRY RUN — no files will be moved.[/yellow]\n")
    console.print(
        f"[bold]mard quarantine[/bold]  device=[cyan]{device}[/cyan]  "
        f"quarantine=[cyan]{quarantine_path}[/cyan]\n"
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[cyan]{task.completed}/{task.total}[/cyan] groups"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Processing duplicate groups…", total=None)

        def on_progress(done: int, total: int, _: str) -> None:
            progress.update(task, completed=done, total=max(total, 1))

        try:
            result = run_quarantine(device, device_id, factory, dry_run=dry_run, progress_cb=on_progress)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            raise typer.Exit(130)

    status = "[yellow]DRY RUN complete.[/yellow]" if dry_run else "[bold green]Quarantine complete.[/bold green]"
    console.print(f"{status}\n")

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column(justify="right", style="bold")
    t.add_row("Duplicate groups scanned", str(result.total_groups))
    t.add_row("Files moved to quarantine", str(result.moved))
    t.add_row("Already gone (exists=False)", str(result.already_gone))
    t.add_row("Skipped — keep file missing", f"[yellow]{result.skipped_no_keep}[/yellow]")
    t.add_row("Skipped — would remove only copy", f"[yellow]{result.skipped_only_copy}[/yellow]")
    t.add_row("Errors", f"[red]{result.error}[/red]")
    console.print(t)

    if result.moved_paths:
        console.print(f"\n[dim]Quarantine location:[/dim] [cyan]{quarantine_path}[/cyan]")
        if dry_run:
            console.print("\n[dim]Files that would be moved:[/dim]")
            for src, _ in result.moved_paths[:20]:
                console.print(f"  [dim]{src}[/dim]")
            if len(result.moved_paths) > 20:
                console.print(f"  [dim]… and {len(result.moved_paths) - 20} more[/dim]")
        else:
            console.print(
                f"\nReview files in [cyan]{quarantine_path}[/cyan] then delete manually when satisfied.\n"
                f"Run [bold]mard scan --device {device}[/bold] to update the index."
            )


# ── meta ──────────────────────────────────────────────────────────────────────

@app.command()
def meta(
    device: Path = typer.Option(..., help="Mount path of the drive"),
    db: Path = typer.Option(_DEFAULT_DB, help="Path to the index database"),
):
    """Extract EXIF metadata: fullhash remaining files, create MediaAssets, read EXIF."""
    if not device.exists() or not device.is_dir():
        console.print(f"[red]Error:[/red] {device} is not a valid directory.")
        raise typer.Exit(1)

    factory = _get_factory(db)

    # Look up device_id from DB via marker file
    from .device import identify_device, MARKER_FILE
    marker = device / MARKER_FILE
    if not marker.exists():
        console.print("[red]Error:[/red] Device not registered. Run [bold]mard scan[/bold] first.")
        raise typer.Exit(1)

    import json as _json
    device_marker_id = _json.loads(marker.read_text())["device_id"]
    with open_session(factory) as s:
        dev = DeviceRepo(s).get_by_marker_id(device_marker_id)
    if dev is None:
        console.print("[red]Error:[/red] Device not found in database.")
        raise typer.Exit(1)
    device_id = dev.id

    console.print(f"[bold]mard meta[/bold]  device=[cyan]{device}[/cyan]  db=[cyan]{db}[/cyan]\n")

    phase_task = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[cyan]{task.completed}/{task.total}[/cyan]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_fullhash = progress.add_task("Phase A: full hash…", total=None)
        task_exif = progress.add_task("Phase C: EXIF…", total=None)

        def on_progress(phase: str, done: int, total: int) -> None:
            if phase == "fullhash":
                progress.update(task_fullhash, completed=done, total=max(total, 1))
            elif phase == "exif":
                progress.update(task_exif, completed=done, total=max(total, 1))

        try:
            result = run_meta(device, device_id, factory, progress_cb=on_progress)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            raise typer.Exit(130)

    console.print("[bold green]meta complete.[/bold green]\n")
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column(justify="right", style="bold")
    t.add_row("Files full-hashed", str(result.fullhash_done))
    t.add_row("MediaAssets created", str(result.assets_created))
    t.add_row("Assets EXIF-updated", str(result.exif_updated))
    console.print(t)


if __name__ == "__main__":
    app()
