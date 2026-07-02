"""
Connector geometry generation for 3MF split parts.

Three connector types are supported:
- MAGNET   : Cylindrical holes in both parts for embedded magnets (5x3mm default)
- PEG      : Cylindrical peg on part A + matching socket in part B (Steg/Zapfen)
- DOVETAIL : Trapezoidal plug on part A + matching pocket in part B (Schwalbenschwanz)

All connector types use boolean subtraction/union via manifold3d. If a boolean
operation fails for a connector position it is silently skipped so that the
remaining connectors are still applied.
"""

from enum import Enum
from typing import List, Tuple

import numpy as np
import trimesh
import trimesh.boolean
import trimesh.creation
import trimesh.transformations

try:
    from shapely.geometry import Polygon as ShapelyPolygon
    _HAS_SHAPELY = True
except ImportError:
    _HAS_SHAPELY = False


class ConnectorType(str, Enum):
    NONE = "none"
    MAGNET = "magnet"
    PEG = "peg"
    DOVETAIL = "dovetail"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rotation_to_normal(normal: np.ndarray) -> np.ndarray:
    """Return a 4x4 rotation matrix that maps +Z to the given normal."""
    normal = normal / np.linalg.norm(normal)
    z = np.array([0.0, 0.0, 1.0])
    axis = np.cross(z, normal)
    axis_len = np.linalg.norm(axis)
    if axis_len < 1e-6:
        # Parallel or anti-parallel
        if np.dot(z, normal) > 0:
            return np.eye(4)
        else:
            return trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0])
    axis = axis / axis_len
    angle = np.arccos(np.clip(np.dot(z, normal), -1.0, 1.0))
    return trimesh.transformations.rotation_matrix(angle, axis)


def _cylinder_at(center: np.ndarray, normal: np.ndarray,
                 radius: float, height: float) -> trimesh.Trimesh:
    """Create a cylinder centred at *center* with its axis along *normal*."""
    cyl = trimesh.creation.cylinder(radius=radius, height=height, sections=32)
    rot = _rotation_to_normal(normal)
    cyl.apply_transform(rot)
    cyl.apply_translation(center)
    return cyl


def _dovetail_mesh(center: np.ndarray, normal: np.ndarray, tangent: np.ndarray,
                   width: float, height: float, depth: float,
                   draft_deg: float, clearance: float = 0.0) -> trimesh.Trimesh:
    """
    Create a dovetail-profile prism extruded along *normal*.

    The cross-section sits in the (tangent × bitangent) plane at *center*.
    A positive *clearance* enlarges the profile on all sides (for the socket).
    """
    if not _HAS_SHAPELY:
        # Fall back to a plain box if shapely is unavailable
        box = trimesh.creation.box([width + 2*clearance,
                                    width + 2*clearance,
                                    height + clearance])
        box.apply_translation(center + normal * (height + clearance) / 2)
        return box

    angle = np.radians(draft_deg)
    hw = width / 2 + clearance
    undercut = (height + clearance) * np.tan(angle)

    poly = ShapelyPolygon([
        (-hw + undercut, 0 - clearance),
        (-hw,            height + clearance),
        ( hw,            height + clearance),
        ( hw - undercut, 0 - clearance),
    ])
    profile = trimesh.creation.extrude_polygon(poly, depth + clearance)

    # Build local frame: tangent→X, bitangent→Y, normal→Z
    normal = normal / np.linalg.norm(normal)
    tangent = tangent / np.linalg.norm(tangent)
    bitangent = np.cross(normal, tangent)

    mat = np.eye(4)
    mat[:3, 0] = tangent
    mat[:3, 1] = bitangent
    mat[:3, 2] = normal
    mat[:3, 3] = center

    profile.apply_transform(mat)
    return profile


def _safe_difference(base: trimesh.Trimesh,
                     tool: trimesh.Trimesh) -> trimesh.Trimesh:
    try:
        result = trimesh.boolean.difference([base, tool], engine='manifold')
        if result is not None and len(result.faces) > 0:
            return result
    except Exception:
        pass
    return base


