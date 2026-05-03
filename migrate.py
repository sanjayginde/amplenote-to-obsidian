#!/usr/bin/env python3
"""
Amplenote → Obsidian migration script.

Usage:
  python migrate.py --input export.zip --output vault/
  python migrate.py --input export.zip --output vault/ --inspect
  python migrate.py --input export.zip --output vault/ --dry-run
  python migrate.py --input export.zip --output vault/ --no-subfolders
  python migrate.py --self-test

Requires: PyYAML  (pip install pyyaml)
"""

import argparse
import hashlib
import os
import re
import sys
import urllib.parse
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class NoteRecord:
    zip_path: str
    title: str
    frontmatter: dict
    body: str
    tags: list
    notebook: str | None
    creation_ts: float | None
    output_path: "Path | None" = None


@dataclass
class MigrationContext:
    notes: list
    title_to_record: dict
    attachment_map: dict  # zip_path (str) → dest Path
    attachment_basenames: dict  # basename (str) → dest Path
    errors: list
    stats: Counter
    dry_run: bool
    output_root: Path
    no_subfolders: bool = False
    attachments_dir: str = "attachments"


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

CODE_FENCE_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)", re.MULTILINE)

UUID_WIKI_RE = re.compile(
    r"\[\[([^\]|]+?)\s*\|?\s*[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\]\]"
)

UUID_LINK_RE = re.compile(r"\[([^\]]+)\]\(amplenote://[a-f0-9\-]+(?:#[^\)]*)?\)")

AMENTION_AT_RE = re.compile(r"@")  # used only in inspect mode to count occurrences

TASK_META_RE = re.compile(
    r"(- \[[ xX]\] .+?)\s*<!--[^>]*-->",
    re.MULTILINE,
)

IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}
    body = raw[m.end() :]
    return fm, body


# ---------------------------------------------------------------------------
# Tag normalization
# ---------------------------------------------------------------------------


def normalize_tags(tags) -> list:
    result = []
    for tag in tags or []:
        tag = str(tag).strip()
        tag = tag.lstrip("#")
        tag = re.sub(r"\s+", "-", tag)
        tag = re.sub(r"[^\w\-/]", "", tag)
        if tag:
            result.append(tag)
    return result


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def parse_creation_date(val) -> "float | None":
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, datetime):
        return val.timestamp()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(val), fmt).timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Filename / path helpers
# ---------------------------------------------------------------------------


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]]', "-", name)
    name = name.strip(". ")
    return name[:200] or "untitled"


def sanitize_dirname(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name)
    return name.strip(". ") or "notes"


def extract_notebook(fm: dict) -> "str | None":
    for key in ("notebook", "folder", "source_folder"):
        val = fm.get(key)
        if val:
            return str(val)
    tags = fm.get("tags", [])
    path_tags = [str(t) for t in (tags or []) if "/" in str(t)]
    if path_tags:
        return max(path_tags, key=len)
    return None


def determine_output_path(record: NoteRecord, ctx: MigrationContext) -> Path:
    safe_title = sanitize_filename(record.title)
    if ctx.no_subfolders or not record.notebook:
        return ctx.output_root / f"{safe_title}.md"
    parts = [sanitize_dirname(p) for p in record.notebook.split("/")]
    return ctx.output_root / Path(*parts) / f"{safe_title}.md"


# ---------------------------------------------------------------------------
# Deduplication of output paths
# ---------------------------------------------------------------------------


def assign_output_paths(notes: list, ctx: MigrationContext):
    seen: dict[Path, int] = {}
    for record in notes:
        base = determine_output_path(record, ctx)
        if base not in seen:
            seen[base] = 1
            record.output_path = base
        else:
            seen[base] += 1
            stem = base.stem
            record.output_path = base.with_name(f"{stem} ({seen[base]}){base.suffix}")


# ---------------------------------------------------------------------------
# Link resolution
# ---------------------------------------------------------------------------


