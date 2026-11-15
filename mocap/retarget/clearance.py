"""Keep the character's arms outside its own torso, legs and clothing.

Runs after the contact solve on the baked stance. Only shoulder rotations change;
hand world orientation, root and legs are preserved.
"""
import json
from pathlib import Path
import numpy as np
from scipy.ndimage import gaussian_filter1d, maximum_filter1d
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation as R
from .mapping import PARENTS

BODY = [0, 1, 2, 3, 4, 5, 6, 9]
ARM = [13, 14, 16, 17, 18, 19, 20, 21]
CFG = dict(radius=.06, tolerance=.004, margin=.006, voxel_body=.015, voxel_arm=.02,
           forearm_skip=.25, twist_weight=8., regularize=.02, limit_deg=40., iterations=4,
           smoothing_s=(.08, .05, .03))


def _fk(local, rest, root):
    world = local.copy()
    p = np.empty(local.shape[:-2] + (3,))
    p[:, 0] = root
    for j in range(1, len(rest)):
        parent = PARENTS[j]
        p[:, j] = p[:, parent] + world[:, parent] @ (rest[j] - rest[parent])
        world[:, j] = world[:, parent] @ local[:, j]
    return p, world


def _local(world):
    q = world.copy()
    for j in range(1, 22):
        q[:, j] = world[:, PARENTS[j]].swapaxes(-1, -2) @ world[:, j]
    return q


def _voxel(points, size):
    keys = np.floor(points / size).astype(np.int64)
    _, first = np.unique(keys, axis=0, return_index=True)
    return np.sort(first)


def _normals(vertices, faces):
    a, b, c = (vertices[faces[:, k]] for k in range(3))
    area = np.cross(b - a, c - a)
    normals = np.zeros_like(vertices)
    for k in range(3):
        np.add.at(normals, faces[:, k], area)
    return normals / np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)


def joint_weights(asset, kind):
    """Character vertices, normals, 22-joint skin weights and mesh layer from its import preview."""
    asset = Path(asset)
    preview = np.load(asset / 'preview.npz')
    rig = json.loads((asset / f'{kind}-rig.json').read_text(encoding='utf8'))
    mapping = [str(n) for n in np.load(asset / f'{kind}-canonical.npz')['names']]
    parents = {b['name']: b['parent'] for b in rig['bones']}
    palette = sorted(parents)
    mapped = {name: i for i, name in enumerate(mapping)}
    if kind == 'pmx':
        for side, suffix in enumerate(['.L', '.R']):
            for stem, joint in [('足D', 1 + side), ('ひざD', 4 + side), ('足首D', 7 + side), ('足先EX', 10 + side)]:
                mapped[stem + suffix] = joint
    owner = np.zeros(len(palette) + 1, int)
    for i, name in enumerate(palette):
        b = name
        while b is not None and b not in mapped:
            b = parents.get(b)
        owner[i] = mapped[b] if b is not None else 0
    vertices = preview['vertices'].astype(float)
    rows = preview['rows'].astype(np.int64)
    influences = preview['weights']
    bone = influences[:, 0].astype(np.int64)
    if len(influences) != rows[:, 1].sum() or bone.max() > len(palette):
        raise ValueError('Character preview weights do not match its rig. Import the character again.')
    W = np.zeros((len(vertices), 22))
    np.add.at(W, (np.repeat(np.arange(len(vertices)), rows[:, 1]), owner[bone]), influences[:, 1])
    W /= np.maximum(W.sum(1, keepdims=True), 1e-8)
    counts = [m['vertices'] for m in rig['meshes']]
    layer = np.repeat(np.arange(len(counts)), counts) if sum(counts) == len(vertices) else np.zeros(len(vertices), int)
    return vertices, _normals(vertices, preview['faces'].astype(np.int64)), W, layer


def geometry(asset, kind, rest):
    vertices, normals, W, layer = joint_weights(asset, kind)
    body = (W[:, ARM].sum(1) < .02) & (W[:, [12, 15]].sum(1) < .5) & (W[:, BODY].sum(1) > .5)
    ids = np.flatnonzero(body)
    ids = np.concatenate([part[_voxel(vertices[part], CFG['voxel_body'])]
                          for part in (ids[layer[ids] == k] for k in np.unique(layer[ids]))])
    collider = dict(v=vertices[ids], n=normals[ids], w=W[ids], layer=layer[ids])
    arms = []
    for side in (0, 1):
        shoulder, elbow, wrist = 16 + side, 18 + side, 20 + side
        chain = W[:, [shoulder, elbow, wrist]]
        dominant = chain.argmax(1)
        axis = rest[wrist] - rest[elbow]
        along = (vertices - rest[elbow]) @ axis / (axis @ axis)
        # Upper arms overlap the torso at the armpit in ordinary skinning; only visible hand and forearm clipping is solved.
        keep = (chain.sum(1) > .9) & ((dominant == 2) | ((dominant == 1) & (along > CFG['forearm_skip'])))
        ids = np.flatnonzero(keep)
        ids = ids[_voxel(vertices[ids], CFG['voxel_arm'])]
        arms.append(dict(v=vertices[ids], joint=np.array([shoulder, elbow, wrist])[dominant[ids]]))
    return collider, arms


def _skin(collider, world, points, rest):
    joints = np.flatnonzero(collider['w'].any(0))
    v = np.zeros_like(collider['v'])
    n = np.zeros_like(collider['n'])
    for j in joints:
        w = collider['w'][:, j:j + 1]
        v += w * ((collider['v'] - rest[j]) @ world[j].T + points[j])
        n += w * (collider['n'] @ world[j].T)
    return v, n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)


def _arm_points(arm, world, points, rest):
    j = arm['joint']
    return np.einsum('nij,nj->ni', world[j], arm['v'] - rest[j]) + points[j]


