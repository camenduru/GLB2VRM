"""GLB -> VRM conversion for the GLB2VRM UI.

VRMify's converter does not read glTF directly, so the GLB is bridged through
Blender: import, rename the mapped bones to the names the PMX/VRM stages
understand, export FBX (with textures), then run the repository's own
``fbx2pmx.py`` and ``pmx2vrm.py``. This is the same sequence that was verified
for ``examples/test.glb``.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONVERT_DIR = ROOT / "convert"
CHECK_VRM = ROOT / "tools" / "qa" / "check_vrm.py"
DATA_DIR = Path(__file__).resolve().parent / "data"
MODEL_DIR = DATA_DIR / "models"
JOB_DIR = DATA_DIR / "jobs"

TOKEN = re.compile(r"[^A-Za-z0-9._ -]+")
GLB_MAGIC = 0x46546C67


class ConvertError(Exception):
    pass


def clean_name(name: str, default: str = "model") -> str:
    stem = TOKEN.sub("_", Path(name).name).strip(" ._")
    return stem or default


# ----------------------------------------------------------------- blender
def _blender_versions(paths):
    found = []
    for path in paths:
        if not path or not Path(path).exists():
            continue
        version = (0, 0)
        match = re.search(r"(\d+)\.(\d+)", str(path))
        if match:
            version = (int(match.group(1)), int(match.group(2)))
        found.append((version, Path(path)))
    found.sort()
    return found


def find_blender():
    """Locate Blender. Prefers a 4.x build (the pipeline was verified on 4.5)."""
    candidates = []
    for env in ("GLB2VRM_BLENDER", "BLENDER_EXE", "BLENDER"):
        value = os.environ.get(env)
        if value:
            candidates.append(value)
    on_path = shutil.which("blender")
    if on_path:
        candidates.append(on_path)
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            candidates += glob.glob(os.path.join(base, "Blender Foundation", "Blender *", "blender.exe"))
            candidates += glob.glob(os.path.join(base, "Blender*", "blender.exe"))
    elif sys.platform == "darwin":
        candidates += glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
    else:
        candidates += ["/usr/bin/blender", "/usr/local/bin/blender",
                       "/snap/bin/blender", os.path.expanduser("~/blender/blender")]

    versions = _blender_versions(candidates)
    if not versions:
        return None
    fours = [item for item in versions if item[0][0] == 4]
    version, path = fours[-1] if fours else versions[-1]
    return {"path": str(path), "version": f"{version[0]}.{version[1]}" if version[0] else "?"}


def status():
    blender = find_blender()
    missing = [name for name, path in (
        ("convert/fbx2pmx.py", CONVERT_DIR / "fbx2pmx.py"),
        ("convert/pmx2vrm.py", CONVERT_DIR / "pmx2vrm.py"),
        ("convert/vrmconv", CONVERT_DIR / "vrmconv"),
    ) if not path.exists()]
    return {
        "ok": blender is not None and not missing,
        "blender": blender,
        "missing": missing,
        "reason": ("ready" if blender and not missing else
                   "Blender was not found - install it or set GLB2VRM_BLENDER"
                   if not blender else f"missing files: {', '.join(missing)}"),
        "python": sys.version.split()[0],
    }


# ---------------------------------------------------------------- helpers
def _run(cmd, cwd, log, timeout=None, sink=None):
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if os.name == "nt":
        env.setdefault("PYOPENGL_PLATFORM", "win32")
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log.append("$ " + " ".join(str(part) for part in cmd))
    proc = subprocess.Popen(
        [str(part) for part in cmd],
        cwd=str(cwd), env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=creation,
    )
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log.append(line)
                del log[:-160]
                if sink:
                    sink(line)
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
    return proc.returncode


def _safe_text(value, limit=4000):
    text = str(value or "").strip()
    return text[:limit]


def vrm_summary(path: Path):
    """Read the VRM GLB's JSON chunk for a short result summary."""
    try:
        data = path.read_bytes()
    except OSError:
        return {}
    if len(data) < 20:
        return {}
    magic, _version, length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        return {}
    offset = 12
    while offset + 8 <= min(length, len(data)):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset:offset + chunk_length]
        offset += chunk_length
        if chunk_type != 0x4E4F534A:
            continue
        try:
            gltf = json.loads(chunk.decode("utf-8-sig").rstrip("\x00 \t\r\n"))
        except (UnicodeDecodeError, ValueError):
            return {}
        vrm = (gltf.get("extensions") or {}).get("VRMC_vrm") or {}
        legacy = (gltf.get("extensions") or {}).get("VRM") or {}
        humanoid = (vrm.get("humanoid") or {}).get("humanBones") or {}
        springs = (vrm.get("springBone") or {}).get("springs") or []
        morphs = 0
        for mesh in gltf.get("meshes") or ():
            for prim in mesh.get("primitives") or ():
                morphs += len(prim.get("targets") or ())
        return {
            "spec": vrm.get("specVersion") or ("0.x" if legacy else "?"),
            "bones": len(humanoid) if humanoid else len((legacy.get("humanoid") or {}).get("humanBones") or ()),
            "springs": len(springs),
            "morphs": morphs,
            "meshes": len(gltf.get("meshes") or ()),
            "materials": len(gltf.get("materials") or ()),
            "images": len(gltf.get("images") or ()),
            "meta": (vrm.get("meta") or legacy.get("meta") or {}).get("name", ""),
        }
    return {}


