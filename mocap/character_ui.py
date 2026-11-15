"""Imported targets are distinct from source people and extraction selections."""
import json,os,sys,time,uuid,shutil
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt,QEvent,QProcess,QTimer,QProcessEnvironment
from PySide6.QtWidgets import QFileDialog,QInputDialog,QListWidgetItem,QMenu,QMessageBox
from .paths import ROOT,HOME
from .storage import atomic_json
from .character_data import CharacterLoader

class CharacterWorkspace:
    def init_characters(self):
        self.character_root=None;self.characters=[];self.character_assets={};self.character_process=None
        self.character_refresh_queue=[];self.character_refresh_total=0
        self.character_session=HOME/'character_scenes'/uuid.uuid4().hex
        self.character_loader=CharacterLoader(self)
        self.character_loader.ready.connect(self.character_ready)
        self.character_loader.evicted.connect(lambda key:self.character_assets.pop(key,None))
        self.character_loader.changed.connect(self.motion_view.update)
        self.character_loader.failed.connect(lambda key,msg:self.status.setText('Character preview: '+msg))
        self.character_loader.preparing.connect(lambda msg:self.status.setText(msg) if self.character_process is None else None)
        self.character_loader.start()
        self.character_timer=QTimer(self);self.character_timer.setInterval(500)
        self.character_timer.timeout.connect(self.character_progress)
        self.people_list.characterMenuRequested.connect(self.character_menu)
        self.motion_view.character_workspace=self
        self._library_filling=False
        self._library_button=Qt.LeftButton
        self.refresh_character_library()
        self.character_library.setContextMenuPolicy(Qt.CustomContextMenu)
        self.character_library.customContextMenuRequested.connect(self.library_menu)
        self.character_library.viewport().installEventFilter(self)
        self.character_library.itemClicked.connect(self.library_character_chosen)

    def sync_characters(self):
        root=Path(self.workspace_root or self.project or self.character_session)
        if root!=self.character_root:
            pending=list(self.characters) if self.character_root==self.character_session else []
            blocked=self.people_list.blockSignals(True)
            for i in range(self.people_list.count()-1,-1,-1):
                if isinstance(self.people_list.item(i).data(Qt.UserRole),str):self.people_list.takeItem(i)
            self.people_list.blockSignals(blocked)
            self.character_loader.reset();self.character_assets.clear();self.motion_view.clear_characters()
            self.character_root=root;self.characters=[]
            p=root/'characters/index.json'
            if p.is_file():
                try:
                    data=json.loads(p.read_text(encoding='utf8'))
                    if len(data)>8:raise ValueError('Too many imported characters (maximum 8).')
                    loaded=[]
                    for c in data:
                        try:
                            self.character_asset_path(c)
                            if c.get('animation'):
                                self.character_path(c['animation'])
                                for take in c.get('takes',[]):self.character_path(c['animation']+'/'+take['folder'])
                            self._copy_library_voice(c)
                            loaded.append(c)
                        except (ValueError,OSError,KeyError) as exc:
                            self.status.setText('Cannot load character: '+str(exc))
                    self.characters=loaded
                except (ValueError,OSError,KeyError) as exc:self.status.setText('Cannot load characters: '+str(exc))
            for c in self.characters:self.request_character(c)
            linked=[c for c in pending if c.get('library_id')]
            copied=[c for c in pending if not c.get('library_id')]
            have={c.get('library_id') for c in self.characters}
            room=8-len(self.characters)
            add=[c for c in linked if c.get('library_id') not in have][:max(0,room)]
            if add:
                self.characters.extend(add)
                try:self.save_characters()
                except OSError as exc:self.status.setText('Cannot save characters: '+str(exc))
                for c in add:self.request_character(c)
            if len(linked)>len(add):
                self.status.setText('Some library characters were kept separately: the project would exceed 8 characters.')
            if copied and len(copied)+len(self.characters)>8:
                self.status.setText('Imported session characters were kept separately: the project would exceed 8 characters.')
                copied=[]
            if copied:
                folder='import-'+uuid.uuid4().hex
                QTimer.singleShot(0,lambda copied=copied,folder=folder:self.start_character_job(dict(mode='adopt',
                    output=str(self.character_path(folder)),prefix=folder,characters=copied,
                    source=str(self.character_session/'characters'))))
        self._catalog_loaded_characters()
        self.append_character_rows()

    def character_asset_path(self,c):
        if c.get('library_id'):
            from .character_library import owned_asset
            return owned_asset(c['library_id'])
        return self.character_path(c['asset'])

    def _catalog_loaded_characters(self):
        """Copy project characters into the app library so later sessions can add them."""
        from .character_library import get, upsert
        changed=False
        for c in self.characters:
            if c.get('library_id') or 'asset' not in c:continue
            try:
                path=self.character_path(c['asset'])
                if not (path/'preview.npz').is_file():continue
                if get(c['id']) is None:
                    upsert(dict(id=c['id'],name=c.get('name') or 'Character',kind=c.get('kind') or 'mixamo',
                                voice_id=c.get('voice_id'),voice_name=c.get('voice_name'),external=str(path.resolve())))
                from .character_library import is_owned, owned_asset
                if not is_owned(c['id']):
                    self.status.setText('Adding %s to the character library...'%(c.get('name') or 'character'))
                owned_asset(c['id'])
                c['library_id']=c['id']
                changed=True
            except (OSError,ValueError,KeyError):
                continue
        if not changed:return
        try:self.save_characters()
        except OSError as exc:self.status.setText('Cannot save characters: '+str(exc));return
        self.refresh_character_library()

    def _copy_library_voice(self,c):
        if not c.get('library_id'):return
        from .character_library import get
        entry=get(c['library_id'])
        if entry is None:return
        if entry.get('name'):c['name']=entry['name']
        c['voice_id']=entry.get('voice_id')
        c['voice_name']=entry.get('voice_name')

    def character_path(self,relative):
        base=(self.character_root/'characters').resolve();p=(base/relative).resolve()
        if not p.is_relative_to(base):raise ValueError('Invalid character project path')
        return p

    def save_characters(self):atomic_json(self.character_root/'characters/index.json',self.characters)

    def selected_character(self):
        item=self.people_list.currentItem()
        key=item.data(Qt.UserRole) if item else None
        return next((c for c in getattr(self,'characters',[]) if key=='character:'+c['id']),None)

    def export_character(self,c):
        if not c.get('takes') or self.busy:return
        if c.get('needs_retarget'):
            self.status.setText('Character motion is out of date. Apply Correction or retarget it again.');return
        from .person_names import export_stem
        from .character_export_dialog import CharacterExportDialog
        dialog=CharacterExportDialog(c['kind'],self)
        accepted=dialog.exec();options=dialog.options();dialog.deleteLater()
        if not accepted:return
        extension=options['format'];label=extension.upper()
        stem=export_stem(c['name']);takes=c['takes']
        if len(takes)==1:
            name,_=QFileDialog.getSaveFileName(self,'Export Character',stem+'.'+extension,f'{label} (*.{extension})')
            if not name:return
            if Path(name).suffix.lower()!='.'+extension:
                name+='.'+extension
                if Path(name).exists() and QMessageBox.question(self,'Export Character','Replace the existing file?')!=QMessageBox.Yes:return
            files=[name]
        else:
            name=QFileDialog.getExistingDirectory(self,'Export Character Takes')
            if not name:return
            files=[str(Path(name)/(stem+f'_take_{i+1}.{extension}')) for i in range(len(takes))]
            if any(Path(p).exists() for p in files) and QMessageBox.question(self,'Export Character','Replace existing files with these names?')!=QMessageBox.Yes:return
        self.start_character_job(dict(mode='export',output=str(self.character_path('export-'+uuid.uuid4().hex)),
            animation=str(self.character_path(c['animation'])),takes=takes,files=files,source_name=c['name'],**options))

    def append_character_rows(self):
        if not hasattr(self,'characters'):return
        blocked=self.people_list.blockSignals(True)
        try:
            existing={self.people_list.item(i).data(Qt.UserRole):self.people_list.item(i) for i in range(self.people_list.count())}
            for c in self.characters:
                key='character:'+c['id'];item=existing.get(key)
                if item is None:
                    item=QListWidgetItem();item.setData(Qt.UserRole,key);item.setData(Qt.UserRole+1,'#69c9b8')
                    item.setFlags(item.flags()|Qt.ItemIsUserCheckable);self.people_list.addItem(item)
                item.setText(c['name']+(' · Needs retarget' if c.get('needs_retarget') else ''));item.setCheckState(Qt.Checked if c.get('visible',True) else Qt.Unchecked)
                voice=('\nVoice: '+c['voice_name']) if c.get('voice_name') else ''
                item.setToolTip('Character · '+c['kind'].upper()+voice+'\nRight-click > Retarget to choose a source person. Check to show; uncheck to hide.\n'+
                    (('Baked from '+c.get('source_name','source')+' ('+c.get('result','')+'). Apply Correction also updates linked characters. Retarget again after replacing source motion.') if c.get('takes') else 'Rest pose. No motion assigned.'))
        finally:self.people_list.blockSignals(blocked)

    def character_checked(self,item):
        key=item.data(Qt.UserRole)
        if not isinstance(key,str) or not key.startswith('character:'):return False
        for c in self.characters:
            if c['id']==key.split(':',1)[1]:c['visible']=item.checkState()==Qt.Checked
        self.save_characters();self.motion_view.update();return True

    def choose_character(self):
        if self.busy:return
        self.sync_characters()
        name,_=QFileDialog.getOpenFileName(self,'Import Character','','Humanoid characters (*.fbx *.vrm *.pmx)')
        if not name:return
        ident=uuid.uuid4().hex
        from .character_library import asset_dir
        self.start_character_job(dict(mode='import',file=name,output=str(asset_dir(ident)),id=ident,library=True))

    def _library_current_id(self):
        box=getattr(self,'character_library',None)
        item=box.currentItem() if box is not None else None
        return item.data(Qt.UserRole) if item is not None else None

    def refresh_character_library(self,select=None):
        box=getattr(self,'character_library',None)
        if box is None:return
        from .character_library import load
        self._library_filling=True
        try:
            current=select if select is not None else self._library_current_id()
            box.clear()
            try:entries=load()
            except (OSError,ValueError,json.JSONDecodeError) as exc:
                self.status.setText('Cannot load the character library: '+str(exc));return
            present={c.get('library_id') for c in getattr(self,'characters',[])}
            for entry in entries:
                name=entry.get('name') or 'Character'
                item=QListWidgetItem(name)
                item.setData(Qt.UserRole,entry['id'])
                item.setToolTip(name+('\nIn this session. Click to show it.' if entry['id'] in present else '\nClick to add it to this session.'))
                if entry['id'] in present:item.setText(name+'  ·  In session')
                box.addItem(item)
            empty=getattr(self,'character_library_empty',None)
            if empty is not None:empty.setVisible(box.count()==0)
            box.setVisible(box.count()>0)
            if current:self.select_library_combo(current)
        finally:self._library_filling=False

    def eventFilter(self,obj,event):
        library=getattr(self,'character_library',None)
        if library is not None and obj is library.viewport() and event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease):
            self._library_button=event.button()
        return False

    def library_character_chosen(self,item):
        if self._library_filling or item is None or self.busy or self._library_button==Qt.RightButton:return
        ident=item.data(Qt.UserRole)
        if ident:self.activate_library_character(ident)

    def library_menu(self,pos):
        if self.busy:return
        item=self.character_library.itemAt(pos)
        if item is None:return
        ident=item.data(Qt.UserRole)
        if not ident:return
        menu=QMenu(self.character_library)
        rename=menu.addAction('Rename...')
        delete=menu.addAction('Delete from Library')
        try:
            chosen=menu.exec(self.character_library.viewport().mapToGlobal(pos))
        finally:
            menu.deleteLater()
        if chosen==rename:self.rename_library_character(ident)
        elif chosen==delete:self.delete_library_character(ident)

    def rename_library_character(self,ident):
        from .character_library import get, rename
        entry=get(ident)
        if entry is None:return
        text,ok=QInputDialog.getText(self,'Rename Character','Name:',text=entry.get('name') or 'Character')
        if not ok:return
        try:name=rename(ident,text)
        except (OSError,ValueError) as exc:
            self.status.setText('Cannot rename character: '+str(exc));return
        changed=False
        for c in self.characters:
            if c.get('library_id')==ident:
                c['name']=name;changed=True
        if changed and self.character_root is not None:
            try:self.save_characters()
            except OSError as exc:
                self.status.setText('Renamed in the library. Cannot update this project: '+str(exc))
        self.refresh_character_library(ident)
        self.append_character_rows()
        if hasattr(self,'session_voice_status') and self._library_current_id()==ident:
            voice=next((c.get('voice_name') for c in self.characters if c.get('library_id')==ident),None)
            self.session_voice_status.setText(name+(' · '+voice if voice else ''))
        self.status.setText('Renamed character to '+name)

    def delete_library_character(self,ident):
        from .character_library import get, delete, asset_dir
        entry=get(ident)
        if entry is None:return
        name=entry.get('name') or 'Character'
        if QMessageBox.question(self,'Delete Character',
            'Delete %s from the character library? Its saved copy is removed, it leaves this session, and a project that uses it will no longer show it.'%name,
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        session=[c for c in self.characters if c.get('library_id')==ident or c.get('id')==ident]
        for c in session:self.remove_character(c['id'])
        # Preview keeps preview-frames.npy mapped; Windows refuses rmtree until that closes.
        try:
            path=asset_dir(ident)
            self.character_loader.drop(path)
            self.character_assets.pop(str(path),None)
            try:self.character_assets.pop(str(path.resolve()),None)
            except OSError:pass
        except (OSError,ValueError,TimeoutError) as exc:
            self.status.setText('Cannot delete character: '+str(exc))
            self.refresh_character_library();return
        try:delete(ident)
        except (OSError,ValueError) as exc:
            self.status.setText('Cannot delete character: '+str(exc))
            self.refresh_character_library();return
        self.refresh_character_library()
        self.status.setText('Deleted %s from the character library.'%name)

    def activate_library_character(self,ident):
        from .character_library import get
        entry=get(ident)
        if entry is None:return
        self.sync_characters()
        existing=next((c for c in self.characters if c.get('library_id')==ident),None)
        if existing is None:
            if len(self.characters)>=8:
                self.status.setText('This project already has 8 characters. Remove one to show another from the library.');return
            existing=dict(id=ident,library_id=ident,name=entry['name'],kind=entry['kind'],visible=True,
                          voice_id=entry.get('voice_id'),voice_name=entry.get('voice_name'))
            self.characters.append(existing)
        else:
            existing['visible']=True
            existing['voice_id']=entry.get('voice_id')
            existing['voice_name']=entry.get('voice_name')
        self.save_characters();self.request_character(existing);self.append_character_rows()
        self.refresh_character_library(existing['id'])
        key='character:'+existing['id']
        blocked=self.people_list.blockSignals(True)
        try:
            for row in range(self.people_list.count()):
                item=self.people_list.item(row)
                if item.data(Qt.UserRole)==key:
                    self.people_list.setCurrentItem(item);break
        finally:self.people_list.blockSignals(blocked)
        self._bind_character_voice()
        self.motion_view.update()

    def current_library_character(self):
        c=self.selected_character()
        if c and c.get('library_id'):return c
        shown=[c for c in getattr(self,'characters',[]) if c.get('library_id') and c.get('visible',True)]
        if len(shown)==1:return shown[0]
        ident=self._library_current_id()
        if not ident:return None
        row=next((c for c in self.characters if c.get('library_id')==ident),None)
        if row:return row
        from .character_library import get
        entry=get(ident)
        if entry is None:return None
        return dict(entry,library_id=ident)

    def select_library_combo(self,ident):
        box=getattr(self,'character_library',None)
        if box is None or not ident:return
        filling=self._library_filling
        self._library_filling=True
        try:
            for row in range(box.count()):
                item=box.item(row)
                if item.data(Qt.UserRole)==ident:
                    if item is not box.currentItem():box.setCurrentItem(item)
                    return
        finally:
            if not filling:self._library_filling=False

    def apply_character_voice(self):
        box=getattr(self,'session_voices',None)
        if box is None or box.count()<1:return False
        c=self.current_library_character()
        if c is None or not c.get('voice_id'):return False
        index=box.findData(c['voice_id'])
        if index<0:return False
        if index!=box.currentIndex():
            blocked=box.blockSignals(True)
            box.setCurrentIndex(index)
            box.blockSignals(blocked)
        if hasattr(self,'session_voice_status'):
            self.session_voice_status.setText('%s · %s'%(c.get('name') or 'Character',box.currentText()))
        return True

    def remember_character_voice(self,index=None):
        if getattr(self,'_filling_voices',False):return
        box=getattr(self,'session_voices',None)
        if box is None:return
        voice_id=box.currentData()
        if not voice_id:return
        c=self.current_library_character()
        if c is None or not c.get('library_id'):return
        name=box.currentText()
        if index is None and c.get('voice_id') and c.get('voice_id')!=voice_id:return
        self.store_character_voice(c,voice_id,name)

    def store_character_voice(self,c,voice_id,name):
        if c.get('voice_id')==voice_id and c.get('voice_name')==name:return
        from .character_library import set_voice
        try:set_voice(c['library_id'],voice_id,name)
        except (OSError,ValueError) as exc:
            self.status.setText('Cannot save character voice: '+str(exc));return
        c['voice_id']=voice_id;c['voice_name']=name
        stored=False
        for row in self.characters:
            if row.get('library_id')==c['library_id']:
                row['voice_id']=voice_id;row['voice_name']=name;stored=True
        if stored:
            try:self.save_characters()
            except OSError as exc:
                self.status.setText('Cannot save character voice: '+str(exc));return
        if hasattr(self,'session_voice_status'):
            self.session_voice_status.setText('%s · %s'%(c.get('name') or 'Character',name))
        self.append_character_rows()

    def _bind_character_voice(self):
        if not self.apply_character_voice():
            self.remember_character_voice()

    def character_sources(self):
        from .motion_tools import result_is_current
        from .people_store import safe_child
        if self.people_state:
            people=self.people_state['people']
        elif self.project:people=[dict(id=1,name=(self._single_actor or {}).get('name','Person 1'),takes=[dict(project=str(self.project),start=self.source_offset)])]
        else:people=[]
        result=[]
        for p in people:
            takes=[]
            for t in p.get('takes',[]):
                path=safe_child(self.people_root,t['project']) if self.people_state else Path(t['project'])
                from .gemx_result import RECEIPT, is_project
                metadata=path/RECEIPT if is_project(path) else path/(self.active_result+'_mixamo.json')
                if not metadata.is_file() or not result_is_current(path,self.active_result):continue
                manifest=json.loads((path/'project.json').read_text(encoding='utf8'))
                start=float(manifest.get('start',0.))
                # Preview scene's stored camera transform is the authoritative shared world.
                transform=None
                if self.scene_data:
                    st=self.scene_data.by_project.get(path.resolve())
                    if st is not None:transform=st.transform.tolist()
                takes.append(dict(project=str(path),start=start,transform=transform))
            if takes:result.append(dict(name=p['name'],takes=sorted(takes,key=lambda t:t['start'])))
        return result

    def character_menu(self,ident,position):
        if self.busy:return
        c=next((c for c in self.characters if 'character:'+c['id']==ident),None)
        if c is None:return
        menu=QMenu(self);retarget=menu.addMenu('Retarget')
        sources=self.character_sources()
        if not sources:retarget.addAction('No prepared source motion').setEnabled(False)
        for source in sources:
            action=retarget.addAction(source['name'])
            action.triggered.connect(lambda checked=False,s=source:self.retarget_character(c,s))
        export=menu.addAction('Export Character...');export.setEnabled(bool(c.get('takes')) and not c.get('needs_retarget'))
        export.triggered.connect(lambda:self.export_character(c))
        menu.addSeparator()
        remove=menu.addAction('Remove Character')
        try:
            if menu.exec(position)==remove:self.remove_character(c['id'])
        finally:menu.deleteLater()

    def remove_character(self,ident):
        if self.busy:return
        removed=next((c for c in self.characters if c['id']==ident),None)
        remaining=[c for c in self.characters if c['id']!=ident]
        if removed is None:return
        # Commit before changing the UI; a failed save must leave the row usable.
        try:atomic_json(self.character_root/'characters/index.json',remaining)
        except OSError as exc:
            self.status.setText('Cannot remove character: '+str(exc));return
        self.characters=remaining
        self._release_character_preview(removed)
        blocked=self.people_list.blockSignals(True)
        try:
            key='character:'+ident
            current=self.people_list.currentItem()
            selected=current is not None and current.data(Qt.UserRole)==key
            # takeItem changes the current row synchronously. Without blocking,
            # person_selected can reload/clear this same list inside Qt's removal,
            # deleting the item while Qt still uses it (native access violation).
            for row in range(self.people_list.count()-1,-1,-1):
                if self.people_list.item(row).data(Qt.UserRole)==key:
                    self.people_list.takeItem(row)
            if selected:
                active=getattr(self,'active_person_id',None)
                items=[self.people_list.item(i) for i in range(self.people_list.count())]
                fallback=next((item for item in items if item.data(Qt.UserRole)==active),None)
                if fallback is None:
                    fallback=next((item for item in items if isinstance(item.data(Qt.UserRole),str)),None)
                self.people_list.setCurrentItem(fallback)
        finally:self.people_list.blockSignals(blocked)
        # Selection restoration is display-only: keep the source, frame and camera.
        self.update_readiness()
        self.motion_view.update()

    def _release_character_preview(self,c):
        paths=[]
        try:paths.append(self.character_asset_path(c))
        except (OSError,ValueError):pass
        for take in c.get('takes') or []:
            folder=take.get('folder')
            if not folder or not c.get('animation'):continue
            try:paths.append(self.character_path(c['animation']+'/'+folder))
            except (OSError,ValueError):pass
        for path in paths:
            try:self.character_loader.drop(path)
            except TimeoutError:
                # Library delete will surface a clear error; session remove can keep memory briefly.
                pass
            self.character_assets.pop(str(path),None)
            try:self.character_assets.pop(str(path.resolve()),None)
            except OSError:pass

    def retarget_character(self,c,source):
        if self.busy or self.contact_saving:return
        if self.active_result.startswith('corrected_') and self.current_export() is None:
            self.status.setText('Apply contact changes before retargeting.');return
        folder=c['id']+'/animation-'+uuid.uuid4().hex
        job=dict(mode='retarget',output=str(self.character_path(folder)),asset=str(self.character_asset_path(c)),id=c['id'],
                 animation=folder,result=self.active_result,source_name=source['name'],takes=source['takes'],
                 arm_clearance=self.arm_clearance.isChecked())
        self.start_character_job(job)

    def _auto_retarget_scope(self):
        return (self.people_root,None) if self.people_root is not None else (None,self.project)

    def queue_auto_retarget(self):
        self._auto_retarget=self._auto_retarget_scope()
        QTimer.singleShot(0,self.run_auto_retarget)

    def run_auto_retarget(self):
        """Retarget every visible character once the processed motion and shared scene are loaded."""
        pending=getattr(self,'_auto_retarget',None)
        if pending is None or self.busy or self.character_process is not None or self.contact_saving:return
        if pending!=self._auto_retarget_scope():
            self._auto_retarget=None;return
        if self.people_root is not None and self.scene_data is None:return
        self._auto_retarget=None
        targets=[c for c in self.characters if c.get('visible',True)]
        sources=self.character_sources() if targets else []
        if not sources:return
        if self.active_result.startswith('corrected_') and self.current_export() is None:
            self.status.setText('Apply contact changes, then retarget characters.');return
        by_name={s['name']:s for s in sources}
        chosen={c['id']:by_name[c['source_name']] for c in targets if c.get('source_name') in by_name}
        free=[s for s in sources if s not in chosen.values()] or sources
        unassigned=[c for c in targets if c['id'] not in chosen]
        for i,c in enumerate(unassigned):chosen[c['id']]=free[i%len(free)]
        self.character_refresh_queue=[dict(id=c['id'],result=self.active_result,source_name=chosen[c['id']]['name'],
                                           takes=chosen[c['id']]['takes']) for c in targets]
        self.character_refresh_total=len(self.character_refresh_queue)
        self.next_character_refresh()

    def prepare_character_refresh(self,jobs):
        from .retarget.refresh import correction_refreshes
        self.character_refresh_queue=correction_refreshes(self.characters,jobs,self.project)
        self.character_refresh_total=len(self.character_refresh_queue)
        ids={p['id'] for p in self.character_refresh_queue}
        for c in self.characters:
            if c['id'] in ids:c['needs_retarget']=True
        if ids:self.save_characters();self.append_character_rows()

    def next_character_refresh(self):
        if not self.character_refresh_queue:return
        plan=self.character_refresh_queue.pop(0)
        c=next(c for c in self.characters if c['id']==plan['id'])
        folder=c['id']+'/animation-'+uuid.uuid4().hex
        job=dict(plan,mode='retarget',output=str(self.character_path(folder)),
                 asset=str(self.character_asset_path(c)),animation=folder,arm_clearance=self.arm_clearance.isChecked(),
                 refresh_label=f"Updating {c['name']} · {self.character_refresh_total-len(self.character_refresh_queue)}/{self.character_refresh_total}")
        self.start_character_job(job)

    def start_character_job(self,job):
        from .retarget.runtime import blender
        from .runtime_registry import command
        try:
            if job['mode']!='adopt':blender()
            path=self.character_root/'characters'/('job-'+uuid.uuid4().hex+'.json');atomic_json(path,job)
            args=['--request',str(path)]
            from .gemx_result import is_project
            if job['mode']=='retarget' and not is_project(job['takes'][0]['project']):
                args+=['--project',job['takes'][0]['project']]
            cmd=command(ROOT,'character',args,frozen=getattr(sys,'frozen',False))
        except Exception as exc:
            self.character_refresh_queue=[]
            self.status.setText(str(exc));return
        self.stop_playback();self.character_job=job;self.character_started=time.monotonic();self.character_line='Starting character processing...';self.character_buffer='';self.character_cancelled=False
        p=QProcess(self);self.character_process=p
        env=QProcessEnvironment.systemEnvironment();env.insert('PYTHONUTF8','1');env.insert('PYTHONIOENCODING','utf-8');p.setProcessEnvironment(env)
        p.setWorkingDirectory(str(ROOT));p.setProcessChannelMode(QProcess.MergedChannels)
        p.readyReadStandardOutput.connect(self.character_output)
        p.finished.connect(self.character_finished)
        p.errorOccurred.connect(lambda error:self.character_finished(-1) if error==QProcess.FailedToStart else None)
        self.busy=True;self.progress.setRange(0,0);self.character_timer.start();self.character_progress();self.update_readiness()
        p.start(cmd[0],cmd[1:])

    def character_output(self):
        if not self.character_process:return
        text=bytes(self.character_process.readAllStandardOutput()).decode('utf8',errors='replace')
        self.character_buffer=(self.character_buffer+text)[-16000:]
        for line in text.splitlines():
            if line.startswith('CHARACTER:'):
                self.character_line=line.partition(':')[2].strip()
                self.log.status_terminal(line.strip())
        self.character_progress()

    def character_progress(self):
        label=self.character_job.get('refresh_label')
        self.status.setText((label+' · ' if label else '')+self.character_line)
        self.task_info.setText(f'Character · {time.monotonic()-self.character_started:.0f}s elapsed')

    def character_finished(self,code,*args):
        if self.character_process is None:return
        self.character_output();p=self.character_process;self.character_process=None;p.deleteLater();self.character_timer.stop();self.busy=False
        job=self.character_job;complete=Path(job['output'])/'complete.json'
        if code==0 and complete.is_file() and not self.character_cancelled:
            result=json.loads(complete.read_text(encoding='utf8'))
            if job['mode']=='adopt':
                self.characters.extend(result['characters'])
                self.save_characters()
                for c in result['characters']:self.request_character(c)
                self.append_character_rows()
            elif job['mode']=='import':
                from .character_library import upsert
                entry=dict(id=job['id'],name=result.get('name') or Path(job['file']).stem,kind=result['kind'],voice_id=None,voice_name=None)
                upsert(entry)
                self.refresh_character_library(entry['id'])
                self.activate_library_character(entry['id'])
                ready=entry['name']+' is in the character library.'
            elif job['mode']=='retarget':
                c=next(c for c in self.characters if c['id']==job['id'])
                c.pop('_starts',None)
                c.pop('needs_retarget',None)
                c.update(animation=job['animation'],takes=result['takes'],source_name=result['source_name'],result=result['result'],
                         arm_clearance=bool(job.get('arm_clearance')))
            if job['mode']=='retarget':
                self.save_characters();self.request_character(c);self.append_character_rows()
            if job['mode']!='import':
                ready=f'Character ready · {result["seconds"]:.1f}s'
            self.status.setText(ready);self.progress.setRange(0,100);self.progress.setValue(100)
        else:
            self.character_refresh_queue=[]
            self.status.setText('Character job cancelled.' if self.character_cancelled else 'Character processing failed. See Console.')
            self.log.append(self.character_buffer);self.progress.setRange(0,100);self.progress.setValue(0)
            if job.get('library'):
                parent=Path(job['output']).parent
                if parent.name==job['id']:shutil.rmtree(parent,ignore_errors=True)
        self.task_info.setText('');self.update_readiness();self.motion_view.update()
        if job.get('refresh_label'):
            self.next_character_refresh()
        else:
            self.run_auto_retarget()

    def cancel_character(self):
        p=self.character_process
        if not p:return
        self.character_cancelled=True;self.character_line='Cancelling character job...'
        helper=QProcess(self);self._character_killer=helper
        helper.start('taskkill',['/PID',str(p.processId()),'/T','/F'])
        helper.finished.connect(helper.deleteLater)

    def request_character(self,c):
        path=self.character_asset_path(c);self.character_loader.request(path)

    def character_ready(self,key,data):
        if self.character_loader.stop:return
        self.character_assets[key]=data
        if self.status.text()=='Loading original character textures...':self.status.setText('Character preview ready.')
        if self.motion_view.joints is None and not self.motion_view._camera_initialized:
            v=data['asset']['vertices'];lo=v.min(0);hi=v.max(0)
            self.motion_view.scene_center=(lo+hi)*.5
            self.motion_view.distance=max(2.,float(np.linalg.norm(hi-lo))*1.5)
            self.motion_view._camera_initialized=True
        self.motion_view.update()

    def character_draws(self):
        from bisect import bisect_right
        time_=self.motion_view.scene_time
        # No body motion yet: keep every imported character in its rest pose so live face has a mesh.
        rest_only=self.motion_view.joints is None
        for c in self.characters:
            if not c.get('visible',True):continue
            path=self.character_asset_path(c);frame=0;transform=None
            if c.get('takes') and not rest_only:
                starts=c.setdefault('_starts',[t['start'] for t in c['takes']])
                i=bisect_right(starts,time_)-1
                take=c['takes'][i] if i>=0 else None
                if take is not None and time_>=take['start']+take['frames']/take['fps']:take=None
                if take is None:continue
                path=self.character_path(c['animation']+'/'+take['folder']);frame=int((time_-take['start'])*take['fps']+1e-6);transform=take.get('transform')
            data=self.character_assets.get(str(path))
            if not data:
                self.character_loader.request(path,frame,self.character_asset_path(c));continue
            matrix=self.character_loader.frame(path,frame)
            if matrix is not None:yield c,data,matrix,frame,transform
