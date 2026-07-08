"""FastAPI route definitions for the 3MF splitter."""

import io
import json
import os
import tempfile
from typing import Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from ..core.connectors import ConnectorType, apply_connectors
from ..core.exporter import export_parts_as_zip
from ..core.parser import apply_rotation, parse_3mf
from ..core.splitter import DEFAULT_MERGE_GAP_MM, split_by_color, split_by_selection

router = APIRouter()

MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "200"))

# Bounds for the untrusted 'selection' form field (JSON text, not covered by
# MAX_UPLOAD_MB, which only bounds the uploaded file itself). Generous for
# any real manual selection, but bounded so a crafted payload can't blow up
# memory/CPU before reaching mesh processing.
MAX_SELECTION_JSON_CHARS = 2_000_000
MAX_SELECTION_GROUPS_PER_OBJECT = 200
MAX_FACE_INDICES_PER_GROUP = 500_000


class SelectionGroup(BaseModel):
    label: str = Field(default="", max_length=80)
    face_indices: List[int] = Field(..., max_length=MAX_FACE_INDICES_PER_GROUP)
    color_hex: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


def _parse_selection(selection: str) -> Dict[str, List[dict]]:
    """Validate the 'selection' form field and return {object_id: [group dict, ...]}.
    Raises HTTPException(400) on any malformed input -- this is untrusted
    client input, so nothing here may raise an uncaught 500."""
    if len(selection) > MAX_SELECTION_JSON_CHARS:
        raise HTTPException(400, "selection payload too large")
    try:
        raw = json.loads(selection)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid 'selection' JSON")
    if not isinstance(raw, dict):
        raise HTTPException(400, "'selection' must be a JSON object keyed by object_id")

    parsed: Dict[str, List[dict]] = {}
    try:
        for object_id, groups in raw.items():
            if not isinstance(groups, list) or len(groups) > MAX_SELECTION_GROUPS_PER_OBJECT:
                raise HTTPException(400, f"invalid group list for object '{object_id}'")
            parsed[object_id] = [g.model_dump() for g in
                                  (SelectionGroup(**group) for group in groups)]
    except ValidationError as e:
        raise HTTPException(400, f"Invalid selection group: {e}")
    except (TypeError, AttributeError):
        raise HTTPException(400, "Invalid 'selection' JSON")
    return parsed


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

    with _tmp_3mf(await _read_upload(file)) as tmp_path:
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
    # Manual pre-split selection from the viewer's selection step: JSON
    # {"<object_id>": [{"label": str, "face_indices": [int, ...], "color_hex": str}]}.
    # Omitted/empty -> unchanged automatic per-color split (back-compat with
    # the CLI and any older client).
    selection: Optional[str] = Form(None),
):
    """Split a .3mf file and return a ZIP of STL parts.

    Without *selection*: automatic per-color split (original behavior).
    With *selection*: split only at the user-chosen face groups; every
    face not claimed by a group stays together as one "rest" part.
    """
    _require_3mf(file.filename)
    n_connectors = max(1, min(n_connectors, 10))

    selection_groups: Optional[Dict[str, List[dict]]] = None
    if selection:
        selection_groups = _parse_selection(selection)

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

    with _tmp_3mf(await _read_upload(file)) as tmp_path:
        mesh_list = parse_3mf(tmp_path)

    if not mesh_list:
        raise HTTPException(400, "No mesh objects found in the file")

    apply_rotation(mesh_list, (rot_x, rot_y, rot_z, rot_w))

    all_parts = []
    for md in mesh_list:
        if selection_groups is not None:
            groups = selection_groups.get(md.object_id, [])
            try:
                all_parts.extend(split_by_selection(md, groups, merge_gap_mm=max(0.0, merge_gap_mm)))
            except ValueError as e:
                raise HTTPException(400, str(e))
        else:
            all_parts.extend(split_by_color(md, merge_gap_mm=max(0.0, merge_gap_mm)))

    if not all_parts:
        raise HTTPException(400, "No parts could be extracted")

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


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds MAX_UPLOAD_MB limit ({MAX_UPLOAD_MB:g} MB)")
    return data


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
