"""Original-size base-color images and per-corner UVs, independent of motion."""
import hashlib
import json
import shutil
import uuid
from pathlib import Path

import bpy
import numpy as np

from .file_paths import io_path

CACHE = 'preview-textures-v2'


def _nodes(tree, seen=None):
    if tree is None:
        return
    seen = seen if seen is not None else set()
    key = tree.as_pointer()
    if key in seen:
        return
    seen.add(key)
    for node in tree.nodes:
        yield node
        if node.type == 'GROUP' and node.node_tree:
            yield from _nodes(node.node_tree, seen)


def _surface(mat):
    """Lit color, including MToon textures that live inside a node group."""
    color = list(mat.diffuse_color) if mat else [.65, .65, .65, 1.]
    image = None
    scale = [1., 1.]
    offset = [0., 0.]
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return color, image, scale, offset
    base = None
    for node in _nodes(mat.node_tree):
        if node.type == 'TEX_IMAGE' and node.image and 'BaseColor' in node.name:
            base = node
            break
    if base is not None:
        image = base.image
        vector = base.inputs.get('Vector')
        src = vector.links[0].from_node if vector and vector.is_linked else None
        if src is not None and src.type == 'GROUP':
            def grab(name, default):
                sock = src.inputs.get(name)
                return float(sock.default_value) if sock is not None else default
            scale = [grab('UV Scale X', 1.), grab('UV Scale Y', 1.)]
            offset = [grab('UV Offset X', 0.), grab('UV Offset Y', 0.)]
        return color, image, scale, offset
    nodes = mat.node_tree.nodes
    bs = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    inp = bs.inputs.get('Base Color') if bs else None
    if inp is not None:
        color = list(inp.default_value)
        if inp.is_linked and inp.links[0].from_node.type == 'TEX_IMAGE':
            image = inp.links[0].from_node.image
    if image is None:
        node = next((n for n in nodes if n.type == 'TEX_IMAGE' and n.image and 'shade' not in n.name.lower()), None)
        if node:
            image = node.image
    return color, image, scale, offset


def _look_offsets():
    """Per-material UV shift at full lookUp/Down/Left/Right, in texture space."""
    found = {}
    try:
        from io_scene_vrm.editor.extension_accessor import get_armature_extension
    except Exception:
        return found
    for arm in bpy.context.scene.objects:
        if arm.type != 'ARMATURE':
            continue
        try:
            preset = get_armature_extension(arm.data).vrm1.expressions.preset
        except Exception:
            continue
        for key, expr in (('up', preset.look_up), ('down', preset.look_down),
                          ('left', preset.look_left), ('right', preset.look_right)):
            for bind in expr.texture_transform_binds:
                if bind.material is None:
                    continue
                slot = found.setdefault(bind.material.name, {})
                slot[key] = (float(bind.offset[0]), float(bind.offset[1]))
    return found


def _eye_bones():
    try:
        from io_scene_vrm.editor.extension_accessor import get_armature_extension
    except Exception:
        return None
    for arm in bpy.context.scene.objects:
        if arm.type != 'ARMATURE':
            continue
        ext = get_armature_extension(arm.data)
        for group in (getattr(ext, 'vrm1', None), getattr(ext, 'vrm0', None)):
            human = getattr(group, 'humanoid', None)
            bones = getattr(human, 'human_bones', None)
            if bones is None:
                continue
            pair = []
            for side in ('left_eye', 'right_eye'):
                node = getattr(getattr(bones, side, None), 'node', None)
                name = getattr(node, 'bone_name', None) if node is not None else None
                pair.append(name)
            if not all(pair):
                continue
            palette = sorted(bone.name for bone in arm.data.bones)
            if all(name in palette for name in pair):
                return dict(left=palette.index(pair[0]), right=palette.index(pair[1]))
    return None


def extract(out):
    out = Path(io_path(out))
    if (out / CACHE / 'manifest.json').is_file():
        return
    temp = out / ('.textures-' + uuid.uuid4().hex)
    temp.mkdir()
    try:
        images, sizes, batches, corners, faces = {}, {}, [], [], []
        looks = _look_offsets()
        offset = total = 0
        for obj in bpy.context.scene.objects:
            if obj.type != 'MESH':
                continue
            mesh = obj.data
            mesh.calc_loop_triangles()
            uv = mesh.uv_layers.active
            faces.extend([[offset + i for i in t.vertices] for t in mesh.loop_triangles])
            for mi in sorted({t.material_index for t in mesh.loop_triangles}):
                mat = mesh.materials[mi] if mi < len(mesh.materials) else None
                color, tex, scale, shift = _surface(mat)
                key = None
                if tex and uv and tex.size[0] and tex.size[1]:
                    key = images.get(tex.name)
                    if key is None:
                        w, h = tex.size
                        total += w * h * 4
                        if w * h * 4 > 256 * 1024**2 or total > 512 * 1024**2:
                            raise ValueError('Original character textures exceed the 256 MB/image or 512 MB/character preview limit.')
                        key = 'image' + str(len(images))
                        images[tex.name] = key
                        sizes[key] = [w, h]
                        pixels = np.empty(w * h * 4, np.float32)
                        tex.pixels.foreach_get(pixels)
                        np.clip(pixels, 0, 1, out=pixels)
                        pixels *= 255
                        np.save(temp / (key + '.npy'), pixels.reshape(h, w, 4).astype(np.uint8))
                        del pixels
                start = len(corners)
                for tri in mesh.loop_triangles:
                    if tri.material_index == mi:
                        for li in tri.loops:
                            u, v = uv.data[li].uv if uv else (0, 0)
                            corners.append([offset + mesh.loops[li].vertex_index,
                                            u * scale[0] + shift[0], v * scale[1] + shift[1]])
                batch = dict(start=start, count=len(corners)-start, image=key, color=color)
                gaze = looks.get(mat.name) if mat is not None else None
                if gaze:
                    order = ('up', 'down', 'left', 'right')
                    batch['look'] = [part for key in order for part in gaze.get(key, (0., 0.))]
                batches.append(batch)
            offset += len(mesh.vertices)
        faces = np.asarray(faces, np.uint32)
        with np.load(out / 'preview.npz', allow_pickle=False) as asset:
            if len(asset['vertices']) != offset or not np.array_equal(asset['faces'], faces):
                raise ValueError('Stored character geometry differs from the texture source. Reimport the character.')
        np.save(temp / 'corners.npy', np.asarray(corners, np.float32))
        metadata = dict(version=1, vertices=offset, faces_sha256=hashlib.sha256(faces.tobytes()).hexdigest(),
                        batches=batches, images=sizes)
        (temp / 'manifest.json').write_text(json.dumps(metadata), encoding='utf8')
        eyes = _eye_bones()
        if eyes:
            (temp / 'eyes.json').write_text(json.dumps(eyes), encoding='utf8')
        temp.rename(out / CACHE)
    finally:
        # Only this invocation's temporary directory, never a user asset.
        if temp.exists():
            shutil.rmtree(temp)
