"""
Color-based mesh splitter.

Groups triangles by their RGBA color, then clusters each color region's
disconnected mesh islands by spatial proximity: islands only a fraction of
a millimetre apart are cut-boundary artifacts (paint triangles of another
color were removed in between) and get fused back into one part, while
islands that are genuinely far apart (e.g. two separate same-color limbs)
stay separate parts. Each open island is turned into a proper watertight
solid by giving it wall thickness (offsetting a copy inward and stitching
the cut boundary), not by re-triangulating a cap across the hole -- capping
a large, irregular boundary loop (e.g. following a scale pattern) produces
chaotic, self-intersecting interior geometry instead of the real shape.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import trimesh
import trimesh.boolean
from scipy.spatial import cKDTree

try:
    import pymeshfix
    _HAS_PYMESHFIX = True
except ImportError:
    _HAS_PYMESHFIX = False

from .parser import MeshData, _hex_to_rgba

RGBA = Tuple[int, int, int, int]

# Fragments with fewer faces than this can never enclose a volume (a
# tetrahedron needs 4) -- they are cut artifacts, not real geometry.
MIN_FRAGMENT_FACES = 4

# Same-color islands whose surfaces are within this distance (mm) of each
# other are merged into one part. Empirically, color-boundary cut artifacts
# leave gaps well under 0.5mm, while genuinely separate physical pieces
# (e.g. two same-color legs) are several mm or more apart.
DEFAULT_MERGE_GAP_MM = 2.0

# Wall thickness (mm) given to an open surface patch to turn it into a
# printable solid -- a few FDM perimeters' worth.
SHELL_THICKNESS_MM = 1.2


def _solidify_island(island: trimesh.Trimesh, thickness: float = SHELL_THICKNESS_MM) -> Optional[trimesh.Trimesh]:
    """Turn a single connected mesh island into a watertight solid.

    Returns None if the island is degenerate noise. If the island is
    already closed (some color regions happen to wrap into a full loop on
    their own) it's returned as-is. Otherwise the outer surface is kept
    exactly as parsed, a copy offset inward along vertex normals is added
    as the inner surface, and the two are stitched together with a wall of
    quads along the open boundary -- this preserves every detail of the
    original geometry instead of guessing a triangulation across the hole.
    """
    if len(island.faces) < MIN_FRAGMENT_FACES:
        return None

    island = island.copy()
    island.fix_normals()

    if island.is_watertight:
        return island

    outer_v = island.vertices
    normals = island.vertex_normals
    inner_v = outer_v - normals * thickness
    n_v = len(outer_v)
    inner_faces = island.faces[:, ::-1] + n_v

    # Boundary edges: those used by exactly one face, kept in the direction
    # the owning face winds them so the stitched wall faces outward too.
    sorted_edges = np.sort(island.edges, axis=1)
    _, inverse, counts = np.unique(sorted_edges, axis=0, return_inverse=True, return_counts=True)
    boundary_edges = island.edges[counts[inverse] == 1]

    if len(boundary_edges) == 0:
        return island  # no hole to stitch, nothing more to do

    a = boundary_edges[:, 0]
    b = boundary_edges[:, 1]
    ia, ib = a + n_v, b + n_v
    wall_faces = np.vstack([
        np.column_stack([a, b, ib]),
        np.column_stack([a, ib, ia]),
    ])

    all_vertices = np.vstack([outer_v, inner_v])
    all_faces = np.vstack([island.faces, inner_faces, wall_faces])

    shell = trimesh.Trimesh(vertices=all_vertices, faces=all_faces, process=True)
    if len(shell.faces) < MIN_FRAGMENT_FACES:
        return None
    shell.fix_normals()

    if not shell.is_watertight and _HAS_PYMESHFIX:
        # Rare case: multiple/non-simple boundary loops made the wall
        # stitch self-intersect. Patch just this one island with pymeshfix
        # -- safe here because it's a single island, not the whole cluster.
        try:
            mf = pymeshfix.MeshFix(shell.vertices, shell.faces)
            mf.repair(remove_smallest_components=False)
            if len(mf.faces) >= MIN_FRAGMENT_FACES:
                shell = trimesh.Trimesh(vertices=mf.points, faces=mf.faces, process=False)
        except Exception:
            pass

    return shell


def _cluster_by_proximity(fragments: List[trimesh.Trimesh], max_gap: float) -> List[List[int]]:
    """Group fragment indices whose surfaces are within *max_gap* of each
    other (transitively, via union-find), regardless of fragment size."""
    n = len(fragments)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    trees = [cKDTree(f.vertices) for f in fragments]
    for i in range(n):
        for j in range(i + 1, n):
            dist, _ = trees[j].query(fragments[i].vertices, k=1)
            if dist.min() <= max_gap:
                union(i, j)

    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


@dataclass
class ColorPart:
    mesh: trimesh.Trimesh
    color: RGBA
    color_hex: str
    name: str


def _faces_to_solids(
    mesh_data: MeshData,
    face_indices: List[int],
    merge_gap_mm: float,
) -> List[trimesh.Trimesh]:
    """Turn an arbitrary set of *mesh_data* face indices into one or more
    watertight solids: the sub-mesh they form is split into raw connected
    islands, each island is solidified, islands within *merge_gap_mm* of
    each other are fused into one physical piece via a real boolean union
    (falling back to a repaired concatenation if that union fails -- see
    module docstring), and each resulting cluster becomes one solid.

    Shared by split_by_color (groups faces by paint color) and
    split_by_selection (groups faces by explicit user choice) so the mesh
    repair logic only has to be gotten right once.
    """
    # Defensive dedup: duplicate indices would otherwise add doubled,
    # coincident triangles (process=False skips trimesh's own merge),
    # which can spuriously break is_watertight and trigger needless repair.
    face_indices = list(dict.fromkeys(face_indices))
    face_idx_arr = np.array(face_indices, dtype=np.int32)
    selected_faces = mesh_data.faces[face_idx_arr]

    unique_verts, inverse = np.unique(selected_faces.flatten(), return_inverse=True)
    new_vertices = mesh_data.vertices[unique_verts].astype(np.float64)
    new_faces = inverse.reshape(-1, 3)
    sub_mesh = trimesh.Trimesh(vertices=new_vertices, faces=new_faces, process=False)

    raw_islands = [f for f in sub_mesh.split(only_watertight=False)
                   if len(f.faces) >= MIN_FRAGMENT_FACES]
    islands = [r for r in (_solidify_island(isl) for isl in raw_islands) if r is not None]
    if not islands:
        return []

    clusters = (_cluster_by_proximity(islands, merge_gap_mm)
                if len(islands) > 1 else [[0]])

    solids: List[trimesh.Trimesh] = []
    for idxs in clusters:
        # Islands are already individually watertight solids -- fuse
        # them with a real boolean union into one coherent body.
        # Plain concatenation leaves each island as its own separate,
        # overlapping shell inside the same file: some slicers (Prusa)
        # tolerate that, but Cura/Creality treat it as a non-manifold
        # compound object and disable tools like "place on face", and
        # the overlaps show up as double interior walls.
        cluster_islands = [islands[i] for i in idxs]
        merged = cluster_islands[0]
        if len(cluster_islands) > 1:
            fell_back = False
            try:
                unioned = trimesh.boolean.union(cluster_islands, engine='manifold')
                if unioned is not None and len(unioned.faces) > 0:
                    merged = unioned
                else:
                    merged = trimesh.util.concatenate(cluster_islands)
                    fell_back = True
            except Exception:
                merged = trimesh.util.concatenate(cluster_islands)
                fell_back = True

            # The concatenate fallback stacks each island as its own
            # overlapping shell instead of one solid -- slicers treat
            # that as non-manifold. Repair it the same way a single
            # island's stitched wall gets repaired in _solidify_island.
            if fell_back and not merged.is_watertight and _HAS_PYMESHFIX:
                try:
                    mf = pymeshfix.MeshFix(merged.vertices, merged.faces)
                    mf.repair(remove_smallest_components=False)
                    if len(mf.faces) >= MIN_FRAGMENT_FACES:
                        merged = trimesh.Trimesh(vertices=mf.points, faces=mf.faces, process=False)
                except Exception:
                    pass
        if len(merged.faces) >= MIN_FRAGMENT_FACES:
            solids.append(merged)
    return solids


def split_by_color(
    mesh_data: MeshData,
    merge_gap_mm: float = DEFAULT_MERGE_GAP_MM,
) -> List[ColorPart]:
    """Split mesh data into separate, individually exportable parts.

    Faces are first grouped by color, then each color region's raw mesh
    islands (broken apart wherever a differently-colored triangle used to
    sit between them) are re-clustered by spatial proximity: islands within
    *merge_gap_mm* of each other are the same physical piece and get fused
    back together, while islands genuinely far apart (e.g. two same-color
    legs) stay separate exportable parts.
    """
    color_to_faces: dict = {}
    for i, c in enumerate(mesh_data.face_colors):
        key = tuple(int(x) for x in c)
        color_to_faces.setdefault(key, []).append(i)

    color_counts: dict = {}
    parts: List[ColorPart] = []

    for color, face_indices in color_to_faces.items():
        r, g, b, a = color
        hex_color = f"#{r:02x}{g:02x}{b:02x}"

        for merged in _faces_to_solids(mesh_data, face_indices, merge_gap_mm):
            n = color_counts.get(hex_color, 0)
            color_counts[hex_color] = n + 1
            suffix = "" if n == 0 else f"_{n + 1}"

            parts.append(ColorPart(
                mesh=merged,
                color=color,
                color_hex=hex_color,
                name=f"{mesh_data.name}_{hex_color}{suffix}",
            ))

    # Sort by face count descending so the largest part is first
    parts.sort(key=lambda p: len(p.mesh.faces), reverse=True)
    return parts


def split_by_selection(
    mesh_data: MeshData,
    groups: List[dict],
    merge_gap_mm: float = DEFAULT_MERGE_GAP_MM,
) -> List[ColorPart]:
    """Split mesh_data using explicit, user-chosen face groups instead of
    automatic per-color grouping -- color boundaries in a model don't
    always make sensible assembly boundaries (e.g. a paint region can cut
    straight through a mating surface), so the caller picks which faces
    belong together instead of every color edge becoming a cut.

    Each entry in *groups* is {"label": str, "face_indices": [int, ...],
    "color_hex": Optional[str]}. Faces not claimed by any group become one
    implicit "rest" part covering the remainder of mesh_data, so nothing
    from the original model is ever silently dropped.

    Raises ValueError if two groups claim the same face -- silently letting
    that through would duplicate volume across two exported parts, which is
    exactly the "doesn't fit back together" problem this whole feature
    exists to avoid.
    """
    total_faces = len(mesh_data.faces)
    claimed: dict = {}  # face index -> label of the group that claimed it
    name_counts: dict = {}
    parts: List[ColorPart] = []

    def _add_parts(face_indices: List[int], label: str, color_hex: str) -> None:
        color = _hex_to_rgba(color_hex)
        for merged in _faces_to_solids(mesh_data, face_indices, merge_gap_mm):
            n = name_counts.get(label, 0)
            name_counts[label] = n + 1
            suffix = "" if n == 0 else f"_{n + 1}"
            parts.append(ColorPart(
                mesh=merged,
                color=color,
                color_hex=color_hex,
                name=f"{mesh_data.name}_{label}{suffix}",
            ))

    for group in groups:
        face_indices = list(dict.fromkeys(
            i for i in group.get("face_indices", []) if 0 <= i < total_faces
        ))
        if not face_indices:
            continue
        label = group.get("label") or f"teil_{len(parts) + 1}"
        for i in face_indices:
            if i in claimed:
                raise ValueError(
                    f"face {i} is selected in both '{claimed[i]}' and '{label}' "
                    "-- selection groups must not overlap"
                )
            claimed[i] = label
        color_hex = group.get("color_hex") or "#808080"
        _add_parts(face_indices, label, color_hex)

    rest_indices = [i for i in range(total_faces) if i not in claimed]
    if rest_indices:
        _add_parts(rest_indices, "rest", "#808080")

    parts.sort(key=lambda p: len(p.mesh.faces), reverse=True)
    return parts
