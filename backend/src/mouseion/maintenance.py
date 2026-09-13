"""Operational CLI: backups, restore drills, FTS rebuilds, and re-embedding."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mouseion.config import Settings, get_settings
from mouseion.db import connect, session, transaction
from mouseion.observability import configure_logging
from mouseion.services import papers as papers_repo
from mouseion.services.embeddings import (
    build_embedding_source,
    get_embedder,
    papers_with_stale_embeddings,
    store_embedding,
)
from mouseion.services.health import run_full_health
from mouseion.services.taxonomy import topics_for_paper

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackupResult:
    snapshot: Path
    dry_run: bool
    papers: int
    pdfs: int
    trees: int
    pruned: int


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _snapshot_dirs(backup_dir: Path) -> list[Path]:
    if not backup_dir.is_dir():
        return []
    return sorted(
        (path for path in backup_dir.glob("20??????T??????Z") if path.is_dir()),
        key=lambda path: path.name,
    )


def _count_files(path: Path, suffix: str) -> int:
    return sum(1 for item in path.glob(f"*{suffix}") if item.is_file()) if path.is_dir() else 0


def create_backup(
    *,
    settings: Settings | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> BackupResult:
    """Create a consistent snapshot; dry-run performs no filesystem writes."""
    settings = settings or get_settings()
    current = (now or datetime.now(UTC)).astimezone(UTC)
    target = settings.backup_dir / _stamp(current)
    with session(settings.db_path) as conn:
        paper_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
    pdf_count = _count_files(settings.pdf_dir, ".pdf")
    tree_count = _count_files(settings.tree_dir, ".json")
    cutoff = current - timedelta(days=settings.backup_retention_days)
    prunable: list[Path] = []
    for path in _snapshot_dirs(settings.backup_dir):
        try:
            created = datetime.strptime(path.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        if created < cutoff:
            prunable.append(path)
    if dry_run:
        return BackupResult(target, True, paper_count, pdf_count, tree_count, len(prunable))

    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    partial = settings.backup_dir / f".{target.name}.partial"
    if target.exists() or partial.exists():
        raise RuntimeError(f"backup snapshot already exists: {target.name}")
    partial.mkdir()
    try:
        with session(settings.db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            conn.execute("VACUUM INTO ?", (str(partial / "library.db"),))
        for source, name in ((settings.pdf_dir, "pdfs"), (settings.tree_dir, "trees")):
            destination = partial / name
            if source.is_dir():
                shutil.copytree(source, destination, copy_function=shutil.copy2)
            else:
                destination.mkdir()
        manifest = {
            "created_at": current.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "database": "library.db",
            "papers": paper_count,
            "pdfs": pdf_count,
            "trees": tree_count,
        }
        (partial / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        partial.replace(target)
    except Exception:
        if partial.is_dir() and partial.resolve().parent == settings.backup_dir.resolve():
            shutil.rmtree(partial)
        raise

    pruned = 0
    root = settings.backup_dir.resolve()
    for old in prunable:
        if old.resolve().parent != root:
            raise RuntimeError(f"refusing to prune path outside backup root: {old}")
        shutil.rmtree(old)
        pruned += 1
    return BackupResult(target, False, paper_count, pdf_count, tree_count, pruned)


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """Rebuild the entire FTS table from authoritative source tables."""
    with transaction(conn):
        conn.execute("DELETE FROM papers_fts")
        conn.execute(
            """
            INSERT INTO papers_fts (
                rowid, paper_id, title, authors, abstract,
                summary_short, summary_long, full_text
            )
            SELECT p.id, p.id, COALESCE(p.title, ''), COALESCE(p.authors, ''),
                   COALESCE(p.abstract, ''), COALESCE(p.summary_short, ''),
                   COALESCE(p.summary_long, ''), COALESCE(pt.full_text, '')
            FROM papers p
            LEFT JOIN paper_texts pt ON pt.paper_id = p.id
            """
        )
    return int(conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0])


def reembed(*, settings: Settings | None = None, dry_run: bool = False) -> tuple[int, int]:
    """Embed papers missing the configured model marker or carrying an old one."""
    settings = settings or get_settings()
    embedder = get_embedder()
    completed = 0
    with session(settings.db_path) as conn:
        stale = papers_with_stale_embeddings(conn, settings.embedding_model)
        if dry_run:
            return len(stale), 0
        for paper_id in stale:
            paper = papers_repo.get_paper(conn, paper_id)
            if paper is None:
                continue
            source = build_embedding_source(
                title=paper["title"],
                abstract=paper["abstract"],
                topic_names=[topic.name for topic in topics_for_paper(conn, paper_id)],
                summary_long=paper["summary_long"],
            )
            if not source:
                log.warning("paper %s has no embedding source", paper_id)
                continue
            vector = embedder.encode(source.text)
            if len(vector) != settings.embedding_dim:
                raise RuntimeError(
                    f"embedding model returned {len(vector)} dimensions; "
                    f"schema expects {settings.embedding_dim}"
                )
            if store_embedding(conn, paper_id, vector, model=embedder.model_name):
                completed += 1
    return len(stale), completed


async def restore_drill(*, settings: Settings | None = None) -> Path:
    """Restore the latest snapshot in isolation and run non-network health checks."""
    settings = settings or get_settings()
    snapshots = _snapshot_dirs(settings.backup_dir)
    if not snapshots:
        raise RuntimeError(f"no backup snapshots found in {settings.backup_dir}")
    latest = snapshots[-1]
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-drill-", dir=settings.backup_dir) as raw:
        scratch = Path(raw)
        shutil.copy2(latest / "library.db", scratch / "library.db")
        for name in ("pdfs", "trees"):
            source = latest / name
            if source.is_dir():
                shutil.copytree(source, scratch / name)
            else:
                (scratch / name).mkdir()
        drill_settings = settings.model_copy(
            update={
                "data_dir": scratch,
                "db_path": scratch / "library.db",
                "health_min_free_disk_mb": 0,
            }
        )
        conn = connect(scratch / "library.db")
        try:
            report = await run_full_health(
                conn=conn,
                settings=drill_settings,
                check_external=False,
            )
        finally:
            conn.close()
        errors = [check for check in report.checks if check.status == "error"]
        if errors:
            raise RuntimeError("restore health failed: " + "; ".join(c.detail for c in errors))
    return latest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="create a consistent dated snapshot")
    backup.add_argument("--dry-run", action="store_true")
    commands.add_parser("restore-drill", help="restore latest snapshot into scratch and verify it")
    reembed_parser = commands.add_parser("reembed", help="re-embed stale or missing paper rows")
    reembed_parser.add_argument("--dry-run", action="store_true")
    commands.add_parser("reindex-fts", help="rebuild FTS from papers and paper_texts")
    health = commands.add_parser("health-check", help="run full startup checks")
    health.add_argument("--offline", action="store_true", help="skip OpenRouter reachability")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_dirs()
    try:
        if args.command == "backup":
            result = create_backup(settings=settings, dry_run=args.dry_run)
            print(json.dumps({**asdict(result), "snapshot": str(result.snapshot)}, default=str))
        elif args.command == "restore-drill":
            latest = asyncio.run(restore_drill(settings=settings))
            print(f"restore drill passed: {latest}")
        elif args.command == "reembed":
            stale, completed = reembed(settings=settings, dry_run=args.dry_run)
            print(json.dumps({"stale": stale, "completed": completed, "dry_run": args.dry_run}))
        elif args.command == "reindex-fts":
            with session() as conn:
                count = rebuild_fts(conn)
            print(json.dumps({"indexed": count}))
        elif args.command == "health-check":
            report = asyncio.run(
                run_full_health(settings=settings, check_external=not args.offline)
            )
            print(
                json.dumps(
                    {"status": report.status, "checks": [asdict(c) for c in report.checks]}
                )
            )
            return 1 if report.status == "error" else 0
    except Exception as exc:  # noqa: BLE001
        log.exception("maintenance command failed", extra={"event": f"maintenance_{args.command}"})
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
