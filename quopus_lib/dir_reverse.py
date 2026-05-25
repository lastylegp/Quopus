"""
AmiExpress /X-style directory listing generator.
Supports:
  - full directory dump
  - recursive dump
  - dump of SPECIFIC files (selected in lister)
  - reverse (newest first) or chronological
"""
from pathlib import Path
from datetime import datetime
import os


def _fmt_size_amiga(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024*1024):.1f}M"
    return f"{n / (1024*1024*1024):.2f}G"


def make_dir_listing(path, reverse=True, include_subdirs=False,
                     uploader="SYSOP", line_width=79,
                     file_paths=None, title_suffix=""):
    """
    Generate an AmiExpress-style DIR listing.

    Args:
        path: base directory for relative paths / header info
        reverse: newest first
        include_subdirs: recurse
        uploader: name column value
        line_width: total line width
        file_paths: optional list of Path objects to include instead of scanning.
                    If provided, only these files are listed.
        title_suffix: extra text for the header
    """
    path = Path(path)
    entries = []  # list of (display_name, size, mtime)

    if file_paths:
        # Explicit file list (Einzelfile-Support)
        for fp in file_paths:
            try:
                fp = Path(fp)
                if fp.is_file():
                    st = fp.stat()
                    # Use filename relative to path if inside, else just name
                    try:
                        rel = fp.relative_to(path)
                    except ValueError:
                        rel = Path(fp.name)
                    entries.append((str(rel), st.st_size, st.st_mtime))
                elif fp.is_dir() and include_subdirs:
                    for sub in fp.rglob("*"):
                        if sub.is_file():
                            try:
                                st = sub.stat()
                                rel = sub.relative_to(fp.parent)
                                entries.append((str(rel), st.st_size, st.st_mtime))
                            except Exception:
                                pass
            except Exception:
                pass
    else:
        if not path.is_dir():
            return f"Not a directory: {path}\n"
        if include_subdirs:
            for p in path.rglob("*"):
                if p.is_file():
                    try:
                        st = p.stat()
                        entries.append((str(p.relative_to(path)), st.st_size, st.st_mtime))
                    except Exception:
                        pass
        else:
            try:
                with os.scandir(path) as it:
                    for e in it:
                        try:
                            if e.is_file(follow_symlinks=False):
                                st = e.stat(follow_symlinks=False)
                                entries.append((e.name, st.st_size, st.st_mtime))
                        except Exception:
                            pass
            except Exception as ex:
                return f"Error: {ex}\n"

    entries.sort(key=lambda x: x[2], reverse=reverse)

    direction = "Newest first (/X)" if reverse else "Oldest first"
    scope = "Selected files" if file_paths else "Full directory"
    if include_subdirs and not file_paths:
        scope += " (recursive)"

    out = []
    out.append("=" * line_width)
    out.append(f"  Directory listing: {path}")
    if title_suffix:
        out.append(f"  {title_suffix}")
    out.append(f"  Generated: {datetime.now().strftime('%d-%b-%y %H:%M:%S')}")
    out.append(f"  Scope:     {scope}")
    out.append(f"  Sort:      {direction}   Entries: {len(entries)}")
    out.append("=" * line_width)
    out.append("")
    out.append(f"{'Filename':<28} {'Size':>10}  {'Date':<10}  {'Uploader':<12}")
    out.append("-" * line_width)

    total_size = 0
    for name, size, mtime in entries:
        total_size += size
        fname = name if len(name) <= 28 else name[:25] + "..."
        size_str = _fmt_size_amiga(size)
        date_str = datetime.fromtimestamp(mtime).strftime("%d-%b-%y")
        out.append(f"{fname:<28} {size_str:>10}  {date_str:<10}  {uploader:<12}")

    out.append("-" * line_width)
    out.append(f"  Total: {len(entries)} files, {_fmt_size_amiga(total_size)}")
    out.append("=" * line_width)
    return "\n".join(out) + "\n"
