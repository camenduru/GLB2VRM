"""Tests for the GLB2VRM UI's GLB reader and humanoid detection."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from glb2vrm import mapping  # noqa: E402
from glb2vrm.glb import GLBError, read_glb  # noqa: E402


def write_glb(path, gltf):
    payload = json.dumps(gltf).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    total = 12 + 8 + len(payload)
    with open(path, "wb") as handle:
        handle.write(struct.pack("<III", 0x46546C67, 2, total))
        handle.write(struct.pack("<II", len(payload), 0x4E4F534A))
        handle.write(payload)


def skeleton_from(names, parents, positions):
    joints = []
    for index, name in enumerate(names):
        joints.append({"name": name, "parent": parents[index],
                       "pos": list(positions[index]), "node": index})
    return {"name": "test", "joints": joints}


# A generic rig shaped like examples/test.glb: bone_0 is the hips, bone_1..3
# the spine, bone_4/5 neck and head, bone_6..9 the left arm, 10..13 the right
# arm, 14..17 the left leg and 18..21 the right leg.
GENERIC = skeleton_from(
    [f"bone_{i}" for i in range(22)],
    [-1, 0, 1, 2, 3, 4, 3, 6, 7, 8, 3, 10, 11, 12, 0, 14, 15, 16, 0, 18, 19, 20],
    [
        (0.001, -0.051, 0.029), (0.001, 0.005, 0.022), (0.001, 0.065, 0.019),
        (0.001, 0.135, 0.015), (0.001, 0.216, 0.012), (0.001, 0.268, 0.005),
        (-0.034, 0.195, 0.012), (-0.100, 0.153, 0.012), (-0.132, 0.079, 0.022),
        (-0.192, -0.012, 0.026), (0.036, 0.195, 0.012), (0.103, 0.153, 0.012),
        (0.131, 0.079, 0.022), (0.191, -0.012, 0.033), (-0.055, -0.079, 0.019),
        (-0.062, -0.233, 0.019), (-0.058, -0.370, 0.029), (-0.072, -0.440, -0.044),
        (0.057, -0.079, 0.019), (0.068, -0.233, 0.019), (0.061, -0.370, 0.029),
        (0.075, -0.440, -0.048),
    ],
)

EXPECTED = {
    "hips": "bone_0", "spine": "bone_1", "chest": "bone_2", "upperChest": "bone_3",
    "neck": "bone_4", "head": "bone_5",
    "leftShoulder": "bone_6", "leftUpperArm": "bone_7", "leftLowerArm": "bone_8",
    "leftHand": "bone_9",
    "rightShoulder": "bone_10", "rightUpperArm": "bone_11", "rightLowerArm": "bone_12",
    "rightHand": "bone_13",
    "leftUpperLeg": "bone_14", "leftLowerLeg": "bone_15", "leftFoot": "bone_16",
    "leftToes": "bone_17",
    "rightUpperLeg": "bone_18", "rightLowerLeg": "bone_19", "rightFoot": "bone_20",
    "rightToes": "bone_21",
}


class NameMappingTests(unittest.TestCase):
    def test_mixamo(self):
        self.assertEqual(mapping.name_slot("mixamorig:Hips"), "hips")
        self.assertEqual(mapping.name_slot("mixamorig:LeftUpLeg"), "leftUpperLeg")
        self.assertEqual(mapping.name_slot("mixamorig:RightForeArm"), "rightLowerArm")
        self.assertEqual(mapping.name_slot("mixamorig:LeftToeBase"), "leftToes")

    def test_vroid_and_blender(self):
        self.assertEqual(mapping.name_slot("J_Bip_C_Hips"), "hips")
        self.assertEqual(mapping.name_slot("J_Bip_L_UpperArm"), "leftUpperArm")
        self.assertEqual(mapping.name_slot("upper_arm.R"), "rightUpperArm")
        self.assertEqual(mapping.name_slot("thigh.L"), "leftUpperLeg")

    def test_mmd(self):
        self.assertEqual(mapping.name_slot("下半身"), "hips")
        self.assertEqual(mapping.name_slot("左ひじ"), "leftLowerArm")
        self.assertEqual(mapping.name_slot("右つま先"), "rightToes")

    def test_unknown(self):
        self.assertIsNone(mapping.name_slot("bone_0"))
        self.assertIsNone(mapping.name_slot("Root"))
        self.assertIsNone(mapping.name_slot(""))


class GeometryTests(unittest.TestCase):
    def test_generic_rig_is_fully_mapped(self):
        result = mapping.detect(GENERIC)
        self.assertEqual(result["mapping"], EXPECTED)
        self.assertEqual(result["missing_required"], [])
        self.assertEqual(result["required_mapped"], result["required_total"])
        self.assertEqual(result["method"], "geometry")

    def test_generic_rig_sides_are_not_swapped(self):
        result = mapping.detect(GENERIC)["mapping"]
        self.assertEqual(result["leftUpperArm"], "bone_7")
        self.assertEqual(result["rightUpperArm"], "bone_11")
        self.assertEqual(result["leftUpperLeg"], "bone_14")
        self.assertEqual(result["rightUpperLeg"], "bone_18")

    def test_name_rig_wins_when_complete(self):
        names = [
            "mixamorig:Hips", "mixamorig:Spine", "mixamorig:Spine1",
            "mixamorig:Spine2", "mixamorig:Neck", "mixamorig:Head",
            "mixamorig:LeftShoulder", "mixamorig:LeftArm", "mixamorig:LeftForeArm",
            "mixamorig:LeftHand", "mixamorig:RightShoulder", "mixamorig:RightArm",
            "mixamorig:RightForeArm", "mixamorig:RightHand",
            "mixamorig:LeftUpLeg", "mixamorig:LeftLeg", "mixamorig:LeftFoot",
            "mixamorig:LeftToeBase", "mixamorig:RightUpLeg", "mixamorig:RightLeg",
            "mixamorig:RightFoot", "mixamorig:RightToeBase",
        ]
        parents = [-1, 0, 1, 2, 3, 4, 3, 6, 7, 8, 3, 10, 11, 12,
                   0, 14, 15, 16, 0, 18, 19, 20]
        skeleton = skeleton_from(names, parents, [j["pos"] for j in GENERIC["joints"]])
        result = mapping.detect(skeleton)
        self.assertEqual(result["method"], "names")
        self.assertEqual(result["mapping"]["hips"], "mixamorig:Hips")
        self.assertEqual(result["mapping"]["leftUpperArm"], "mixamorig:LeftArm")
        self.assertEqual(result["mapping"]["rightToes"], "mixamorig:RightToeBase")
        self.assertEqual(result["missing_required"], [])


class GLBTests(unittest.TestCase):
    def test_skeleton_and_world_positions(self):
        gltf = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [
                {"name": "root", "children": [1, 2], "translation": [0, 1, 0]},
                {"name": "left", "translation": [-0.2, 0, 0]},
                {"name": "right", "translation": [0.2, 0, 0]},
            ],
            "skins": [{"name": "rig", "joints": [0, 1, 2]}],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tiny.glb"
            write_glb(path, gltf)
            doc = read_glb(path)
            skeleton = doc.skeleton()
            self.assertEqual([j["name"] for j in skeleton["joints"]],
                             ["root", "left", "right"])
            self.assertEqual(skeleton["joints"][1]["parent"], 0)
            self.assertEqual(skeleton["joints"][2]["parent"], 0)
            self.assertEqual(skeleton["joints"][0]["pos"], [0.0, 1.0, 0.0])
            self.assertEqual(skeleton["joints"][1]["pos"], [-0.2, 1.0, 0.0])
            info = doc.model_info()
            self.assertFalse(info["is_vrm"])
            self.assertEqual(info["stats"]["joints"], 3)

    def test_bad_magic(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.glb"
            path.write_bytes(b"not a glb at all........")
            with self.assertRaises(GLBError):
                read_glb(path)

    def test_vrm_extension_is_detected(self):
        gltf = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"name": "root"}],
            "extensions": {"VRMC_vrm": {"specVersion": "1.0"}},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "avatar.vrm"
            write_glb(path, gltf)
            doc = read_glb(path)
            self.assertTrue(doc.is_vrm)
            self.assertIsNone(doc.skeleton())


if __name__ == "__main__":
    unittest.main()