def resolve_title(raw_title: str, ctx: MigrationContext) -> str:
    raw_title = raw_title.strip()
    if raw_title in ctx.title_to_record:
        return raw_title
    lower = raw_title.lower()
    candidates = [t for t in ctx.title_to_record if t.lower() == lower]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        ctx.stats["ambiguous_links"] += 1
    else:
        ctx.stats["unresolved_links"] += 1
    return raw_title


def apply_outside_code(text: str, fn) -> str:
    """Apply fn(segment) to prose segments, leaving code spans/fences unchanged."""
    parts = CODE_FENCE_RE.split(text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(part)
        else:
            result.append(fn(part))
    return "".join(result)


def convert_amentions(
    text: str, sorted_titles: list, ctx: MigrationContext, count: list
) -> str:
    """Replace @Title with [[Title]] using known note titles (longest-match first)."""
    result = []
    i = 0
    while i < len(text):
        if text[i] != "@":
            result.append(text[i])
            i += 1
            continue
        # Skip if preceded by a word character (part of an email address or similar)
        if i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_-."):
            result.append(text[i])
            i += 1
            continue
        matched = False
        rest = text[i + 1 :]
        for title in sorted_titles:
            if rest.lower().startswith(title.lower()):
                end_pos = i + 1 + len(title)
                next_ch = text[end_pos] if end_pos < len(text) else ""
                if not (next_ch.isalnum() or next_ch in "_'-"):
                    resolved = resolve_title(title, ctx)
                    result.append(f"[[{resolved}]]")
                    i = end_pos
                    count[0] += 1
                    matched = True
                    break
        if not matched:
            result.append("@")
            i += 1
    return "".join(result)


# ---------------------------------------------------------------------------
# Transformations
# ---------------------------------------------------------------------------


def transform_links(body: str, ctx: MigrationContext) -> tuple[str, int]:
    count = [0]

    def strip_wiki_uuid(m):
        count[0] += 1
        return f"[[{m.group(1).strip()}]]"

    body = UUID_WIKI_RE.sub(strip_wiki_uuid, body)

    def convert_uri_link(m):
        count[0] += 1
        title = resolve_title(m.group(1), ctx)
        return f"[[{title}]]"

    body = UUID_LINK_RE.sub(convert_uri_link, body)

    sorted_titles = sorted(ctx.title_to_record.keys(), key=len, reverse=True)
    body = apply_outside_code(
        body,
        lambda seg: convert_amentions(seg, sorted_titles, ctx, count),
    )

    return body, count[0]


def transform_tasks(body: str) -> tuple[str, int]:
    count = [0]

    def replacer(m):
        count[0] += 1
        return m.group(1).rstrip()

    result = TASK_META_RE.sub(replacer, body)
    return result, count[0]


def transform_image_refs(
    body: str,
    ctx: MigrationContext,
    missing_log: list,
) -> tuple[str, int]:
    count = [0]
    attachments_dir = ctx.attachments_dir

    def replacer(m):
        alt = m.group(1)
        src = m.group(2)
        if src.startswith("http://") or src.startswith("https://"):
            return m.group(0)
        src_decoded = urllib.parse.unquote(src)
        basename = Path(src_decoded).name
        if basename in ctx.attachment_basenames:
            count[0] += 1
            return f"![{alt}]({attachments_dir}/{basename})"
        missing_log.append(src)
        return m.group(0)

    result = IMAGE_RE.sub(replacer, body)
    return result, count[0]


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def build_index(zf: zipfile.ZipFile, ctx: MigrationContext):
    title_counter: dict[str, int] = {}
    title_to_record: dict[str, NoteRecord] = {}

    for member in zf.infolist():
        if not member.filename.endswith(".md") or member.filename.endswith("/"):
            continue
        try:
            raw = zf.read(member.filename).decode("utf-8", errors="replace")
        except Exception as e:
            ctx.errors.append((member.filename, f"read error: {e}"))
            continue

        try:
            fm, body = parse_frontmatter(raw)
        except Exception as e:
            ctx.errors.append((member.filename, f"frontmatter error: {e}"))
            fm, body = {}, raw

        title = fm.get("title") or Path(member.filename).stem

        if title in title_counter:
            title_counter[title] += 1
            unique_title = f"{title} ({title_counter[title]})"
        else:
            title_counter[title] = 1
            unique_title = title

        record = NoteRecord(
            zip_path=member.filename,
            title=unique_title,
            frontmatter=fm,
            body=body,
            tags=normalize_tags(fm.get("tags", [])),
            notebook=extract_notebook(fm),
            creation_ts=parse_creation_date(fm.get("creation")),
        )
        ctx.notes.append(record)
        title_to_record[unique_title] = record

    ctx.title_to_record.update(title_to_record)
    assign_output_paths(ctx.notes, ctx)


def build_attachment_map(zf: zipfile.ZipFile, ctx: MigrationContext):
    attachments_root = ctx.output_root / ctx.attachments_dir
    basename_counter: dict[str, int] = {}

    for member in zf.infolist():
        if member.filename.endswith(".md") or member.filename.endswith("/"):
            continue
        basename = Path(member.filename).name
        if not basename:
            continue

        if basename in basename_counter:
            basename_counter[basename] += 1
            stem = Path(basename).stem
            suffix = Path(basename).suffix
            unique_basename = f"{stem}_{basename_counter[basename]}{suffix}"
        else:
            basename_counter[basename] = 1
            unique_basename = basename

        dest = attachments_root / unique_basename
        ctx.attachment_map[member.filename] = dest
        ctx.attachment_basenames[unique_basename] = dest
        ctx.attachment_basenames[basename] = dest


# ---------------------------------------------------------------------------
# Inspect mode
# ---------------------------------------------------------------------------


def inspect_and_report(ctx: MigrationContext, zf: zipfile.ZipFile):
    print("\n=== AMPLENOTE EXPORT INSPECTION ===\n")
    print(f"Notes found:       {len(ctx.notes)}")
    print(f"Attachments found: {len(ctx.attachment_map)}")

    mention_count = mention_notes = 0
    wiki_count = wiki_notes = 0
    uuid_uri_count = uuid_uri_notes = 0
    task_notes = task_total = task_with_meta = 0
    duplicate_titles: dict[str, int] = {}
    missing_images: list[tuple[str, str]] = []
    all_tags: Counter = Counter()
    notebooks: Counter = Counter()

    for record in ctx.notes:
        body = record.body

        m_count = len(AMENTION_AT_RE.findall(body))
        if m_count:
            mention_count += m_count
            mention_notes += 1

        w_count = len(UUID_WIKI_RE.findall(body))
        if w_count:
            wiki_count += w_count
            wiki_notes += 1

        u_count = len(UUID_LINK_RE.findall(body))
        if u_count:
            uuid_uri_count += u_count
            uuid_uri_notes += 1

        task_lines = re.findall(r"- \[[ xX]\].*", body)
        if task_lines:
            task_notes += 1
            task_total += len(task_lines)
            task_with_meta += sum(1 for line in task_lines if "<!-- " in line)

        for tag in record.tags:
            all_tags[tag] += 1

        if record.notebook:
            notebooks[record.notebook] += 1
        else:
            notebooks["(unassigned)"] += 1

        base_title = re.sub(r" \(\d+\)$", "", record.title)
        if record.title != base_title:
            duplicate_titles[base_title] = duplicate_titles.get(base_title, 1) + 1

        missing = []
        transform_image_refs(body, ctx, missing)
        for src in missing:
            missing_images.append((record.title, src))

    print("\n--- Link Formats ---")
    print(f"  @mention style:    {mention_count} in {mention_notes} notes")
    print(f"  [[wiki-link]]:     {wiki_count} in {wiki_notes} notes")
    print(f"  UUID-URI style:    {uuid_uri_count} in {uuid_uri_notes} notes")

    print("\n--- Tag Summary ---")
    print(f"  Unique tags: {len(all_tags)}")
    top = all_tags.most_common(10)
    if top:
        print("  Top tags: " + ", ".join(f"{t} ({n})" for t, n in top))

    print("\n--- Notebook Structure ---")
    for nb, count in notebooks.most_common():
        print(f"  {nb}: {count} notes")

    print("\n--- Task Summary ---")
    print(f"  Notes with tasks:  {task_notes}")
    print(f"  Total tasks:       {task_total}")
    print(f"  With HTML metadata:{task_with_meta}")

    if duplicate_titles or missing_images:
        print("\n--- Potential Issues ---")
        if duplicate_titles:
            print(f"  Duplicate titles ({len(duplicate_titles)}):")
            for title, n in list(duplicate_titles.items())[:10]:
                print(f"    '{title}' appears {n} times")
        if missing_images:
            print(f"  Missing images ({len(missing_images)}):")
            for note_title, src in missing_images[:10]:
                print(f"    '{src}' referenced in '{note_title}' — not in ZIP")

    print()


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------


def content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def write_note(record: NoteRecord, new_body: str, new_fm: dict, ctx: MigrationContext):
    fm_text = yaml.dump(
        new_fm, allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    content = f"---\n{fm_text}---\n\n{new_body}"

    if ctx.dry_run:
        print(f"  [DRY RUN] {record.output_path}")
        return

    output_path = record.output_path
    if output_path is None:
        raise ValueError(f"Note '{record.title}' has no output_path assigned")
    if output_path.exists():
        existing = output_path.read_text(encoding="utf-8")
        if content_hash(existing) == content_hash(content):
            ctx.stats["notes_skipped"] += 1
            return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")


def copy_attachments(zf: zipfile.ZipFile, ctx: MigrationContext):
    attachments_dir = ctx.output_root / ctx.attachments_dir
    if not ctx.dry_run:
        attachments_dir.mkdir(parents=True, exist_ok=True)

    for zip_path, dest_path in ctx.attachment_map.items():
        try:
            if ctx.dry_run:
                print(f"  [DRY RUN] attachment: {zip_path} → {dest_path.name}")
                continue
            if dest_path.exists():
                existing_size = dest_path.stat().st_size
                source_size = zf.getinfo(zip_path).file_size
                if existing_size == source_size:
                    ctx.stats["attachments_skipped"] += 1
                    continue
            dest_path.write_bytes(zf.read(zip_path))
            ctx.stats["attachments_copied"] += 1
        except Exception as e:
            ctx.errors.append((zip_path, f"attachment copy error: {e}"))


def restore_timestamps(records: list, dry_run: bool):
    for record in records:
        if record.creation_ts and record.output_path and record.output_path.exists():
            if not dry_run:
                try:
                    os.utime(
                        record.output_path, (record.creation_ts, record.creation_ts)
                    )
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Main migration loop
# ---------------------------------------------------------------------------


def run_migration(ctx: MigrationContext, zf: zipfile.ZipFile):
    total = len(ctx.notes)
    for i, record in enumerate(ctx.notes, 1):
        if total > 20 and i % max(1, total // 20) == 0:
            pct = int(i / total * 100)
            print(f"  {pct}% ({i}/{total})...", flush=True)

        try:
            missing_images: list = []
            body = record.body
            body, links_conv = transform_links(body, ctx)
            body, tasks_clean = transform_tasks(body)
            body, images_upd = transform_image_refs(body, ctx, missing_images)

            new_fm = dict(record.frontmatter)
            new_fm["tags"] = record.tags
            for key in ("creation", "notebook", "folder", "source_folder"):
                new_fm.pop(key, None)
            if record.creation_ts:
                new_fm["created"] = datetime.fromtimestamp(
                    record.creation_ts
                ).isoformat()
            new_fm["migrated_from"] = "amplenote"

            ctx.stats["links_converted"] += links_conv
            ctx.stats["tasks_cleaned"] += tasks_clean
            ctx.stats["images_updated"] += images_upd

            for src in missing_images:
                ctx.errors.append((record.zip_path, f"missing image: {src}"))

            write_note(record, body, new_fm, ctx)
            ctx.stats["notes_processed"] += 1

        except Exception as e:
            ctx.errors.append((record.zip_path, str(e)))
            ctx.stats["notes_failed"] += 1

    copy_attachments(zf, ctx)

    if not ctx.dry_run:
        restore_timestamps(ctx.notes, ctx.dry_run)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(ctx: MigrationContext):
    s = ctx.stats
    print("\n=== MIGRATION COMPLETE ===\n")
    print(f"Notes processed:    {s['notes_processed']}")
    if s["notes_skipped"]:
        print(f"Notes skipped:      {s['notes_skipped']}  (already up to date)")
    if s["notes_failed"]:
        print(f"Notes failed:       {s['notes_failed']}")
    print(f"Links converted:    {s['links_converted']}")
    if s["ambiguous_links"]:
        print(
            f"Ambiguous links:    {s['ambiguous_links']}  (kept as-is, review in Obsidian)"
        )
    if s["unresolved_links"]:
        print(f"Unresolved links:   {s['unresolved_links']}  (no matching note found)")
    print(f"Tasks cleaned:      {s['tasks_cleaned']}")
    print(f"Images updated:     {s['images_updated']}")
    print(f"Attachments copied: {s['attachments_copied']}")
    if s["attachments_skipped"]:
        print(f"Attachments skipped:{s['attachments_skipped']}  (already present)")

    if ctx.errors:
        print(f"\nErrors ({len(ctx.errors)}):")
        for path, msg in ctx.errors[:20]:
            print(f"  {path}: {msg}")
        if len(ctx.errors) > 20:
            print(f"  ... and {len(ctx.errors) - 20} more")

    if not ctx.dry_run:
        print(f"\nOutput: {ctx.output_root}")
        print("\nNext steps:")
        print("  1. Open the output folder as an Obsidian vault")
        print("  2. Check Graph View for orphaned (unresolved) links")
        print("  3. Search for '@' in prose — should return 0 hits")
        print("  4. Search for '<!-- uuid' — should return 0 hits")


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def self_test():
    failures = []

    def check(name, got, expected):
        if got != expected:
            failures.append(f"FAIL [{name}]: got {got!r}, expected {expected!r}")
        else:
            print(f"  PASS [{name}]")

    print("Running self-tests...\n")

    # parse_frontmatter
    raw = "---\ntitle: Test Note\ntags:\n  - work\n  - personal\n---\nBody text here."
    fm, body = parse_frontmatter(raw)
    check("frontmatter/title", fm.get("title"), "Test Note")
    check("frontmatter/body", body, "Body text here.")

    raw_no_fm = "No frontmatter here."
    fm2, body2 = parse_frontmatter(raw_no_fm)
    check("frontmatter/absent", fm2, {})
    check("frontmatter/absent-body", body2, "No frontmatter here.")

    # normalize_tags
    check(
        "tags/strip-hash", normalize_tags(["#work", "personal"]), ["work", "personal"]
    )
    check("tags/spaces", normalize_tags(["my tag"]), ["my-tag"])
    check("tags/hierarchy", normalize_tags(["project/work"]), ["project/work"])
    check("tags/empty", normalize_tags([]), [])

    # parse_creation_date
    ts = parse_creation_date(1700000000.0)
    check("date/float", isinstance(ts, float), True)
    ts2 = parse_creation_date("2024-01-15")
    check("date/string", ts2 is not None, True)
    ts3 = parse_creation_date(datetime(2024, 6, 1, 12, 0, 0))
    check("date/datetime", ts3 is not None, True)
    check("date/none", parse_creation_date(None), None)

    # transform_tasks
    task_body = '- [x] Do something <!-- uuid="abc" completed="2024-01-01" -->'
    cleaned, count = transform_tasks(task_body)
    check("tasks/strip-meta", cleaned, "- [x] Do something")
    check("tasks/count", count, 1)

    task_body2 = '- [ ] Not done\n- [x] Done <!-- uuid="xyz" -->'
    cleaned2, count2 = transform_tasks(task_body2)
    check("tasks/mixed", "<!-- uuid" not in cleaned2, True)
    check("tasks/count2", count2, 1)

    # transform_links — needs a minimal context
    ctx = MigrationContext(
        notes=[],
        title_to_record={"My Note": None, "Other Note": None},
        attachment_map={},
        attachment_basenames={},
        errors=[],
        stats=Counter(),
        dry_run=True,
        output_root=Path("/tmp"),
    )

    body, count = transform_links("See @My Note for details.", ctx)
    check("links/@mention", "[[My Note]]" in body, True)
    check("links/@mention-count", count, 1)

    body2, count2 = transform_links(
        "[[My Note|abc12345-1234-1234-1234-123456789012]]", ctx
    )
    check("links/uuid-wiki", body2.strip(), "[[My Note]]")
    check("links/uuid-wiki-count", count2, 1)

    body3, count3 = transform_links("[Other Note](amplenote://abc-def-123)", ctx)
    check("links/uuid-uri", "[[Other Note]]" in body3, True)
    check("links/uuid-uri-count", count3, 1)

    body4, _ = transform_links("email@example.com stays unchanged", ctx)
    check("links/@mention-email", "[[example" not in body4, True)

    body5, _ = transform_links("```\n@My Note inside code\n```", ctx)
    check("links/skip-code-fence", "[[My Note]]" not in body5, True)

    # transform_image_refs
    ctx.attachment_basenames["photo.png"] = Path("/tmp/attachments/photo.png")
    img_body = "![alt text](images/photo.png)"
    updated, count = transform_image_refs(img_body, ctx, [])
    check("images/rewrite", "attachments/photo.png" in updated, True)
    check("images/count", count, 1)

    img_body2 = "![](https://example.com/remote.png)"
    updated2, count2 = transform_image_refs(img_body2, ctx, [])
    check("images/skip-url", updated2, img_body2)

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)} test(s) FAILED")
        sys.exit(1)
    else:
        print("All tests passed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Amplenote export ZIP to an Obsidian vault.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", help="Path to amplenote_export.zip")
    parser.add_argument("--output", help="Output directory for Obsidian vault")
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Analyze the export and print a report, then exit (no files written)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be done without writing any files",
    )
    parser.add_argument(
        "--no-subfolders",
        action="store_true",
        help="Place all notes in the vault root instead of notebook subfolders",
    )
    parser.add_argument(
        "--attachments-dir",
        default="attachments",
        help="Name of the attachments subfolder (default: attachments)",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="Run built-in unit tests and exit"
    )
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    if not args.input or not args.output:
        parser.error("--input and --output are required (or use --self-test)")

    input_path = Path(args.input)
    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")

    output_root = Path(args.output)
    dry_run = args.dry_run or args.inspect

    ctx = MigrationContext(
        notes=[],
        title_to_record={},
        attachment_map={},
        attachment_basenames={},
        errors=[],
        stats=Counter(),
        dry_run=dry_run,
        output_root=output_root,
        no_subfolders=args.no_subfolders,
        attachments_dir=args.attachments_dir,
    )

    print(f"Reading {input_path} ...")
    with zipfile.ZipFile(input_path, "r") as zf:
        build_index(zf, ctx)
        build_attachment_map(zf, ctx)

        if args.inspect:
            inspect_and_report(ctx, zf)
            return

        mode = "DRY RUN — " if args.dry_run else ""
        print(f"{mode}Migrating {len(ctx.notes)} notes to {output_root} ...")

        run_migration(ctx, zf)

    print_summary(ctx)


if __name__ == "__main__":
    main()
