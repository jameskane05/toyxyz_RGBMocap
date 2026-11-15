"""Graph editor for the face channels that moved in a take."""
import math
from copy import deepcopy
from PySide6.QtCore import Qt, QRectF, QPointF, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QPainterPath, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem, QLabel,
    QSizePolicy, QMenu, QApplication, QAbstractItemView)
from .face_curves import (
    FaceCurves, active_names, eval_channel, handles_for, linear_handles,
    reduce_channel, sort_keys)

CURVE_COLORS = (
    '#e0b15a', '#7eb6e0', '#e08a8a', '#8dcaa0',
    '#c58ad4', '#e09a6a', '#7ec8c3', '#d4d27a',
    '#9bb0e0', '#e0a0c0',
)
LEFT, RIGHT, TOP, BOTTOM = 40, 12, 18, 8
HISTORY = 50


def _index(keys, key):
    for index, item in enumerate(keys):
        if item is key:
            return index
    return -1


class CurveCanvas(QWidget):
    def __init__(self, graph):
        super().__init__(graph)
        self.graph = graph
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._mode = None
        self._press = None
        self._moved = False
        self._pushed = False
        self._additive = False
        self._rubber = None
        self._hit = None
        self._originals = []
        self._pan = None
        self._double = False

    def frame_x(self, frame):
        width = max(1, self.width() - LEFT - RIGHT)
        slider = self.graph.slider
        return LEFT + (float(frame) - slider.view_start) / slider.span() * width

    def x_frame(self, x):
        width = max(1, self.width() - LEFT - RIGHT)
        slider = self.graph.slider
        return slider.view_start + (float(x) - LEFT) / width * slider.span()

    def value_y(self, value):
        height = max(1, self.height() - TOP - BOTTOM)
        span = self.graph.value_max - self.graph.value_min
        return TOP + (self.graph.value_max - float(value)) / span * height

    def y_value(self, y):
        height = max(1, self.height() - TOP - BOTTOM)
        span = self.graph.value_max - self.graph.value_min
        return self.graph.value_max - (float(y) - TOP) / height * span

    def wheelEvent(self, event):
        slider = self.graph.slider
        dy = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier:
            self._zoom_values(event.position().y(), dy)
        elif event.modifiers() & Qt.ShiftModifier:
            slider.view_start -= dy / 120 * slider.span() * 0.12
            slider._clamp_view()
        else:
            old = slider.span()
            width = max(1, self.width() - LEFT - RIGHT)
            fraction = max(0.0, min(1.0, (event.position().x() - LEFT) / width))
            anchor = slider.view_start + fraction * old
            total = max(1, slider.maximum() - slider.minimum())
            slider.view_span = max(min(12, total), min(total, old * math.exp(-dy / 120 * 0.22)))
            slider.view_start = anchor - fraction * slider.view_span
            slider._clamp_view()
        slider.update()
        self.update()
        event.accept()

    def _zoom_values(self, y, dy):
        height = max(1, self.height() - TOP - BOTTOM)
        fraction = max(0.0, min(1.0, (float(y) - TOP) / height))
        span = self.graph.value_max - self.graph.value_min
        anchor = self.graph.value_max - fraction * span
        span = max(0.08, span * math.exp(-dy / 120 * 0.22))
        self.graph.value_max = anchor + fraction * span
        self.graph.value_min = self.graph.value_max - span

    def mousePressEvent(self, event):
        self.setFocus()
        pos = event.position()
        if event.button() == Qt.MiddleButton:
            self._pan = (pos.x(), pos.y(), self.graph.slider.view_start,
                         self.graph.value_min, self.graph.value_max)
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            return
        hit = self._hit_at(pos.x(), pos.y())
        self._press = (pos.x(), pos.y())
        self._moved = False
        self._pushed = False
        self._additive = bool(event.modifiers() & Qt.ShiftModifier)
        self._hit = hit
        self._originals = []
        if hit and hit[0] == 'key':
            _kind, _name, key = hit
            chosen = {id(item) for item in self.graph.selected}
            if self._additive:
                if id(key) in chosen:
                    self.graph.selected = [item for item in self.graph.selected if item is not key]
                    self._mode = None
                else:
                    self.graph.selected.append(key)
                    self._mode = 'keys'
            else:
                if id(key) not in chosen:
                    self.graph.selected = [key]
                self._mode = 'keys'
            self._originals = [(item, float(item['t']), float(item['v'])) for item in self.graph.selected]
        elif hit and hit[0] in ('left', 'right'):
            self.graph.selected = [hit[2]]
            self._mode = 'handle'
        elif abs(pos.x() - self.frame_x(self.graph.slider.value())) <= 6 or pos.y() < TOP:
            self._mode = 'playhead'
            self.graph.seek_frame(self.x_frame(pos.x()))
        else:
            if not self._additive:
                self.graph.selected = []
            self._mode = 'box'
            self._rubber = QRectF(pos.x(), pos.y(), 0, 0)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._pan is not None and event.buttons() & Qt.MiddleButton:
            x0, y0, start, vmin, vmax = self._pan
            slider = self.graph.slider
            width = max(1, self.width() - LEFT - RIGHT)
            height = max(1, self.height() - TOP - BOTTOM)
            slider.view_start = start - (pos.x() - x0) * slider.span() / width
            slider._clamp_view()
            span = vmax - vmin
            shift = (pos.y() - y0) * span / height
            self.graph.value_min = vmin + shift
            self.graph.value_max = vmax + shift
            slider.update()
            self.update()
            return
        if self._press is None or not (event.buttons() & Qt.LeftButton):
            self._hover_cursor(pos)
            return
        if self._mode == 'playhead':
            self.graph.seek_frame(self.x_frame(pos.x()))
            return
        dx = pos.x() - self._press[0]
        dy = pos.y() - self._press[1]
        if not self._moved and dx * dx + dy * dy < 9:
            return
        self._moved = True
        if self._mode == 'keys':
            if not self._pushed:
                self._pushed = self.graph.begin_edit()
            if not self._pushed:
                return
            frame_delta = self.x_frame(pos.x()) - self.x_frame(self._press[0])
            value_delta = self.y_value(pos.y()) - self.y_value(self._press[1])
            if event.modifiers() & Qt.ShiftModifier:
                if abs(dx) >= abs(dy):
                    value_delta = 0.0
                else:
                    frame_delta = 0.0
            t_delta = self.graph.record_time(frame_delta) - self.graph.record_time(0)
            self.graph.move_keys(self._originals, t_delta, value_delta)
        elif self._mode == 'handle' and self._hit is not None:
            if not self._pushed:
                self._pushed = self.graph.begin_edit()
                if self._pushed:
                    self.graph.begin_handle(self._hit[1], self._hit[2])
            if not self._pushed:
                return
            self.graph.move_handle(
                self._hit[2], self._hit[0],
                self.graph.record_time(self.x_frame(pos.x())), self.y_value(pos.y()))
        elif self._mode == 'box':
            self._rubber = QRectF(self._press[0], self._press[1], dx, dy).normalized()
            self.update()
            return
        self.graph.preview()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._pan = None
            return
        if event.button() != Qt.LeftButton:
            return
        if self._double:
            self._double = False
            self._reset_press()
            return
        if self._mode == 'box' and self._rubber is not None:
            found = self._keys_in(self._rubber)
            if self._additive:
                chosen = {id(item) for item in self.graph.selected}
                for key in found:
                    if id(key) not in chosen:
                        self.graph.selected.append(key)
            else:
                self.graph.selected = found
        elif self._mode == 'keys' and not self._moved and self._hit and not self._additive:
            self.graph.selected = [self._hit[2]]
        if self._pushed:
            self.graph.finish_edit()
        else:
            self.update()
        self._reset_press()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._double = True
        self._mode = None
        pos = event.position()
        frame = self.x_frame(pos.x())
        self.graph.insert_at(frame, self.y_value(pos.y()))
        event.accept()

    def contextMenuEvent(self, event):
        self._select_right(event.pos())
        if self.graph.take is None:
            return
        menu = QMenu(self)
        for label, kind in (('Auto Clamped', 'auto'), ('Linear', 'linear'),
                            ('Constant', 'constant'), ('Free', 'free')):
            menu.addAction(label, lambda kind=kind: self.graph.set_interpolation(kind))
        menu.addSeparator()
        menu.addAction('Delete', self.graph.delete_selected)
        menu.addAction('Reset Channel', self.graph.reset_selected)
        menu.exec(event.globalPos())

    def _select_right(self, pos):
        hit = self._hit_at(pos.x(), pos.y())
        if hit and hit[0] == 'key' and all(item is not hit[2] for item in self.graph.selected):
            self.graph.selected = [hit[2]]
            self.update()

    def _reset_press(self):
        self._mode = None
        self._press = None
        self._rubber = None
        self._hit = None
        self._originals = []
        self._moved = False
        self._pushed = False

    def _hover_cursor(self, pos):
        hit = self._hit_at(pos.x(), pos.y())
        if hit and hit[0] == 'key':
            self.setCursor(Qt.OpenHandCursor)
        elif hit:
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def _hit_at(self, x, y):
        best = None
        best_dist = 8 * 8
        for name in reversed(self.graph.visible_names()):
            keys = self.graph.take.curves.keys(name) if self.graph.take and self.graph.take.curves else []
            chosen = {id(item) for item in self.graph.selected}
            for key in keys:
                if id(key) not in chosen:
                    continue
                for side in ('left', 'right'):
                    point = self._handle_point(name, keys, key, side)
                    if point is None:
                        continue
                    dist = (point[0] - x) ** 2 + (point[1] - y) ** 2
                    if dist <= best_dist:
                        best = (side, name, key)
                        best_dist = dist
        if best is not None:
            return best
        for name in reversed(self.graph.visible_names()):
            keys = self.graph.take.curves.keys(name) if self.graph.take and self.graph.take.curves else []
            for key in keys:
                px, py = self.frame_x(self.graph.frame_of(key['t'])), self.value_y(key['v'])
                dist = (px - x) ** 2 + (py - y) ** 2
                if dist <= best_dist:
                    best = ('key', name, key)
                    best_dist = dist
        return best

    def _handle_point(self, name, keys, key, side):
        index = _index(keys, key)
        if index < 0 or (key.get('interp') or 'auto') in ('linear', 'constant'):
            return None
        if side == 'left' and index == 0:
            return None
        if side == 'right' and index == len(keys) - 1:
            return None
        left, right = handles_for(keys, index)
        dt, dv = left if side == 'left' else right
        return (self.frame_x(self.graph.frame_of(float(key['t']) + dt)),
                self.value_y(float(key['v']) + dv))

    def _keys_in(self, rect):
        found = []
        if rect.width() < 2 and rect.height() < 2:
            return found
        for name in self.graph.visible_names():
            for key in self.graph.take.curves.keys(name):
                point = QPointF(self.frame_x(self.graph.frame_of(key['t'])), self.value_y(key['v']))
                if rect.contains(point):
                    found.append(key)
        return found

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#242424'))
        self._paint_grid(painter)
        graph = self.graph
        if graph.take is None:
            self._message(painter, 'No face capture')
            painter.end()
            return
        if not graph.visible_names():
            self._message(painter, 'No moving face channels' if not graph.channels.count()
                          else 'No curves selected')
        plot = QRectF(LEFT, TOP, max(1, self.width() - LEFT - RIGHT), max(1, self.height() - TOP - BOTTOM))
        painter.setClipRect(plot.adjusted(-6, -6, 6, 6))
        painter.setRenderHint(QPainter.Antialiasing, True)
        for name in graph.visible_names():
            self._paint_curve(painter, name, graph.color(name))
        self._paint_playhead(painter)
        if self._rubber is not None and self._mode == 'box':
            painter.setClipping(False)
            shade = QColor('#74a6e0')
            shade.setAlpha(40)
            painter.setPen(QPen(QColor('#74a6e0'), 1))
            painter.setBrush(shade)
            painter.drawRect(self._rubber)
        painter.end()

    def _message(self, painter, text):
        painter.setPen(QColor('#777777'))
        painter.drawText(self.rect(), Qt.AlignCenter, text)

    def _paint_grid(self, painter):
        painter.setPen(QColor('#8a8a8a'))
        for value in (0.0, 0.5, 1.0):
            if not (self.graph.value_min < value < self.graph.value_max):
                continue
            y = self.value_y(value)
            color = '#555555' if value == 0.0 else '#3a3a3a'
            painter.setPen(QPen(QColor(color), 1))
            painter.drawLine(QPointF(LEFT, y), QPointF(self.width() - RIGHT, y))
            painter.setPen(QColor('#8a8a8a'))
            painter.drawText(QRectF(0, y - 8, LEFT - 4, 16), Qt.AlignRight | Qt.AlignVCenter, f'{value:g}')
        slider = self.graph.slider
        step = _tick(slider.span())
        first = math.ceil(slider.view_start / step) * step
        last = slider.view_start + slider.span()
        painter.setPen(QColor('#8a8a8a'))
        frame = first
        while frame <= last + 1e-6:
            x = self.frame_x(frame)
            painter.setPen(QPen(QColor('#3a3a3a'), 1))
            painter.drawLine(QPointF(x, TOP), QPointF(x, self.height() - BOTTOM))
            painter.setPen(QColor('#9a9a9a'))
            painter.drawText(QRectF(x - 24, 0, 48, TOP - 1), Qt.AlignHCenter | Qt.AlignBottom, str(int(round(frame)) + 1))
            frame += step

    def _paint_curve(self, painter, name, color):
        width = max(1, self.width() - LEFT - RIGHT)
        step = max(1, int(width / 360))
        slider = self.graph.slider
        path = QPainterPath()
        started = False
        for pixel in range(0, width + 1, step):
            frame = slider.view_start + pixel / width * slider.span()
            value = self.graph.value_at(name, self.graph.record_time(frame))
            x, y = LEFT + pixel, self.value_y(value)
            if not started:
                path.moveTo(x, y)
                started = True
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(QColor(color), 1.6))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        keys = self.graph.take.curves.keys(name)
        chosen = {id(item) for item in self.graph.selected}
        for key in keys:
            if id(key) not in chosen:
                continue
            self._paint_handles(painter, keys, key, color)
        for key in keys:
            x, y = self.frame_x(self.graph.frame_of(key['t'])), self.value_y(key['v'])
            if x < LEFT - 8 or x > self.width() - RIGHT + 8:
                continue
            selected = id(key) in chosen
            painter.setPen(QPen(QColor('#ffffff' if selected else color), 1.2))
            painter.setBrush(QColor('#ffffff' if selected else color))
            painter.drawPolygon(_diamond(x, y, 4.5 if selected else 3.8))

    def _paint_handles(self, painter, keys, key, color):
        index = _index(keys, key)
        if index < 0 or (key.get('interp') or 'auto') in ('linear', 'constant'):
            return
        left, right = handles_for(keys, index)
        origin = QPointF(self.frame_x(self.graph.frame_of(key['t'])), self.value_y(key['v']))
        painter.setBrush(QColor('#d8d8d8'))
        painter.setPen(QPen(QColor(color), 1))
        for side, (dt, dv) in (('left', left), ('right', right)):
            if side == 'left' and index == 0:
                continue
            if side == 'right' and index == len(keys) - 1:
                continue
            point = QPointF(self.frame_x(self.graph.frame_of(float(key['t']) + dt)),
                            self.value_y(float(key['v']) + dv))
            painter.drawLine(origin, point)
            painter.drawRect(QRectF(point.x() - 3, point.y() - 3, 6, 6))

    def _paint_playhead(self, painter):
        x = self.frame_x(self.graph.slider.value())
        painter.setPen(QPen(QColor('#74a6e0'), 1.4))
        painter.drawLine(QPointF(x, TOP), QPointF(x, self.height() - BOTTOM))


