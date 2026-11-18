"""Runs *inside Blender*: convert Kimodo BVH clips to FBX for game engines (Unity cannot read BVH).

    blender -b --factory-startup --python blender_bvh_to_fbx.py -- JOBS_JSON

JOBS_JSON is a list of ``{"bvh": path, "fbx": path, "name": clip name}``. One
``WATERFREE_RESULT {json}`` line is printed per job; a failed job does not stop
the others.

Settings, and why:

* BVH import: Kimodo writes centimetres, Y up, 30 fps. ``global_scale=0.01``
  gives metres; ``update_scene_fps`` matters -- without it the scene stays at
  Blender's 24 fps and the exported clip plays 25% slow.
* FBX export: armature only, every frame baked (no key simplification), no
  leaf bones, ``FBX_SCALE_UNITS`` so the rig imports at scale 1 instead of a
  100x root with a 0.01 transform, -Z forward / Y up for Unity.
"""

import json
import sys

import bpy


def convert(job: dict) -> dict:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_anim.bvh(filepath=job["bvh"], global_scale=0.01, rotate_mode="NATIVE",
                            axis_forward="-Z", axis_up="Y", update_scene_fps=True, update_scene_duration=True)
    scene = bpy.context.scene
    armature = next(o for o in scene.objects if o.type == "ARMATURE")
    name = job.get("name") or armature.name
    armature.name = name
    action = armature.animation_data.action if armature.animation_data else None
    if action is not None:
        action.name = name
        # update_scene_duration only ever *extends* the scene, so the factory
        # 250-frame range survives a shorter clip and the FBX bake pads it with
        # frozen frames (a 4 s clip imported into Unity as 8.3 s). Set it exactly.
        start, end = (round(v) for v in action.frame_range)
        scene.frame_start, scene.frame_end = start, end
    # The exported take is named after the scene: Unity shows it as the clip name.
    scene.name = name
    bpy.ops.export_scene.fbx(
        filepath=job["fbx"], use_selection=False, object_types={"ARMATURE"}, add_leaf_bones=False,
        bake_anim=True, bake_anim_use_all_actions=False, bake_anim_use_nla_strips=False,
        bake_anim_simplify_factor=0.0, apply_unit_scale=True, apply_scale_options="FBX_SCALE_UNITS",
        axis_forward="-Z", axis_up="Y",
    )
    return {"bvh": job["bvh"], "fbx": job["fbx"], "ok": True, "frames": [scene.frame_start, scene.frame_end],
            "fps": scene.render.fps, "bones": len(armature.data.bones)}


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:]
    with open(args[0], encoding="utf-8") as handle:
        jobs = json.load(handle)
    for job in jobs:
        try:
            result = convert(job)
        except Exception as exc:  # noqa: BLE001 - one bad clip must not sink the batch
            result = {"bvh": job.get("bvh"), "fbx": job.get("fbx"), "ok": False,
                      "error": f"{type(exc).__name__}: {exc}"}
        print("WATERFREE_RESULT " + json.dumps(result), flush=True)


main()