class Collider:
    """One posed frame of the body collider, searched per mesh layer."""

    def __init__(self, collider, world, points, rest):
        v, n = _skin(collider, world, points, rest)
        self.layers = []
        for k in np.unique(collider['layer']):
            ids = np.flatnonzero(collider['layer'] == k)
            if len(ids) >= 3:
                self.layers.append((cKDTree(v[ids]), v[ids], n[ids]))
        if not self.layers:
            raise ValueError('The character has no torso or leg surface to keep the arms outside.')

    def depth(self, q):
        """Penetration behind the nearest surface of each mesh layer; the deepest layer wins.

        Within a layer only neighbors facing the same way as the nearest one count, so far
        sides of limbs and inner linings do not register as penetration.
        """
        best = np.full(len(q), -np.inf)
        normal = np.zeros_like(q)
        for tree, v, n in self.layers:
            distance, index = tree.query(q, k=3, distance_upper_bound=CFG['radius'])
            found = np.isfinite(distance)
            index = np.where(found, index, 0)
            agree = found & (np.einsum('qkd,qd->qk', n[index], n[index[:, 0]]) > .5)
            weight = agree / np.maximum(distance, 1e-4)
            depth = np.einsum('qkd,qkd->qk', v[index] - q[:, None], n[index])
            layer = np.where(found[:, 0], (weight * depth).sum(1) / np.maximum(weight.sum(1), 1e-12) + CFG['margin'], -np.inf)
            deeper = layer > best
            best[deeper] = layer[deeper]
            normal[deeper] = n[index[deeper, 0]]
        return best, normal


def _rotate_arm(world, points, rest, side, rotvec):
    """Rotate one shoulder in world space while keeping the hand's world orientation."""
    shoulder, elbow, wrist = 16 + side, 18 + side, 20 + side
    Q = R.from_rotvec(rotvec).as_matrix()
    w = world.copy()
    p = points.copy()
    for j in (shoulder, elbow):
        w[j] = Q @ world[j]
    for j in (elbow, wrist):
        p[j] = points[shoulder] + Q @ (points[j] - points[shoulder])
    return w, p


def _solve_frame(collider, arm, world, points, rest, side, start):
    shoulder = 16 + side
    axis = points[18 + side] - points[shoulder]
    axis /= max(np.linalg.norm(axis), 1e-12)
    rotvec = start.copy()
    limit = np.radians(CFG['limit_deg'])
    for _ in range(CFG['iterations']):
        w, p = _rotate_arm(world, points, rest, side, rotvec)
        q = _arm_points(arm, w, p, rest)
        depth, normal = collider.depth(q)
        hit = depth > CFG['tolerance']
        if not hit.any():
            break
        J = np.cross(q[hit] - p[shoulder], normal[hit])
        H = J.T @ J + CFG['regularize'] * np.eye(3) + CFG['regularize'] * CFG['twist_weight'] * np.outer(axis, axis)
        step = np.linalg.solve(H, J.T @ depth[hit])
        rotvec = (R.from_rotvec(step) * R.from_rotvec(rotvec)).as_rotvec()
        angle = np.linalg.norm(rotvec)
        if angle > limit:
            rotvec *= limit / angle
    return rotvec


def _measure(colliders, arms, world, points, rest):
    worst = np.zeros((len(world), 2))
    for t, collider in enumerate(colliders):
        for side in (0, 1):
            depth, _ = collider.depth(_arm_points(arms[side], world[t], points[t], rest))
            worst[t, side] = max(0., float(depth.max()) - CFG['margin'])
    return worst


def apply(stance_path, asset, kind, contacts, fps):
    with np.load(stance_path) as stored:
        data = dict(stored)
    rest = data['rest']
    collider, arms = geometry(asset, kind, rest)
    world, points = data['world'], data['points']
    T = len(world)
    colliders = [Collider(collider, world[t], points[t], rest) for t in range(T)]
    before = _measure(colliders, arms, world, points, rest)
    free = np.ones((T, 2), bool)
    if contacts is not None:
        free = ~maximum_filter1d(contacts[:, 2:4].astype(bool), size=max(1, int(round(.25 * fps))) * 2 + 1, axis=0)
    rotvec = np.zeros((T, 2, 3))
    for sigma in CFG['smoothing_s']:
        for side in (0, 1):
            for t in np.flatnonzero(free[:, side] & ((before[:, side] > CFG['tolerance']) | (np.linalg.norm(rotvec[:, side], axis=1) > 0))):
                rotvec[t, side] = _solve_frame(colliders[t], arms[side], world[t], points[t], rest, side, rotvec[t, side])
        rotvec = gaussian_filter1d(rotvec, sigma * fps, axis=0, mode='nearest') * free[..., None]
    corrected = world.copy()
    for t in range(T):
        for side in (0, 1):
            if np.any(rotvec[t, side]):
                corrected[t], _ = _rotate_arm(corrected[t], points[t], rest, side, rotvec[t, side])
    local = _local(corrected)
    points2, world2 = _fk(local, rest, data['root'])
    after = _measure(colliders, arms, world2, points2, rest)
    data.update(local=local, world=world2, points=points2)
    np.savez(stance_path, **data)
    angle = np.degrees(np.linalg.norm(rotvec, axis=-1))
    return dict(frames=T, collider_points=len(collider['v']), arm_points=[len(a['v']) for a in arms],
                corrected_frames=int((angle > .5).any(1).sum()), max_correction_deg=float(angle.max()),
                penetration_before_max_m=float(before.max()), penetration_after_max_m=float(after.max()),
                penetrating_frames_before=int((before > CFG['tolerance']).any(1).sum()),
                penetrating_frames_after=int((after > CFG['tolerance']).any(1).sum()))
