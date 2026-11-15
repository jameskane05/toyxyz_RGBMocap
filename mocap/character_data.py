"""Off-thread asset reads and coalesced, bounded animation windows."""
import json
import time
from collections import OrderedDict
from pathlib import Path
from threading import Condition
import numpy as np
from PySide6.QtCore import QThread,Signal
from .preview_data import FrameArray

def _attach_skeleton(folder, asset):
    """Body skeleton for characters imported before bone heads were stored."""
    if 'bone_heads' in asset:
        return
    try:
        _attach_saved_skeleton(folder, asset)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return

def _attach_saved_skeleton(folder, asset):
    canon = next(folder.glob('*-canonical.npz'), None)
    rig_path = next(folder.glob('*-rig.json'), None)
    if canon is None or rig_path is None or not (folder / 'preview-frames.npy').is_file():
        return
    with np.load(canon) as data:
        names = [str(name) for name in data['names']]
        rest = np.asarray(data['rest'], np.float32)
        parents = np.asarray(data['parents'], np.int32)
    bake = folder / 'bake.json'
    if bake.is_file():
        palette = sorted(json.loads(bake.read_text(encoding='utf8')).get('keyed_bones') or [])
    else:
        palette = sorted(bone['name'] for bone in json.loads(rig_path.read_text(encoding='utf8')).get('bones') or [])
    frames = np.load(folder / 'preview-frames.npy', mmap_mode='r')
    width = int(frames.shape[1])
    del frames
    if len(palette) + 1 != width or not names or len(names) != len(rest):
        return
    try:
        index = [palette.index(name) for name in names]
    except ValueError:
        return
    asset['bone_heads'] = rest
    asset['bone_parents'] = parents
    asset['bone_index'] = np.asarray(index, np.int32)

