"""Actual Harbor ranged download and actual offline model import; no inference claim."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def main():
    def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args],text=True).strip()
    commit=git('rev-parse','HEAD');dirty=bool(git('status','--porcelain'))
    spec=json.loads((ROOT/'release-config.json').read_text('utf-8'))
    home=ROOT/spec['resources_root']/'payload/home/cix/gateway-admin'
    sys.path.insert(0,str(home))
    from agent.model_store import Catalog,ModelStore,CHUNK
    from agent.registry import RegistryClient
    catalog=Catalog();cache=ROOT/'build/validation/harbor-120-cache';existing=ModelStore(cache,catalog)
    existing.fetch(['mobilenet_v2','glm-ocr-0.9b'])
    requests=[]
    class Observed(RegistryClient):
        def open(self,request,timeout=None):
            response=super().open(request,timeout)
            requests.append({'range_requested':request.get_header('Range'),'status':response.status,'blob':'/blobs/' in request.full_url})
            return response
    checks={}
    with tempfile.TemporaryDirectory(prefix='modeldeck-supply-',dir=ROOT/'build/validation') as folder:
        root=Path(folder);network=ModelStore(root/'network',catalog)
        item=catalog.models['mobilenet_v2']['files'][0]
        with existing.cached(item).open('rb') as src:network.cached(item).with_suffix('.part').write_bytes(src.read(min(CHUNK,item['size']//2)))
        with patch('agent.registry.RegistryClient',Observed):network.fetch(['mobilenet_v2'])
        checks['actual_harbor_resumed_blob_download']=network.ready(item) and any(r['range_requested'] and r['blob'] for r in requests)
        offline=ModelStore(root/'offline',catalog)
        offline.import_local(network.cached(item))
        checks['actual_raw_weight_import']=offline.ready(item)
        model=catalog.models['glm-ocr-0.9b'];source=model['files'][0]['oci_source']
        archive=existing.root/'cache'/source['blob_digest'][7:]
        offline.import_local(archive)
        checks['actual_harbor_archive_import_and_all_files_verified']=all(offline.ready(f) for f in model['files'])
        checks['main_model_and_mmproj_present']=len(model['files'])==2
    report={'schema':1,'source_commit':commit,'source_dirty':dirty or bool(git('status','--porcelain')) or git('rev-parse','HEAD')!=commit,
            'checked_at':time.time(),'checks':checks,'network_responses':requests,'passed':all(checks.values()),
            'hardware_inference':'not_run','full_catalog_download':'not_run','scope':'actual network and filesystem, no simulated registry'}
    path=ROOT/'build/validation/model-supply.json';path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
