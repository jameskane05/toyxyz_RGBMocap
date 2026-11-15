"""Frame ruler with bounded cached tracks and direct seeking."""
import math
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QPainterPath
from PySide6.QtWidgets import QSlider, QSizePolicy
from .footlock_editor import FootLockEditor


def normalize_segments(segments):
    """Sort clips and shorten any overlap. Each clip is (place, source in, source out), exclusive out."""
    items = []
    for place, src_in, src_out in segments or []:
        place, src_in, src_out = int(place), int(src_in), int(src_out)
        if src_out - src_in >= 1:
            items.append((place, src_in, src_out))
    items.sort()
    out = []
    for place, src_in, src_out in items:
        if out:
            previous, src_a, src_b = out[-1]
            previous_end = previous + (src_b - src_a)
            if place < previous_end:
                keep = place - previous
                if keep < 1:
                    out.pop()
                else:
                    out[-1] = (previous, src_a, src_a + keep)
        if src_out - src_in >= 1:
            out.append((place, src_in, src_out))
    return out


def subtract_range(segments, start, end):
    """Drop timeline [start, end). Later audio stays where it is, so the picture does not slip."""
    start, end = int(start), int(end)
    if end <= start:
        return list(segments or [])
    out = []
    for place, src_in, src_out in segments or []:
        length = src_out - src_in
        seg_end = place + length
        if seg_end <= start or place >= end:
            out.append((place, src_in, src_out))
            continue
        if place < start:
            out.append((place, src_in, src_in + (start - place)))
        if seg_end > end:
            cut = end - place
            out.append((end, src_in + cut, src_out))
    return normalize_segments(out)


def extract_range(segments, start, end):
    start, end = int(start), int(end)
    out = []
    for place, src_in, src_out in segments or []:
        seg_end = place + (src_out - src_in)
        begin = max(place, start)
        finish = min(seg_end, end)
        if finish <= begin:
            continue
        out.append((begin, src_in + (begin - place), src_in + (finish - place)))
    return out


def move_range(segments, start, end, delta, low, high):
    """Slide the audio inside [start, end) by `delta` frames. Returns (segments, applied delta)."""
    pieces = extract_range(segments, start, end)
    if not pieces:
        return list(segments or []), 0
    delta = int(delta)
    low, high = int(low), int(high)
    earliest = max(low - place for place, _src_in, _src_out in pieces)
    latest = min(high - (place + src_out - src_in) for place, src_in, src_out in pieces)
    delta = 0 if earliest > latest else max(earliest, min(latest, delta))
    if delta == 0:
        return list(segments or []), 0
    remaining = subtract_range(segments, start, end)
    moved = [(place + delta, src_in, src_out) for place, src_in, src_out in pieces]
    result = remaining
    for piece in moved:
        place, src_in, src_out = piece
        result = subtract_range(result, place, place + (src_out - src_in))
        result.append(piece)
    return normalize_segments(result), delta


def split_at(segments, frame):
    frame = int(frame)
    out = []
    changed = False
    for place, src_in, src_out in segments or []:
        seg_end = place + (src_out - src_in)
        if place < frame < seg_end:
            cut = frame - place
            out.append((place, src_in, src_in + cut))
            out.append((frame, src_in + cut, src_out))
            changed = True
        else:
            out.append((place, src_in, src_out))
    return out if changed else list(segments or [])