def _tick(span):
    raw = max(float(span) / 8.0, 1.0)
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if raw <= factor * power:
            return max(1, int(factor * power))
    return max(1, int(10 * power))


def _diamond(x, y, radius):
    return QPolygonF([
        QPointF(x, y - radius), QPointF(x + radius, y),
        QPointF(x, y + radius), QPointF(x - radius, y),
    ])


class FaceGraph(QWidget):
    changed = Signal()
    edit_started = Signal()
    edit_finished = Signal()

    def __init__(self, slider):
        super().__init__()
        self.slider = slider
        self.take = None
        self.only_selected = True
        self.value_min = -0.05
        self.value_max = 1.05
        self.selected = []
        self._undo = []
        self._redo = []
        self._color = {}
        self._history_claimed = False
        self.record_time = lambda frame: float(frame) / float(self.slider.fps or 30.0)
        self.frame_of = lambda t: float(t) * float(self.slider.fps or 30.0)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(220)
        self.channels = QListWidget()
        self.channels.setObjectName('curveChannels')
        self.channels.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.channels.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.channels.setIconSize(QSize(8, 12))
        self.channels.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.channels.setToolTip('Channels that moved in this take. Check to show a curve. Select to edit it.')
        self.canvas = CurveCanvas(self)
        self.canvas.setToolTip('I inserts a key at the playhead. Drag a key to reshape. Shift locks one axis. Delete removes keys. Right-click sets interpolation.')
        side = QWidget()
        side.setFixedWidth(196)
        column = QVBoxLayout(side)
        column.setContentsMargins(6, 4, 4, 4)
        column.setSpacing(2)
        title = QLabel('Channels')
        title.setObjectName('muted')
        column.addWidget(title)
        column.addWidget(self.channels, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(side)
        layout.addWidget(self.canvas, 1)
        self.channels.itemChanged.connect(lambda _item: self.canvas.update())
        self.channels.itemSelectionChanged.connect(self.canvas.update)
        self.slider.valueChanged.connect(self.canvas.update)
        self.slider.rangeChanged.connect(lambda *_args: self.canvas.update())
        for key, slot in (
            ('I', self.insert_at_playhead),
            ('Delete', self.delete_selected),
            ('Backspace', self.delete_selected),
            ('Ctrl+Z', lambda: self.claim_history(False)),
            ('Ctrl+Y', lambda: self.claim_history(True)),
            ('Ctrl+Shift+Z', lambda: self.claim_history(True)),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

    def set_take(self, take):
        self.take = take
        if take is not None and take.curves is None:
            take.curves = FaceCurves()
        self.selected = []
        self._undo = []
        self._redo = []
        self._fill_channels(False)

    def set_only_selected(self, value):
        self.only_selected = bool(value)
        self.canvas.update()

    def refresh(self):
        self.canvas.update()

    def color(self, name):
        return self._color.get(name, CURVE_COLORS[0])

    def visible_names(self):
        checked = []
        selected = set()
        for index in range(self.channels.count()):
            item = self.channels.item(index)
            name = item.data(Qt.UserRole)
            if item.checkState() == Qt.Checked:
                checked.append(name)
            if item.isSelected():
                selected.add(name)
        if self.only_selected and selected:
            return [name for name in checked if name in selected]
        return checked

    def _selected_names(self):
        return [item.data(Qt.UserRole) for item in self.channels.selectedItems()]

    def value_at(self, name, t):
        take = self.take
        if take is None or take.curves is None:
            return 0.0
        keys = take.curves.keys(name)
        if keys and float(keys[0]['t']) - 1e-4 <= float(t) <= float(keys[-1]['t']) + 1e-4:
            value = eval_channel(keys, t)
            if value is not None:
                return value
        return self._raw(name, t)

    def _raw(self, name, t):
        take = self.take
        if take is None:
            return 0.0
        try:
            index = list(take.names).index(name)
        except ValueError:
            return 0.0
        raw = take.raw_at(float(t) + take.offset)
        if index >= len(raw):
            return 0.0
        return float(raw[index])

    def _column(self, name):
        index = list(self.take.names).index(name)
        return self.take.times, self.take.values[:, index]

    def _bounds(self):
        take = self.take
        if take is None or not len(take.times):
            return 0.0, 0.0
        return float(take.times[0]), float(take.times[-1])

    def seek_frame(self, frame):
        slider = self.slider
        slider.setValue(int(max(slider.minimum(), min(slider.maximum(), round(frame)))))

    def insert_at_playhead(self):
        if self.take is None:
            return
        names = self._selected_names()
        if not names:
            return
        self.insert_keys(names, self.record_time(self.slider.value()))

    def insert_at(self, frame, value):
        if self.take is None:
            return
        t = self.record_time(frame)
        name = self._nearest(t, value)
        names = [name] if name else self._selected_names()
        if not names:
            return
        self.seek_frame(frame)
        self.insert_keys(names, t)

    def _nearest(self, t, value):
        best = None
        best_dist = None
        for name in self.visible_names():
            dist = abs(self.value_at(name, t) - float(value))
            if best_dist is None or dist < best_dist:
                best = name
                best_dist = dist
        return best

    def insert_keys(self, names, t):
        if self.take is None or self.take.curves is None or not names:
            return
        if not self.begin_edit():
            return
        placed = []
        for name in names:
            key = self._insert_one(name, t)
            if key is not None:
                placed.append(key)
        self.selected = placed
        self.finish_edit()

    def _insert_one(self, name, t):
        curves = self.take.curves
        keys = list(curves.keys(name))
        if not keys:
            try:
                times, column = self._column(name)
            except ValueError:
                return None
            keys = reduce_channel(times, column)
        t = float(t)
        for key in keys:
            if abs(float(key['t']) - t) <= 1e-3:
                curves.channels[name] = keys
                return key
        if keys and float(keys[0]['t']) <= t <= float(keys[-1]['t']):
            value = eval_channel(keys, t)
        else:
            value = self._raw(name, t)
        key = {'t': t, 'v': float(value if value is not None else 0.0), 'interp': 'auto'}
        keys.append(key)
        sort_keys(keys, nudge=True)
        curves.channels[name] = keys
        return key

    def delete_selected(self):
        if not self.selected or self.take is None or self.take.curves is None:
            return
        if not self.begin_edit():
            return
        chosen = {id(key) for key in self.selected}
        curves = self.take.curves
        for name in list(curves.channels):
            keys = [key for key in curves.channels[name] if id(key) not in chosen]
            if keys:
                curves.channels[name] = keys
            else:
                del curves.channels[name]
        self.selected = []
        self.finish_edit()

    def reset_selected(self):
        self._reset(self._selected_names())

    def reset_all(self):
        if self.take is None or self.take.curves is None:
            return
        self._reset(list(self.take.curves.channels))

    def _reset(self, names):
        curves = self.take.curves if self.take is not None else None
        if curves is None or not any(curves.keys(name) for name in names):
            return
        if not self.begin_edit():
            return
        for name in names:
            curves.clear(name)
        self.selected = []
        self.finish_edit()

    def set_interpolation(self, kind):
        if not self.selected or self.take is None or self.take.curves is None:
            return
        if not self.begin_edit():
            return
        chosen = {id(key) for key in self.selected}
        for keys in self.take.curves.channels.values():
            for index, key in enumerate(keys):
                if id(key) not in chosen:
                    continue
                current = key.get('interp') or 'auto'
                if kind == 'free':
                    left, right = linear_handles(keys, index) if current == 'linear' else handles_for(keys, index)
                    key['left'] = [left[0], left[1]]
                    key['right'] = [right[0], right[1]]
                else:
                    key.pop('left', None)
                    key.pop('right', None)
                key['interp'] = kind
        self.finish_edit()

    def begin_edit(self):
        if self.take is None or self.take.curves is None:
            return False
        self._undo.append(deepcopy(self.take.curves.channels))
        if len(self._undo) > HISTORY:
            del self._undo[:-HISTORY]
        self._redo.clear()
        self.edit_started.emit()
        return True

    def begin_handle(self, name, key):
        keys = self.take.curves.channels.get(name) or []
        index = _index(keys, key)
        if index < 0 or key.get('interp') == 'free':
            return
        current = key.get('interp') or 'auto'
        left, right = linear_handles(keys, index) if current == 'linear' else handles_for(keys, index)
        key['interp'] = 'free'
        key['left'] = [left[0], left[1]]
        key['right'] = [right[0], right[1]]

    def move_keys(self, originals, t_delta, value_delta):
        lo, hi = self._bounds()
        touched = set()
        for key, t0, v0 in originals:
            key['t'] = min(hi, max(lo, t0 + float(t_delta)))
            key['v'] = min(1.5, max(-0.25, v0 + float(value_delta)))
            touched.add(id(key))
        for keys in self.take.curves.channels.values():
            if any(id(key) in touched for key in keys):
                sort_keys(keys, nudge=False)

    def move_handle(self, key, side, t, v):
        _name, keys = self._channel_of(key)
        if keys is None:
            return
        dt = float(t) - float(key['t'])
        dv = min(2.0, max(-2.0, float(v) - float(key['v'])))
        key[side] = [dt, dv]
        self._clamp_handle(keys, key, side)

    def finish_edit(self):
        if self.take is not None and self.take.curves is not None:
            for keys in self.take.curves.channels.values():
                sort_keys(keys, nudge=True)
                for key in keys:
                    if key.get('interp') == 'free':
                        self._clamp_handle(keys, key, 'left')
                        self._clamp_handle(keys, key, 'right')
        self.changed.emit()
        self.edit_finished.emit()
        self._fill_channels(True)
        self.setFocus()

    def preview(self):
        self.changed.emit()
        self.canvas.update()

    def _channel_of(self, key):
        for name, keys in self.take.curves.channels.items():
            if any(item is key for item in keys):
                return name, keys
        return None, None

    def _clamp_handle(self, keys, key, side):
        pair = key.get(side)
        if not pair:
            return
        dt, dv = float(pair[0]), float(pair[1])
        index = _index(keys, key)
        if index < 0:
            return
        if side == 'right':
            dt = 0.0 if index + 1 >= len(keys) else min(max(0.0, dt), max(0.0, float(keys[index + 1]['t']) - float(key['t'])))
        else:
            dt = 0.0 if index == 0 else max(min(0.0, dt), -max(0.0, float(key['t']) - float(keys[index - 1]['t'])))
        key[side] = [dt, dv]

    def undo(self):
        return self._step(self._undo, self._redo)

    def redo(self):
        return self._step(self._redo, self._undo)

    def _step(self, source, other):
        if not source or self.take is None or self.take.curves is None:
            return False
        other.append(deepcopy(self.take.curves.channels))
        if len(other) > HISTORY:
            del other[:-HISTORY]
        self.take.curves.channels = source.pop()
        self.selected = []
        self.changed.emit()
        self.edit_finished.emit()
        self._fill_channels(True)
        self.setFocus()
        return True

    def claim_history(self, redo):
        focus = QApplication.focusWidget()
        focused = self.isVisible() and focus is not None and (focus is self or self.isAncestorOf(focus))
        if self._history_claimed:
            return True
        if not focused:
            return False
        self._history_claimed = True
        QTimer.singleShot(0, self._release_history)
        if redo:
            self.redo()
        else:
            self.undo()
        return True

    def _release_history(self):
        self._history_claimed = False

    def frame_all(self):
        self.value_min = -0.05
        self.value_max = 1.05
        self.slider.frame_all()
        self.canvas.update()

    def frame_selected(self):
        keys = list(self.selected)
        if not keys:
            self.frame_all()
            return
        frames = [self.frame_of(key['t']) for key in keys]
        values = [float(key['v']) for key in keys]
        lo, hi = min(frames), max(frames)
        pad = max(8.0, (hi - lo) * 0.15)
        slider = self.slider
        slider.view_start = lo - pad
        slider.view_span = max(12.0, hi - lo + pad * 2)
        slider._clamp_view()
        vlo, vhi = min(values), max(values)
        vpad = max(0.08, (vhi - vlo) * 0.2)
        self.value_min = vlo - vpad
        self.value_max = vhi + vpad
        slider.update()
        self.canvas.update()

    def _fill_channels(self, keep):
        checks = {}
        chosen = set()
        if keep:
            for index in range(self.channels.count()):
                item = self.channels.item(index)
                name = item.data(Qt.UserRole)
                checks[name] = item.checkState()
                if item.isSelected():
                    chosen.add(name)
            if self.selected:
                for name, keys in (self.take.curves.channels.items() if self.take and self.take.curves else []):
                    if any(id(key) in {id(item) for item in self.selected} for key in keys):
                        chosen.add(name)
        self.channels.blockSignals(True)
        self.channels.clear()
        self._color = {}
        take = self.take
        names = [] if take is None else active_names(take.names, take.values, take.curves)
        for index, name in enumerate(names):
            color = CURVE_COLORS[index % len(CURVE_COLORS)]
            self._color[name] = color
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(checks.get(name, Qt.Checked))
            item.setForeground(QColor(color))
            swatch = QPixmap(8, 12)
            swatch.fill(QColor(color))
            item.setIcon(QIcon(swatch))
            self.channels.addItem(item)
            if name in chosen:
                item.setSelected(True)
        if not chosen and self.channels.count():
            self.channels.item(0).setSelected(True)
        self.channels.blockSignals(False)
        self.canvas.update()
