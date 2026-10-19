"""Native desktop wizard; preview mode cannot dispatch privileged operations."""
from __future__ import annotations
import argparse,json,os,subprocess,sys,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import request
from PySide6.QtCore import Qt,QThread,Signal,QTimer,QProcess,QProcessEnvironment,QUrl
from PySide6.QtGui import QColor,QFont,QDesktopServices,QFontDatabase
from PySide6.QtWidgets import (QApplication,QWidget,QFrame,QLabel,QVBoxLayout,QHBoxLayout,QPushButton,
 QStackedWidget,QLineEdit,QCheckBox,QProgressBar,QPlainTextEdit,QTableWidget,QTableWidgetItem,
 QHeaderView,QMessageBox,QFileDialog,QDialog,QRadioButton,QScrollArea,QTabWidget,QGridLayout)
from common import PRODUCT,PUBLIC_STATE,STEPS,UNIT,read_json,quick_checks,check_password,atomic_json,detect_device_count
from artifacts import load_catalog,source_config,source_roots,worker_source_roots,plan_sources,remote_candidates,oci_source,SourceError
from download_speed import DownloadSpeedMeter
from selection import selected_catalog,validate_selected,supported_on
from workflow import COMPONENTS,COMPONENT_DEPENDENCIES,requested_components,resolved_components
from probe import detect_probe,matching_probe

STYLE='''
QWidget {font-family: "Noto Sans CJK SC"; font-size: 14px; color:#1c2c43; background:#f6f8fc;}
QFrame#sidebar,QFrame#sidebar QLabel {background:#142c4c; color:#d4e0f3;}
QLabel#brand {font-size:12px; font-weight:700; color:#7dc9fc; letter-spacing:2px;}
QLabel#sideTitle {font-size:27px; font-weight:700; color:white;}
QLabel#step {padding:12px 10px; border-radius:7px; color:#adbed5;}
QLabel#step[active="true"] {background:#2a4e77; color:white; font-weight:700;}
QLabel#title {font-size:27px;font-weight:700;color:#152b49;}
QLabel#subtitle {color:#617188;font-size:13px;}
QLabel#cardTitle {font-size:17px;font-weight:700;}
QLabel#number {font-size:32px;font-weight:700;color:#216bce;}
QFrame#card {background:white;border:1px solid #dce4ef;border-radius:10px;}
QFrame#card QLabel {background:transparent;}
QLabel#notice {background:#eaf2ff;color:#31557e;border-radius:7px;padding:12px;}
QPushButton {background:white;border:1px solid #cbd7e5;border-radius:6px;padding:10px 20px;min-width:76px;}
QPushButton:hover {background:#edf3fc;border-color:#669ce1;}
QPushButton#primary {background:#216bce;border:1px solid #216bce;color:white;font-weight:700;}
QPushButton#primary:hover {background:#1857ad;}
QPushButton:disabled {background:#e9edf3;color:#99a5b4;border-color:#e0e6ee;}
QLineEdit {background:white;border:1px solid #cbd7e5;border-radius:6px;padding:10px;min-height:22px;}
QLineEdit:focus {border:1px solid #216bce;}
QProgressBar {border:0;border-radius:6px;background:#e2e9f3;min-height:15px;text-align:center;}
QProgressBar::chunk {background:#2477dc;border-radius:6px;}
QPlainTextEdit,QTableWidget {background:white;border:1px solid #dce4ef;border-radius:6px;selection-background-color:#e0edff;}
QHeaderView::section {background:#edf2f8;border:0;padding:8px;text-align:left;font-weight:600;}
QCheckBox {spacing:8px;} QCheckBox::indicator {width:18px;height:18px;}
'''

def label(text,name=None,wrap=True):
    w=QLabel(text);w.setWordWrap(wrap)
    if name:w.setObjectName(name)
    return w

def card(title,text):
    frame=QFrame();frame.setObjectName('card');layout=QVBoxLayout(frame);layout.setContentsMargins(18,15,18,15)
    layout.addWidget(label(title,'cardTitle'));layout.addWidget(label(text,'subtitle'));return frame


def valid_model_library(root,release_id):
    root=Path(root).expanduser().resolve()
    marker=root/'model-library.json'
    if not root.is_dir() or not (root/'files').is_dir() or not marker.is_file():return False
    try:return isinstance(json.loads(marker.read_text(encoding='utf-8')).get('files'),list)
    except (OSError,ValueError):return False


def discovered_model_library(bundle,config,release_id):
    return next((root for root in source_roots(bundle,config) if valid_model_library(root,release_id)),None)


def check_online_model_sources(config,plan):
    """Check selected remote model manifests or file headers before installation."""
    from registry import RegistryClient,RegistryError
    remote={row['path']:row for row in plan['rows'] if row['category']=='models' and row['source']=='remote'}
    if not remote:return []
    checks=[];covered=set();timeout=min(config.get('timeout_seconds',45),10)
    seen=set()
    for path,row in remote.items():
        chosen=oci_source(config,row)
        if chosen:
            name,source=chosen;covered.add(path)
            if name not in seen:checks.append(('oci',name,source));seen.add(name)
    for path,row in remote.items():
        if path not in covered:checks.append(('file',row['models'][0],(row,remote_candidates(config,row))))

    def verify(item):
        kind,name,source=item
        try:
            if kind=='oci':RegistryClient(source,timeout).verify_manifest()
            else:
                row,urls=source
                if not urls:raise RuntimeError('No URL')
                for url in urls:
                    try:
                        with request.urlopen(request.Request(url,method='HEAD'),timeout=timeout) as response:
                            if response.status==200 and int(response.headers.get('Content-Length','-1'))==row['size']:
                                break
                    except Exception:pass
                else:raise RuntimeError('No matching file URL')
        except Exception as exc:
            detail=str(exc) if isinstance(exc,RegistryError) else '仓库或模型文件暂不可用，请检查网络后重试。'
            return {'name':name+' 远端模型','ok':False,'detail':detail,'warning':False}
        return {'name':name+' 远端模型','ok':True,'detail':'清单或模型文件可访问；正式下载仍会核对 SHA-256。','warning':False}

    with ThreadPoolExecutor(max_workers=min(8,len(checks))) as pool:
        return list(pool.map(verify,checks))

class CheckThread(QThread):
    ready=Signal(object)
    def __init__(self,bundle,config,selected,components=None):
        super().__init__();self.bundle=bundle;self.config=config;self.selected=selected;self.components=components
    def run(self):
        try:
            device_count=detect_device_count()
            catalog=selected_catalog(load_catalog(self.bundle),self.selected,device_count);plan=plan_sources(self.bundle,catalog,self.config)
            missing=[r for r in plan['rows'] if r['source']=='missing']
            detail='全部资源都有来源；正式准备阶段逐一校验' if plan['ready'] else str(len(missing))+' 个资源缺少本地文件或远端地址：'+', '.join(sorted({m for r in missing for m in r['models']}))
            rows=[{'name':'资源来源','ok':plan['ready'],'detail':detail,'warning':False}]
            rows+=check_online_model_sources(self.config,plan)
            rows+=quick_checks(self.bundle,self.selected,self.components)
            self.ready.emit({'rows':rows,'plan':plan,'config':self.config,'selected':self.selected,'components':self.components,'device_count':device_count})
        except Exception as exc:self.ready.emit({'rows':[{'name':'来源 / 环境检查','ok':False,'detail':str(exc),'warning':False}],'plan':None,'config':self.config,'selected':self.selected,'components':self.components})

