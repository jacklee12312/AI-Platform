"""Verified in-place ModelDeck 1.1.2 -> 1.2.0 update; preserves model and database data."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import time


def sha(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--resources', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args=parser.parse_args()
    sys.path.insert(0,str(args.bundle/'installer'))
    from artifacts import load_catalog
    from common import no_symlink_parents
    catalog=load_catalog(args.bundle)
    baseline=json.loads((args.bundle/'catalog/upgrade-baseline.json').read_text())
    home=Path('/home/cix'); root=Path('/'); count=2 if Path('/dev/xh2a_ipu1').is_char_device() else 1
    rows={r['path']:r for r in catalog['resources']}
    old={r['path']:r for r in baseline['resources']}
    if not (home/'gateway-admin/.env').is_file() or not (home/'houmo-docker-deployment/state/model-installations.json').is_file():
        raise RuntimeError('未检测到完整的 1.1.2 平台，请使用完整安装器')
    changes=[]
    for relative,row in rows.items():
        if not relative.startswith('payload/home/cix/gateway-admin/') or '/tests/' in relative: continue
        destination=root/relative.removeprefix('payload/')
        no_symlink_parents(destination,root)
        if destination.is_symlink(): raise RuntimeError('升级目标不能是链接：'+str(destination))
        selected='variants/single/'+relative.removeprefix('payload/home/cix/') if count==1 else relative
        selected=selected if selected in rows else relative
        source=args.resources/selected; desired=rows[selected]
        if not source.is_file() or source.is_symlink() or sha(source)!=desired['sha256']:
            raise RuntimeError('升级资源校验失败：'+selected)
        if destination.exists():
            actual=sha(destination)
            if actual==desired['sha256']: continue
            prior=old.get(selected,old.get(relative))
            if not prior or actual!=prior['sha256']:
                raise RuntimeError('已有文件与 1.1.2 基线不同，请先核查本地修改：'+str(destination))
        changes.append((source,destination))
    unit=args.resources/'systemd/modeldeck-ops.service'
    if sha(unit)!=rows['systemd/modeldeck-ops.service']['sha256']: raise RuntimeError('运维服务定义校验失败')
    ui=args.resources/'runtime/ui-static.tar.gz'
    if sha(ui)!=rows['runtime/ui-static.tar.gz']['sha256']: raise RuntimeError('前端资源校验失败')
    print(json.dumps({'from':'1.1.2','to':'1.2.0','device_count':count,'changed_files':len(changes),'apply':args.apply}))
    if not args.apply: return
    if os.geteuid()!=0 or platform.machine() not in ('aarch64','arm64'):
        raise RuntimeError('请在 Linux ARM64 设备上使用 sudo 执行升级')
    backup=Path('/var/lib/modeldeck-ops')/('upgrade-backup-'+time.strftime('%Y%m%d%H%M%S'))
    backup.mkdir(parents=True,mode=0o700)
    if shutil.disk_usage(home).free < sum(s.stat().st_size for s,d in changes)*2+128*1024**2:
        raise RuntimeError('升级和备份空间不足，请先释放至少 128 MiB 额外空间')
    target=Path('/etc/systemd/system/modeldeck-ops.service')
    no_symlink_parents(target,root)
    if target.is_symlink() or (target.exists() and sha(target)!=sha(unit)):
        raise RuntimeError('已有运维服务定义不同，不自动覆盖')
    changes.append((unit,target))
    journal=[]
    for source,destination in changes:
        saved=None
        if destination.exists():
            saved=backup/destination.relative_to(root);saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(destination,saved)
        journal.append((destination,saved))
    dist=home/'gateway-admin/frontend/dist'
    no_symlink_parents(dist,home)
    if dist.is_symlink():raise RuntimeError('前端目录不能是链接')
    if dist.exists():shutil.copytree(dist,backup/'frontend-dist',symlinks=True)
    subprocess.run(['systemctl','stop','gateway-admin.service'],check=True)
    try:
        for source,destination in changes:
            destination.parent.mkdir(parents=True,exist_ok=True)
            temporary=destination.with_name(destination.name+'.upgrade-120')
            shutil.copyfile(source,temporary);temporary.chmod(0o644);temporary.replace(destination)
        no_symlink_parents(home/'model-imports',home)
        if (home/'model-imports').is_symlink():raise RuntimeError('导入目录不能是链接')
        (home/'model-imports').mkdir(exist_ok=True)
        import pwd
        user=pwd.getpwnam('cix');os.chown(home/'model-imports',user.pw_uid,user.pw_gid)
        dist.mkdir(parents=True,exist_ok=True)
        with tarfile.open(ui) as tar:
            for member in tar:
                name=Path(member.name)
                if not member.isfile() or name.is_absolute() or '..' in name.parts:
                    raise RuntimeError('前端资源包包含非法路径')
                output=dist/name;no_symlink_parents(output,home)
                if output.is_symlink():raise RuntimeError('前端目标不能是链接')
                output.parent.mkdir(parents=True,exist_ok=True)
                with tar.extractfile(member) as src,output.open('wb') as dest:shutil.copyfileobj(src,dest)
                output.chmod(0o644)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','enable','--now','modeldeck-ops.service'],check=True)
        subprocess.run(['systemctl','start','gateway-admin.service'],check=True)
    except Exception as exc:
        subprocess.run(['systemctl','stop','modeldeck-ops.service'],check=False)
        if any(destination==target and saved is None for destination,saved in journal):
            subprocess.run(['systemctl','disable','modeldeck-ops.service'],check=False)
        for destination,saved in reversed(journal):
            if saved is not None:shutil.copy2(saved,destination)
            elif destination.is_file() and not destination.is_symlink():destination.unlink()
        if (backup/'frontend-dist').is_dir():shutil.copytree(backup/'frontend-dist',dist,dirs_exist_ok=True,symlinks=True)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        restored=subprocess.run(['systemctl','start','gateway-admin.service'],check=False).returncode==0
        print('升级未完成，已恢复原文件。原管理服务'+('已启动' if restored else '启动失败，请检查日志')+'；备份：'+str(backup),file=sys.stderr)
        raise RuntimeError('升级已停止，请核查服务和备份后重试；模型、数据库和凭据未修改') from exc
    print('ModelDeck v1.2.0 已更新；请在浏览器刷新并检查组件、模型和探针状态。备份：'+str(backup))


if __name__=='__main__':main()
