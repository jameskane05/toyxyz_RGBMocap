"""Caption lines timed from speech-to-text words. Times are seconds on the take audio's clock."""
import json
import re
from difflib import SequenceMatcher

from .face_capture import folder
from .storage import atomic_json

FILE = 'captions.json'
_STRIP = re.compile(r"[^\w']+", re.UNICODE)


def normalize(token):
    return _STRIP.sub('', str(token or '').lower()).replace('_', '')


def timed_words(words):
    return [w for w in words or []
            if str(w.get('type') or 'word').strip() in ('word', '')
            and isinstance(w.get('start'), (int, float)) and isinstance(w.get('end'), (int, float))]


def align(words, text):
    """Each non-empty line becomes one caption spanning its words, in transcript order.
    Corrected words take the timing of the transcript words they replace. Returns
    (captions, number of line words that differ from the transcript)."""
    entries = timed_words(words)
    if not entries:
        raise ValueError('No timed words to align. Transcribe again.')
    keys = [normalize(w.get('text')) for w in entries]
    lines, tokens, owner = [], [], []
    for raw in str(text or '').splitlines():
        line = raw.strip()
        found = [token for token in (normalize(part) for part in line.split()) if token]
        if found:
            owner += [len(lines)] * len(found)
            tokens += found
            lines.append(line)
    if not lines:
        raise ValueError('Add at least one line of caption text.')
    mapped = [None] * len(tokens)
    same = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, tokens, keys, autojunk=False).get_opcodes():
        if tag == 'equal':
            same += i2 - i1
            mapped[i1:i2] = range(j1, j2)
        elif tag == 'replace':
            for k in range(i2 - i1):
                mapped[i1 + k] = j1 + k * (j2 - j1) // (i2 - i1)
    if same < len(tokens) / 2:
        raise ValueError('The lines do not resemble the transcript. Transcribe this take again.')
    captions = []
    for index, line in enumerate(lines):
        spans = [m for m, o in zip(mapped, owner) if o == index and m is not None]
        if not spans:
            short = line if len(line) <= 72 else line[:72] + '…'
            raise ValueError('Line %d has no words in the transcript: "%s". Edit its times in the captions below instead.'
                             % (index + 1, short))
        captions.append(dict(text=line, start=float(entries[min(spans)]['start']), end=float(entries[max(spans)]['end'])))
    return captions, len(tokens) - same


_ROW = re.compile(r'^\s*(\d+(?:\.\d*)?)\s*[-–]\s*(\d+(?:\.\d*)?)\s*s?\s+(.*\S)\s*$')


def parse(text):
    """Captions typed as `start - end  text`, one per line, in seconds."""
    captions = []
    for number, raw in enumerate(str(text or '').splitlines(), 1):
        if not raw.strip():
            continue
        match = _ROW.match(raw)
        if not match:
            raise ValueError('Caption %d needs "start - end  text", for example "1.25 - 2.80  Hello."' % number)
        start, end = float(match.group(1)), float(match.group(2))
        if end <= start:
            raise ValueError('Caption %d ends before it starts.' % number)
        captions.append(dict(text=match.group(3), start=start, end=end))
    return captions


def trimmed(rows, lead, duration):
    """Rows with start/end moved onto a clip whose time 0 is `lead` seconds into the audio and
    which lasts `duration`. Rows outside the clip are dropped; rows crossing an edge are clipped."""
    out = []
    for row in rows or []:
        start = max(0.0, float(row['start']) - float(lead))
        end = min(float(duration), float(row['end']) - float(lead))
        if end - start > 1e-3:
            out.append(dict(row, start=round(start, 3), end=round(end, 3)))
    return out


def source_clock(words, segments):
    """Map word times from an arranged wav back onto the source file. Gaps are dropped."""
    pieces = []
    for place, src_in, src_out in segments or []:
        place, src_in, src_out = float(place), float(src_in), float(src_out)
        if src_out > src_in:
            pieces.append((place, src_in, src_out))
    if not pieces:
        return []
    out = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        try:
            start, end = float(word['start']), float(word['end'])
        except (KeyError, TypeError, ValueError):
            out.append(dict(word))
            continue
        placed = None
        for place, src_in, src_out in pieces:
            begin = max(start, place)
            finish = min(end, place + (src_out - src_in))
            if finish - begin <= 1e-3:
                continue
            shift = src_in - place
            placed = dict(word, start=round(begin + shift, 4), end=round(finish + shift, 4))
            break
        if placed is not None:
            out.append(placed)
    return out


def retimed(rows, segments):
    """Move caption times onto the arranged audio. Segments are (place, source in, source out)."""
    kept = []
    for row in rows or []:
        try:
            row_start, row_end = float(row['start']), float(row['end'])
        except (KeyError, TypeError, ValueError):
            continue
        for place, src_in, src_out in segments or []:
            begin = max(row_start, float(src_in))
            finish = min(row_end, float(src_out))
            if finish - begin < 0.05:
                continue
            shift = float(place) - float(src_in)
            kept.append(dict(row, start=round(begin + shift, 4), end=round(finish + shift, 4)))
    kept.sort(key=lambda row: row['start'])
    return kept


def clip_rows(rows, start, end):
    """Keep the overlap with [start, end) on the same clock as the rows."""
    kept = []
    start, end = float(start), float(end)
    for row in rows or []:
        begin = max(start, float(row['start']))
        finish = min(end, float(row['end']))
        if finish - begin < 0.05:
            continue
        kept.append(dict(row, start=round(begin, 4), end=round(finish, 4)))
    return kept


def shifted(captions, lead, duration):
    """Guii's {text, duration, startTime} form, on the exported clip's clock."""
    return [dict(text=c['text'], duration=round(c['end'] - c['start'], 3), startTime=c['start'])
            for c in trimmed(captions, lead, duration)]


def shift_entry(entry, lead, duration):
    out = dict(entry)
    out['captions'] = trimmed(entry.get('captions'), lead, duration)
    out['words'] = trimmed(timed_words(entry.get('words')), lead, duration)
    if out['captions']:
        out['lines'] = '\n'.join(c['text'] for c in out['captions'])
    return out


def summary(captions):
    return '\n'.join('%.2f - %.2f  %s' % (c['start'], c['end'], c['text']) for c in captions or [])


def srt(captions):
    def stamp(seconds):
        ms = max(0, int(round(float(seconds) * 1000)))
        return '%02d:%02d:%02d,%03d' % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
    return '\n\n'.join('%d\n%s --> %s\n%s' % (i, stamp(c['start']), stamp(c['end']), c['text'].replace('\n', ' '))
                       for i, c in enumerate(captions or [], 1))


def load(root):
    try:
        data = json.loads((folder(root) / FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    takes = data.get('takes') if isinstance(data, dict) else None
    return takes if isinstance(takes, dict) else {}


def for_take(root, audio):
    entry = load(root).get(str(audio or ''))
    return entry if isinstance(entry, dict) else {}


def save_take(root, audio, **entry):
    takes = load(root)
    takes[str(audio)] = {**takes.get(str(audio), {}), **entry}
    folder(root).mkdir(parents=True, exist_ok=True)
    atomic_json(folder(root) / FILE, dict(version=1, takes=takes))