class Wizard(QWidget):
    def __init__(self,bundle,preview=False):
        super().__init__();self.bundle=Path(bundle);self.preview=preview;self.page_index=0;self.check_pass=False
        self.latest={};self.request=None;self.check_thread=None;self.pending=False;self.pause_pending=False;self.dispatch_time=0;self.last_service_check=0;self.service_active=True
        self.speed_meter=DownloadSpeedMeter()
        self.release=read_json(self.bundle/'release.json',{'release_id':'gui-dev','version':'1.4.0-dev.1'})
        self.sources=source_config(self.bundle);self.source_plan=None;self.editing_sources=False
        self.catalog=read_json(self.bundle/'catalog/release-catalog.json',{'models':[],'resources':[]})
        self.workflow_enabled=bool(self.catalog.get('workflow_sha256'))
        self.yiqiban_enabled=bool(self.release.get('yiqiban_resource_id'))
        self.component_requested=set();self.component_checks={};self.component_rows={}
        self.probe_installed=None;self.probe_detection_error=''
        if self.workflow_enabled:self.refresh_probe_detection()
        try:self.device_count=detect_device_count() if not preview else None
        except RuntimeError:self.device_count=None
        revision=self.release.get('debian_revision')
        self.setWindowTitle(PRODUCT+(' · Debian 修订 '+str(revision) if revision else ''));self.resize(1120,760);self.setMinimumSize(1000,700)
        outer=QHBoxLayout(self);outer.setContentsMargins(0,0,0,0);outer.setSpacing(0)
        sidebar=QFrame();sidebar.setObjectName('sidebar');sidebar.setFixedWidth(238);sl=QVBoxLayout(sidebar);sl.setContentsMargins(25,32,20,24)
        sl.addWidget(label('AI PLATFORM','brand'));sl.addSpacing(8);sl.addWidget(label('AI 平台\n安装向导','sideTitle'));sl.addSpacing(28)
        self.step_labels=[]
        for i,t in enumerate(['欢迎使用','选择组件与模型' if self.workflow_enabled else '选择模型与来源','检查结果','安装设置','确认安装','资源与安装','完成']):
            w=label(f'{i+1:02d}   {t}','step');self.step_labels.append(w);sl.addWidget(w)
        sl.addStretch();sl.addWidget(label('选择所需模型\n本地导入 / 联网下载','subtitle'));sl.addSpacing(12);sl.addWidget(label('CIX P1 · Houmo LQ50\nDebian 12 / ARM64','subtitle'))
        outer.addWidget(sidebar);body=QWidget();bl=QVBoxLayout(body);bl.setContentsMargins(30,26,30,20);bl.setSpacing(14)
        self.title=label('欢迎安装完整 AI 平台','title');self.subtitle=label('一次安装完整项目，不只是管理界面。','subtitle');bl.addWidget(self.title);bl.addWidget(self.subtitle)
        if preview:bl.addWidget(label('界面预览模式 · 不会执行安装，不代表实际安装已通过','notice'))
        self.stack=QStackedWidget();bl.addWidget(self.stack,1)
        self.build_welcome();self.build_choices();self.build_checks();self.build_settings();self.build_confirm();self.build_progress();self.build_finish()
        footer=QHBoxLayout();self.footer=label('按需获取资源 · 完整平台安装 · 断点继续','subtitle');footer.addWidget(self.footer,1)
        self.back=QPushButton('上一步');self.back.clicked.connect(self.go_back);footer.addWidget(self.back)
        self.next=QPushButton('下一步');self.next.setObjectName('primary');self.next.clicked.connect(self.go_next);footer.addWidget(self.next)
        self.exit_button=QPushButton('关闭');self.exit_button.clicked.connect(self.close);footer.addWidget(self.exit_button);bl.addLayout(footer);outer.addWidget(body,1)
        self.show_page(0);self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(1000)
        if not preview:self.poll()

    def page(self):
        widget=QWidget();layout=QVBoxLayout(widget);layout.setContentsMargins(0,5,0,5);layout.setSpacing(14);self.stack.addWidget(widget);return layout

    def build_welcome(self):
        v=self.page();stats=QHBoxLayout()
        stages=('2 阶段','组件库 → 模型') if self.probe_installed else ('3 阶段','探针 → 组件库 → 模型')
        if self.yiqiban_enabled:stages=('3 阶段','组件库 → 模型 → 翼起办') if self.probe_installed else ('4 阶段','探针 → 组件库 → 模型 → 翼起办')
        for number,title in [(str(len(self.catalog['models'])),'注册模型及服务'),(str(self.catalog['image_count']+(6 if self.workflow_enabled else 0)+(10 if self.yiqiban_enabled else 0)),'配套容器镜像'),stages if self.workflow_enabled else ('2 种','本地与远端来源')]:
            f=QFrame();f.setObjectName('card');l=QVBoxLayout(f);l.setContentsMargins(18,14,18,14);l.addWidget(label(number,'number'));l.addWidget(label(title,'subtitle'));stats.addWidget(f)
        v.addLayout(stats)
        v.addWidget(card('完整推理能力','LLM / 图文理解 / OCR / 语音识别与合成 / 向量与重排 / CV / Guard / 民主测评识别'))
        v.addWidget(card('从运行环境到管理入口','Docker 与 Compose、配套镜像和运行时、所选模型、API 网关、管理界面、Agent 和开机自启。'))
        if self.workflow_enabled:v.addWidget(label('按选择安装组件库和模型平台；依次完成服务检查，支持兼容组件复用。' if self.probe_installed else '本机没有探针时先完成必需的探针安装和注册，确认 MQTT 已连接；再安装勾选的组件库服务和模型。组件库可全部不选。','notice'))
        v.addWidget(label('安装器与资源分开交付，模型地址可在之后配置。\n支持本地文件、离线缓存与远端断点下载，资源齐全后再安装。\n要求厂家 Debian 12 ARM64 及匹配驱动。','notice'))
        b=QPushButton('查看完整安装清单');b.clicked.connect(self.show_catalog);v.addWidget(b,alignment=Qt.AlignLeft);v.addStretch()

    def build_choices(self):
        v=self.page()
        if self.workflow_enabled:
            self.choice_tabs=QTabWidget();v.addWidget(self.choice_tabs)
            models=QWidget();v=QVBoxLayout(models);v.setContentsMargins(0,10,0,0);v.setSpacing(14)
            self.choice_tabs.addTab(models,'模型与来源')
        v.addWidget(label('勾选要安装的模型；未勾选项不会下载、部署或参加验收。模型平台基础环境固定安装，至少需要一个 LQ50 主模型。Qwen3.8 自动选择单卡或双卡多模态权重；Qwen3.6-35B-A3B 仅支持双卡。','notice'))
        self.hardware_label=label('设备：'+('待检查' if self.device_count is None else f'{self.device_count} 卡；自动选择对应模型版本'),'subtitle')
        v.addWidget(self.hardware_label)
        self.models_table=QTableWidget(len(self.catalog['models']),3)
        self.models_table.setHorizontalHeaderLabels(['安装','模型 / 服务','模型文件'])
        self.models_table.verticalHeader().hide();self.models_table.setSelectionMode(QTableWidget.NoSelection)
        self.models_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.models_table.setColumnWidth(0,75);self.models_table.setColumnWidth(2,165)
        self.models_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        for index,model in enumerate(self.catalog['models']):
            check=QTableWidgetItem();check.setFlags(Qt.ItemIsEnabled|Qt.ItemIsUserCheckable);check.setCheckState(Qt.Checked if supported_on(self.catalog,model,self.device_count) else Qt.Unchecked)
            if not supported_on(self.catalog,model,self.device_count):check.setFlags(Qt.NoItemFlags)
            self.models_table.setItem(index,0,check)
            self.models_table.setItem(index,1,QTableWidgetItem(model+('（仅双卡）' if self.catalog.get('model_device_counts',{}).get(model)==[2] else '')))
            self.models_table.setItem(index,2,QTableWidgetItem(self.model_size_label(model)))
        self.models_table.itemChanged.connect(self.sources_changed);v.addWidget(self.models_table,1)
        actions=QHBoxLayout();all_button=QPushButton('全选');all_button.clicked.connect(lambda:self.set_all_models(True));actions.addWidget(all_button)
        none_button=QPushButton('清空');none_button.clicked.connect(lambda:self.set_all_models(False));actions.addWidget(none_button)
        self.selected_summary=label('已按设备卡数选择可用模型','subtitle');actions.addWidget(self.selected_summary,1);v.addLayout(actions)
        sources=QFrame();sources.setObjectName('card');source_layout=QVBoxLayout(sources)
        source_layout.addWidget(label('模型来源','cardTitle'))
        choices=QHBoxLayout();self.online=QRadioButton('联网下载');self.local=QRadioButton('本地导入')
        (self.local if self.sources.get('model_source')=='local' else self.online).setChecked(True)
        choices.addWidget(self.online);choices.addWidget(self.local);choices.addStretch();source_layout.addLayout(choices)
        self.local_path=QLineEdit();self.local_path.setPlaceholderText('选择 AI-Platform-Model-Library 顶层文件夹')
        self.local_path.setText('')
        local_row=QHBoxLayout();local_row.addWidget(self.local_path,1);browse=QPushButton('选择文件夹');browse.clicked.connect(self.add_source_root);local_row.addWidget(browse);source_layout.addLayout(local_row)
        self.library_hint=label('正在查找模型库…','subtitle');source_layout.addWidget(self.library_hint)
        source_layout.addWidget(label('联网下载使用预设仓库；本地导入可使用自动发现的模型库，也可手动选择任意磁盘上的完整模型库。','subtitle'))
        self.local_path.setVisible(self.local.isChecked());browse.setVisible(self.local.isChecked());self.library_hint.setVisible(self.local.isChecked());self.local_browse=browse
        self.online.toggled.connect(self.source_mode_changed);self.local_path.textChanged.connect(self.refresh_library_hint)
        self.refresh_library_hint()
        v.addWidget(sources)
        if self.workflow_enabled:self.build_component_choices()

    def build_component_choices(self):
        self.component_group=QWidget();v=QVBoxLayout(self.component_group)
        self.choice_tabs.addTab(self.component_group,'组件库（按需选择）')
        v.addWidget(label('勾选需要的组件；翼起办所需组件固定勾选，其余服务按需安装。选择 Milvus 时自动包含 MinIO 和 etcd。' if self.yiqiban_enabled else '勾选需要的组件；未勾选的服务不会安装、启动或参加验收。可以全部不选。选择 Milvus 时自动包含必需的 MinIO 和 etcd。','notice'))
        if self.yiqiban_enabled:v.addWidget(label('翼起办复用本组件库，MySQL、Redis、ParadeDB 必须安装；其他组件仍可按需选择。','notice'))
        base=QCheckBox('Docker / Compose（模型平台必需）');base.setChecked(True);base.setEnabled(False);v.addWidget(base)
        grid=QGridLayout()
        for index,(name,title) in enumerate(COMPONENTS.items()):
            check=QCheckBox(title);self.component_checks[name]=check
            check.toggled.connect(lambda checked,name=name:self.component_toggled(name,checked))
            grid.addWidget(check,index//2,index%2)
        v.addLayout(grid)
        row=QHBoxLayout()
        for title,selected in [('全选组件',list(COMPONENTS)),('清空组件',[])]:
            button=QPushButton(title);button.clicked.connect(lambda checked=False,selected=selected:self.set_component_selection(selected));row.addWidget(button)
        row.addStretch();v.addLayout(row)
        self.component_summary=label('','notice');v.addWidget(self.component_summary);v.addStretch()
        self.refresh_component_selection()

    def component_selection(self):
        from yiqiban_config import REQUIRED_COMPONENTS
        chosen=self.component_requested | (REQUIRED_COMPONENTS if self.yiqiban_enabled else set())
        return [name for name in COMPONENTS if name in chosen]

    def set_component_selection(self,selected):
        self.component_requested=set(requested_components({'selected':selected}))
        self.refresh_component_selection()

    def component_toggled(self,name,checked):
        if checked:self.component_requested.add(name)
        else:self.component_requested.discard(name)
        self.refresh_component_selection()

    def refresh_component_selection(self):
        requested=self.component_selection();selected=resolved_components({'selected':requested})
        required={dep for name in requested for dep in COMPONENT_DEPENDENCIES.get(name,())}
        from yiqiban_config import REQUIRED_COMPONENTS
        wing_required=REQUIRED_COMPONENTS if self.yiqiban_enabled else set();required |= wing_required
        for name,check in self.component_checks.items():
            check.blockSignals(True);check.setChecked(name in selected);check.setEnabled(name not in required);check.blockSignals(False)
            check.setText(COMPONENTS[name]+('（翼起办必需）' if name in wing_required else '（Milvus 必需）' if name in required else ''))
        text='所选组件：'+('、'.join(COMPONENTS[name] for name in selected) if selected else '无，将跳过组件库安装')
        automatic=[COMPONENTS[name] for name in selected if name not in requested]
        if automatic:text+='\n自动包含依赖：'+'、'.join(automatic)
        if hasattr(self,'component_summary'):self.component_summary.setText(text)
        if hasattr(self,'confirm_components'):self.confirm_components.setText(text)
        if hasattr(self,'component_credentials_hint'):
            self.component_credentials_hint.setText(text+'\n仅需填写所选组件的凭据。')
        for key,row in self.component_rows.items():
            name='minio' if key=='minio_user' else key.removesuffix('_password')
            row.setVisible(name in selected)
        if hasattr(self,'choice_tabs'):
            self.choice_tabs.setTabText(1,f'组件库（已选 {len(selected)} 项）')
        if hasattr(self,'consent'):self.consent.setChecked(False)
        self.sources_changed()

    def build_checks(self):
        v=self.page();self.check_hint=label('检查所选模型的来源、发行签名和盒子环境。','notice');v.addWidget(self.check_hint)
        self.table=QTableWidget(0,3);self.table.setHorizontalHeaderLabels(['检查项','结果','说明']);self.table.setEditTriggers(QTableWidget.NoEditTriggers);self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.verticalHeader().hide();self.table.setWordWrap(True);self.table.setColumnWidth(0,135);self.table.setColumnWidth(1,68);self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch);v.addWidget(self.table,1)
        self.source_summary=label('尚未检查来源。','subtitle');v.addWidget(self.source_summary)
        self.recheck=QPushButton('重新检查');self.recheck.clicked.connect(self.run_checks);v.addWidget(self.recheck,alignment=Qt.AlignLeft)

    def set_all_models(self,checked):
        self.models_table.blockSignals(True)
        for row,model in enumerate(self.catalog['models']):
            self.models_table.item(row,0).setCheckState(Qt.Checked if checked and supported_on(self.catalog,model,self.device_count) else Qt.Unchecked)
        self.models_table.blockSignals(False);self.sources_changed()

    def selected_models(self):
        values=validate_selected(self.catalog,[self.catalog['models'][row] for row in range(self.models_table.rowCount()) if self.models_table.item(row,0).checkState()==Qt.Checked])
        if self.yiqiban_enabled:
            from yiqiban_config import model_plan
            model_plan(values)
        return values

    def model_size_label(self,model):
        if self.device_count is not None and not supported_on(self.catalog,model,self.device_count):return '仅双卡可用'
        rows=[row for row in self.catalog['resources'] if model in row['models']]
        if self.device_count is None and self.catalog.get('model_device_counts',{}).get(model)==[2]:
            return f'双卡 {sum(row["size"] for row in rows)/1e9:.2f} GB'
        if self.device_count is None and any(row.get('device_count') for row in rows):
            sizes=[sum(row['size'] for row in rows if not row.get('device_count') or row['device_count']==count) for count in (1,2)]
            return f'单 {sizes[0]/1e9:.1f} / 双 {sizes[1]/1e9:.1f} GB'
        size=sum(row['size'] for row in rows if not row.get('device_count') or row['device_count']==self.device_count)
        return '平台内置' if size==0 else f'{size/1e9:.2f} GB'

    def source_mode_changed(self,*args):
        local=self.local.isChecked();self.local_path.setVisible(local);self.local_browse.setVisible(local);self.library_hint.setVisible(local);self.sources_changed()

    def refresh_library_hint(self,*args):
        chosen=self.local_path.text().strip()
        if chosen:
            path=Path(chosen).expanduser().resolve()
            self.library_hint.setText(('已选择模型库：' if valid_model_library(path,self.release['release_id']) else '请选择包含model-library.json 和 files 的模型库顶层目录：')+str(path))
        else:
            found=discovered_model_library(self.bundle,self.sources,self.release['release_id'])
            self.library_hint.setText('已自动发现模型库：'+str(found) if found else '未找到完整模型库；请手动选择 AI-Platform-Model-Library 顶层目录。')
        if hasattr(self,'next'):self.sources_changed()

    def collect_sources(self):
        config=json.loads(json.dumps(self.sources));config['model_source']='local' if self.local.isChecked() else 'online'
        config['mode']='local-only' if self.local.isChecked() and self.sources['mode']=='local-only' else 'prefer-local'
        roots=list(self.sources['local_roots']);chosen=self.local_path.text().strip()
        if self.local.isChecked():
            if chosen:
                path=Path(chosen).expanduser().resolve()
                if not valid_model_library(path,self.release['release_id']):
                    raise SourceError('请选择AI-Platform-Model-Library 顶层目录（内含 model-library.json 和 files）。')
                # An explicit choice replaces any automatically discovered model library.
                roots=[reference for reference,root in zip(roots,source_roots(self.bundle,config))
                       if not valid_model_library(root,self.release['release_id'])]
                roots.append(str(path))
            elif not discovered_model_library(self.bundle,config,self.release['release_id']):
                raise SourceError('未自动发现完整模型库；请选择 AI-Platform-Model-Library 顶层目录。')
        config['local_roots']=list(dict.fromkeys(roots))
        return source_config(self.bundle,config)

    def sources_changed(self,*args):
        self.check_pass=False;self.source_plan=None
        if hasattr(self,'selected_summary'):
            count=sum(self.models_table.item(row,0).checkState()==Qt.Checked for row in range(self.models_table.rowCount()))
            self.selected_summary.setText(f'已选 {count} / {self.models_table.rowCount()} 项')
        if hasattr(self,'check_hint'):self.check_hint.setText('选项已改变，请重新检查。')
        self.update_buttons()

    def add_source_root(self):
        folder=QFileDialog.getExistingDirectory(self,'选择 AI-Platform-Model-Library 顶层目录')
        if folder:self.local_path.setText(folder)

    def save_sources(self):
        try:
            config=self.collect_sources();atomic_json(Path(os.environ.get('AI_PLATFORM_SOURCES_FILE') or self.bundle/'sources.json'),config,0o644);self.sources=config
            QMessageBox.information(self,'来源已保存','设置保存在 sources.json；模型内容与发行签名保持不变。')
        except Exception as exc:QMessageBox.warning(self,'无法保存来源',str(exc))

    def edit_source_settings(self):
        self.editing_sources=True
        saved=self.latest.get('selected_models')
        if saved:
            self.models_table.blockSignals(True)
            for row,model in enumerate(self.catalog['models']):
                self.models_table.item(row,0).setCheckState(Qt.Checked if model in saved else Qt.Unchecked)
            self.models_table.blockSignals(False)
        self.models_table.setEnabled(False);self.show_page(1)
        if self.workflow_enabled:
            self.set_component_selection(self.latest.get('requested_components',self.latest.get('selected_components',list(COMPONENTS))))
            self.component_group.setEnabled(False)

    def build_settings(self):
        page=self.page();scroll=QScrollArea();scroll.setWidgetResizable(True);content=QWidget();v=QVBoxLayout(content);scroll.setWidget(content);page.addWidget(scroll)
        self.settings_scope=card('安装范围：探针、所选组件与所选模型' if self.workflow_enabled else '安装范围：平台基础组件 + 已选模型','组件镜像按勾选联网拉取，也可本地导入；Milvus 自动包含 MinIO 与 etcd。所需端口由安装器自动接管：停用旧服务、归档旧容器、备份隔离旧数据后安装；失败恢复对应旧环境。Docker 配套版本不一致时将自动卸载旧软件包并安装包内版本，保留数据，现有容器会短暂停止。' if self.workflow_enabled else '配套平台镜像、运行时、网关与管理界面固定安装；模型权重、模型服务和验收仅包含上一步勾选项。');v.addWidget(self.settings_scope)
        if self.yiqiban_enabled:
            v.addWidget(label('翼起办将在模型验收后安装，复用共享组件并自动配置模型网关。安装期逐项切换模型测试，结束后恢复原模式；日常仍按现有方式切换模型。首次管理员注册、许可证导入和业务验收需在部署后完成。','notice'))
            v.addWidget(label('翼起办访问 IP / 域名（留空自动识别本机默认网络地址）'))
            self.yiqiban_host=QLineEdit();self.yiqiban_host.setPlaceholderText('例如 192.168.1.47，不含协议和端口');v.addWidget(self.yiqiban_host)
        self.probe_fields={};self.component_fields={}
        if self.workflow_enabled:
            import_button=QPushButton('导入安装配置');import_button.clicked.connect(self.import_workflow_settings);v.addWidget(import_button,alignment=Qt.AlignLeft)
            self.probe_detection_notice=label('','notice');v.addWidget(self.probe_detection_notice)
            self.probe_install_panel=QWidget();probe_layout=QVBoxLayout(self.probe_install_panel);probe_layout.setContentsMargins(0,0,0,0)
            probe_layout.addWidget(label('安装数据探针（必装）','cardTitle'))
            probe_layout.addWidget(label('本机尚未安装数据探针。请填写设备注册信息及接入令牌，安装并连接成功后才会继续。','notice'))
            fields=[('platform_url','平台地址','http://36.111.34.121:15433'),('owner','负责人',''),('project','项目',''),('city','城市',''),('location','部署地点',''),('deploymentPurpose','部署用途','生产交付'),('accessPolicy','访问策略','允许公网'),('hardwareModel','设备型号','cix'),('bindActivateToken','接入令牌','')]
            for key,title,default in fields:
                probe_layout.addWidget(label(title));field=QLineEdit();field.setText(default);self.probe_fields[key]=field
                if key=='bindActivateToken':field.setEchoMode(QLineEdit.Password)
                probe_layout.addWidget(field)
            v.addWidget(self.probe_install_panel)
            v.addWidget(label('所选组件凭据','cardTitle'))
            self.component_credentials_hint=label('','notice');v.addWidget(self.component_credentials_hint)
            for key,title,default in [('minio_user','MinIO 用户名','minioadmin'),('mysql_password','MySQL root 密码',''),('redis_password','Redis 密码',''),('paradedb_password','ParadeDB 密码',''),('neo4j_password','Neo4j 密码',''),('minio_password','MinIO 密码（同时供 Milvus 使用）','')]:
                row=QWidget();row_layout=QVBoxLayout(row);row_layout.setContentsMargins(0,0,0,0)
                row_layout.addWidget(label(title));field=QLineEdit();field.setText(default);self.component_fields[key]=field
                if key!='minio_user':field.setEchoMode(QLineEdit.Password);field.setPlaceholderText('12–128 个字符')
                row_layout.addWidget(field);v.addWidget(row);self.component_rows[key]=row
            v.addWidget(label('安装后生成仅安装用户可读的组件连接配置；已有组件只在版本、挂载、端口和认证兼容时复用。','notice'))
            v.addWidget(label('模型管理账号','cardTitle'))
        v.addWidget(label('管理员密码（12–128 个字符）'));self.password=QLineEdit();self.password.setEchoMode(QLineEdit.Password);self.password.setPlaceholderText('设置管理界面的登录密码');v.addWidget(self.password)
        v.addWidget(label('再次输入密码'));self.password2=QLineEdit();self.password2.setEchoMode(QLineEdit.Password);v.addWidget(self.password2)
        self.show_password=QCheckBox('显示管理员密码');self.show_password.toggled.connect(lambda checked:[x.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password) for x in [self.password,self.password2]]);v.addWidget(self.show_password)
        v.addWidget(label('安装位置：/home/cix（与已验证部署配置一致）\n管理界面：17780　API 网关：17700\nAPI 和 Agent 密钥独立生成，不复用安装源盒子的密钥。','notice'));v.addStretch()
        if self.workflow_enabled:
            self.refresh_component_selection();self.refresh_probe_detection()

    def refresh_probe_detection(self):
        previous=(self.probe_installed,getattr(self,'probe_replacement_needed',False))
        try:self.probe_installed=detect_probe();self.probe_detection_error=''
        except (OSError,RuntimeError) as exc:self.probe_installed=None;self.probe_detection_error=str(exc)
        if hasattr(self,'probe_install_panel'):self.probe_install_panel.setVisible(self.probe_installed is False)
        if hasattr(self,'probe_detection_notice'):
            self.probe_detection_notice.setText(self.probe_detection_error)
            self.probe_detection_notice.setVisible(bool(self.probe_detection_error))
        existing=self.probe_installed is True
        expected=read_json(self.bundle/'catalog/workflow.json',{}).get('probe',{}).get('binary_sha256')
        replacement=existing and not matching_probe(expected)
        self.probe_replacement_needed=replacement
        if hasattr(self,'probe_detection_notice') and replacement and not self.probe_detection_error:
            self.probe_detection_notice.setText('已有探针与安装包摘要不同：将先备份，再替换程序和服务，保留原注册信息及配置。')
            self.probe_detection_notice.setVisible(True)
        if hasattr(self,'settings_scope'):
            self.settings_scope.layout().itemAt(0).widget().setText('安装范围：替换探针程序及服务、所选组件与所选模型' if replacement else '安装范围：所选组件与所选模型' if existing else '安装范围：必需探针、所选组件与所选模型')
        if hasattr(self,'confirm_overview'):
            self.confirm_overview.layout().itemAt(0).widget().setText('将替换探针程序及服务，并安装所选组件与模型' if replacement else '将安装所选组件与所选模型' if existing else '将安装必需探针、所选组件与所选模型')
            self.confirm_overview.layout().itemAt(1).widget().setText(('① 备份旧探针，替换包内程序和服务，保留原注册配置并验证 MQTT' if replacement else '① 检查现有基础环境' if existing else '① 安装必需探针，完成注册及 MQTT 连接检查')+'\n② 自动接管所选组件端口，备份隔离旧数据，安装并验证凭据与健康状态（未选则跳过）\n③ 所选模型、网关与管理中心安装及推理验收\n④ 平台联检')
        if hasattr(self,'consent'):
            self.consent.setText('我确认备份后替换探针程序及服务，保留原注册配置，并安装所选组件及模型。' if replacement else '我确认安装所选组件及模型，并授权接管所需端口、备份隔离旧数据及配置开机服务。' if existing else '我确认安装必需探针、所选组件及模型，并授权接管所需端口、备份隔离旧数据、设备注册和开机服务配置。')
            if previous!=(self.probe_installed,replacement):self.consent.setChecked(False)
        if self.yiqiban_enabled:
            if hasattr(self,'settings_scope'):
                title=self.settings_scope.layout().itemAt(0).widget();title.setText(title.text()+'、翼起办')
            if hasattr(self,'confirm_overview'):
                title=self.confirm_overview.layout().itemAt(0).widget();title.setText(title.text()+'及翼起办')
                details=self.confirm_overview.layout().itemAt(1).widget()
                details.setText(details.text().replace('④ 平台联检','④ 复用共享组件，部署翼起办并从应用容器逐项测试模型，恢复原运行模式\n⑤ 平台联检；首次管理员注册与许可证导入另行完成'))
            if hasattr(self,'consent'):
                probe='替换探针、' if replacement else '' if existing else '必需探针、'
                self.consent.setText('我确认安装'+probe+'所选组件、模型及翼起办，并授权端口接管、备份和业务库初始化。')

    def collect_workflow_settings(self):
        from workflow import validate_settings
        self.refresh_probe_detection()
        if self.probe_detection_error:raise ValueError(self.probe_detection_error)
        probe={'mode':'auto'}
        if not self.probe_installed:probe.update({key:field.text() for key,field in self.probe_fields.items()})
        return validate_settings({'probe':probe,
                                  'components':{'selected':self.component_selection(),**{key:field.text() for key,field in self.component_fields.items()}}},probe_installed=self.probe_installed)

    def import_workflow_settings(self):
        path,_=QFileDialog.getOpenFileName(self,'导入安装配置','','JSON (*.json)')
        if not path:return
        try:
            from workflow import validate_settings
            if Path(path).stat().st_size>262144:raise ValueError('接入配置文件过大')
            self.refresh_probe_detection()
            if self.probe_detection_error:raise ValueError(self.probe_detection_error)
            value=read_json(path);value=value.get('workflow',value)
            value=dict(value,probe=dict(value.get('probe') or {},mode='auto'))
            value=validate_settings(value,probe_installed=self.probe_installed)
            for key,field in self.probe_fields.items():field.setText(value['probe'].get(key,''))
            self.set_component_selection(value['components']['selected'])
            for key,field in self.component_fields.items():field.setText(value['components'].get(key,'minioadmin' if key=='minio_user' else ''))
        except Exception as exc:QMessageBox.warning(self,'导入失败',str(exc))

    def build_confirm(self):
        v=self.page();self.confirm_overview=card('将安装探针、所选组件与所选模型' if self.workflow_enabled else '将安装 AI 平台与所选模型',
            '① 探针注册和当前进程 MQTT 连接检查\n② 自动接管所选组件端口，备份隔离旧数据，安装并验证凭据与健康状态（未选则跳过）\n③ 所选模型、网关与管理中心安装及推理验收\n④ 三阶段联检，开启探针采集' if self.workflow_enabled else '① 准备所需资源，逐文件校验\n② 安装平台基础组件与勾选模型\n③ 导入配套镜像，配置账号、网关和开机自启\n④ 启动并验收勾选模型');v.addWidget(self.confirm_overview)
        if self.workflow_enabled:
            self.confirm_components=label('','notice');v.addWidget(self.confirm_components);self.refresh_component_selection()
        self.confirm_sources=label('来源与下载量将在检查后显示。','subtitle');v.addWidget(self.confirm_sources)
        v.addWidget(label('所选组件和平台端口会自动接管，旧数据与配置备份隔离后安装。\n清单之外的模型和其他项目保持不动。\n请保持电源和 U 盘连接；关闭此窗口不会终止后台安装。','notice'))
        self.consent=QCheckBox('我确认安装探针、所选组件、模型及翼起办，并授权所需端口接管、备份、业务库初始化和开机服务配置。' if self.yiqiban_enabled else '我确认安装探针、所选组件及模型，并授权接管所需端口、备份隔离旧数据、设备注册和开机服务配置。' if self.workflow_enabled else '我确认在本机安装所选模型及平台组件，并允许配置开机服务。');self.consent.setChecked(False);self.consent.toggled.connect(self.update_buttons);v.addWidget(self.consent)
        v.addWidget(label('点击“开始安装”后，系统会请求管理员授权。\n资源复制和真实推理验收可能持续较长时间；不自动重启或复位加速卡。','subtitle'));v.addStretch()
        if self.workflow_enabled:self.refresh_probe_detection()

    def build_progress(self):
        v=self.page();self.stage_title=label('等待启动安装','cardTitle');v.addWidget(self.stage_title)
        self.progress=QProgressBar();self.progress.setRange(0,1000);self.progress.setValue(0);self.progress.setTextVisible(False);v.addWidget(self.progress)
        self.percent=label('0% · 阶段进度','subtitle');v.addWidget(self.percent)
        self.detail=label('安装任务将在后台运行。','notice');self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse);v.addWidget(self.detail)
        self.transfer=label('资源准备与平台部署分阶段执行。','subtitle');v.addWidget(self.transfer)
        self.download_speed=label('下载速度：等待下载','subtitle');v.addWidget(self.download_speed)
        self.log=QPlainTextEdit();self.log.setReadOnly(True);self.log.setMaximumBlockCount(500);v.addWidget(self.log,1)
        row=QHBoxLayout();self.pause=QPushButton('安全暂停');self.pause.clicked.connect(self.request_pause);row.addWidget(self.pause)
        self.resume=QPushButton('继续安装');self.resume.clicked.connect(self.continue_task);row.addWidget(self.resume)
        self.report=QPushButton('导出诊断摘要');self.report.clicked.connect(self.export_report);row.addWidget(self.report)
        self.edit_sources=QPushButton('修改资源来源');self.edit_sources.clicked.connect(self.edit_source_settings);row.addWidget(self.edit_sources);row.addStretch();v.addLayout(row)

    def build_finish(self):
        v=self.page();self.finish_title=label('平台与翼起办模型联通检查通过，业务授权待完成' if self.yiqiban_enabled else '探针、组件库及模型平台已通过安装检查' if self.workflow_enabled else '平台与所选模型已安装并验收通过','cardTitle');v.addWidget(self.finish_title)
        v.addWidget(card('你的 AI 平台已就绪','所选模型资源安装完成。管理中心、API 网关和配套服务已经配置，可通过管理中心切换已安装模型。'))
        v.addWidget(label('管理中心：http://127.0.0.1:17780/\nAPI 网关：http://127.0.0.1:17700/v1\n使用安装时设置的管理员密码登录；局域网访问时改用盒子的 IP。','notice'))
        open_button=QPushButton('打开 AI 平台管理中心');open_button.setObjectName('primary');open_button.clicked.connect(lambda:None if self.preview else QDesktopServices.openUrl(QUrl('http://127.0.0.1:17780/')));v.addWidget(open_button,alignment=Qt.AlignLeft)
        if self.yiqiban_enabled:
            self.yiqiban_finish=label('翼起办模型联通报告将在安装完成后显示。','notice');v.addWidget(self.yiqiban_finish)
            for key,title in [('portal','打开翼起办门户'),('workflow','打开星辰工作流'),('paw','打开 QwenPaw 工作台')]:
                button=QPushButton(title);button.clicked.connect(lambda checked=False,key=key:None if self.preview else QDesktopServices.openUrl(QUrl(self.latest.get('yiqiban_urls',{}).get(key,''))));v.addWidget(button,alignment=Qt.AlignLeft)
        report=QPushButton('导出安装报告');report.clicked.connect(self.export_report);v.addWidget(report,alignment=Qt.AlignLeft)
        if self.workflow_enabled:
            connections=QPushButton('导出组件连接配置（含密码）');connections.clicked.connect(self.export_connections);v.addWidget(connections,alignment=Qt.AlignLeft)
        v.addWidget(label('已配置开机自启。离线重启验证需另行进行，不因本页成功而自动视为通过。','subtitle'));v.addStretch()

    def show_page(self,index):
        self.page_index=index;self.stack.setCurrentIndex(index)
        if self.workflow_enabled and index in (3,4):self.refresh_probe_detection()
        titles=['欢迎安装 AI 平台','选择组件、模型与来源' if self.workflow_enabled else '选择模型与来源','检查结果','设置安装参数','确认安装','准备资源与安装平台','安装完成']
        subtitles=['安装网关、管理中心及所需模型服务。','勾选所需模型，选择联网下载或本地导入。','确认来源与盒子的安装条件。','设置管理账号。','请确认范围与资源准备计划。','后台任务持续执行；更新安装器会保留任务进度，可继续安装。','已选模型服务已通过本次安装验收。']
        if self.workflow_enabled:
            subtitles[1]='按需勾选组件与模型，选择联网下载或本地导入。'
            subtitles[3]='填写所选组件密码及管理账号。' if self.probe_installed else '填写必需的探针注册信息、所选组件密码及管理账号。'
        self.title.setText(titles[index]);self.subtitle.setText(subtitles[index])
        for i,w in enumerate(self.step_labels):w.setProperty('active',i==index);w.style().unpolish(w);w.style().polish(w)
        self.update_buttons()

    def update_buttons(self):
        if not hasattr(self,'next'):return
        i=self.page_index;self.back.setVisible(i in [1,2,3,4]);self.back.setEnabled(not self.pending)
        self.next.setVisible(i!=5);self.next.setText('开始安装' if i==4 else '完成' if i==6 else '下一步')
        if i==2 and self.editing_sources:self.next.setText('使用新来源继续')
        selected=any(self.models_table.item(row,0).checkState()==Qt.Checked for row in range(self.models_table.rowCount()))
        self.next.setEnabled(not self.pending and (selected if i==1 else self.check_pass if i==2 else self.consent.isChecked() and not self.preview if i==4 else True))

    def go_back(self):
        if self.editing_sources:self.editing_sources=False;self.show_page(5)
        else:self.show_page(max(0,self.page_index-1))
    def go_next(self):
        i=self.page_index
        if i==0:self.show_page(1)
        elif i==1:
            self.show_page(2);self.run_checks()
        elif i==2:
            if self.editing_sources:self.dispatch('resume')
            else:self.show_page(3)
        elif i==3:
            try:
                check_password(self.password.text())
                if self.password.text()!=self.password2.text():raise ValueError('两次输入的密码不一致。')
                if self.workflow_enabled:self.collect_workflow_settings()
                self.show_page(4)
            except ValueError as e:QMessageBox.warning(self,'请检查安装设置',str(e))
        elif i==4:self.dispatch('install')
        elif i==6:self.close()

    def run_checks(self):
        if self.check_thread and self.check_thread.isRunning():return
        self.check_pass=False;self.recheck.setEnabled(False);self.update_buttons();self.check_hint.setText('正在检查环境，请稍候……')
        if self.preview:
            self.apply_checks([{'name':n,'ok':True,'detail':d,'warning':False} for n,d in [('勾选模型',f'{len(self.selected_models())} 项'),('目标平台','Debian 12 / ARM64'),('模型来源','预览：不请求真实网络'),('环境条件','预览：不执行真实检查')]]);return
        try:config=self.collect_sources();selected=self.selected_models()
        except Exception as exc:self.apply_checks([{'name':'资源来源','ok':False,'detail':str(exc)}]);return
        self.check_thread=CheckThread(self.bundle,config,selected,self.component_selection() if self.workflow_enabled else None);self.check_thread.ready.connect(self.apply_checks);self.check_thread.start()

    def apply_checks(self,rows):
        if isinstance(rows,dict):
            result=rows
            try:current=self.collect_sources();selected=self.selected_models()
            except Exception:current=None
            if current!=result['config'] or selected!=result['selected'] or (self.workflow_enabled and result.get('components')!=self.component_selection()):
                self.recheck.setEnabled(True);self.check_hint.setText('检查过程中来源已改变，请重新检查。');return
            self.device_count=result.get('device_count')
            self.hardware_label.setText('设备：'+('待检查' if self.device_count is None else f'{self.device_count} 卡；自动选择对应模型版本'))
            for index,model in enumerate(self.catalog['models']):
                check=self.models_table.item(index,0)
                enabled=supported_on(self.catalog,model,self.device_count)
                check.setFlags(Qt.ItemIsEnabled|Qt.ItemIsUserCheckable if enabled else Qt.NoItemFlags)
                if not enabled:check.setCheckState(Qt.Unchecked)
                self.models_table.item(index,2).setText(self.model_size_label(model))
            self.source_plan=result.get('plan');rows=result['rows']
            if self.source_plan:
                b=self.source_plan['bytes'];download=self.source_plan.get('remote_transfer_bytes',b['remote']);summary=f"待读本地 {b['local']/1e9:.2f} GB · 待下载约 {download/1e9:.2f} GB · 待校验缓存 {b['cache']/1e9:.2f} GB · 缺失 {b['missing']/1e9:.2f} GB"
                self.source_summary.setText(summary);self.confirm_sources.setText(summary)
        self.table.setRowCount(len(rows));self.check_pass=all(x['ok'] for x in rows)
        for i,r in enumerate(rows):
            values=[r['name'],'提示' if r.get('warning') else '通过' if r['ok'] else '阻止',r['detail']]
            for j,value in enumerate(values):
                item=QTableWidgetItem(value);item.setToolTip(value)
                if j==1:item.setForeground(QColor('#ae7213' if r.get('warning') else '#178358' if r['ok'] else '#bb3246'))
                self.table.setItem(i,j,item)
        self.table.resizeRowsToContents();self.recheck.setEnabled(True)
        self.check_hint.setText('基础环境检查通过；正式安装前还会校验所有文件和已有模型。' if self.check_pass else '存在阻止安装的问题。请按表格说明处理后重新检查。')
        self.update_buttons()

    def dispatch(self,action):
        if self.preview:return
        if self.request and self.request.state()!=QProcess.NotRunning:return
        self.pending=True;self.update_buttons();self.request=QProcess(self)
        env=QProcessEnvironment.systemEnvironment();env.remove('PYTHONPATH');env.remove('LD_LIBRARY_PATH');env.insert('PYTHONDONTWRITEBYTECODE','1');self.request.setProcessEnvironment(env)
        if action in ['install','resume']:
            try:payload={'sources':self.collect_sources(),'selected_models':self.selected_models(),'device_count':self.device_count}
            except Exception as exc:self.pending=False;self.update_buttons();QMessageBox.warning(self,'来源配置错误',str(exc));return
            if action=='install':payload['admin_password']=self.password.text()
            if action=='install' and self.yiqiban_enabled:
                from yiqiban_config import validate_host
                try:
                    host=self.yiqiban_host.text().strip()
                    payload['yiqiban']={'public_host':validate_host(host) if host else ''}
                except ValueError as exc:self.pending=False;self.update_buttons();QMessageBox.warning(self,'翼起办访问地址',str(exc));return
            if action=='install' and self.workflow_enabled:
                try:
                    previous=self.probe_installed;payload['workflow']=self.collect_workflow_settings()
                    if previous!=self.probe_installed:raise ValueError('本机探针状态已变化，请重新确认安装范围。')
                except ValueError as exc:self.pending=False;self.update_buttons();QMessageBox.warning(self,'安装配置不完整',str(exc));return
            payload['sources']['local_roots']=worker_source_roots(self.bundle,payload['sources'])
            secret=json.dumps(payload).encode()
            self.request.started.connect(lambda:(self.request.write(secret),self.request.closeWriteChannel()))
        else:self.request.started.connect(self.request.closeWriteChannel)
        self.request.finished.connect(lambda code,status:self.dispatched(action,code))
        self.request.errorOccurred.connect(lambda e:self.dispatch_error())
        self.request.start('pkexec',['/usr/bin/python3',str(self.bundle/'installer/service_entry.py'),action,'--bundle',str(self.bundle)])

    def dispatch_error(self):
        self.pending=False;self.update_buttons();QMessageBox.warning(self,'无法启动系统授权','请确认 pkexec 可用，并从盒子的图形桌面启动安装向导。')

    def dispatched(self,action,code):
        self.pending=False;self.update_buttons()
        if code:
            error=bytes(self.request.readAllStandardError()).decode(errors='replace').strip()
            QMessageBox.warning(self,'安装任务未启动',error or '系统授权被取消或后台任务启动失败。');return
        if action=='pause':self.pause_pending=True;self.detail.setText('已请求安全暂停；当前不可中断步骤完成后暂停。');return
        self.editing_sources=False;self.dispatch_time=time.time();self.password.clear();self.password2.clear();self.show_page(5);self.detail.setText('后台任务已启动，等待第一条进度……');self.poll()
        for key,field in self.component_fields.items():
            if key!='minio_user':field.clear()
        if self.probe_fields:self.probe_fields['bindActivateToken'].clear()

    def continue_task(self):
        if self.latest.get('can_resume',True):self.dispatch('resume')
        else:self.dispatch_time=0;self.latest={};self.show_page(3)

    def request_pause(self):
        if QMessageBox.question(self,'安全暂停','不会强杀正在进行的镜像导入或模型验收。到达安全检查点后暂停，是否继续？')==QMessageBox.Yes:self.dispatch('pause')

    def poll(self):
        if self.preview:return
        try:state=read_json(PUBLIC_STATE,{})
        except (OSError,json.JSONDecodeError):return
        if time.time()-self.last_service_check>5:
            self.last_service_check=time.time()
            try:
                result=subprocess.run(['systemctl','is-active',UNIT],capture_output=True,text=True,timeout=2)
                self.service_active=result.stdout.strip() in ['active','activating','deactivating']
            except (OSError,subprocess.TimeoutExpired):self.service_active=True
        if not state and self.dispatch_time and time.time()-self.dispatch_time>15 and not self.service_active:
            state={'release_id':self.release['release_id'],'status':'failed','message':'后台任务在创建安装记录前停止。可返回设置重新启动；管理员可查看 /var/lib/ai-platform-installer/private/worker.log。','can_resume':False}
        if not state or state.get('release_id')!=self.release['release_id']:return
        if state.get('status')=='running' and time.time()-state.get('updated_at',0)>15 and not self.service_active:
            state=dict(state,status='failed',message='后台任务已停止（例如断电或系统重启）。保留的安装记录可用于继续；不会自动回滚或重置设备。')
        self.latest=state;status=state.get('status');current=state.get('current','verify')
        if status=='installed':
            if self.yiqiban_enabled:
                urls=state.get('yiqiban_urls',{});report=state.get('yiqiban_acceptance',{})
                self.yiqiban_finish.setText('翼起办容器到模型网关联通测试：'+('通过' if report.get('passed') else '未通过')+
                    '\n'+'\n'.join(k+'：'+v for k,v in urls.items())+
                    '\n首次注册管理员并导入有效许可证后，再完成业务验收。\n独立工作台账号保存在 ~/AI-Platform-1.3-YiQiBan-connections.json（仅安装用户可读）。')
            if self.page_index!=6:self.show_page(6)
            return
        if status in ['running','paused','failed'] and self.page_index!=5 and not self.editing_sources:self.show_page(5)
        self.stage_title.setText(dict(STEPS).get(current,'安装任务'))
        if current=='integration' and state.get('probe_skipped'):self.stage_title.setText('联检组件库和模型平台')
        if current=='components' and state.get('probe_skipped'):self.stage_title.setText('安装所选组件库并检查健康状态')
        self.progress.setValue(int(float(state.get('percent',0))*10));self.percent.setText(f"{state.get('percent',0):g}% · {'执行中' if status=='running' else '已暂停' if status=='paused' else '安装失败'}")
        self.detail.setText(state.get('message',''));self.log.setPlainText('\n'.join(x['time']+'  '+x['message'] for x in state.get('events',[])));self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        t=state.get('transfer',{})
        self.transfer.setText(f"资源准备：{t.get('ready_bytes',0)/1e9:.2f} / {t.get('total_bytes',0)/1e9:.2f} GB；当前文件 {t.get('file_bytes',0)/1e6:.1f} / {t.get('file_total',0)/1e6:.1f} MB" if t else '全部资源准备完成后才进入部署，验收通过后才显示完成。')
        self.download_speed.setText(self.speed_meter.label(status,t))
        self.edit_sources.setEnabled(status in ['failed','paused'])
        self.pause.setEnabled(status=='running' and not self.pause_pending);self.resume.setEnabled(status in ['failed','paused'])
        self.resume.setText('继续安装' if state.get('can_resume',True) else '返回设置')
        if status!='running':self.pause_pending=False
        self.footer.setText('安装失败不等于全部撤销；修复原因后可继续。' if status=='failed' else '关闭窗口不停止后台任务；请保持电源和 U 盘连接。')

    def export_report(self):
        path,_=QFileDialog.getSaveFileName(self,'导出安装诊断摘要','AI平台安装报告.json','JSON (*.json)')
        if path:Path(path).write_text(json.dumps(self.latest or {'preview':self.preview,'release':self.release},ensure_ascii=False,indent=2),encoding='utf-8')

    def export_connections(self):
        if self.preview:return
        path,_=QFileDialog.getSaveFileName(self,'导出组件连接配置（包含现场密码）','AI-Platform-1.1-connections.json','JSON (*.json)')
        if not path:return
        try:
            if Path(path).is_symlink():raise ValueError('导出目标不能是符号链接')
            atomic_json(path,read_json('/home/cix/AI-Platform-1.1-connections.json'),0o600)
        except Exception as exc:QMessageBox.warning(self,'导出失败',str(exc))

    def show_catalog(self):
        m=read_json(self.bundle/'catalog/platform-manifest.json',{})
        d=QDialog(self);d.setWindowTitle('完整安装清单');d.resize(640,600);v=QVBoxLayout(d);text=QPlainTextEdit();text.setReadOnly(True)
        contents='可选模型及服务\n\n'+'\n'.join(m.get('models',[]))+'\n\n固定配套镜像\n\n'+'\n'.join(x['tag'] for x in m.get('images',[]))
        if self.workflow_enabled:
            workflow=read_json(self.bundle/'catalog/workflow.json')
            contents='安装顺序：探针 → 组件库 → 模型\n\n探针 '+workflow['probe']['version']+'\n组件库：Docker、Nginx、etcd\n'+'\n'.join(x['image']+'\n  '+x['manifest_digest'] for x in workflow['images'])+'\n\n'+contents
        if self.yiqiban_enabled:
            app=read_json(self.bundle/'yiqiban/manifest.json')
            contents=contents.replace('探针 → 组件库 → 模型','探针 → 组件库 → 模型 → 翼起办')
            contents+='\n\n翼起办应用镜像（复用 MySQL、Redis、ParadeDB）\n\n'+'\n'.join(x['tag']+'\n  '+x['id'] for x in app['images'])
        text.setPlainText(contents);v.addWidget(text);b=QPushButton('关闭');b.clicked.connect(d.accept);v.addWidget(b);d.exec()

    def closeEvent(self,event):
        if self.pending:
            QMessageBox.information(self,'正在进行系统授权','请先完成或取消系统授权，再关闭窗口。');event.ignore();return
        if self.check_thread and self.check_thread.isRunning():
            QMessageBox.information(self,'正在检查','环境检查即将完成，请稍后关闭。');event.ignore();return
        if self.latest.get('status')=='running' and not self.preview:
            if QMessageBox.question(self,'关闭安装窗口','后台安装会继续，稍后重新打开可查看进度。确认关闭窗口？')!=QMessageBox.Yes:event.ignore();return
        event.accept()

