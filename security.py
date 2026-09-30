"""Security helpers for the Upload UI (no pipeline changes).

Covers the small but important risks around file handling:
  - path traversal / unsafe filenames from file dialogs
  - fake / oversized PDFs (magic-byte + size check before processing)
  - destructive "clear output" scoped STRICTLY to output/ (never elsewhere)
  - safe copy into the OS Downloads folder (no overwrite, no traversal)

All functions are pure stdlib and never raise on bad input — they return
(False, reason) / safe fallbacks so the UI can show a message instead of
crashing.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# Upload guard: refuse PDFs bigger than this (prevents disk/memory abuse).
MAX_PDF_MB = 200
# Output guard: never delete anything outside these extensions on clear.
OUTPUT_SAFE_SUFFIXES = {".xlsx", ".json"}
GITKEEP = ".gitkeep"

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, allowed_suffix: str = ".pdf") -> str:
    """Strip directories, hostile chars; always return a safe non-empty name."""
    base = Path(name).name.strip()  # drops any directory part (traversal safe)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    if len(base) > 80:
        stem, dot, _ = base.rpartition(".")
        base = (stem[:76] + dot + "pdf") if dot else base[:80]
    if base.lower() in _WINDOWS_RESERVED:
        base = f"_{base}"
    suffix = Path(base).suffix.lower()
    if suffix != allowed_suffix.lower():
        stem = Path(base).stem
        base = f"{stem}{allowed_suffix}"
    return base


def is_within(base: Path, target: Path) -> bool:
    """True if resolved target lives inside resolved base dir."""
    try:
        base_r = base.resolve()
        tgt_r = target.resolve() if target.exists() else (base_r / target.name).resolve()
        return base_r == tgt_r.parent or base_r in tgt_r.parents or base_r == tgt_r
    except OSError:
        return False


def validate_pdf(path: Path, max_mb: int = MAX_PDF_MB) -> tuple[bool, str]:
    """Check a user-picked file is a real, sensibly-sized PDF."""
    if not path.exists() or not path.is_file():
        return False, "File not found."
    if path.suffix.lower() != ".pdf":
        return False, "Only .pdf files are allowed."
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"Cannot read file: {exc}"
    if size <= 0:
        return False, "PDF is empty (0 bytes)."
    if size > max_mb * 1024 * 1024:
        return False, f"PDF too large ({size / 1048576:.1f} MB > {max_mb} MB)."
    try:
        with open(path, "rb") as fh:
            magic = fh.read(5)
        if magic != b"%PDF-":
            return False, "Not a valid PDF (missing %PDF- header)."
    except OSError as exc:
        return False, f"Cannot read file: {exc}"
    return True, "OK"


def downloads_dir() -> Path:
    """OS Downloads folder, falling back to cwd if unavailable."""
    dl = Path.home() / "Downloads"
    try:
        if dl.exists() and dl.is_dir():
            return dl
    except OSError:
        pass
    return Path.cwd()


def unique_download_path(directory: Path, stem: str, suffix: str = ".xlsx") -> Path:
    """Non-overwriting destination: stem.xlsx, stem (1).xlsx, ..."""
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "voters"
    candidate = directory / f"{safe_stem}{suffix}"
    i = 1
    while candidate.exists():
        candidate = directory / f"{safe_stem} ({i}){suffix}"
        i += 1
        if i > 999:  # absolute fallback: timestamp
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            return directory / f"{safe_stem}_{ts}{suffix}"
    return candidate


def timestamped_stem(prefix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", prefix).strip("._") or "voters"
    return f"{safe}_{ts}"


def safe_copy_to_downloads(src: Path, dest_stem: str) -> tuple[Path | None, str]:
    """Copy an excel into Downloads without overwriting. Returns (dest, msg)."""
    if not src.exists() or not src.is_file():
        return None, f"Nothing to download (missing {src.name})."
    if src.suffix.lower() != ".xlsx":
        return None, "Only .xlsx downloads are supported."
    dl = downloads_dir()
    try:
        dl.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"Downloads folder unavailable: {exc}"
    dest = unique_download_path(dl, dest_stem, ".xlsx")
    # Confinement: dest MUST be directly inside Downloads.
    try:
        if dest.resolve().parent != dl.resolve():
            return None, "Unsafe download path blocked."
    except OSError:
        return None, "Unsafe download path blocked."
    try:
        import shutil
        shutil.copy2(str(src), str(dest))
        return dest, f"Saved to {dest}"
    except OSError as exc:
        return None, f"Download failed: {exc}"


def clear_output_folder(output_dir: Path) -> tuple[int, str]:
    """Delete generated files INSIDE output_dir only. Keeps .gitkeep.

    Deletes: *.xlsx / *.json at top level + everything under
    output/merge_parts/ (snapshots). Never touches parent dirs, never
    follows symlinks, never deletes other suffixes.
    Returns (removed_count, message).
    """
    try:
        root = output_dir.resolve()
    except OSError as exc:
        return 0, f"Cannot resolve output folder: {exc}"
    if not root.exists() or not root.is_dir():
        return 0, "Output folder not found."
    if root.name.lower() != "output":
        return 0, "Safety stop: target is not the output folder."
    removed = 0
    # Top-level generated files only.
    for child in sorted(root.iterdir()):
        if child.name == GITKEEP:
            continue
        try:
            if child.is_symlink():
                continue  # never follow/delete links
            if child.is_file():
                if child.suffix.lower() not in OUTPUT_SAFE_SUFFIXES:
                    continue
                child.unlink()
                removed += 1
            elif child.is_dir() and child.name == "merge_parts":
                for sub in sorted(child.iterdir()):
                    if sub.name == GITKEEP or sub.is_symlink():
                        continue
                    if sub.is_file():
                        sub.unlink()
                        removed += 1
        except OSError:
            continue
    # Re-create .gitkeep so the folder survives in git.
    try:
        (root / GITKEEP).touch(exist_ok=True)
    except OSError:
        pass
    return removed, f"Cleared {removed} file(s) from output/."