BLENDER_SCRIPT = r'''
import bpy, json, os, sys

CFG = json.loads(os.environ["GLB2VRM_CFG"])

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=CFG["glb"])

# glTF helpers (custom bone shapes and the like) are not part of the avatar
for coll in list(bpy.data.collections):
    if coll.name == "glTF_not_exported":
        for ob in list(coll.objects):
            bpy.data.objects.remove(ob, do_unlink=True)
        bpy.data.collections.remove(coll)
for ob in list(bpy.data.objects):
    if ob.name.startswith("Icosphere") and ob.type == "MESH" and ob.parent is None:
        if getattr(ob, "users_collection", None):
            names = [c.name for c in ob.users_collection]
            if "glTF_not_exported" in names or not ob.modifiers:
                bpy.data.objects.remove(ob, do_unlink=True)

targets = CFG["targets"]          # original bone name -> VRM bone name
armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
report = {"armatures": [], "meshes": [], "bones": 0}

for arm in armatures:
    arm.data.pose_position = "REST"
    for pose_bone in arm.pose.bones:
        pose_bone.matrix_basis.identity()
    old_names = [b.name for b in arm.data.bones]
    # two-pass rename: never let Blender append .001 because of a collision
    for name in old_names:
        if name in targets:
            bone = arm.data.bones.get(name)
            if bone is not None:
                bone.name = "__g2v__" + targets[name]
    for name in old_names:
        if name in targets:
            final = targets[name]
            bone = arm.data.bones.get("__g2v__" + final)
            if bone is not None:
                bone.name = final
    for pose_bone in arm.pose.bones:
        pose_bone.matrix_basis.identity()
    report["armatures"].append({"name": arm.name, "bones": len(arm.data.bones)})
    report["bones"] += len(arm.data.bones)

for mesh in meshes:
    vgs = mesh.vertex_groups
    for name in list(vgs.keys()):
        if name in targets:
            try:
                vgs[name].name = "__g2v__" + targets[name]
            except Exception:
                pass
    for name in list(vgs.keys()):
        if name.startswith("__g2v__"):
            try:
                vgs[name].name = name[len("__g2v__"):]
            except Exception:
                pass
    report["meshes"].append({"name": mesh.name, "verts": len(mesh.data.vertices),
                             "materials": len(mesh.data.materials)})

# Save images next to the FBX so the FBX references real files on disk.
texdir = CFG["textures"]
os.makedirs(texdir, exist_ok=True)
used = set()
for index, img in enumerate(bpy.data.images):
    if img.source != "FILE" or img.size[0] == 0:
        continue
    stem = os.path.splitext(img.name.rsplit("\\", 1)[-1].rsplit("/", 1)[-1])[0] or ("image_%d" % index)
    stem = "".join(ch if (ch.isalnum() or ch in "._- ") else "_" for ch in stem)
    stem = stem.strip(" ._") or ("image_%d" % index)
    name = stem
    n = 2
    while name.lower() in used:
        name = "%s_%d" % (stem, n)
        n += 1
    used.add(name.lower())
    path = os.path.join(texdir, name + ".png")
    try:
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
    except Exception as exc:
        print("texture save failed: %s: %s" % (img.name, exc))

bpy.ops.export_scene.fbx(
    filepath=CFG["fbx"],
    use_selection=False,
    object_types={"ARMATURE", "MESH"},
    use_mesh_modifiers=False,
    use_mesh_edges=False,
    use_tspace=False,
    add_leaf_bones=False,
    bake_anim=False,
    bake_anim_use_all_actions=False,
    bake_anim_use_nla_strips=False,
    path_mode="ABSOLUTE",
    embed_textures=False,
    mesh_smooth_type="FACE",
    apply_scale_options="FBX_SCALE_NONE",
    axis_forward="-Z",
    axis_up="Y",
)
report["fbx"] = CFG["fbx"]
report["textures"] = len(used)
with open(CFG["report"], "w", encoding="utf-8") as handle:
    json.dump(report, handle)
print("GLB2VRM_OK %s" % CFG["fbx"])
'''


