"""
Export split color parts to STL files or a ZIP archive.
"""

import io
import json
import os
import re
import zipfile
from typing import List

import trimesh

_UNSAFE_FILENAME_CHARS = re.compile(r'[^A-Za-z0-9_-]+')
_MAX_NAME_LEN = 60


def _safe_name(name: str) -> str:
    """Sanitize a part name (which may embed a user-chosen selection-group
    label) into a safe filename fragment: only alphanumerics/dash/underscore,
    length-capped. A crafted label must not be able to produce path
    separators, '..', or absurdly long zip entry names."""
    safe = _UNSAFE_FILENAME_CHARS.sub('_', name).strip('_')
    return (safe or "part")[:_MAX_NAME_LEN]


def _legend(parts, filenames: List[str]) -> str:
    """colors.json content: STL has no per-part color, so this legend is
    the only place the color chosen for each part (auto-detected or
    manually assigned in the "einfaerben" step) survives the export."""
    return json.dumps(
        [{"file": fn, "label": part.name, "color": part.color_hex}
         for fn, part in zip(filenames, parts)],
        indent=2,
    )


def export_parts_as_stl(parts, output_dir: str) -> List[str]:
    """Export each ColorPart as an STL file in *output_dir*, plus a
    colors.json legend. Returns the STL file paths (not the legend's)."""
    os.makedirs(output_dir, exist_ok=True)
    paths = []
    filenames = []
    for i, part in enumerate(parts):
        safe_name = _safe_name(part.name)
        filename = f"{i + 1:02d}_{safe_name}.stl"
        path = os.path.join(output_dir, filename)
        part.mesh.export(path)
        paths.append(path)
        filenames.append(filename)
    with open(os.path.join(output_dir, "colors.json"), "w", encoding="utf-8") as f:
        f.write(_legend(parts, filenames))
    return paths


def export_parts_as_zip(parts) -> bytes:
    """Return a ZIP archive (bytes) containing one STL per ColorPart plus a
    colors.json legend (file -> label -> color)."""
    buf = io.BytesIO()
    filenames = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, part in enumerate(parts):
            safe_name = _safe_name(part.name)
            filename = f"{i + 1:02d}_{safe_name}.stl"
            stl_bytes = part.mesh.export(file_type='stl')
            zf.writestr(filename, stl_bytes)
            filenames.append(filename)
        zf.writestr("colors.json", _legend(parts, filenames))
    return buf.getvalue()