def _safe_union(base: trimesh.Trimesh,
                addition: trimesh.Trimesh) -> trimesh.Trimesh:
    try:
        result = trimesh.boolean.union([base, addition], engine='manifold')
        if result is not None and len(result.faces) > 0:
            return result
    except Exception:
        pass
    return base


# ---------------------------------------------------------------------------
# Interface detection
# ---------------------------------------------------------------------------

def find_interface_points(
    part_a: trimesh.Trimesh,
    part_b: trimesh.Trimesh,
    n_connectors: int = 3,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Estimate connector placement positions at the interface between two parts.

    Returns (points, normals, tangents) each of shape (K, 3) where K <= n_connectors.
    Normals point from part_a toward part_b.
    """
    bounds_a = part_a.bounds        # [[min_x, min_y, min_z], [max_x, max_y, max_z]]
    bounds_b = part_b.bounds

    overlap_min = np.maximum(bounds_a[0], bounds_b[0])
    overlap_max = np.minimum(bounds_a[1], bounds_b[1])

    # Direction from A centroid to B centroid
    ab_vec = part_b.centroid - part_a.centroid
    ab_len = np.linalg.norm(ab_vec)
    ab_norm = ab_vec / ab_len if ab_len > 1e-6 else np.array([0.0, 0.0, 1.0])

    # Pick a tangent perpendicular to ab_norm
    up = np.array([0.0, 0.0, 1.0])
    if abs(np.dot(ab_norm, up)) > 0.9:
        up = np.array([1.0, 0.0, 0.0])
    tangent = np.cross(ab_norm, up)
    tangent /= np.linalg.norm(tangent)

    if np.any(overlap_min >= overlap_max):
        # No bounding-box overlap: place single connector at midpoint
        midpoint = (part_a.centroid + part_b.centroid) / 2
        return np.array([midpoint]), np.array([ab_norm]), np.array([tangent])

    overlap_center = (overlap_min + overlap_max) / 2
    overlap_size = overlap_max - overlap_min

    # Distribute connectors along the longest overlap axis
    longest_axis = int(np.argmax(overlap_size))
    spread = overlap_size[longest_axis] * 0.6

    points = []
    for i in range(n_connectors):
        pt = overlap_center.copy()
        if n_connectors > 1:
            t = (i / (n_connectors - 1) - 0.5) * spread
            pt[longest_axis] += t
        points.append(pt)

    pts_arr = np.array(points)
    normals = np.tile(ab_norm, (len(pts_arr), 1))
    tangents = np.tile(tangent, (len(pts_arr), 1))
    return pts_arr, normals, tangents


# ---------------------------------------------------------------------------
# Public connector API
# ---------------------------------------------------------------------------

def add_magnet_holes(
    part_a: trimesh.Trimesh,
    part_b: trimesh.Trimesh,
    points: np.ndarray,
    normals: np.ndarray,
    radius: float = 2.6,
    depth: float = 3.2,
) -> Tuple[trimesh.Trimesh, trimesh.Trimesh]:
    """
    Subtract cylindrical magnet pockets from both parts at each interface point.

    Default dimensions fit standard 5 mm x 3 mm disc magnets with 0.1 mm clearance.
    The hole in part_a goes into A (against the normal), the hole in part_b goes
    into B (along the normal).
    """
    for pos, normal in zip(points, normals):
        normal = normal / np.linalg.norm(normal)
        center_a = pos - normal * depth / 2
        center_b = pos + normal * depth / 2

        hole_a = _cylinder_at(center_a, -normal, radius, depth + 1.0)
        hole_b = _cylinder_at(center_b,  normal, radius, depth + 1.0)

        part_a = _safe_difference(part_a, hole_a)
        part_b = _safe_difference(part_b, hole_b)

    return part_a, part_b


def add_pegs(
    part_a: trimesh.Trimesh,
    part_b: trimesh.Trimesh,
    points: np.ndarray,
    normals: np.ndarray,
    radius: float = 2.0,
    height: float = 3.5,
    clearance: float = 0.15,
) -> Tuple[trimesh.Trimesh, trimesh.Trimesh]:
    """
    Add cylindrical pegs (Steg/Zapfen) to part_a and matching sockets to part_b.

    The peg protrudes from part_a toward part_b along the interface normal.
    """
    for pos, normal in zip(points, normals):
        normal = normal / np.linalg.norm(normal)
        peg_center = pos + normal * height / 2
        socket_center = pos + normal * (height / 2 + clearance)

        peg    = _cylinder_at(peg_center,    normal,  radius,             height)
        socket = _cylinder_at(socket_center, normal,  radius + clearance, height + 2 * clearance)

        part_a = _safe_union(part_a, peg)
        part_b = _safe_difference(part_b, socket)

    return part_a, part_b


def add_dovetails(
    part_a: trimesh.Trimesh,
    part_b: trimesh.Trimesh,
    points: np.ndarray,
    normals: np.ndarray,
    tangents: np.ndarray,
    width: float = 7.0,
    height: float = 4.5,
    depth: float = 5.0,
    draft_deg: float = 12.0,
    clearance: float = 0.2,
) -> Tuple[trimesh.Trimesh, trimesh.Trimesh]:
    """
    Add trapezoidal dovetail plugs (Schwalbenschwanz) to part_a and matching
    pockets to part_b.

    The plug protrudes from part_a toward part_b along the interface normal.
    The draft angle creates the characteristic dovetail undercut.
    """
    for pos, normal, tangent in zip(points, normals, tangents):
        normal  = normal  / np.linalg.norm(normal)
        tangent = tangent / np.linalg.norm(tangent)

        center_plug   = pos + normal * height / 2
        center_socket = pos + normal * (height / 2 + clearance)

        plug   = _dovetail_mesh(center_plug,   normal, tangent,
                                width, height, depth, draft_deg, clearance=0.0)
        socket = _dovetail_mesh(center_socket, normal, tangent,
                                width, height, depth, draft_deg, clearance=clearance)

        part_a = _safe_union(part_a, plug)
        part_b = _safe_difference(part_b, socket)

    return part_a, part_b


def _bbox_gap(mesh_a: trimesh.Trimesh, mesh_b: trimesh.Trimesh) -> float:
    """Distance between two axis-aligned bounding boxes (<= 0 if they overlap)."""
    amin, amax = mesh_a.bounds
    bmin, bmax = mesh_b.bounds
    axis_gap = np.maximum(0.0, np.maximum(amin - bmax, bmin - amax))
    return float(np.linalg.norm(axis_gap))


# Parts whose bounding boxes are within this distance are considered to be
# touching at a print seam (small gaps are expected from mesh repair).
ADJACENCY_MAX_GAP = 1.5


def find_adjacent_pairs(parts: List, max_gap: float = ADJACENCY_MAX_GAP) -> List[Tuple[int, int]]:
    """Return index pairs of parts whose bounding boxes touch or nearly touch."""
    pairs = []
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            if _bbox_gap(parts[i].mesh, parts[j].mesh) <= max_gap:
                pairs.append((i, j))
    return pairs


def apply_connectors(
    parts: List,  # List[ColorPart] — imported lazily to avoid circular import
    connector_type: ConnectorType,
    n_connectors: int = 3,
    params: dict = None,
) -> List:
    """
    Apply connectors between every physically adjacent pair of parts
    (detected via bounding-box proximity, not list order -- several parts
    can share a color without being neighbours).
    *params* is forwarded as kwargs to the specific connector function.
    """
    if connector_type == ConnectorType.NONE or len(parts) < 2:
        return parts

    kw = params or {}

    for i, j in find_adjacent_pairs(parts):
        a = parts[i]
        b = parts[j]

        pts, normals, tangents = find_interface_points(a.mesh, b.mesh, n_connectors)

        if connector_type == ConnectorType.MAGNET:
            a.mesh, b.mesh = add_magnet_holes(a.mesh, b.mesh, pts, normals, **kw)
        elif connector_type == ConnectorType.PEG:
            a.mesh, b.mesh = add_pegs(a.mesh, b.mesh, pts, normals, **kw)
        elif connector_type == ConnectorType.DOVETAIL:
            a.mesh, b.mesh = add_dovetails(a.mesh, b.mesh, pts, normals, tangents, **kw)

    return parts