class CharacterLoader(QThread):
    ready=Signal(str,object)
    changed=Signal()
    failed=Signal(str,str)
    evicted=Signal(str)
    preparing=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self.condition=Condition();self.pending=OrderedDict();self.data={};self.windows={};self.stop=False;self.epoch=0;self.errors=set();self.texture_sources={};self.drop_keys=set()
    def request(self,path,frame=0,texture_source=None):
        key=str(path)
        with self.condition:
            if key in self.errors:return
            if texture_source is not None:self.texture_sources[key]=str(texture_source)
            self.pending[key]=(frame,self.epoch);self.condition.notify()
    def frame(self,path,index):
        key=str(path)
        with self.condition:
            window=self.windows.get(key)
            if window and window[0]<=index<window[0]+len(window[1]):
                # A final window already contains every remaining frame.
                # Reloading it emits ready/changed and can create a repaint loop,
                # especially for imported characters with a one-frame rest pose.
                if window[0]+len(window[1])<window[2] and index>=window[0]+len(window[1])*3//4:
                    self.pending[key]=(index,self.epoch);self.condition.notify()
                return window[1][index-window[0]]
        self.request(path,index);return None
    def drop(self,path,timeout=5.0):
        """Close mapped preview files for this asset so Windows can delete them."""
        keys={str(path)}
        try:keys.add(str(Path(path).resolve()))
        except OSError:pass
        with self.condition:
            self.drop_keys.update(keys)
            for key in keys:
                self.pending.pop(key,None)
                self.windows.pop(key,None)
                self.texture_sources.pop(key,None)
                self.errors.discard(key)
                self.data.pop(key,None)
            self.condition.notify()
            end=time.monotonic()+timeout
            while self.drop_keys.intersection(keys) and not self.stop:
                remaining=end-time.monotonic()
                if remaining<=0:
                    raise TimeoutError('Character preview files are still open')
                self.condition.wait(timeout=remaining)
    def reset(self):
        with self.condition:
            self.epoch+=1;self.pending.clear();self.windows.clear();self.errors.clear();self.texture_sources.clear()
            self.drop_keys.clear();self.condition.notify_all()
    def close(self):
        with self.condition:self.stop=True;self.drop_keys.clear();self.condition.notify_all()
        self.wait()
        with self.condition:
            self.pending.clear();self.windows.clear();self.data.clear();self.texture_sources.clear();self.drop_keys.clear()
    def run(self):
        arrays=OrderedDict();epoch=-1
        try:
            while True:
                with self.condition:
                    while not self.stop and not self.pending and not self.drop_keys and epoch==self.epoch:self.condition.wait()
                    if self.stop:return
                    if epoch!=self.epoch:
                        for a in arrays.values():a.close()
                        arrays.clear();self.data.clear();epoch=self.epoch
                        if self.drop_keys:
                            self.drop_keys.clear();self.condition.notify_all()
                        if not self.pending:continue
                    dropping=list(self.drop_keys)
                    if dropping:
                        key=index=requested=None
                    elif not self.pending:continue
                    else:
                        key,(index,requested)=self.pending.popitem(last=False)
                if dropping:
                    for item in dropping:
                        array=arrays.pop(item,None)
                        if array is not None:array.close()
                        self.data.pop(item,None)
                        self.evicted.emit(item)
                    with self.condition:
                        for item in dropping:self.drop_keys.discard(item)
                        self.condition.notify_all()
                    continue
                if key is None:continue
                try:
                    if key not in arrays:
                        while arrays and (len(arrays)>=8 or sum(sum(v.nbytes for v in asset.values() if hasattr(v,'nbytes')) for asset in self.data.values())>256*1024**2):
                            old,a=arrays.popitem(last=False);a.close();self.data.pop(old,None)
                            with self.condition:self.windows.pop(old,None)
                            self.evicted.emit(old)
                        p=Path(key)
                        if (p/'preview.npz').stat().st_size>128*1024**2:raise ValueError('Character preview exceeds the 128 MB asset limit.')
                        with np.load(p/'preview.npz') as archive:asset={k:archive[k] for k in archive.files}
                        _attach_skeleton(p, asset)
                        if sum(a.nbytes for a in asset.values())>192*1024**2:raise ValueError('Character preview exceeds memory limits.')
                        asset['key']=key
                        from .name_overlay import character_samples
                        asset['name_samples']=character_samples(asset)
                        from .character_textures import load,validate_geometry
                        source=str(Path(self.texture_sources.get(key,key)).resolve())
                        textures=next((v['textures'] for v in self.data.values() if v.get('textures',{}).get('key')==source),None)
                        if textures is None:
                            self.preparing.emit('Loading original character textures...')
                            textures=load(source,asset,lambda:self.stop or requested!=self.epoch)
                        else:validate_geometry(textures['metadata'],asset)
                        asset['textures']=textures
                        # Shared images count once across rest/animated takes. Keep
                        # texture residency bounded independently of clip length.
                        def resident_bytes():
                            unique={v['textures']['key']:v['textures']['bytes'] for v in self.data.values()}
                            unique[source]=textures['bytes']
                            return sum(unique.values())
                        if resident_bytes()>2048*1024**2:
                            raise ValueError('Character textures exceed the 2 GB loaded-preview limit. Open fewer characters in this project.')
                        if self.stop or requested!=self.epoch:continue
                        a=FrameArray(p/'preview-frames.npy')
                        if a.shape[-2:]!=(4,4) or a.shape[1]>4096:
                            a.close();raise ValueError('Invalid character animation palette')
                        with self.condition:
                            # A concurrent library delete must be able to close this handle.
                            if key in self.drop_keys or requested!=self.epoch:
                                a.close();continue
                            arrays[key]=a;self.data[key]=asset
                    a=arrays[key];arrays.move_to_end(key);index=max(0,min(a.shape[0]-1,index))
                    # At most 4 MB per active take, regardless of clip duration.
                    count=max(1,min(128,4*1024**2//(a.shape[1]*64)));start=max(0,index-count//4)
                    block=np.asarray(a[start:min(a.shape[0],start+count)]).copy()
                    with self.condition:
                        if requested!=self.epoch:continue
                        self.windows[key]=(start,block,a.shape[0])
                    self.ready.emit(key,dict(asset=self.data[key],frames=a.shape[0],palette=a.shape[1]))
                    self.changed.emit()
                except Exception as exc:
                    if self.stop or requested!=self.epoch:continue
                    with self.condition:self.errors.add(key)
                    self.failed.emit(key,str(exc))
        finally:
            for a in arrays.values():a.close()
