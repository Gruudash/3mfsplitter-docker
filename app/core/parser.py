"""
3MF parser: extracts mesh data with per-face color assignments.

Supports standard 3MF color groups as well as slicer-specific formats:
- Bambu Studio / OrcaSlicer  : Metadata/model_settings.config (XML extruder map)
                               Metadata/project_settings.config (JSON filament colors)
                               3D/Objects/*.model (per-object sub-files)
- PrusaSlicer                : Metadata/Slic3r_PE_model.config (XML extruder map)
                               Metadata/Slic3r_PE.config (INI filament colors)
- Creality Slicer (Cura)     : separate mesh objects per material (standard fallback)
"""

import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import numpy as np

RGBA = Tuple[int, int, int, int]
DEFAULT_COLOR: RGBA = (128, 128, 128, 255)

AUTO_PALETTE: List[str] = [
    "#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6",
    "#1abc9c", "#e67e22", "#c0392b", "#e91e63", "#00bcd4",
    "#ff5722", "#607d8b", "#795548", "#009688", "#673ab7",
]


@dataclass
class MeshData:
    vertices: np.ndarray    # (N, 3) float32
    faces: np.ndarray       # (M, 3) int32
    face_colors: np.ndarray # (M, 4) uint8 RGBA
    name: str
    object_id: str


def _hex_to_rgba(hex_str: str) -> RGBA:
    s = hex_str.strip().lstrip("#")
    if len(s) == 6:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255)
    if len(s) == 8:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16))
    return DEFAULT_COLOR


def _local_tag(elem) -> str:
    tag = elem.tag
    return tag.split("}")[-1] if "}" in tag else tag


def _decode_paint_color(paint_color: str) -> Optional[int]:
    """Decode a Bambu/PrusaSlicer 'paint_color' triangle attribute into an
    extruder index (1-based), or None if the dominant state is "not
    painted" (0) and the triangle's already-resolved color should stand.

    Format reverse-engineered from BambuStudio's
    FacetsAnnotation::set_triangle_from_string / TriangleSelector::(de)serialize
    (libslic3r/Model.cpp): the string is hex digits read right-to-left, each
    expanding to 4 bits (LSB first) appended to a bitstream. The bitstream is
    a tree, one node per 4-bit nibble: the low 2 bits give split_sides (0 =
    leaf, otherwise split_sides+1 children follow, depth-first); a leaf's
    state is either its top 2 bits directly (state < 3) or, when both top
    bits are 1, an extended value read from following nibbles (0xF-chained,
    +3 offset). State 0 = unpainted; state N>=1 = ExtruderN.

    A triangle can be recursively subdivided for fine detail painting. We
    don't reproduce the sub-triangle geometry, so as an approximation we
    return whichever state covers the largest share of the leaves.
    """
    try:
        bits: List[int] = []
        for ch in reversed(paint_color):
            v = int(ch, 16)
            bits.extend((v >> i) & 1 for i in range(4))

        pos = 0

        def read_nibble() -> int:
            nonlocal pos
            n = 0
            for i in range(4):
                n |= bits[pos] << i
                pos += 1
            return n

        def parse_node() -> List[int]:
            code = read_nibble()
            split_sides = code & 0b11
            if split_sides == 0:
                if (code & 0b1100) == 0b1100:
                    num = 0
                    next_code = read_nibble()
                    while next_code == 0b1111:
                        num += 1
                        next_code = read_nibble()
                    return [next_code + 15 * num + 3]
                return [code >> 2]
            states: List[int] = []
            for _ in range(split_sides + 1):
                states.extend(parse_node())
            return states

        states = parse_node()
        if not states:
            return None
        dominant, _ = Counter(states).most_common(1)[0]
        return dominant if dominant > 0 else None
    except (ValueError, IndexError):
        return None


# -- Slicer-specific metadata readers -----------------------------------------

