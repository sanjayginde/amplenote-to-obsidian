# amplenote-to-obsidian

A Python script that converts an Amplenote export ZIP into an Obsidian-ready vault.

## What it does

Amplenote and Obsidian both use markdown, but they differ in several important ways that break a naive copy-paste migration. This script handles all of them:

| Problem | What the script does |
|---|---|
| `@Note Title` and `[[Note\|uuid]]` links | Converts to Obsidian `[[Note Title]]` wiki-links |
| Completed tasks with HTML comment metadata | Strips `<!-- uuid="..." completed="..." -->` from task lines |
| Tags missing from YAML or using `#` prefix | Normalizes to a clean Obsidian-compatible list |
| Images with Amplenote-relative paths | Rewrites to `attachments/filename.ext`, copies files |
| All notes exported with the same creation date | Restores original timestamps from YAML `creation` field |
| Notes organized by notebook | Places notes into matching subfolders |

## Requirements

Python 3.10+ and PyYAML:

```bash
pip install pyyaml
```

## Usage

**1. Export from Amplenote**

Go to Account Settings → Import & Export → Start Export. This downloads a ZIP file.

**2. Inspect the export (recommended first step)**

```bash
python3 migrate.py --input amplenote_export.zip --output obsidian_vault/ --inspect
```

Prints a report of link formats, tags, notebooks, and potential issues — no files are written.

**3. Dry run**

```bash
python3 migrate.py --input amplenote_export.zip --output obsidian_vault/ --dry-run
```

Shows what would be written without touching the filesystem.

**4. Run the migration**

```bash
python3 migrate.py --input amplenote_export.zip --output obsidian_vault/
```

**5. Open in Obsidian**

Open `obsidian_vault/` as a new vault. Go to Graph View — orphaned floating nodes indicate unresolved links you can fix manually.

## Options

| Flag | Description |
|---|---|
| `--input` | Path to the Amplenote export ZIP |
| `--output` | Output directory for the Obsidian vault |
| `--inspect` | Analyze the export and print a report, then exit |
| `--dry-run` | Report what would happen without writing any files |
| `--no-subfolders` | Put all notes in the vault root instead of notebook subfolders |
| `--attachments-dir` | Name of the attachments subfolder (default: `attachments`) |
| `--self-test` | Run built-in unit tests and exit |

## Post-migration checks

- **Graph View** — orphaned nodes = unresolved `@mention` links (note title didn't match any known note)
- Search `@` in prose — should return 0 hits
- Search `<!-- uuid` — should return 0 hits if task cleanup worked
- Check that a note's date in the Obsidian file browser matches its original Amplenote creation date

## Notes

- **Unresolved links**: If an `@mention` in your notes doesn't exactly match a known note title (case-insensitive), it's kept as `[[raw text]]`. Obsidian shows these as unresolved in Graph View.
- **Idempotent**: Safe to re-run — notes whose content hasn't changed are skipped.
- **Duplicate titles**: If multiple notes share the same title, they get `(2)`, `(3)` suffixes. The original title is preserved in the `title:` frontmatter field.

---

Built with [Claude Code](https://claude.ai/code).
