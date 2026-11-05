"""Minimal GLB (binary glTF 2.0) reader for the GLB2VRM UI.

Only what the UI needs: the skeleton (joints, parents, rest positions), mesh
bounds and statistics, and whether the file already carries a VRM extension.
Pure standard library so the app has no extra dependencies.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942


class GLBError(Exception):
    """A file that is not a usable GLB."""


def _mat_identity():
    return [1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0]


def _mat_mul(a, b):
    """Column-major 4x4 multiply (glTF convention): result = a * b."""
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            out[col * 4 + row] = (
                a[0 * 4 + row] * b[col * 4 + 0]
                + a[1 * 4 + row] * b[col * 4 + 1]
                + a[2 * 4 + row] * b[col * 4 + 2]
                + a[3 * 4 + row] * b[col * 4 + 3]
            )
    return out


def _quat_matrix(x, y, z, w):
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return [
        1 - 2 * (yy + zz), 2 * (xy + wz), 2 * (xz - wy), 0.0,
        2 * (xy - wz), 1 - 2 * (xx + zz), 2 * (yz + wx), 0.0,
        2 * (xz + wy), 2 * (yz - wx), 1 - 2 * (xx + yy), 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _trs_matrix(node):
    if "matrix" in node and len(node["matrix"]) == 16:
        return [float(v) for v in node["matrix"]]
    t = node.get("translation") or (0.0, 0.0, 0.0)
    r = node.get("rotation") or (0.0, 0.0, 0.0, 1.0)
    s = node.get("scale") or (1.0, 1.0, 1.0)
    m = _quat_matrix(float(r[0]), float(r[1]), float(r[2]), float(r[3]))
    for row in range(3):
        m[row] *= float(s[0])
        m[4 + row] *= float(s[1])
        m[8 + row] *= float(s[2])
    m[12], m[13], m[14] = float(t[0]), float(t[1]), float(t[2])
    return m


def _transform_point(m, p):
    x, y, z = p
    return (
        m[0] * x + m[4] * y + m[8] * z + m[12],
        m[1] * x + m[5] * y + m[9] * z + m[13],
        m[2] * x + m[6] * y + m[10] * z + m[14],
    )


class GLBDoc:
    """Parsed GLB document with the skeleton and display statistics."""

    def __init__(self, path: Path, gltf: dict, bin_chunk: bytes | None):
        self.path = Path(path)
        self.gltf = gltf
        self.bin = bin_chunk or b""
        self.nodes = gltf.get("nodes") or []
        self.meshes = gltf.get("meshes") or []
        self.skins = gltf.get("skins") or []
        self.materials = gltf.get("materials") or []
        self.images = gltf.get("images") or []
        self.animations = gltf.get("animations") or []
        self.parent = {}
        for index, node in enumerate(self.nodes):
            for child in node.get("children") or ():
                self.parent[child] = index
        self.world = {}
        self._compute_world()
        self.extensions = gltf.get("extensions") or {}
        self.is_vrm = ("VRMC_vrm" in self.extensions
                       or "VRM" in self.extensions
                       or "VRMC_vrm" in (gltf.get("extensionsUsed") or []))

    # ------------------------------------------------------------- transforms
    def _compute_world(self):
        roots = []
        scene_index = self.gltf.get("scene", 0)
        scenes = self.gltf.get("scenes") or []
        if 0 <= scene_index < len(scenes):
            roots = list(scenes[scene_index].get("nodes") or ())
        if not roots:
            roots = [i for i in range(len(self.nodes)) if i not in self.parent]

        def walk(index, parent_world):
            local = _trs_matrix(self.nodes[index])
            world = _mat_mul(parent_world, local)
            self.world[index] = world
            for child in self.nodes[index].get("children") or ():
                walk(child, world)

        for root in roots:
            walk(root, _mat_identity())

    def world_pos(self, index):
        m = self.world.get(index)
        if m is None:
            return (0.0, 0.0, 0.0)
        return (m[12], m[13], m[14])

    # --------------------------------------------------------------- skeleton
    def skeleton(self):
        """Largest skin: joints with name, parent joint index and rest position."""
        if not self.skins:
            return None
        skin = max(self.skins, key=lambda s: len(s.get("joints") or ()))
        joints = list(skin.get("joints") or ())
        if not joints:
            return None
        joint_set = set(joints)
        index_of = {node: k for k, node in enumerate(joints)}
        parents = []
        for node in joints:
            up = self.parent.get(node)
            while up is not None and up not in joint_set:
                up = self.parent.get(up)
            parents.append(index_of.get(up, -1))
        entries = []
        for k, node in enumerate(joints):
            entries.append({
                "name": str(self.nodes[node].get("name") or f"bone_{node}"),
                "parent": parents[k],
                "pos": [round(v, 5) for v in self.world_pos(node)],
                "node": node,
            })
        return {
            "name": str(skin.get("name") or (self.nodes[joints[0]].get("name") or "skeleton")),
            "joints": entries,
            "skeleton_node": skin.get("skeleton"),
        }

    # ------------------------------------------------------------------ stats
    def stats(self):
        verts = 0
        tris = 0
        prims = 0
        skinned = False
        for mesh in self.meshes:
            for prim in mesh.get("primitives") or ():
                prims += 1
                attrs = prim.get("attributes") or {}
                pos = attrs.get("POSITION")
                if pos is not None and pos < len(self.gltf.get("accessors") or ()):
                    acc = self.gltf["accessors"][pos]
                    verts += int(acc.get("count") or 0)
                    if "JOINTS_0" in attrs:
                        skinned = True
                indices = prim.get("indices")
                if indices is not None and indices < len(self.gltf.get("accessors") or ()):
                    tris += int(self.gltf["accessors"][indices].get("count") or 0) // 3
                elif pos is not None and pos < len(self.gltf.get("accessors") or ()):
                    tris += int(self.gltf["accessors"][pos].get("count") or 0) // 3
        morphs = 0
        for mesh in self.meshes:
            for prim in mesh.get("primitives") or ():
                morphs += len(prim.get("targets") or ())
        return {
            "meshes": len(self.meshes),
            "primitives": prims,
            "vertices": verts,
            "triangles": tris,
            "materials": len(self.materials),
            "textures": len(self.gltf.get("textures") or ()),
            "images": len(self.images),
            "image_bytes": self._image_bytes(),
            "skins": len(self.skins),
            "joints": len(self.skins[0].get("joints") or ()) if self.skins else 0,
            "animations": len(self.animations),
            "morph_targets": morphs,
            "skinned": skinned,
            "extensions": sorted(self.extensions.keys()),
            "generator": str((self.gltf.get("asset") or {}).get("generator") or ""),
            "bytes": self.path.stat().st_size,
        }

    def _image_bytes(self):
        total = 0
        for image in self.images:
            view = image.get("bufferView")
            views = self.gltf.get("bufferViews") or []
            if view is not None and 0 <= view < len(views):
                total += int(views[view].get("byteLength") or 0)
        return total

    def bounds(self):
        """Axis-aligned bounds of the meshes in scene space (POSITION min/max)."""
        accessors = self.gltf.get("accessors") or []
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3
        found = False

        def add_point(p):
            nonlocal found
            found = True
            for k in range(3):
                lo[k] = min(lo[k], p[k])
                hi[k] = max(hi[k], p[k])

        for index, node in enumerate(self.nodes):
            mesh_index = node.get("mesh")
            if mesh_index is None or not (0 <= mesh_index < len(self.meshes)):
                continue
            world = self.world.get(index)
            if world is None:
                continue
            for prim in self.meshes[mesh_index].get("primitives") or ():
                pos = (prim.get("attributes") or {}).get("POSITION")
                if pos is None or not (0 <= pos < len(accessors)):
                    continue
                acc = accessors[pos]
                mn, mx = acc.get("min"), acc.get("max")
                if not mn or not mx:
                    continue
                for cx in (mn[0], mx[0]):
                    for cy in (mn[1], mx[1]):
                        for cz in (mn[2], mx[2]):
                            add_point(_transform_point(world, (cx, cy, cz)))
        if not found:
            return {"min": [-0.5, 0.0, -0.5], "max": [0.5, 1.8, 0.5],
                    "height": 1.8, "width": 1.0}
        return {
            "min": [round(v, 4) for v in lo],
            "max": [round(v, 4) for v in hi],
            "height": round(hi[1] - lo[1], 4),
            "width": round(max(hi[0] - lo[0], hi[2] - lo[2]), 4),
        }

    def model_info(self):
        skeleton = self.skeleton()
        return {
            "name": self.path.name,
            "stats": self.stats(),
            "bounds": self.bounds(),
            "skeleton": skeleton,
            "is_vrm": self.is_vrm,
        }


def read_glb(path) -> GLBDoc:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GLBError(f"cannot read the file: {exc}") from exc
    if len(data) < 20:
        raise GLBError("file is too small to be a GLB")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        raise GLBError("not a GLB file (bad magic) - export or save it as binary .glb")
    if version != 2:
        raise GLBError(f"unsupported glTF version {version}")
    if length > len(data):
        raise GLBError("GLB header says the file is longer than it is")
    offset = 12
    gltf = None
    bin_chunk = None
    while offset + 8 <= length:
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset:offset + chunk_length]
        offset += chunk_length
        if chunk_type == CHUNK_JSON:
            gltf = chunk
        elif chunk_type == CHUNK_BIN:
            bin_chunk = chunk
    if gltf is None:
        raise GLBError("GLB has no JSON chunk")
    try:
        document = json.loads(gltf.decode("utf-8-sig").rstrip("\x00 \t\r\n"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GLBError(f"invalid glTF JSON: {exc}") from exc
    if not isinstance(document, dict) or not document.get("nodes"):
        raise GLBError("GLB has no nodes")
    return GLBDoc(path, document, bin_chunk)