def _read_extruder_colors(zf: zipfile.ZipFile, names: List[str]) -> Dict[str, str]:
    """Return {extruder_num_str: hex_color} from slicer metadata files."""

    # Bambu / OrcaSlicer: project_settings.config  (JSON)
    for candidate in ("Metadata/project_settings.config",
                       "Metadata/slicing_info.config"):
        if candidate in names:
            try:
                data = json.loads(zf.read(candidate))
                colors = data.get("filament_colour", [])
                if isinstance(colors, str):
                    # may be newline- or semicolon-separated
                    parts = []
                    for seg in colors.splitlines():
                        parts.extend(seg.split(";"))
                    colors = [c.strip() for c in parts if c.strip()]
                if isinstance(colors, list) and colors:
                    return {
                        str(i + 1): (c if c.startswith("#") else "#" + c)
                        for i, c in enumerate(colors)
                    }
            except Exception:
                pass

    # PrusaSlicer: Slic3r_PE.config  (INI-style)
    for candidate in ("Metadata/Slic3r_PE.config",
                       "Metadata/Slic3rPE.config",
                       "Metadata/slic3r_pe.config"):
        if candidate in names:
            try:
                text = zf.read(candidate).decode("utf-8", errors="replace")
                # .+ matches everything to end of line (dot excludes newline)
                m = re.search(r"filament_colour\s*=\s*(.+)", text)
                if m:
                    colors = [c.strip() for c in m.group(1).split(";") if c.strip()]
                    if colors:
                        return {
                            str(i + 1): (c if c.startswith("#") else "#" + c)
                            for i, c in enumerate(colors)
                        }
            except Exception:
                pass

    return {}


def _read_object_extruders(zf: zipfile.ZipFile, names: List[str]) -> Dict[str, str]:
    """Return {object_id_str: extruder_num_str} from slicer metadata files."""

    # Bambu / OrcaSlicer: model_settings.config  (XML)
    if "Metadata/model_settings.config" in names:
        try:
            root = ET.fromstring(zf.read("Metadata/model_settings.config"))
            result: Dict[str, str] = {}
            for elem in root.iter():
                tag = _local_tag(elem)
                if tag in ("object", "part"):
                    obj_id = elem.get("id")
                    if not obj_id:
                        continue
                    for meta in elem:
                        if (_local_tag(meta) == "metadata"
                                and meta.get("key") == "extruder"):
                            result[obj_id] = meta.get("value", "1")
            # Propagate parent extruder to component children.
            # Bambu: 3D/3dmodel.model object id="2" has <component objectid="1">
            # but model_settings.config records id="2", while the mesh file uses id="1".
            if "3D/3dmodel.model" in names:
                try:
                    mroot = ET.fromstring(zf.read("3D/3dmodel.model"))
                    for mobj in mroot.iter():
                        if _local_tag(mobj) != "object":
                            continue
                        parent_id = mobj.get("id")
                        if not parent_id or parent_id not in result:
                            continue
                        for comp in mobj.iter():
                            if _local_tag(comp) != "component":
                                continue
                            child_id = comp.get("objectid")
                            if child_id and child_id not in result:
                                result[child_id] = result[parent_id]
                except Exception:
                    pass
            return result
        except Exception:
            pass

    # PrusaSlicer: Slic3r_PE_model.config  (XML)
    for candidate in ("Metadata/Slic3r_PE_model.config",
                       "Metadata/Slic3rPE_model.config",
                       "Metadata/slic3r_pe_model.config"):
        if candidate in names:
            try:
                root = ET.fromstring(zf.read(candidate))
                result = {}
                for elem in root.iter():
                    if _local_tag(elem) != "object":
                        continue
                    obj_id = elem.get("id")
                    if not obj_id:
                        continue
                    for meta in elem.iter():
                        if _local_tag(meta) != "metadata":
                            continue
                        if meta.get("key") == "extruder":
                            result[obj_id] = meta.get("value", "1")
                return result
            except Exception:
                pass

    return {}


