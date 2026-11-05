"""Guess the VRM humanoid mapping of a GLB skeleton.

Two passes:

* name pass - the bone-name tables the converter already uses (MMD, Mixamo,
  VRoid, Unity, Blender, HumanIK, 3ds Max Biped, ...);
* geometry pass - for generic rigs (``bone_0`` ... ``bone_21``): find the legs,
  hips, spine, arms and head from the hierarchy and joint positions.

When the names cover every required humanoid bone they win; otherwise the
geometric result is used and names only fill slots it could not find.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

# The converter package (convert/) holds the shared name tables.
_ROOT = Path(__file__).resolve().parents[1]
_CONVERT = _ROOT / "convert"
if str(_CONVERT) not in sys.path:
    sys.path.insert(0, str(_CONVERT))

try:  # pragma: no cover - only fails if the repo is copied without convert/
    from vrmconv.humanoid import MMD_TO_VRM, _en_bone
except Exception:  # noqa: BLE001
    MMD_TO_VRM = {}
    _en_bone = None


# (slot, UI label, required by VRM 1.0)
SLOTS = [
    ("hips", "Hips", True),
    ("spine", "Spine", True),
    ("chest", "Chest", False),
    ("upperChest", "Upper chest", False),
    ("neck", "Neck", False),
    ("head", "Head", True),
    ("leftShoulder", "Left shoulder", False),
    ("leftUpperArm", "Left upper arm", True),
    ("leftLowerArm", "Left lower arm", True),
    ("leftHand", "Left hand", True),
    ("rightShoulder", "Right shoulder", False),
    ("rightUpperArm", "Right upper arm", True),
    ("rightLowerArm", "Right lower arm", True),
    ("rightHand", "Right hand", True),
    ("leftUpperLeg", "Left upper leg", True),
    ("leftLowerLeg", "Left lower leg", True),
    ("leftFoot", "Left foot", True),
    ("leftToes", "Left toes", False),
    ("rightUpperLeg", "Right upper leg", True),
    ("rightLowerLeg", "Right lower leg", True),
    ("rightFoot", "Right foot", True),
    ("rightToes", "Right toes", False),
]
SLOT_NAMES = [s[0] for s in SLOTS]
REQUIRED = [s[0] for s in SLOTS if s[2]]


# ------------------------------------------------------------------ names
def name_slot(name):
    """Bone name -> VRM slot (or None). Covers MMD and the common English rigs."""
    if not name:
        return None
    if name in MMD_TO_VRM:
        return MMD_TO_VRM[name]
    if _en_bone is None:
        return None
    try:
        found = _en_bone(name)
    except Exception:  # noqa: BLE001 - a weird name must never break loading
        return None
    if found and found[0] == "slot":
        return found[1]
    return None


def _name_map(joints):
    mapping = {}
    duplicates = []
    for joint in joints:
        slot = name_slot(joint["name"])
        if not slot or slot not in SLOT_NAMES:
            continue
        if slot in mapping:
            duplicates.append((slot, joint["name"]))
            continue
        mapping[slot] = joint["name"]
    return mapping, duplicates


# --------------------------------------------------------------- geometry
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _len(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _h_len(v):
    return math.hypot(v[0], v[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vertical(v):
    n = _len(v)
    return n > 1e-9 and abs(v[1]) / n >= 0.62


def _horizontal(v):
    n = _len(v)
    return n > 1e-9 and _h_len(v) / n >= 0.62


def _geometric_map(joints):
    n = len(joints)
    if n < 5:
        return {}, ["skeleton has too few joints for geometric detection"]
    pos = [tuple(j["pos"]) for j in joints]
    parent = [int(j.get("parent", -1)) for j in joints]
    children = [[] for _ in range(n)]
    for i, p in enumerate(parent):
        if 0 <= p < n and p != i:
            children[p].append(i)
    leaves = [i for i in range(n) if not children[i]]
    if len(leaves) < 2:
        return {}, ["skeleton has no leaf bones"]
    ys = [p[1] for p in pos]
    height = max(ys) - min(ys)
    if height <= 1e-6:
        height = 1.0
    xs = sorted(p[0] for p in pos)
    center_x = xs[len(xs) // 2]
    warnings = []

    # -- feet: the lowest leaf on each side of the body
    lowest = min(leaves, key=lambda i: pos[i][1])
    opposite = [i for i in leaves
                if (pos[i][0] - center_x) * (pos[lowest][0] - center_x) < 0
                and abs(pos[i][0] - pos[lowest][0]) > 0.03 * height]
    if not opposite:
        opposite = [i for i in leaves
                    if abs(pos[i][0] - pos[lowest][0]) > 0.03 * height]
    if not opposite:
        return {}, ["could not find two separate feet"]
    other = min(opposite, key=lambda i: pos[i][1])

    def leg_from(leaf):
        chain = []
        cur = leaf
        while 0 <= cur < n and len(chain) < 16:
            chain.append(cur)
            cur = parent[cur]
        if len(chain) < 4:
            return None
        ankle = None
        for k in range(1, len(chain) - 2):
            parent_dir = _sub(pos[chain[k + 1]], pos[chain[k]])
            child_dir = _sub(pos[chain[k - 1]], pos[chain[k]])
            if _vertical(parent_dir) and _horizontal(child_dir):
                ankle = k
                break
        if ankle is None:
            # no toe bone: the leaf itself is the ankle
            if _vertical(_sub(pos[chain[1]], pos[chain[0]])):
                ankle = 0
        if ankle is None:
            return None
        foot = chain[ankle]
        toes = chain[ankle - 1] if ankle > 0 else None
        lower = chain[ankle + 1]
        upper = chain[ankle + 2]
        hips = chain[ankle + 3] if ankle + 3 < len(chain) else parent[upper]
        if hips < 0:
            return None
        return {"leaf": leaf, "foot": foot, "toes": toes,
                "lower": lower, "upper": upper, "hips": hips}

    leg_a = leg_from(lowest)
    leg_b = leg_from(other)
    if not leg_a or not leg_b:
        return {}, ["could not identify the leg chains"]
    legs = [leg_a, leg_b]

    # the two legs must meet at the hips; if they do not, use their common ancestor
    hips = leg_a["hips"]
    if leg_a["hips"] != leg_b["hips"]:
        ancestors = set()
        cur = leg_a["upper"]
        while cur >= 0:
            ancestors.add(cur)
            cur = parent[cur]
        cur = leg_b["upper"]
        while cur >= 0:
            if cur in ancestors:
                hips = cur
                break
            cur = parent[cur]
        else:
            warnings.append("the two legs do not share a hips joint")
    for leg in legs:
        leg["hips"] = hips

    # -- forward / left-right from the toe bones (fall back to glTF's +Z front)
    forward = (0.0, 0.0, 1.0)
    toe_vectors = []
    for leg in legs:
        if leg["toes"] is not None:
            v = _sub(pos[leg["toes"]], pos[leg["foot"]])
            v = (v[0], 0.0, v[2])
            if _len(v) > 1e-5:
                toe_vectors.append(v)
    if toe_vectors:
        fx = sum(v[0] for v in toe_vectors)
        fz = sum(v[2] for v in toe_vectors)
        ln = math.hypot(fx, fz)
        if ln > 1e-6:
            forward = (fx / ln, 0.0, fz / ln)
    left_dir = (forward[2], 0.0, -forward[0])  # up x forward
    origin = pos[hips]

    def side_of(index):
        return "left" if _dot(_sub(pos[index], origin), left_dir) >= 0 else "right"

    mapping = {}
    used = set()

    def put(slot, index):
        if index is None or index < 0 or slot in mapping:
            return
        bone = joints[index]["name"]
        if bone in used:
            return
        mapping[slot] = bone
        used.add(bone)

    put("hips", hips)

    # -- legs
    leg_sides = {}
    for leg in legs:
        side = side_of(leg["upper"])
        leg_sides[side] = leg
        put(side + "UpperLeg", leg["upper"])
        put(side + "LowerLeg", leg["lower"])
        put(side + "Foot", leg["foot"])
        if leg["toes"] is not None:
            put(side + "Toes", leg["toes"])
    if len(leg_sides) < 2:
        warnings.append("could not tell the left leg from the right")

    # -- spine: walk up from the hips until the arms branch off
    leg_next = {leg["upper"] for leg in legs}
    spine_chain = []
    branch = None
    cur = hips
    for _ in range(24):
        candidates = [c for c in children[cur] if c not in leg_next]
        if not candidates:
            break
        if len(candidates) >= 2:
            branch = cur
            break
        nxt = candidates[0]
        spine_chain.append(nxt)
        cur = nxt
        if len(children[cur]) >= 2:
            branch = cur
            break
    if spine_chain:
        if len(spine_chain) >= 3:
            put("spine", spine_chain[0])
            put("chest", spine_chain[1])
            put("upperChest", spine_chain[-1])
        elif len(spine_chain) == 2:
            put("spine", spine_chain[0])
            put("chest", spine_chain[1])
        else:
            put("spine", spine_chain[0])
    if branch is None:
        warnings.append("could not find where the arms branch off the spine")
    else:
        arm_children = []
        up_child = None
        if len(children[branch]) >= 2:
            # The arms are the children farthest to each side of the body; the
            # neck continues roughly straight up (lateral offset close to 0).
            ranked = sorted(
                children[branch],
                key=lambda c: _dot(_sub(pos[c], pos[branch]), left_dir),
            )
            if len(ranked) >= 2:
                right_child = ranked[0]
                left_child = ranked[-1]
                lateral = abs(_dot(_sub(pos[left_child], pos[branch]), left_dir)) + \
                    abs(_dot(_sub(pos[right_child], pos[branch]), left_dir))
                if lateral > 0.01 * height:
                    arm_children = [left_child, right_child]
                    rest = [c for c in children[branch] if c not in arm_children]
                    if rest:
                        rest.sort(key=lambda c: abs(pos[c][1] - pos[branch][1]),
                                  reverse=True)
                        up_child = rest[0]
            if len(arm_children) < 2:
                # fallback: the two children with the largest |x| separation
                rest = sorted(children[branch],
                              key=lambda c: -abs(pos[c][0] - pos[branch][0]))
                arm_children = rest[:2]
                rest = rest[2:]
                up_child = rest[0] if rest else None

        # neck + head above the branch
        if up_child is not None:
            above = [up_child]
            cur = up_child
            for _ in range(12):
                if len(children[cur]) != 1:
                    break
                cur = children[cur][0]
                above.append(cur)
            if len(above) >= 2:
                put("neck", above[-2])
                put("head", above[-1])
            else:
                put("head", above[-1])
        else:
            warnings.append("no neck/head chain above the arms")

        # arms
        arm_sides = {}
        for child in arm_children:
            chain = []
            cur = child
            for _ in range(16):
                chain.append(cur)
                kids = children[cur]
                if len(kids) != 1:
                    break  # leaf, or the hand branching into fingers
                cur = kids[0]
            side = side_of(child)
            arm_sides[side] = chain
            if len(chain) >= 4:
                put(side + "Shoulder", chain[0])
                put(side + "UpperArm", chain[1])
                put(side + "LowerArm", chain[2])
                put(side + "Hand", chain[3])
            elif len(chain) == 3:
                put(side + "UpperArm", chain[0])
                put(side + "LowerArm", chain[1])
                put(side + "Hand", chain[2])
            elif len(chain) == 2:
                put(side + "UpperArm", chain[0])
                put(side + "Hand", chain[1])
                warnings.append(f"{side} arm has no forearm bone")
            elif len(chain) == 1:
                put(side + "UpperArm", chain[0])
                warnings.append(f"{side} arm has a single bone")
        if len(arm_sides) < 2:
            warnings.append("could not tell the left arm from the right")

    return mapping, warnings


# ------------------------------------------------------------------ merge
def _dedupe(mapping):
    seen = {}
    for slot in SLOT_NAMES:
        bone = mapping.get(slot)
        if not bone:
            continue
        if bone in seen:
            mapping.pop(slot, None)
        else:
            seen[bone] = slot
    return mapping


def detect(skeleton):
    """Return the mapping, its sources and warnings for a parsed skeleton."""
    if not skeleton or not skeleton.get("joints"):
        return {"mapping": {}, "sources": {}, "warnings": ["no skeleton found"],
                "missing_required": REQUIRED, "required_mapped": 0,
                "required_total": len(REQUIRED), "mapped": 0, "total": len(SLOT_NAMES),
                "auto": False}

    joints = skeleton["joints"]
    names, duplicates = _name_map(joints)
    geo, geo_warnings = _geometric_map(joints)
    warnings = list(geo_warnings)
    for slot, bone in duplicates:
        warnings.append(f"'{slot}' appears more than once; using the first ({bone})")

    name_required = sum(1 for slot in REQUIRED if slot in names)
    use_names = name_required == len(REQUIRED)
    if use_names:
        mapping = {slot: names[slot] for slot in SLOT_NAMES if slot in names}
        for slot in SLOT_NAMES:
            if slot not in mapping and slot in geo:
                mapping[slot] = geo[slot]
        sources = {slot: "name" for slot in names}
    else:
        mapping = dict(geo)
        for slot in SLOT_NAMES:
            if slot not in mapping and slot in names:
                mapping[slot] = names[slot]
                sources.setdefault(slot, "name")
        sources = {slot: ("name" if slot in names and mapping.get(slot) == names[slot]
                          else "geometry") for slot in mapping}
        if geo:
            warnings.append("bone names are not a known humanoid rig; "
                            f"the mapping was detected from the skeleton ({name_required} "
                            f"of {len(REQUIRED)} required bones matched by name)")
        elif not names:
            warnings.append("no humanoid bone names and geometric detection failed")

    mapping = _dedupe(mapping)
    missing = [slot for slot in REQUIRED if slot not in mapping]
    return {
        "mapping": mapping,
        "sources": sources,
        "warnings": warnings,
        "missing_required": missing,
        "required_mapped": len(REQUIRED) - len(missing),
        "required_total": len(REQUIRED),
        "mapped": len(mapping),
        "total": len(SLOT_NAMES),
        "auto": bool(mapping),
        "method": "names" if use_names else ("geometry" if geo else "names"),
    }
