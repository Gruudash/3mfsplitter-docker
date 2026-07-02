"""
Export split color parts to STL files or a ZIP archive.
"""

import io
import os
import zipfile
from typing import List

import trimesh


def export_parts_as_stl(parts, output_dir: str) -> List[str]:
    """Export each ColorPart as an STL file in *output_dir*. Returns file paths."""
    os.makedirs(output_dir, exist_ok=True)
    paths = []
    for i, part in enumerate(parts):
        safe_name = part.name.replace('#', '').replace('/', '_').replace(' ', '_')
        filename = f"{i + 1:02d}_{safe_name}.stl"
        path = os.path.join(output_dir, filename)
        part.mesh.export(path)
        paths.append(path)
    return paths


def export_parts_as_zip(parts) -> bytes:
    """Return a ZIP archive (bytes) containing one STL per ColorPart."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, part in enumerate(parts):
            safe_name = part.name.replace('#', '').replace('/', '_').replace(' ', '_')
            filename = f"{i + 1:02d}_{safe_name}.stl"
            stl_bytes = part.mesh.export(file_type='stl')
            zf.writestr(filename, stl_bytes)
    return buf.getvalue()