def main():
    p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parent.parent);p.add_argument('--preview',action='store_true');p.add_argument('--screen',type=int,default=0,choices=range(7));p.add_argument('--screenshot',type=Path);args=p.parse_args()
    if args.screenshot and not args.preview:raise SystemExit('截图参数只用于不会安装的预览模式')
    app=QApplication(sys.argv);app.setStyle('Fusion')
    font=args.bundle/'assets/fonts/NotoSansCJKsc-Regular.otf'
    if font.exists():
        font_id=QFontDatabase.addApplicationFont(str(font))
        families=QFontDatabase.applicationFontFamilies(font_id)
        if families:app.setFont(QFont(families[0],10))
    app.setStyleSheet(STYLE);w=Wizard(args.bundle,args.preview)
    if args.preview:
        w.show_page(args.screen)
        if args.screen==2:w.run_checks()
        if args.screen==5:
            w.stage_title.setText('安装所选模型与项目文件');w.progress.setValue(470);w.percent.setText('47% · 演示数据，不代表实际进度')
            w.detail.setText('示例：正在复制 Qwen3.8-27B 模型文件\n真实安装会显示当前文件与实际字节进度。')
            w.log.setPlainText('09:20:00  所选资源 SHA-256 校验通过（示例）\n09:22:10  运行时展开完成（示例）\n09:25:40  已有文件核对通过（示例）\n09:26:00  开始安装所选模型与项目文件（示例）');w.pause.setEnabled(False);w.resume.setEnabled(False)
    w.show()
    if args.screenshot:
        args.screenshot.parent.mkdir(parents=True,exist_ok=True)
        QTimer.singleShot(700,lambda:(w.grab().save(str(args.screenshot)),app.quit()))
    sys.exit(app.exec())

if __name__=='__main__':main()