def _read_prusa_volume_ranges(
    zf: zipfile.ZipFile,
    names: List[str],
    extruder_colors: Dict[str, str],
) -> Dict[str, List[dict]]:
    """Return {object_id: [{firstid, lastid, color}]} from PrusaSlicer volume info."""
    for candidate in ("Metadata/Slic3r_PE_model.config",
                       "Metadata/Slic3rPE_model.config",
                       "Metadata/slic3r_pe_model.config"):
        if candidate not in names:
            continue
        try:
            root = ET.fromstring(zf.read(candidate))
            result: Dict[str, List[dict]] = {}
            for elem in root.iter():
                if _local_tag(elem) != "object":
                    continue
                obj_id = elem.get("id")
                if not obj_id:
                    continue
                ranges: List[dict] = []
                for vol in elem:
                    if _local_tag(vol) != "volume":
                        continue
                    firstid = int(vol.get("firstid", "0"))
                    lastid  = int(vol.get("lastid", "-1"))
                    for meta in vol:
                        if (_local_tag(meta) == "metadata"
                                and meta.get("key") == "extruder"):
                            ext = meta.get("value", "1")
                            color = extruder_colors.get(ext)
                            if color:
                                ranges.append({"firstid": firstid,
                                               "lastid":  lastid,
                                               "color":   _hex_to_rgba(color)})
                if ranges:
                    result[obj_id] = ranges
            return result
        except Exception:
            pass
    return {}


# -- Per-model-file mesh parser ------------------------------------------------

def _parse_model_xml(
    xml_bytes: bytes,
    color_groups: Dict[str, List[RGBA]],
    extruder_colors: Dict[str, str],
    object_extruders: Dict[str, str],
    volume_ranges: Dict[str, List[dict]],
) -> List[MeshData]:
    """Parse a single .model XML file and return MeshData objects."""
    root = ET.fromstring(xml_bytes)

    # Collect <m:colorgroup> from this file
    for elem in root.iter():
        if _local_tag(elem) == "colorgroup":
            gid = elem.get("id")
            if gid is None:
                continue
            colors: List[RGBA] = []
            for child in elem:
                if _local_tag(child) == "color":
                    colors.append(_hex_to_rgba(child.get("color", "#808080")))
            color_groups[gid] = colors

    results: List[MeshData] = []

    for obj in root.iter():
        if _local_tag(obj) != "object":
            continue
        if obj.get("type") == "support":
            continue

        obj_id = obj.get("id", "")
        obj_name = obj.get("name", f"object_{obj_id}")
        obj_pid = obj.get("pid")
        obj_p1 = obj.get("p1")

        # Priority: standard colorgroup > direct color attr > extruder metadata > gray
        # (applied lowest-priority-first so each later check overrides the last)
        default_color: RGBA = DEFAULT_COLOR

        ext_num = object_extruders.get(obj_id)
        if ext_num and ext_num in extruder_colors:
            default_color = _hex_to_rgba(extruder_colors[ext_num])

        raw_color_attr = obj.get("color")
        if raw_color_attr:
            default_color = _hex_to_rgba(raw_color_attr)

        if obj_pid and obj_p1 and obj_pid in color_groups:
            idx = int(obj_p1)
            cg = color_groups[obj_pid]
            if 0 <= idx < len(cg):
                default_color = cg[idx]

        # Find <mesh>
        mesh_elem: Optional[ET.Element] = None
        for child in obj.iter():
            if _local_tag(child) == "mesh":
                mesh_elem = child
                break
        if mesh_elem is None:
            continue

        verts_elem: Optional[ET.Element] = None
        tris_elem: Optional[ET.Element] = None
        for child in mesh_elem:
            lt = _local_tag(child)
            if lt == "vertices":
                verts_elem = child
            elif lt == "triangles":
                tris_elem = child

        if verts_elem is None or tris_elem is None:
            continue

        raw_verts = []
        for v in verts_elem:
            if _local_tag(v) == "vertex":
                raw_verts.append([
                    float(v.get("x", 0)),
                    float(v.get("y", 0)),
                    float(v.get("z", 0)),
                ])
        if not raw_verts:
            continue

        vol_ranges = volume_ranges.get(obj_id, [])

        raw_faces = []
        raw_colors = []
        face_idx = 0
        for tri in tris_elem:
            if _local_tag(tri) != "triangle":
                continue
            raw_faces.append([
                int(tri.get("v1", 0)),
                int(tri.get("v2", 0)),
                int(tri.get("v3", 0)),
            ])
            pid = tri.get("pid", obj_pid)
            p1 = tri.get("p1", obj_p1)
            color = default_color

            # Standard 3MF color group (pid/p1)
            if pid and p1 and pid in color_groups:
                idx = int(p1)
                cg = color_groups[pid]
                if 0 <= idx < len(cg):
                    color = cg[idx]

            # Bambu paint_color attribute: recursive per-triangle MMU paint
            # state, decoded via _decode_paint_color(). A dominant state of
            # 0 (unpainted) keeps the color already resolved above.
            paint_color = tri.get("paint_color")
            if paint_color and extruder_colors:
                paint_ext_num = _decode_paint_color(paint_color)
                if paint_ext_num is not None and str(paint_ext_num) in extruder_colors:
                    color = _hex_to_rgba(extruder_colors[str(paint_ext_num)])

            # PrusaSlicer volume face ranges
            for rng in vol_ranges:
                if rng["firstid"] <= face_idx <= rng["lastid"]:
                    color = rng["color"]
                    break

            raw_colors.append(color)
            face_idx += 1

        if not raw_faces:
            continue

        results.append(MeshData(
            vertices=np.array(raw_verts, dtype=np.float32),
            faces=np.array(raw_faces, dtype=np.int32),
            face_colors=np.array(raw_colors, dtype=np.uint8),
            name=obj_name,
            object_id=obj_id,
        ))

    return results


