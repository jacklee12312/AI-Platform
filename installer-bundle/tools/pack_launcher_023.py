"""Pack a sealed installer folder as a Python ZIP .run without overwriting output."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();bundle=args.bundle.resolve();output=args.output.resolve()
    if output.exists():raise SystemExit('目标安装器已存在，拒绝覆盖：'+str(output))
    manifest=json.loads((bundle/'installer-manifest.json').read_text(encoding='utf-8'))
    paths=sorted({row['path'] for row in manifest['files']} | {'sources.json','installer-manifest.json','SHA256SUMS'})
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream:stream.write(b'#!/usr/bin/env python3\n')
    with zipfile.ZipFile(output,'a',compression=zipfile.ZIP_STORED) as archive:
        epoch=int(os.environ.get('SOURCE_DATE_EPOCH','315532800'))
        timestamp=datetime.fromtimestamp(max(epoch,315532800),timezone.utc).timetuple()[:6]
        def add(path,name):
            info=zipfile.ZipInfo(name,date_time=timestamp);info.create_system=3;info.external_attr=0o100644<<16
            with path.open('rb') as src,archive.open(info,'w') as dst:shutil.copyfileobj(src,dst)
        add(bundle/'tools/launcher_entry.py','__main__.py')
        for name in paths:add(bundle/name,'bundle/'+name)
    with output.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    output.with_suffix('.run.sha256').write_text(digest+'  '+output.name+'\n',encoding='utf-8')
    shutil.copyfile(bundle/'sources.json',output.with_suffix('.sources.json'))
    print(json.dumps({'launcher':str(output),'bytes':output.stat().st_size,'sha256':digest},ensure_ascii=False))


if __name__=='__main__':main()
