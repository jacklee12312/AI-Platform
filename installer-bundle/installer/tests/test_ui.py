"""Offscreen interaction tests; never invokes pkexec or installs anything."""
import json,sys,tempfile,time,unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt,QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import wizard

APP=QApplication.instance() or QApplication([])
BUNDLE=Path(__file__).resolve().parents[2]


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.w=wizard.Wizard(BUNDLE,preview=True);self.w.timer.stop()
        if self.w.workflow_enabled:
            import test_workflow
            config=test_workflow.settings()
            for key,field in self.w.probe_fields.items():field.setText(config['probe'][key])
            for key,field in self.w.component_fields.items():field.setText(config['components'][key])
        self.temporary=tempfile.TemporaryDirectory()
        self.library=Path(self.temporary.name)/'AI-Platform-Model-Library'
        (self.library/'files').mkdir(parents=True)
        (self.library/'model-library.json').write_text(json.dumps({'release_id':self.w.release['release_id'],'files':[]}))
        self.w.sources['local_roots'].append(str(self.library));self.w.refresh_library_hint()
    def tearDown(self):
        self.w.preview=True;self.w.pending=False;self.w.close();self.w.deleteLater();APP.processEvents();self.temporary.cleanup()

    def test_preview_never_dispatches(self):
        with mock.patch('wizard.QProcess') as process:self.w.dispatch('install');process.assert_not_called()

    def test_existing_probe_hides_registration_and_needs_no_fields(self):
        with mock.patch('wizard.detect_probe',return_value=True):
            self.w.show_page(3)
            for field in self.w.probe_fields.values():field.clear()
            self.assertTrue(self.w.probe_install_panel.isHidden())
            self.assertTrue(self.w.probe_detection_notice.isHidden())
            self.assertNotIn('探针',self.w.subtitle.text())
            self.assertEqual(self.w.collect_workflow_settings()['probe'],{'mode':'auto','reuse_existing':True})
            self.w.show_page(4)
            self.assertNotIn('探针',self.w.confirm_overview.layout().itemAt(0).widget().text())
            self.assertNotIn('设备注册',self.w.consent.text())
        self.assertFalse(hasattr(self.w,'reuse_probe'))
    def test_missing_probe_is_visible_and_registration_is_mandatory(self):
        with mock.patch('wizard.detect_probe',return_value=False):
            self.w.show_page(3);self.assertFalse(self.w.probe_install_panel.isHidden())
            self.w.probe_fields['bindActivateToken'].clear()
            self.w.password.setText('fixture-password-123');self.w.password2.setText('fixture-password-123')
            with mock.patch('wizard.QMessageBox.warning') as warning:
                self.w.go_next();warning.assert_called_once()
            self.assertEqual(self.w.page_index,3)
            self.w.probe_fields['bindActivateToken'].setText('fixture-registration-secret');self.w.go_next()
            self.assertEqual(self.w.page_index,4);self.assertIn('必需探针',self.w.consent.text())
    def test_probe_detection_changes_reset_consent_and_reveal_required_form(self):
        with mock.patch('wizard.detect_probe',return_value=True):self.w.show_page(3)
        self.w.consent.setChecked(True)
        with mock.patch('wizard.detect_probe',return_value=False):self.w.refresh_probe_detection()
        self.assertFalse(self.w.probe_install_panel.isHidden());self.assertFalse(self.w.consent.isChecked())
    def test_detection_error_blocks_installation_instead_of_assuming_absence(self):
        with mock.patch('wizard.detect_probe',side_effect=RuntimeError('无法自动识别本机探针')):
            self.w.show_page(3);self.assertFalse(self.w.probe_detection_notice.isHidden())
            with self.assertRaisesRegex(ValueError,'无法自动识别'):self.w.collect_workflow_settings()
    def test_existing_probe_import_accepts_only_component_settings(self):
        value={'components':{'selected':['mysql'],'mysql_password':'fixture-password-123'}}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'onsite.json';path.write_text(json.dumps(value))
            with mock.patch('wizard.detect_probe',return_value=True),mock.patch('wizard.QFileDialog.getOpenFileName',return_value=(str(path),'')),mock.patch('wizard.QMessageBox.warning') as warning:
                self.w.import_workflow_settings();warning.assert_not_called()
                self.assertEqual(self.w.collect_workflow_settings()['probe'],{'mode':'auto','reuse_existing':True})
        self.assertEqual(self.w.component_selection(),['mysql'])

    def test_components_default_to_none_and_need_no_passwords(self):
        self.assertEqual(self.w.component_selection(),[])
        self.assertTrue(all(not check.isChecked() for check in self.w.component_checks.values()))
        self.assertEqual(self.w.collect_workflow_settings()['components'],{'selected':[]})
        self.assertTrue(all(row.isHidden() for row in self.w.component_rows.values()))
    def test_milvus_auto_dependencies_follow_the_actual_checkbox_click(self):
        self.w.show_page(1);self.w.choice_tabs.setCurrentIndex(1);self.w.show();APP.processEvents()
        check=self.w.component_checks['milvus'];QTest.mouseClick(check,Qt.LeftButton,pos=QPoint(10,check.height()//2))
        self.assertEqual(self.w.component_selection(),['milvus'])
        for name in ['minio','etcd']:
            self.assertTrue(self.w.component_checks[name].isChecked());self.assertFalse(self.w.component_checks[name].isEnabled())
        self.assertFalse(self.w.component_rows['minio_password'].isHidden())
        self.assertTrue(self.w.component_rows['mysql_password'].isHidden())
        self.assertIn('MinIO',self.w.confirm_components.text())
        QTest.mouseClick(check,Qt.LeftButton,pos=QPoint(10,check.height()//2))
        self.assertEqual(self.w.component_selection(),[])
        self.assertFalse(self.w.component_checks['minio'].isChecked());self.assertTrue(self.w.component_checks['minio'].isEnabled())
    def test_explicit_minio_is_kept_after_milvus_is_deselected(self):
        self.w.component_checks['minio'].setChecked(True)
        self.w.component_checks['milvus'].setChecked(True);self.w.component_checks['milvus'].setChecked(False)
        self.assertEqual(self.w.component_selection(),['minio'])
        self.assertTrue(self.w.component_checks['minio'].isChecked());self.assertFalse(self.w.component_checks['etcd'].isChecked())
    def test_scope_changes_invalidate_preflight_and_consent(self):
        self.w.check_pass=True;self.w.consent.setChecked(True)
        self.w.set_component_selection(['redis'])
        self.assertFalse(self.w.check_pass);self.assertFalse(self.w.consent.isChecked())
        self.assertFalse(self.w.component_rows['redis_password'].isHidden())
        self.assertTrue(self.w.component_rows['mysql_password'].isHidden())
    def test_import_and_dispatch_preserve_component_subset(self):
        import test_workflow
        value=test_workflow.settings();value['components']={'selected':['mysql'],'mysql_password':'fixture-password-123'}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'onsite.json';path.write_text(json.dumps(value))
            with mock.patch('wizard.QFileDialog.getOpenFileName',return_value=(str(path),'')):
                self.w.import_workflow_settings()
        self.assertEqual(self.w.component_selection(),['mysql'])
        self.w.preview=False
        with mock.patch('wizard.QProcess') as process:
            self.w.dispatch('install');request=process.return_value;request.started.connect.call_args.args[0]()
            payload=json.loads(request.write.call_args.args[0])
        self.assertEqual(payload['workflow']['components'],value['components'])
    def test_resume_locks_original_component_selection(self):
        self.w.latest={'selected_models':['copaw-flash-9b'],'requested_components':['milvus'],'selected_components':['etcd','minio','milvus']}
        self.w.edit_source_settings()
        self.assertEqual(self.w.component_selection(),['milvus'])
        self.assertFalse(self.w.component_group.isEnabled())

    def test_card_count_controls_qwen36_checkbox(self):
        for count,number in [(1,26),(2,27)]:
            self.w.device_count=count;self.w.set_all_models(True)
            selected=self.w.selected_models()
            self.assertEqual(len(selected),number)
            self.assertEqual('qwen3.6-35b-a3b' in selected,count==2)

    def test_online_preflight_only_probes_matching_projector_repository(self):
        for count,repo in [(1,'aistore/qwen3-8-27b'),(2,'aistore/qwen3-8-27b-2')]:
            subset=wizard.selected_catalog(self.w.catalog,['qwen3.8-27b'],count)
            rows=[dict(r,source='remote') for r in subset['resources'] if r['category']=='models']
            with mock.patch('registry.RegistryClient') as client:
                checked=wizard.check_online_model_sources(self.w.sources,{'rows':rows})
            self.assertEqual(len(checked),2)
            self.assertEqual({call.args[0]['repository'] for call in client.call_args_list},{repo,'aistore/qwen3-8-27b-mmproj'})
    def test_online_dispatch_preserves_bundled_source_marker(self):
        self.w.preview=False
        with mock.patch('wizard.QProcess') as process:
            self.w.dispatch('install')
            request=process.return_value
            request.started.connect.call_args.args[0]()
            payload=json.loads(request.write.call_args.args[0])
        roots=payload['sources']['local_roots']
        self.assertEqual(roots[0],'@bundle/support-files')
        self.assertTrue(all(Path(root).is_absolute() for root in roots[1:]))
    def test_default_online_source_and_all_selected(self):
        self.assertTrue(self.w.online.isChecked());self.assertFalse(self.w.local.isChecked())
        self.assertEqual(len(self.w.selected_models()),26)
        self.assertEqual(self.w.collect_sources()['model_source'],'online')
        self.assertEqual(self.w.collect_sources()['mode'],'prefer-local')
        self.assertTrue(self.w.library_hint.isHidden())
        self.w.local.setChecked(True)
        self.assertEqual(self.w.collect_sources()['model_source'],'local')
        self.assertIn(str(self.library),self.w.library_hint.text())
    def test_model_table_checkbox_changes_selection(self):
        self.w.models_table.item(0,0).setCheckState(Qt.Unchecked)
        self.assertEqual(len(self.w.selected_models()),25)
        self.assertFalse(self.w.check_pass)
    def test_model_checkbox_is_clickable(self):
        self.w.show_page(1);self.w.show();APP.processEvents()
        rect=self.w.models_table.visualItemRect(self.w.models_table.item(0,0))
        QTest.mouseClick(self.w.models_table.viewport(),Qt.LeftButton,pos=QPoint(rect.left()+10,rect.center().y()))
        self.assertEqual(len(self.w.selected_models()),25)
    def test_empty_selection_blocks_next(self):
        self.w.show_page(1);self.w.set_all_models(False)
        self.assertFalse(self.w.next.isEnabled())
    def test_local_import_requires_folder(self):
        self.w.sources=dict(self.w.sources,mode='prefer-local',model_source='online',local_roots=['@bundle/support-files'])
        self.w.online.setChecked(True)
        self.w.local.setChecked(True)
        with self.assertRaises(wizard.SourceError):self.w.collect_sources()
        self.w.local_path.setText(str(self.library))
        self.assertEqual(self.w.collect_sources()['model_source'],'local')
    def test_manual_library_choice_replaces_discovered_library(self):
        self.w.local.setChecked(True)
        other=Path(self.temporary.name)/'other-library';(other/'files').mkdir(parents=True)
        (other/'model-library.json').write_text(json.dumps({'release_id':self.w.release['release_id'],'files':[]}))
        self.w.local_path.setText(str(other))
        roots=self.w.collect_sources()['local_roots']
        self.assertIn(str(other),roots);self.assertNotIn(str(self.library),roots)
        self.w.local_path.setText(str(other/'files'))
        with self.assertRaises(wizard.SourceError):self.w.collect_sources()
    def test_relocated_media_is_discovered_without_a_fixed_mount_path(self):
        media=Path(self.temporary.name)/'different-mount';library=media/'AI-Platform-Model-Library'
        (library/'files').mkdir(parents=True)
        (library/'model-library.json').write_text(json.dumps({'release_id':self.w.release['release_id'],'files':[]}))
        config=dict(self.w.sources,local_roots=['../AI-Platform-Model-Library'])
        with mock.patch.dict('os.environ',AI_PLATFORM_SOURCE_BASE=str(media/'installer-source-base')):
            self.assertEqual(wizard.discovered_model_library(BUNDLE,config,self.w.release['release_id']),library)
    def test_online_check_reports_selected_registry_unavailable(self):
        path='files/model.gguf'
        config={'mode':'prefer-local','model_source':'online','timeout_seconds':2,'oci_artifacts':{'selected-model':{'files':{path:None}},
                                                     'other-model':{'files':{'other':None}}}}
        plan={'rows':[{'path':path,'category':'models','source':'remote','models':['selected-model']},
                      {'path':'other','category':'models','source':'local','models':['other-model']}]}
        with mock.patch('registry.RegistryClient') as client:
            client.return_value.verify_manifest.side_effect=RuntimeError('offline')
            rows=wizard.check_online_model_sources(config,plan)
        self.assertEqual(len(rows),1)
        self.assertFalse(rows[0]['ok'])
        self.assertIn('selected-model',rows[0]['name'])
    def test_check_result_is_its_own_step(self):
        self.w.show_page(1);self.w.go_next()
        self.assertEqual(self.w.page_index,2)
        self.assertEqual(self.w.stack.count(),7)
    def test_failed_check_blocks_next(self):
        self.w.show_page(2);self.w.apply_checks([{'name':'disk','ok':False,'detail':'full'}])
        self.assertFalse(self.w.next.isEnabled())
    def test_source_change_invalidates_check(self):
        self.w.local.setChecked(True);self.w.show_page(2);self.w.check_pass=True;self.w.online.setChecked(True)
        self.assertFalse(self.w.check_pass);self.assertFalse(self.w.next.isEnabled())
    def test_preview_install_disabled_even_with_consent(self):
        self.w.show_page(4);self.w.consent.setChecked(True);self.assertFalse(self.w.next.isEnabled())
    def test_real_install_requires_consent(self):
        self.w.preview=False;self.w.show_page(4);self.assertFalse(self.w.next.isEnabled())
        self.w.consent.setChecked(True);self.assertTrue(self.w.next.isEnabled())
    def test_password_mismatch_blocks_progress(self):
        self.w.show_page(3);self.w.password.setText('long-enough-password');self.w.password2.setText('different-password')
        with mock.patch('wizard.QMessageBox.warning') as message:self.w.go_next();message.assert_called_once()
        self.assertEqual(self.w.page_index,3)
    def test_password_match_proceeds(self):
        self.w.show_page(3);self.w.password.setText('long-enough-password');self.w.password2.setText('long-enough-password');self.w.go_next()
        self.assertEqual(self.w.page_index,4)
    def test_stale_task_can_resume(self):
        self.w.preview=False;self.w.service_active=False;self.w.last_service_check=time.time()
        state={'release_id':self.w.release['release_id'],'status':'running','updated_at':0}
        with mock.patch('wizard.read_json',return_value=state):self.w.poll()
        self.assertEqual(self.w.latest['status'],'failed');self.assertEqual(self.w.page_index,5);self.assertTrue(self.w.resume.isEnabled())
    def test_only_installed_state_shows_finish(self):
        self.w.preview=False;self.w.last_service_check=time.time()
        for status,page in [('failed',5),('paused',5),('installed',6)]:
            with mock.patch('wizard.read_json',return_value={'release_id':self.w.release['release_id'],'status':status}):self.w.poll()
            self.assertEqual(self.w.page_index,page)
    def test_progress_page_shows_current_download_speed(self):
        self.w.preview=False;self.w.last_service_check=time.time()
        state={'release_id':self.w.release['release_id'],'status':'running','current':'acquire',
               'updated_at':time.time(),'percent':4.3,'message':'下载：OCI/model-a',
               'transfer':{'phase':'下载','file':'OCI/model-a','file_bytes':10_000_000,
                           'file_total':20_000_000,'ready_bytes':10_000_000,'total_bytes':20_000_000}}
        with mock.patch('wizard.read_json',return_value=state),mock.patch.object(self.w.speed_meter,'label',return_value='下载速度：5.0 MB/s（近几秒）'):
            self.w.poll()
        self.assertEqual(self.w.page_index,5)
        self.assertEqual(self.w.download_speed.text(),'下载速度：5.0 MB/s（近几秒）')
    def test_resume_cannot_change_selected_models(self):
        self.w.latest={'selected_models':['copaw-flash-9b']};self.w.edit_source_settings()
        self.assertFalse(self.w.models_table.isEnabled())
        self.assertEqual(self.w.selected_models(),['copaw-flash-9b'])


if __name__=='__main__':unittest.main(verbosity=2)