# -- Public API ----------------------------------------------------------------

def parse_3mf(path: str) -> List[MeshData]:
    """Parse a .3mf file and return one MeshData per colored object."""
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()

        # Read slicer metadata
        extruder_colors  = _read_extruder_colors(zf, names)
        object_extruders = _read_object_extruders(zf, names)
        volume_ranges    = _read_prusa_volume_ranges(zf, names, extruder_colors)

        # Shared color_groups dict, populated across all .model files
        color_groups: Dict[str, List[RGBA]] = {}

        # Parse every .model file (Bambu puts objects in 3D/Objects/*.model)
        all_results: List[MeshData] = []
        model_paths = [n for n in names if n.endswith(".model")]
        if not model_paths:
            raise ValueError("No .model file found inside the 3MF archive")

        for model_path in model_paths:
            xml_bytes = zf.read(model_path)
            results = _parse_model_xml(
                xml_bytes, color_groups, extruder_colors,
                object_extruders, volume_ranges
            )
            all_results.extend(results)

    if not all_results:
        return all_results

    # Auto-color: if every part is gray, assign palette colors by position
    if all(tuple(md.face_colors[0]) == DEFAULT_COLOR for md in all_results):
        for i, md in enumerate(all_results):
            color = _hex_to_rgba(AUTO_PALETTE[i % len(AUTO_PALETTE)])
            md.face_colors[:] = color

    return all_results


def apply_rotation(mesh_list: List[MeshData], quat_xyzw: Tuple[float, float, float, float]) -> None:
    """Rotate every MeshData's vertices in place around the shared bounding-box
    center of the whole model, using an (x, y, z, w) quaternion.

    Mirrors the frontend's "place on plate" tool, which lets the user pick a
    face of the assembled model to orient it before splitting -- so the
    orientation chosen in the viewer is reflected in the exported parts.
    """
    x, y, z, w = quat_xyzw
    if abs(x) < 1e-9 and abs(y) < 1e-9 and abs(z) < 1e-9 and abs(w - 1.0) < 1e-9:
        return  # identity quaternion, nothing to do

    from scipy.spatial.transform import Rotation

    all_verts = np.concatenate([md.vertices for md in mesh_list], axis=0)
    center = (all_verts.min(axis=0) + all_verts.max(axis=0)) / 2.0

    rot = Rotation.from_quat([x, y, z, w])
    for md in mesh_list:
        md.vertices = (rot.apply(md.vertices.astype(np.float64) - center) + center).astype(np.float32)