def _run_blender(blender, glb: Path, target_map: dict, work: Path, log, sink=None):
    textures = work / "textures"
    fbx = work / "avatar.fbx"
    report = work / "blender.json"
    script = work / "prepare_fbx.py"
    cfg = {
        "glb": str(glb),
        "fbx": str(fbx),
        "textures": str(textures),
        "report": str(report),
        "targets": target_map,
    }
    script.write_text(BLENDER_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["GLB2VRM_CFG"] = json.dumps(cfg)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [blender, "-b", "--factory-startup", "--python", str(script)]
    log.append("$ " + " ".join(cmd))
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd, cwd=str(work), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=creation,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log.append(line)
            del log[:-160]
            if sink:
                sink(line)
    proc.wait()
    if proc.returncode != 0 or not fbx.exists():
        tail = "\n".join(log[-12:])
        raise ConvertError("Blender could not export the GLB:\n" + tail)
    info = {}
    try:
        info = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return fbx, info


def convert_model(glb: Path, mapping: dict, options: dict, work: Path, report,
                  sink=None):
    """Run the whole GLB -> VRM chain.

    ``report(phase, message)`` updates the job's phase; ``sink(line)`` receives
    the tool output live so the UI can stream it.
    """
    glb = Path(glb)
    if not glb.exists():
        raise ConvertError("model file disappeared")
    work.mkdir(parents=True, exist_ok=True)
    log = []

    blender = find_blender()
    if not blender:
        raise ConvertError("Blender was not found; install it or set GLB2VRM_BLENDER")
    target_map = {}
    for slot, bone in mapping.items():
        if not slot or not bone:
            continue
        bone = str(bone)
        if bone and bone not in target_map:
            target_map[bone] = str(slot)
    if not target_map:
        raise ConvertError("no bones are mapped - map at least the required humanoid bones")

    report("blender", "importing the GLB and exporting an FBX")
    fbx, blender_info = _run_blender(blender["path"], glb, target_map, work, log, sink)
    report("pmx", "building the PMX model")
    pmx = work / "avatar.pmx"
    fbx_cmd = [sys.executable, CONVERT_DIR / "fbx2pmx.py", fbx, "-o", pmx]
    if options.get("face180"):
        fbx_cmd.append("--face-180")
    code = _run(fbx_cmd, work, log, timeout=60 * 30, sink=sink)
    if code != 0 or not pmx.exists():
        raise ConvertError("fbx2pmx failed:\n" + "\n".join(log[-12:]))

    spec = str(options.get("spec") or "vrm1")
    out = work / (clean_name(glb.stem) + ".vrm")
    cmd = [sys.executable, CONVERT_DIR / "pmx2vrm.py", pmx, "-o", out]
    if spec == "vrm0":
        cmd += ["--spec", "0x"]
    if options.get("tpose") is False:
        cmd += ["--keep-pose"]
    if options.get("spring") is False:
        cmd += ["--no-auto-spring"]
    title = _safe_text(options.get("title"), 120)
    author = _safe_text(options.get("author"), 120)
    if title:
        cmd += ["--title", title]
    if author:
        cmd += ["--author", author]
    tex_max = options.get("tex_max")
    if tex_max:
        try:
            value = int(tex_max)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            cmd += ["--tex-max", str(value)]
    if options.get("tex_jpeg"):
        cmd.append("--tex-jpeg")

    report("vrm", "writing the VRM avatar")
    code = _run(cmd, work, log, timeout=60 * 30, sink=sink)
    if code != 0 or not out.exists():
        raise ConvertError("pmx2vrm failed:\n" + "\n".join(log[-12:]))

    report("check", "validating the VRM")
    valid = None
    if CHECK_VRM.exists():
        code = _run([sys.executable, CHECK_VRM, out], ROOT, log, timeout=60 * 10, sink=sink)
        valid = code == 0

    summary = vrm_summary(out)
    size = out.stat().st_size
    return {
        "vrm": out,
        "name": out.name,
        "bytes": size,
        "summary": summary,
        "valid": valid,
        "log": log,
        "blender": blender_info,
        "elapsed": round(time.time(), 1),
    }
