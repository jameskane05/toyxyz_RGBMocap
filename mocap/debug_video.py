"""Overlay videos of person tracking and 2D skeletons.

These files are diagnostic. They are not inputs to inference, correction, or export.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .video import executable

def _fingers(wrist, start):
    bones = []
    for finger in range(5):
        joint = start + finger * 4
        bones.append((wrist, joint))
        bones.extend((joint + index, joint + index + 1) for index in range(3))
    return tuple(bones)


# Same body links as the video overlay.
COCO_BONES = ((5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12),
              (11, 13), (13, 15), (12, 14), (14, 16), (0, 1), (0, 2), (1, 3), (2, 4))
# COCO-WholeBody feet, then four joints per finger starting at the knuckle.
EXMR_BONES = COCO_BONES + ((15, 17), (15, 18), (15, 19), (16, 20), (16, 21), (16, 22),
                           *_fingers(9, 23), *_fingers(10, 43))
COCO_THRESHOLD = 0.25
EXMR_THRESHOLD = 0.5
OBSERVED = (191, 231, 64)
MISSED = (98, 189, 255)
BONE = (255, 236, 124)
JOINT = (207, 246, 255)


def enabled():
    return os.environ.get('RGB_MOCAP_DEBUG_VIDEOS') == '1'


def export_for_stage(project, stage, emit, person_id=None):
    if not enabled():
        return
    project = Path(project)
    try:
        if stage == 'person':
            _export_person_tracking(project, person_id, emit)
        elif stage == 'track':
            export_tracking(project, emit)
        elif stage == 'exmr_preprocess':
            points, confidence = _torch_points(project / 'exmr_keypoints.pt', 63)
            _export_skeleton(project, emit, points, confidence, EXMR_BONES, EXMR_THRESHOLD,
                             [project / 'exmr_keypoints.pt'])
        elif stage == 'gvhmr':
            points, confidence = _torch_points(project / 'gvhmr_keypoints.pt', 17)
            _export_skeleton(project, emit, points, confidence, COCO_BONES, COCO_THRESHOLD,
                             [project / 'gvhmr_keypoints.pt'])
        elif stage == 'pva' and not (project / 'gvhmr_keypoints.pt').is_file():
            points, confidence = _pva_points(project / 'keypoints.npz')
            _export_skeleton(project, emit, points, confidence, COCO_BONES, COCO_THRESHOLD,
                             [project / 'keypoints.npz'])
        elif stage == 'flow_features':
            stored = project / 'debug' / 'flow_keypoints.npz'
            if not stored.is_file():
                _report(emit, 'Skeleton debug video needs a new FlowHMR feature extraction. Reused features have no saved body points.')
                return
            with np.load(stored, allow_pickle=False) as data:
                points = data['keypoints'].astype(np.float32)
            confidence = np.isfinite(points).all(-1).astype(np.float32)
            _export_skeleton(project, emit, points, confidence, _mhr_bones(), 0.5, [stored])
    except Exception as error:
        _report(emit, 'Debug video was not written: ' + str(error))


def export_tracking(project, emit):
    project = Path(project)
    tracking = project / 'tracking.npz'
    if not tracking.is_file() or not (project / 'input.mp4').is_file():
        raise RuntimeError('Tracking debug video needs the person box results.')
    with np.load(tracking, allow_pickle=False) as data:
        boxes = np.asarray(data['boxes'], np.float32)
        observed = np.asarray(data['observed']).astype(bool).reshape(-1)
    _expect_frames(project, len(boxes))
    if len(observed) != len(boxes):
        raise ValueError('Tracking observations and boxes have different frame counts.')

    def draw(frame, index):
        _box(frame, boxes[index], bool(observed[index]))
        _label(frame, f'Tracking  {index + 1}')

    _encode(project, 'tracking.mp4', draw, [tracking, project / 'input.mp4'], emit, 'tracking')


@contextmanager
def record_flow_keypoints(project):
    """Keep SAM body points without changing the feature function or its cache identity."""
    project = Path(project)
    if not enabled():
        try:
            yield
        except BaseException:
            raise
        else:
            # A new extraction without this option must not leave the previous skeleton video.
            _remove(project / 'debug' / 'flow_keypoints.npz')
            _remove(project / 'debug' / 'skeleton.mp4')
        return
    import mocap.flow_sam as flow_sam
    original = flow_sam.extract_tokens
    captured = []

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            captured.append(_flow_sample(result[1]))
        else:
            captured.append(None)
        return result

    flow_sam.extract_tokens = wrapped
    try:
        yield
    except BaseException:
        raise
    else:
        try:
            _store_flow_keypoints(project, captured)
        except Exception as error:
            _remove(project / 'debug' / 'flow_keypoints.npz')
            _remove(project / 'debug' / 'skeleton.mp4')
            print('Skeleton observations were not saved: ' + str(error), file=sys.stderr, flush=True)
    finally:
        flow_sam.extract_tokens = original


def _export_person_tracking(root, person_id, emit):
    from .people_store import load_people, safe_child
    state = load_people(root)
    person = next((item for item in state['people'] if item['id'] == person_id), None) if state else None
    if person is None:
        return
    failed = None
    for take in person.get('takes', []):
        folder = safe_child(root, take['project'])
        if not (folder / 'tracking.npz').is_file() or not (folder / 'input.mp4').is_file():
            continue
        try:
            export_tracking(folder, emit)
        except Exception as error:
            failed = error
    if failed is not None:
        raise failed


def _export_skeleton(project, emit, points, confidence, bones, threshold, sources):
    _expect_frames(project, len(points))
    if confidence.shape[:2] != points.shape[:2]:
        raise ValueError('Skeleton points and confidence have different shapes.')

    def draw(frame, index):
        _pose(frame, points[index], confidence[index], bones, threshold)
        _label(frame, f'Skeleton  {index + 1}')

    _encode(project, 'skeleton.mp4', draw, [*sources, project / 'input.mp4'], emit, 'skeleton')


def _torch_points(path, joints):
    import torch
    value = torch.load(path, map_location='cpu', weights_only=False)
    array = value.detach().float().cpu().numpy() if hasattr(value, 'detach') else np.asarray(value, np.float32)
    if array.ndim == 4 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 3 or array.shape[1] < joints or array.shape[2] < 3:
        raise ValueError(f'Unexpected keypoint shape {getattr(array, "shape", None)}')
    return array[:, :joints, :2].astype(np.float32), array[:, :joints, 2].astype(np.float32)


def _pva_points(path):
    with np.load(path, allow_pickle=False) as data:
        points = np.asarray(data['keypoints'], np.float32)
        confidence = np.asarray(data['confidence'], np.float32)
    if points.ndim != 3 or points.shape[1:] != (17, 2):
        raise ValueError(f'Unexpected Analyze 2D keypoint shape {points.shape}')
    confidence = confidence.reshape((len(points), -1))
    if confidence.shape != (len(points), 17):
        raise ValueError(f'Unexpected Analyze 2D confidence shape {confidence.shape}')
    return points, confidence


def _flow_sample(outputs):
    if not outputs or not isinstance(outputs[0], dict):
        return None
    value = outputs[0].get('pred_keypoints_2d')
    if value is None:
        return None
    array = np.asarray(value, np.float32)
    if array.ndim != 2 or array.shape[0] < 70 or array.shape[1] < 2:
        return None
    return np.array(array[:70, :2], np.float32, copy=True)


def _store_flow_keypoints(project, captured):
    if not captured:
        return
    points = np.full((len(captured), 70, 2), np.nan, np.float32)
    for index, sample in enumerate(captured):
        if sample is not None:
            points[index] = sample
    folder = project / 'debug'
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / 'flow_keypoints.partial.npz'
    try:
        np.savez_compressed(temporary, keypoints=points)
        os.replace(temporary, folder / 'flow_keypoints.npz')
    finally:
        temporary.unlink(missing_ok=True)


def _mhr_bones():
    cached = getattr(_mhr_bones, 'value', None)
    if cached is not None:
        return cached
    from .paths import UPSTREAM
    path = UPSTREAM / 'FlowHMR/third_party/sam-3d-body/sam_3d_body/metadata/mhr70.py'
    if not path.is_file():
        path = UPSTREAM / 'FlowHMR/third_party/fast-sam-3d-body/sam_3d_body/metadata/mhr70.py'
    namespace = {}
    exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), namespace)
    info = namespace['pose_info']
    names = {item['name']: item['id'] for item in info['keypoint_info'].values()}
    bones = tuple((names[link[0]], names[link[1]]) for link in (item['link'] for item in info['skeleton_info'].values()))
    _mhr_bones.value = bones
    return bones


def _expect_frames(project, count):
    frames = int(json.loads((Path(project) / 'project.json').read_text(encoding='utf-8'))['frames'])
    if count != frames:
        raise ValueError('Debug observations and the working video have different frame counts.')


def _fresh(output, sources):
    if not output.is_file() or output.stat().st_size < 32:
        return False
    stamp = output.stat().st_mtime_ns
    return all(Path(source).is_file() and Path(source).stat().st_mtime_ns <= stamp for source in sources)


def _encode(project, filename, draw, sources, emit, label):
    project = Path(project)
    output = project / 'debug' / filename
    if _fresh(output, sources):
        return
    state = json.loads((project / 'project.json').read_text(encoding='utf-8'))
    width, height = int(state['width']), int(state['height'])
    frames, fps = int(state['frames']), float(state['fps'])
    if width % 2 or height % 2:
        raise RuntimeError('Debug video needs an even frame size.')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(filename + '.partial.mp4')
    temporary.unlink(missing_ok=True)
    import cv2
    capture = cv2.VideoCapture(str(project / 'input.mp4'))
    if not capture.isOpened():
        raise RuntimeError('Cannot open the working video for a debug export.')
    command = [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y',
               '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{width}x{height}', '-r', str(fps), '-i', 'pipe:0',
               '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p', '-threads', '2',
               str(temporary)]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                                   creationflags=0x08000000)
        written = False
        try:
            for index in range(frames):
                ok, frame = capture.read()
                if not ok:
                    raise RuntimeError(f'Cannot read video frame {index + 1} for the debug video.')
                if frame.shape[1] != width or frame.shape[0] != height:
                    raise RuntimeError('Debug video frame size does not match the project.')
                draw(frame, index)
                process.stdin.write(np.ascontiguousarray(frame).tobytes())
                if index % 15 == 0 or index == frames - 1:
                    emit('progress', current=index + 1, total=frames, message=f'Writing the {label} debug video')
            process.stdin.close()
            process.stdin = None
            if process.wait():
                errors.seek(0)
                detail = errors.read(2048).decode('utf-8', 'replace').strip()
                raise RuntimeError('Could not encode the debug video.' + (f' {detail}' if detail else ''))
            os.replace(temporary, output)
            written = True
        finally:
            capture.release()
            if process.stdin:
                process.stdin.close()
                process.stdin = None
            if process.poll() is None:
                process.kill()
                process.wait()
            if not written:
                temporary.unlink(missing_ok=True)
    print('Saved ' + output.relative_to(project).as_posix(), flush=True)
    emit('progress', current=frames, total=frames, message='Saved ' + output.relative_to(project).as_posix())


def _box(frame, box, seen):
    if box.shape != (4,) or not np.isfinite(box).all():
        return
    import cv2
    color = OBSERVED if seen else MISSED
    corners = np.rint(box).astype(int)
    x1, y1, x2, y2 = corners.tolist()
    thickness = max(2, int(round(frame.shape[0] / 540)))
    if seen:
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness, cv2.LINE_AA)
        return
    for start, end in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
        _dashed(frame, start, end, color, thickness)


def _dashed(frame, start, end, color, thickness):
    import cv2
    length = float(np.hypot(end[0] - start[0], end[1] - start[1]))
    if length < 1:
        return
    direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    dash, gap = 10, 7
    position = 0.0
    while position < length:
        stop = min(length, position + dash)
        p1 = (int(round(start[0] + direction[0] * position)), int(round(start[1] + direction[1] * position)))
        p2 = (int(round(start[0] + direction[0] * stop)), int(round(start[1] + direction[1] * stop)))
        cv2.line(frame, p1, p2, color, thickness, cv2.LINE_AA)
        position += dash + gap


def _pose(frame, points, confidence, bones, threshold):
    import cv2
    thickness = max(2, int(round(frame.shape[0] / 540)))
    radius = max(3, int(round(frame.shape[0] / 360)))
    for start, end in bones:
        if start >= len(points) or end >= len(points):
            continue
        if confidence[start] < threshold or confidence[end] < threshold:
            continue
        if not np.isfinite(points[start]).all() or not np.isfinite(points[end]).all():
            continue
        cv2.line(frame, _pixel(points[start]), _pixel(points[end]), BONE, thickness, cv2.LINE_AA)
    for point, score in zip(points, confidence):
        if score >= threshold and np.isfinite(point).all():
            cv2.circle(frame, _pixel(point), radius, JOINT, -1, cv2.LINE_AA)


def _pixel(point):
    return int(round(float(point[0]))), int(round(float(point[1])))


def _label(frame, text):
    import cv2
    scale = max(0.5, frame.shape[0] / 1080)
    origin = (12, int(round(28 * scale)) + 8)
    cv2.putText(frame, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), max(2, int(round(3 * scale))), cv2.LINE_AA)
    cv2.putText(frame, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), max(1, int(round(scale))), cv2.LINE_AA)


def _report(emit, message):
    print(message, flush=True)
    emit('progress', current=0, total=0, message=message)


def _remove(path):
    Path(path).unlink(missing_ok=True)