class TimelineSlider(FootLockEditor,QSlider):
    clip_changed = Signal(int, int)  # Inclusive in, exclusive out, in media frames.
    trim_started = Signal()
    audio_edit_finished = Signal()
    contacts_changed=Signal(object)
    contacts_edit_started=Signal()
    contacts_reset=Signal()
    contacts_preview_changed=Signal()
    LEFT, RIGHT = 64, 14
    TRACK_HEIGHT = 26
    RULER_HEIGHT = 25

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setMinimumHeight(248)
        self.face_span=None;self.audio_span=None
        self.face_wave=None;self.audio_wave=None;self.face_revision=0
        self.face_empty='No face capture'
        self.audio_empty='No audio'
        self.audio_level=0.0
        self.audio_meter_on=False
        self.init_contacts()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)
        self.fps = 30.0
        self.has_motion = False
        self.person_color = self.person_name = None
        self.view_start = 0.0
        self.view_span = None
        self._cache = None
        self._cache_key = None
        self._pan_origin = None
        self._playhead_drag = None
        self.clip_in, self.clip_out = 0, 1
        self.audio_segments = []
        self.audio_selection = None
        self._audio_limit_key = None
        self.trim_enabled = True
        self._trim_drag = None
        self._audio_gesture = None
        self.setMouseTracking(True)
        self.rangeChanged.connect(self.frame_all)
        self.rangeChanged.connect(self.reset_trim)
        self.valueChanged.connect(self._follow)

    def frame_all(self, *_):
        self.view_start = float(self.minimum())
        self.view_span = None
        self.update()

    def set_media(self, fps, has_motion=None):
        self.fps = fps
        if has_motion is not None:
            self.has_motion = has_motion
        self.update()

    def minimum_clip_frames(self):
        # Reconstruction prepares at 30 fps and requires at least four frames.
        return min(self.maximum() + 1, max(1, math.ceil(self.fps * 4 / 30)))

    def reset_trim(self, *_):
        self.set_clip(0, self.maximum() + 1)

    def set_audio_span(self, span, wave, crop=None, restore=False, edits=None):
        """Show `span` as (timeline start seconds, duration). `edits` are arranged clips."""
        limits = (self.minimum(), self.maximum(), float(self.fps or 30.0))
        limits_changed = limits != self._audio_limit_key
        same = span == self.audio_span and not restore and not limits_changed
        self.audio_span = span
        self.audio_wave = wave
        dragging = self._audio_gesture is not None or bool(self._trim_drag and self._trim_drag[0] == 'audio')
        if not span:
            self.audio_segments = []
            self.audio_selection = None
        elif dragging or (same and (self.audio_segments or self.audio_is_edited())):
            self.audio_segments = self._clamp_segments(self.audio_segments)
        elif edits is not None:
            self._apply_edits(edits)
        elif crop is not None:
            self._apply_file_crop(crop)
        else:
            self.reset_audio_trim()
        if not (dragging and limits_changed):
            self._audio_limit_key = limits
        self.update()

    def _audio_limits(self):
        origin, duration = self.audio_span
        fps = self.fps or 30.0
        first = int(math.floor(origin * fps + 1e-4))
        last = int(math.ceil((origin + duration) * fps - 1e-4))
        first = max(self.minimum(), first)
        last = min(self.maximum() + 1, max(first + 1, last))
        return first, last

    def _clamp_segments(self, segments):
        if not self.audio_span:
            return []
        src_lo, src_hi = self._audio_limits()
        low, high = self.minimum(), self.maximum() + 1
        clamped = []
        for place, src_in, src_out in segments or []:
            src_in = max(src_lo, min(src_hi, int(src_in)))
            src_out = max(src_in, min(src_hi, int(src_out)))
            if src_out <= src_in:
                continue
            length = src_out - src_in
            place = max(low, min(high - 1, int(place)))
            if place + length > high:
                src_out -= place + length - high
            if src_out - src_in >= 1:
                clamped.append((place, src_in, src_out))
        return normalize_segments(clamped)

    def reset_audio_trim(self):
        self.audio_selection = None
        if not self.audio_span:
            self.audio_segments = []
            self.update()
            return
        first, last = self._audio_limits()
        self.audio_segments = [(first, first, last)]
        self.update()

    def _apply_file_crop(self, crop):
        origin, _duration = self.audio_span
        fps = self.fps or 30.0
        first = int(round((origin + float(crop[0])) * fps))
        last = int(round((origin + float(crop[1])) * fps))
        self.audio_selection = None
        self.audio_segments = self._clamp_segments([(first, first, last)])

    def _apply_edits(self, edits):
        origin = self.audio_span[0]
        fps = self.fps or 30.0
        segments = []
        for item in edits or []:
            if not isinstance(item, dict):
                continue
            try:
                place = int(round((origin + float(item['t'])) * fps))
                src_in = int(round((origin + float(item['in'])) * fps))
                src_out = int(round((origin + float(item['out'])) * fps))
            except (KeyError, TypeError, ValueError):
                continue
            segments.append((place, src_in, src_out))
        self.audio_selection = None
        self.audio_segments = self._clamp_segments(segments)

    def audio_is_edited(self):
        if not self.audio_span:
            return False
        first, last = self._audio_limits()
        return self.audio_segments != [(first, first, last)]

    def audio_is_cropped(self):
        return self.audio_is_edited()

    def audio_segments_seconds(self):
        """Clips as (timeline place, source in, source out) in seconds on the wav clock."""
        if not self.audio_span:
            return []
        origin = self.audio_span[0]
        fps = self.fps or 30.0
        return [(place / fps - origin, src_in / fps - origin, src_out / fps - origin)
                for place, src_in, src_out in self.audio_segments]

    def audio_edit_payload(self):
        if not self.audio_is_edited():
            return None
        payload = []
        for place, src_in, src_out in self.audio_segments_seconds():
            if src_out - src_in < 1e-3:
                continue
            payload.append({'t': round(place, 4), 'in': round(src_in, 4), 'out': round(src_out, 4)})
        return payload

    def delete_audio_selection(self):
        if not self.audio_selection:
            return False
        start, end = self.audio_selection
        self.audio_segments = subtract_range(self.audio_segments, start, end)
        self.audio_selection = None
        self.update()
        self.audio_edit_finished.emit()
        return True

    def split_audio_at_playhead(self):
        if not self.audio_segments:
            return False
        changed = split_at(self.audio_segments, self.value())
        if changed == self.audio_segments:
            return False
        self.audio_segments = changed
        self.audio_selection = None
        self.update()
        self.audio_edit_finished.emit()
        return True

    def _segment_at(self, frame):
        for index, (place, src_in, src_out) in enumerate(self.audio_segments):
            if place <= frame < place + (src_out - src_in):
                return index
        return None

    def _segment_bounds(self, index):
        place, src_in, src_out = self.audio_segments[index]
        return place, place + (src_out - src_in)

    def set_clip(self, first, end):
        total, minimum = self.maximum() + 1, self.minimum_clip_frames()
        first = max(0, min(total - minimum, int(first)))
        end = max(first + minimum, min(total, int(end)))
        if (first, end) != (self.clip_in, self.clip_out):
            self.clip_in, self.clip_out = first, end
            self.clip_changed.emit(first, end)
            self.update()

    def clip_edges(self):
        return self.frame_x(self.clip_in), self.frame_x(self.clip_out - 1)

    def _segment_edge_hits(self, event):
        hits = []
        x = event.position().x()
        for index, (place, src_in, src_out) in enumerate(self.audio_segments):
            end = place + (src_out - src_in) - 1
            for frame, edge in ((place, 'in'), (end, 'out')):
                pixel = self.frame_x(frame)
                if self.LEFT - 1 <= pixel <= self.width() - self.RIGHT + 1:
                    hits.append((abs(x - pixel), index, edge, pixel))
        return hits

    def _row_band(self, row):
        top = 28 + row * self.TRACK_HEIGHT
        return top, top + self.TRACK_HEIGHT

    def _edge_handle(self, event, edges):
        x = event.position().x()
        hits = [(abs(x - edge), name) for edge, name in zip(edges, ("in", "out"))
                if self.LEFT - 1 <= edge <= self.width() - self.RIGHT + 1]
        distance, name = min(hits, default=(float("inf"), None))
        return name if distance <= 8 else None

    def _trim_handle(self, event):
        if not self.trim_enabled:
            return None
        y = event.position().y()
        video_top, video_bottom = self._row_band(0)
        if video_top <= y <= video_bottom:
            edge = self._edge_handle(event, self.clip_edges())
            return ("video", edge) if edge else None
        audio_top, audio_bottom = self._row_band(3)
        if self.audio_segments and audio_top <= y <= audio_bottom:
            hits = self._segment_edge_hits(event)
            if hits:
                distance, index, edge, _pixel = min(hits)
                if distance <= 8:
                    return ("audio", index, edge)
        return None

    def _selection_edge(self, event):
        if not self.audio_selection:
            return None
        top, bottom = self._row_band(3)
        if not top <= event.position().y() <= bottom:
            return None
        start, end = self.audio_selection
        x = event.position().x()
        edges = []
        for frame, name in ((start, 'in'), (max(start, end - 1), 'out')):
            pixel = self.frame_x(frame)
            if self.LEFT - 1 <= pixel <= self.width() - self.RIGHT + 1:
                edges.append((abs(x - pixel), name))
        if not edges:
            return None
        distance, name = min(edges)
        return name if distance <= 8 else None

    def _audio_press(self, event):
        top, bottom = self._row_band(3)
        point = event.position()
        if not self.trim_enabled or not self.audio_segments or not top <= point.y() <= bottom:
            return False
        if point.x() < self.LEFT:
            return False
        frame = self._position(event)
        edge = self._selection_edge(event)
        if edge and not self._trim_handle(event):
            self._audio_gesture = {'mode': 'select-edge', 'edge': edge, 'selection': self.audio_selection}
            self.setCursor(Qt.SizeHorCursor)
            return True
        if self.audio_selection and self.audio_selection[0] <= frame < self.audio_selection[1]:
            self._audio_gesture = {
                'mode': 'move', 'anchor': frame,
                'segments': list(self.audio_segments), 'selection': self.audio_selection}
            self.setCursor(Qt.ClosedHandCursor)
            return True
        self._audio_gesture = {'mode': 'select', 'anchor': frame, 'moved': False}
        self.audio_selection = (frame, frame + 1)
        self.setCursor(Qt.IBeamCursor)
        self.update()
        return True

    def _drag_audio(self, event):
        gesture = self._audio_gesture
        frame = self._position(event)
        mode = gesture['mode']
        if mode == 'select':
            anchor = gesture['anchor']
            gesture['moved'] = gesture['moved'] or frame != anchor
            start, end = min(anchor, frame), max(anchor, frame) + 1
            self.audio_selection = (start, end)
            self.update()
            return
        if mode == 'select-edge':
            start, end = gesture['selection']
            if gesture['edge'] == 'in':
                start = min(frame, end - 1)
            else:
                end = max(frame + 1, start + 1)
            self.audio_selection = (start, end)
            gesture['selection'] = self.audio_selection
            self.update()
            return
        segments, delta = move_range(
            gesture['segments'], gesture['selection'][0], gesture['selection'][1],
            frame - gesture['anchor'], self.minimum(), self.maximum() + 1)
        self.audio_segments = segments
        start, end = gesture['selection']
        self.audio_selection = (start + delta, end + delta)
        self.update()

    def _finish_audio_gesture(self, event):
        gesture = self._audio_gesture
        self._drag_audio(event)
        self._audio_gesture = None
        changed = False
        if gesture['mode'] == 'select' and not gesture['moved']:
            index = self._segment_at(gesture['anchor'])
            if index is None:
                self.audio_selection = None
            else:
                self.audio_selection = self._segment_bounds(index)
            self.update()
        elif gesture['mode'] == 'move' and self.audio_segments != gesture['segments']:
            changed = True
        if changed:
            self.audio_edit_finished.emit()
        self.setCursor(Qt.ArrowCursor)

    def _drag_trim(self, event):
        track = self._trim_drag[0]
        frame = self._position(event)
        if track == "video":
            edge = self._trim_drag[1]
            if edge == "in":
                self.set_clip(min(frame, self.clip_out - self.minimum_clip_frames()), self.clip_out)
            else:
                self.set_clip(self.clip_in, max(frame + 1, self.clip_in + self.minimum_clip_frames()))
            self.setValue(self.clip_in if edge == "in" else self.clip_out - 1)
            return
        _track, index, edge = self._trim_drag
        if not 0 <= index < len(self.audio_segments):
            return
        segments = [tuple(item) for item in self.audio_segments]
        place, src_in, src_out = segments[index]
        length = src_out - src_in
        seg_end = place + length
        src_lo, src_hi = self._audio_limits()
        low, high = self.minimum(), self.maximum() + 1
        others = [item for item_index, item in enumerate(segments) if item_index != index]
        if edge == 'in':
            previous = max((item_place + (item_out - item_in) for item_place, item_in, item_out in others if item_place < place), default=low)
            frame = max(previous, min(seg_end - 1, frame))
            delta = frame - place
            new_src = src_in + delta
            if new_src < src_lo:
                frame += src_lo - new_src
                new_src = src_lo
            if new_src >= src_out:
                return
            segments[index] = (frame, new_src, src_out)
            self.setValue(frame)
        else:
            following = min((item_place for item_place, _item_in, _item_out in others if item_place >= seg_end), default=high)
            new_end = max(place + 1, min(following, frame + 1))
            new_src = src_in + (new_end - place)
            if new_src > src_hi:
                new_end -= new_src - src_hi
                new_src = src_hi
            if new_end <= place:
                return
            segments[index] = (place, src_in, new_src)
            self.setValue(new_end - 1)
        self.audio_segments = normalize_segments(segments)
        self.update()

    def span(self):
        return self.view_span if self.view_span is not None else max(1, self.maximum() - self.minimum())

    def frame_x(self, frame):
        return self.LEFT + (frame - self.view_start) / self.span() * max(1, self.width() - self.LEFT - self.RIGHT)

    def _position(self, event):
        width = max(1, self.width() - self.LEFT - self.RIGHT)
        value = self.view_start + (event.position().x() - self.LEFT) / width * self.span()
        return max(self.minimum(), min(self.maximum(), round(value)))

    def playhead_rect(self):
        """Share painted and clickable geometry; the line and pointer are inert."""
        width = max(25, self.fontMetrics().horizontalAdvance(str(self.value() + 1)) + 12)
        return QRectF(self.frame_x(self.value()) - width / 2, 1, width, 21)

    def _in_ruler(self,event):
        point=event.position()
        return 0 <= point.y() < self.RULER_HEIGHT and self.LEFT <= point.x() <= self.width()-self.RIGHT

    def _drag_playhead(self, event):
        pixel, frame, scale = self._playhead_drag
        value = round(frame + (event.position().x() - pixel) * scale)
        self.setValue(max(self.minimum(), min(self.maximum(), value)))

    def _clamp_view(self):
        self.view_start = max(self.minimum(), min(max(self.minimum(), self.maximum() - self.span()), self.view_start))

    def _follow(self, value):
        if self.view_span is not None and not self.view_start <= value <= self.view_start + self.span():
            self.view_start = value - self.span() * .2
            self._clamp_view()
        self.update()

    def mousePressEvent(self, event):
        if self._foot_press(event):return
        if event.button() == Qt.MiddleButton:
            self._pan_origin = (event.position().x(), self.view_start)
            event.accept()
        elif event.button() == Qt.LeftButton:
            self._trim_drag = self._trim_handle(event)
            if self._trim_drag:
                self.trim_started.emit()
                self.setCursor(Qt.SizeHorCursor)
                self._selected_contact = None
            elif self._audio_press(event):
                self.trim_started.emit()
                self.setFocus(Qt.MouseFocusReason)
                self._selected_contact = None
            elif self.playhead_rect().contains(event.position()):
                self.setFocus(Qt.MouseFocusReason)
                self._playhead_drag = (event.position().x(), self.value(),
                    self.span() / max(1, self.width() - self.LEFT - self.RIGHT))
                self.setSliderDown(True)
                self.setCursor(Qt.ClosedHandCursor)
            elif self._in_ruler(event):
                self.setFocus(Qt.MouseFocusReason)
                self.setValue(self._position(event))
                self._playhead_drag = (event.position().x(), self.value(),
                    self.span() / max(1, self.width() - self.LEFT - self.RIGHT))
                self.setSliderDown(True)
                self.setCursor(Qt.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._foot_move(event):return
        if self._pan_origin is not None:
            pixel, start = self._pan_origin
            self.view_start = start - (event.position().x() - pixel) * self.span() / max(1, self.width() - self.LEFT - self.RIGHT)
            self._clamp_view()
            self.update()
            event.accept()
        elif self._trim_drag:
            self._drag_trim(event)
            event.accept()
        elif self._audio_gesture is not None:
            self._drag_audio(event)
            event.accept()
        elif self._playhead_drag is not None:
            self._drag_playhead(event)
            event.accept()
        else:
            foot=self._contact_row(event)
            if foot is not None and self.contact_edit_enabled and event.position().x()>=self.LEFT:
                _,mode=self._contact_hit(foot,event)
                self.setCursor(Qt.SizeHorCursor if mode in ('left','right') else Qt.OpenHandCursor if mode=='move' else Qt.CrossCursor)
            else:self.setCursor(self._hover_cursor(event))
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._foot_release(event):return
        if event.button() == Qt.MiddleButton:
            self._pan_origin = None
            event.accept()
        elif event.button() == Qt.LeftButton:
            if self._trim_drag:
                track = self._trim_drag[0]
                self._drag_trim(event)
                self._trim_drag = None
                if track == "audio":
                    self.audio_edit_finished.emit()
            elif self._audio_gesture is not None:
                self._finish_audio_gesture(event)
            elif self._playhead_drag is not None:
                self._drag_playhead(event)
                self._playhead_drag = None
                self.setSliderDown(False)
            self.setCursor(Qt.ArrowCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        old_span = self.span()
        if event.modifiers() & Qt.ShiftModifier:
            self.view_start -= event.angleDelta().y() / 120 * old_span * .12
        else:
            fraction = max(0, min(1, (event.position().x() - self.LEFT) / max(1, self.width() - self.LEFT - self.RIGHT)))
            anchor = self.view_start + fraction * old_span
            total = max(1, self.maximum() - self.minimum())
            self.view_span = max(min(12, total), min(total, old_span * math.exp(-event.angleDelta().y() / 120 * .22)))
            self.view_start = anchor - fraction * self.view_span
        self._clamp_view()
        self.update()
        event.accept()

    def _hover_cursor(self, event):
        if self.playhead_rect().contains(event.position()):
            return Qt.OpenHandCursor
        if self._in_ruler(event):
            return Qt.PointingHandCursor
        if self._trim_handle(event) or self._selection_edge(event):
            return Qt.SizeHorCursor
        top, bottom = self._row_band(3)
        point = event.position()
        if self.audio_segments and top <= point.y() <= bottom and point.x() >= self.LEFT:
            frame = self._position(event)
            if self.audio_selection and self.audio_selection[0] <= frame < self.audio_selection[1]:
                return Qt.OpenHandCursor
            return Qt.IBeamCursor
        return Qt.ArrowCursor

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.audio_selection:
            self.audio_selection = None
            self.update()
            event.accept()
            return
        contact_selected = self.contact_edit_enabled and self._selected_contact is not None
        if not contact_selected and event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.delete_audio_selection():
            event.accept()
            return
        if event.key() == Qt.Key_K and not event.modifiers() and self.split_audio_at_playhead():
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _):
        key = (self.size(), self.devicePixelRatioF(), self.minimum(), self.maximum(), self.view_start,
               self.span(), self.fps, self.has_motion, self.clip_in, self.clip_out,self.contact_revision,self.contact_edit_enabled,self.contact_track_enabled,
               self.person_color,self.person_name,self.face_span,self.audio_span,tuple(self.audio_wave) if self.audio_wave else None,tuple(self.audio_segments),self.audio_selection,self.face_revision,self.face_empty,self.audio_empty,self.audio_meter_on,
               (self._foot_drag['foot'],self._foot_drag['index']) if self._foot_drag else None,self._selected_contact)
        if key != self._cache_key:
            ratio = self.devicePixelRatioF()
            cache = QPixmap(round(self.width() * ratio), round(self.height() * ratio))
            cache.setDevicePixelRatio(ratio)
            cache.fill(QColor("#242424"))
            p = QPainter(cache)
            p.fillRect(0, 0, self.width(), self.RULER_HEIGHT, QColor("#303030"))
            target = self.span() / max(1, (self.width() - self.LEFT) / 85)
            power = 10 ** math.floor(math.log10(max(1, target)))
            step = next((v * power for v in (1, 2, 5, 10) if v * power >= target), power * 10)
            first = math.ceil((self.view_start + 1) / step) * step - 1
            row_height = self.TRACK_HEIGHT
            rows = ((0, "Video", "#6289ad"), (1, "Motion", (self.person_color or "#6aa186") if self.has_motion else "#303030"))
            # Rows keep an editor-like height; extra panel space is available
            # below them for future channels rather than stretching controls.
            for row, label, color in rows:
                y = 28 + row * row_height
                tint = QColor(color)
                tint.setAlpha(80)
                left, right = self.clip_edges() if row == 0 else (self.LEFT, self.width() - self.RIGHT)
                left, right = max(self.LEFT, left), min(self.width() - self.RIGHT, right)
                p.fillRect(QRectF(left, y + 2, max(0, right - left), row_height - 5), tint)
            self.paint_contact_tracks(p)
            self.paint_face_tracks(p)
            # Draw the time grid above the translucent tracks. The visible tick
            # count remains bounded by viewport width, independent of duration.
            p.save()
            p.setClipRect(QRectF(self.LEFT, 25, max(1,self.width()-self.LEFT-self.RIGHT),self.height()-25))
            minor_step = max(1,step//5)
            minor_first = math.ceil((self.view_start+1)/minor_step)*minor_step-1
            p.setPen(QPen(QColor(133,133,133,40),1))
            for frame in range(minor_first,math.ceil(self.view_start+self.span())+1,minor_step):
                x = self.frame_x(frame)
                p.drawLine(QPointF(x,25),QPointF(x,self.height()))
            p.setPen(QPen(QColor(115,115,115,145),1))
            for frame in range(first,math.ceil(self.view_start+self.span())+1,step):
                x = self.frame_x(frame)
                p.drawLine(QPointF(x,25),QPointF(x,self.height()))
            p.restore()
            p.setPen(QColor("#999999"))
            p.drawText(9,17,"Frame")
            for frame in range(first,math.ceil(self.view_start+self.span())+1,step):
                x = self.frame_x(frame)
                p.setPen(QColor("#b3b3b3"))
                p.drawText(QRectF(x-35,2,70,20),Qt.AlignCenter,str(frame+1))
            for row,label,color in rows:
                y = 28+row*row_height
                p.setPen(QColor("#aaaaaa"))
                p.drawText(QRectF(8, y, 53, row_height), Qt.AlignVCenter, label)
                p.setPen(QColor("#b1b9c1" if row == 0 else "#aebeb4"))
                text = "Source clip" if row == 0 else ((self.person_name or "Body animation") if self.has_motion else "No motion data")
                left, right = self.clip_edges() if row == 0 else (self.LEFT, self.width() - self.RIGHT)
                left, right = max(self.LEFT, left), min(self.width() - self.RIGHT, right)
                p.drawText(QRectF(left + 12, y, max(0, right - left - 24), row_height), Qt.AlignVCenter, text)
            # Edge grips belong to a clip, never to the playhead/ruler.
            p.setPen(QPen(QColor("#a6bed5"), 1))
            left, right = self.clip_edges()
            for edge, direction in ((left, 1), (right, -1)):
                if self.LEFT <= edge <= self.width() - self.RIGHT:
                    p.fillRect(QRectF(edge if direction == 1 else edge - 6, 30, 6, 21), QColor(139,177,211,150))
                    p.drawLine(QPointF(edge,30),QPointF(edge,51))
            self._paint_audio_grips(p)
            p.end()
            self._cache, self._cache_key = cache, key
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        self.paint_audio_meter(p)
        self.paint_contact_selection(p)
        x = self.frame_x(self.value())
        p.setPen(QPen(QColor("#74a6e0"), 1.4))
        p.drawLine(QPointF(x, 23), QPointF(x, self.height()))
        p.setRenderHint(QPainter.Antialiasing)
        text = str(self.value() + 1)
        head = self.playhead_rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#477bb7"))
        p.drawRoundedRect(head, 3, 3)
        p.drawPolygon(QPolygonF([QPointF(x - 4, 21), QPointF(x + 4, 21), QPointF(x, 26)]))
        p.setPen(QColor("#ffffff"))
        p.drawText(head, Qt.AlignCenter, text)

    def paint_face_tracks(self,p):
        fps=self.fps or 30.0
        extras=((2,'Face','#c4a36a',self.face_span,self.face_empty),
                (3,'Audio','#8a7bb8',self.audio_span,self.audio_empty))
        for row,label,color,span,empty in extras:
            y=28+row*self.TRACK_HEIGHT
            p.setPen(QColor('#aaaaaa'))
            p.drawText(QRectF(8,y,53,self.TRACK_HEIGHT),Qt.AlignVCenter,label)
            if not span:
                p.setPen(QColor('#777777'))
                pad=62 if label=='Audio' and self.audio_meter_on else 8
                p.drawText(QRectF(self.LEFT+pad,y,max(0,self.width()-self.LEFT-pad-17),self.TRACK_HEIGHT),Qt.AlignVCenter,empty)
                continue
            if label=='Audio':
                self._paint_audio_clips(p,y,color,fps)
                continue
            start,duration=span
            left=max(self.LEFT,self.frame_x(start*fps))
            right=min(self.width()-self.RIGHT,self.frame_x((start+duration)*fps))
            kept=QColor(color);kept.setAlpha(90)
            p.fillRect(QRectF(left,y+2,max(0,right-left),self.TRACK_HEIGHT-5),kept)
            wave=self.face_wave
            if wave and right-left>2:
                self._paint_wave(p,wave,left,right,y+2,self.TRACK_HEIGHT-5,color,170)
            p.setPen(QColor('#d5c7a6'))
            p.drawText(QRectF(left+12,y,max(0,right-left-24),self.TRACK_HEIGHT),Qt.AlignVCenter,'%.1f s'%duration)

    def _paint_audio_clips(self, p, y, color, fps):
        origin, duration = self.audio_span
        height = self.TRACK_HEIGHT - 5
        top = y + 2
        view_left, view_right = self.LEFT, self.width() - self.RIGHT
        for place, src_in, src_out in self.audio_segments:
            length = src_out - src_in
            left = max(view_left, self.frame_x(place))
            right = min(view_right, self.frame_x(place + length - 1))
            if right - left <= 0:
                continue
            tint = QColor(color)
            tint.setAlpha(90)
            p.fillRect(QRectF(left, top, right - left, height), tint)
            if self.audio_wave and duration > 0 and right - left > 2:
                src_start = src_in / fps - origin
                src_end = src_out / fps - origin
                self._paint_source_wave(p, self.audio_wave, duration, src_start, src_end, left, right, top, height, color, 180)
        if self.audio_selection:
            start, end = self.audio_selection
            left = max(view_left, self.frame_x(start))
            right = min(view_right, self.frame_x(max(start, end - 1)))
            if right - left > 0:
                shade = QColor('#f2d36b')
                shade.setAlpha(70)
                p.fillRect(QRectF(left, top, right - left, height), shade)
                p.setPen(QPen(QColor('#f2d36b'), 1))
                p.drawRect(QRectF(left, top, max(1, right - left), height))

    def _paint_audio_grips(self, p):
        if not self.audio_segments:
            return
        top, _bottom = self._row_band(3)
        p.setPen(QPen(QColor('#c8bddc'), 1))
        seen = set()
        for place, src_in, src_out in self.audio_segments:
            for frame, direction in ((place, 1), (place + (src_out - src_in) - 1, -1)):
                edge = self.frame_x(frame)
                key = (round(edge), direction)
                if key in seen or not self.LEFT <= edge <= self.width() - self.RIGHT:
                    continue
                seen.add(key)
                p.fillRect(QRectF(edge if direction == 1 else edge - 6, top + 2, 6, self.TRACK_HEIGHT - 5), QColor(176, 164, 210, 170))
                p.drawLine(QPointF(edge, top + 2), QPointF(edge, top + self.TRACK_HEIGHT - 3))
        if not self.audio_selection:
            return
        start, end = self.audio_selection
        p.setPen(QPen(QColor('#f2d36b'), 1))
        for frame, direction in ((start, 1), (max(start, end - 1), -1)):
            edge = self.frame_x(frame)
            if self.LEFT <= edge <= self.width() - self.RIGHT:
                p.fillRect(QRectF(edge if direction == 1 else edge - 4, top + 2, 4, self.TRACK_HEIGHT - 5), QColor(242, 211, 107, 210))

    def _paint_source_wave(self, p, wave, duration, src_in, src_out, left, right, top, height, color, alpha):
        count = len(wave)
        if count < 2 or duration <= 0 or right - left <= 1:
            return
        i0 = max(0, min(count - 1, int(max(0.0, src_in) / duration * (count - 1))))
        i1 = max(i0 + 1, min(count, int(max(0.0, src_out) / duration * (count - 1)) + 1))
        base = top + height
        path = QPainterPath()
        path.moveTo(left, base)
        span = max(1, i1 - i0 - 1)
        for index in range(i0, i1):
            level = max(0.0, min(1.0, float(wave[index])))
            x = left + (right - left) * ((index - i0) / span)
            path.lineTo(x, base - level * height)
        path.lineTo(right, base)
        fill = QColor(color)
        fill.setAlpha(alpha)
        p.fillPath(path, fill)

    def _paint_wave(self,p,wave,left,right,top,height,color,alpha,clip_left=None,clip_right=None):
        if clip_left is not None and clip_right-clip_left<=0.5:
            return
        if clip_left is not None:
            p.save()
            p.setClipRect(QRectF(clip_left,top,clip_right-clip_left,height))
        base=top+height
        path=QPainterPath()
        path.moveTo(left,base)
        last=max(1,len(wave)-1)
        for i,level in enumerate(wave):
            x=left+(right-left)*(i/last)
            path.lineTo(x,base-max(0.0,min(1.0,float(level)))*height)
        path.lineTo(right,base)
        fill=QColor(color);fill.setAlpha(alpha)
        p.fillPath(path,fill)
        if clip_left is not None:
            p.restore()

    def paint_audio_meter(self,p):
        if not self.audio_meter_on:
            return
        y=28+3*self.TRACK_HEIGHT
        rect=QRectF(self.LEFT+8,y+(self.TRACK_HEIGHT-8)/2,46,8)
        p.setPen(QPen(QColor('#4a445c'),1))
        p.setBrush(QColor('#1e1e1e'))
        p.drawRoundedRect(rect,2,2)
        level=max(0.0,min(1.0,float(self.audio_level)))
        if level<=0.004:
            return
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#d07070') if level>0.92 else QColor('#8a7bb8'))
        p.drawRoundedRect(QRectF(rect.x()+1,rect.y()+1,(rect.width()-2)*level,rect.height()-2),1,1)
