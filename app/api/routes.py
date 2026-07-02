"""FastAPI route definitions for the 3MF splitter."""

import io
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..core.connectors import ConnectorType, apply_connectors
from ..core.exporter import export_parts_as_zip
from ..core.parser import apply_rotation, parse_3mf
from ..core.splitter import DEFAULT_MERGE_GAP_MM, split_by_color

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "3mf-splitter"}


@router.post("/info")
async def file_info(
    file: UploadFile = File(...),
    merge_gap_mm: float = Form(DEFAULT_MERGE_GAP_MM),
):
    """Return metadata about a .3mf file without splitting it."""
    _require_3mf(file.filename)

    with _tmp_3mf(await file.read()) as tmp_path:
        mesh_list = parse_3mf(tmp_path)

    if not mesh_list:
        raise HTTPException(400, "No mesh objects found in the file")

    all_colors: set = set()
    objects = []
    total_parts = 0
    for md in mesh_list:
        parts = split_by_color(md, merge_gap_mm=max(0.0, merge_gap_mm))
        colors = [p.color_hex for p in parts]
        all_colors.update(colors)
        total_parts += len(parts)
        objects.append({
            "id": md.object_id,
            "name": md.name,
            "vertex_count": int(len(md.vertices)),
            "face_count": int(len(md.faces)),
            "colors": colors,
            "part_count": len(parts),
        })

    return {
        "filename": file.filename,
        "objects": objects,
        "total_unique_colors": len(all_colors),
        "all_colors": sorted(all_colors),
        "total_parts": total_parts,
    }


@router.post("/split")
async def split(
    file: UploadFile = File(...),
    connector: str = Form("none"),
    n_connectors: int = Form(3),
    merge_gap_mm: float = Form(DEFAULT_MERGE_GAP_MM),
    # Magnet parameters
    magnet_radius: float = Form(2.6),
    magnet_depth: float = Form(3.2),
    # Peg / Steg parameters
    peg_radius: float = Form(2.0),
    peg_height: float = Form(3.5),
    peg_clearance: float = Form(0.15),
    # Dovetail / Schwalbenschwanz parameters
    dt_width: float = Form(7.0),
    dt_height: float = Form(4.5),
    dt_depth: float = Form(5.0),
    dt_draft_deg: float = Form(12.0),
    dt_clearance: float = Form(0.2),
    # Whole-model orientation chosen via the viewer's "place on plate" tool
    rot_x: float = Form(0.0),
    rot_y: float = Form(0.0),
    rot_z: float = Form(0.0),
    rot_w: float = Form(1.0),
):
    """Split a .3mf file by face color and return a ZIP of STL parts."""
    _require_3mf(file.filename)
    n_connectors = max(1, min(n_connectors, 10))

    try:
        conn_type = ConnectorType(connector)
    except ValueError:
        raise HTTPException(400, f"Unknown connector type '{connector}'. "
                                 "Use: none, magnet, peg, dovetail")

    # Build connector-specific parameter dict
    params: dict = {}
    if conn_type == ConnectorType.MAGNET:
        params = {"radius": max(0.5, magnet_radius), "depth": max(0.5, magnet_depth)}
    elif conn_type == ConnectorType.PEG:
        params = {"radius": max(0.5, peg_radius), "height": max(0.5, peg_height),
                  "clearance": max(0.0, peg_clearance)}
    elif conn_type == ConnectorType.DOVETAIL:
        params = {"width": max(1.0, dt_width), "height": max(0.5, dt_height),
                  "depth": max(0.5, dt_depth), "draft_deg": max(0.0, min(45.0, dt_draft_deg)),
                  "clearance": max(0.0, dt_clearance)}

    with _tmp_3mf(await file.read()) as tmp_path:
        mesh_list = parse_3mf(tmp_path)

    if not mesh_list:
        raise HTTPException(400, "No mesh objects found in the file")

    apply_rotation(mesh_list, (rot_x, rot_y, rot_z, rot_w))

    all_parts = []
    for md in mesh_list:
        all_parts.extend(split_by_color(md, merge_gap_mm=max(0.0, merge_gap_mm)))

    if not all_parts:
        raise HTTPException(400, "No colored parts could be extracted")

    if conn_type != ConnectorType.NONE:
        all_parts = apply_connectors(all_parts, conn_type, n_connectors, params)

    zip_bytes = export_parts_as_zip(all_parts)
    base_name = os.path.splitext(file.filename or "model")[0]

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{base_name}_parts.zip"',
            "X-Part-Count": str(len(all_parts)),
        },
    )



@router.post("/debug")
async def debug_3mf(file: UploadFile = File(...)):
    """Inspect the internal structure of a .3mf file for diagnostics."""
    _require_3mf(file.filename)
    import zipfile, io
    data = await file.read()
    result = {"files": [], "objects": [], "metadata": {}}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        result["files"] = sorted(zf.namelist())
        # Read model files
        for name in zf.namelist():
            if not name.endswith('.model'):
                continue
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(zf.read(name))
                for obj in root.iter():
                    tag = obj.tag.split('}')[-1] if '}' in obj.tag else obj.tag
                    if tag == 'object':
                        obji = {
                            "file": name,
                            "id": obj.get("id"),
                            "name": obj.get("name"),
                            "type": obj.get("type"),
                            "pid": obj.get("pid"),
                            "p1": obj.get("p1"),
                            "color": obj.get("color"),
                            "has_mesh": False,
                            "has_components": False,
                            "face_count": 0,
                            "colorgroups_in_file": []
                        }
                        for child in obj.iter():
                            ctag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                            if ctag == 'mesh':
                                obji["has_mesh"] = True
                            if ctag == 'components':
                                obji["has_components"] = True
                            if ctag == 'triangles':
                                obji["face_count"] = sum(1 for t in child if (t.tag.split('}')[-1] if '}' in t.tag else t.tag) == 'triangle')
                        # colorgroups
                        for cg in root.iter():
                            cgtag = cg.tag.split('}')[-1] if '}' in cg.tag else cg.tag
                            if cgtag == 'colorgroup':
                                obji["colorgroups_in_file"].append({"id": cg.get("id"), "count": sum(1 for c in cg)})
                        result["objects"].append(obji)
            except Exception as e:
                result["objects"].append({"file": name, "error": str(e)})
        # Read metadata files
        for name in zf.namelist():
            if 'metadata' in name.lower() or name.endswith('.config') or name.endswith('.json'):
                try:
                    content = zf.read(name).decode('utf-8', errors='replace')
                    result["metadata"][name] = content[:2000]  # first 2000 chars
                except:
                    pass
    return result

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_3mf(filename: Optional[str]) -> None:
    if not filename or not filename.lower().endswith('.3mf'):
        raise HTTPException(400, "Only .3mf files are supported")


class _tmp_3mf:
    """Context manager: write bytes to a temp .3mf file and clean up after."""
    def __init__(self, data: bytes):
        self._data = data
        self._path: Optional[str] = None

    def __enter__(self) -> str:
        with tempfile.NamedTemporaryFile(suffix='.3mf', delete=False) as f:
            f.write(self._data)
            self._path = f.name
        return self._path

    def __exit__(self, *_):
        if self._path and os.path.exists(self._path):
            os.unlink(self._path)
