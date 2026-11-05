# GLB2VRM

A small local web app that turns a **rigged GLB** into a **VRM avatar**, with a
three.js preview, automatic humanoid bone mapping, and a download button. The
look and flow follow the ExMR/VRMExMR UI in this workspace.

```
GLB2VRM.bat                       # Windows: starts the server and opens the browser
python glb2vrm/server.py          # or run the server directly
```

or

```
python -m glb2vrm.server --port 8020
```

## What it does

1. **Add a GLB** (drop it on the left panel, or *Add model…*). The server reads
   the skeleton and matches it to the 22 VRM humanoid bones:
   * by name for known rigs (Mixamo, VRoid, Unity, Blender, HumanIK, 3ds Max
     Biped, MMD);
   * from the skeleton shape for unnamed rigs (`bone_0`…`bone_21`): the feet,
     hips, spine, arm branch, neck and head are recovered from the hierarchy.
2. **Check the mapping.** Every slot is a dropdown; required VRM 1.0 bones are
   marked. The preview draws the skeleton and highlights mapped bones in green.
3. **Convert.** The server bridges the GLB through Blender (import + FBX
   export), then runs VRMify's own `convert/fbx2pmx.py` and
   `convert/pmx2vrm.py`. The result is validated with
   `tools/qa/check_vrm.py` and can be previewed in the same window.
4. **Play a VRM Animation.** Once the VRM is loaded, use **Play .vrma…**
   (or drop a `.vrma` onto the preview) to play it on the generated avatar,
   with play/pause, a seek bar and Clear. VRMAs from ExMR, VRoid and other
   tools work; the animation is applied in the browser by
   `@pixiv/three-vrm-animation`, so nothing is uploaded.

The conversion is the same pipeline used for `examples/test.glb`, so a GLB
with a humanoid skeleton comes out as a valid VRM 1.0 avatar in a T-pose, with
its textures and materials converted to MToon.

## Requirements

* Python 3.10+ (the app itself uses only the standard library).
* **Blender 4.x** installed (tested with 4.5). Blender is only used as the glTF
  importer/exporter; the VRM is written by VRMify.
  If Blender is not on `PATH`, set `GLB2VRM_BLENDER` to `blender.exe`.
* The UI uses the three.js / three-vrm bundles already vendored in
  `ui/viewer/vendor/`; no npm build is needed.

## Options

| Option | Effect |
|---|---|
| Target | VRM 1.0 (default) or VRM 0.x |
| T-pose | Raise A-pose arms to the VRM T-pose (on by default) |
| Spring bones | Add hair/skirt/ribbon chains from bone names |
| Texture size | Downscale atlases (needs Pillow) |
| JPEG textures | Store opaque textures as JPEG (needs Pillow) |
| Extra 180° turn | Override the facing detection when the model comes out backwards |
| Title / Author | Written to the VRM metadata |

Finished jobs are kept in `glb2vrm/data/jobs/`; reloading the page (or
restarting the server) reconnects to the last conversion for the selected
model, so the VRM stays downloadable.

## Layout

```
glb2vrm/
  glb.py       minimal GLB reader (skeleton, bounds, stats)
  mapping.py   name + geometry humanoid detection
  convert.py   Blender bridge -> fbx2pmx -> pmx2vrm -> check_vrm
  server.py    stdlib HTTP server and job runner
  web/         index.html, style.css, main.js (three.js preview + VRMA player)
  data/        uploaded models and job outputs (ignored by git)
```

Each conversion keeps its FBX/PMX/VRM intermediates in `glb2vrm/data/jobs/<id>/`
so the result stays previewable and downloadable; delete that folder to reclaim
space. Uploaded GLBs live in `glb2vrm/data/models/` and are removed with the
**Remove** button.
