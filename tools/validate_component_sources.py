"""Read-only verification of the exact ARM64 manifests used by on-demand pulls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
ACCEPT='application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'


def get(url, headers=None):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=headers or {}),timeout=45) as response:
                return response.read(4*1024**2)
        except OSError:
            if attempt==2:raise
            time.sleep(attempt+1)


def verify(row):
    repository=row['pinned'].split('@')[0]
    if '/' not in repository:repository='library/'+repository
    token=json.loads(get('https://auth.docker.io/token?'+urllib.parse.urlencode({
        'service':'registry.docker.io','scope':'repository:'+repository+':pull'})))['token']
    headers={'Authorization':'Bearer '+token,'Accept':ACCEPT}
    base='https://registry-1.docker.io/v2/'+repository
    raw=get(base+'/manifests/'+row['manifest_digest'],headers)
    if 'sha256:'+hashlib.sha256(raw).hexdigest()!=row['manifest_digest']:raise RuntimeError('Manifest changed: '+row['id'])
    manifest=json.loads(raw)
    if manifest['config']['digest']!=row['config_id'] or manifest['layers']!=row['layers']:raise RuntimeError('Image recipe changed: '+row['id'])
    raw=get(base+'/blobs/'+row['config_id'],headers)
    if 'sha256:'+hashlib.sha256(raw).hexdigest()!=row['config_id']:raise RuntimeError('Config changed: '+row['id'])
    config=json.loads(raw)
    if config['architecture']!='arm64' or config['os']!='linux' or config['rootfs']['diff_ids']!=row['rootfs_diff_ids']:
        raise RuntimeError('Image platform or filesystem changed: '+row['id'])
    return {'component':row['id'],'pinned':row['pinned'],'architecture':'arm64','passed':True}


def main():
    def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args],text=True).strip()
    commit=git('rev-parse','HEAD');dirty=bool(git('status','--porcelain'))
    rows=json.loads((ROOT/'installer-bundle/catalog/workflow.json').read_text('utf-8'))['images']
    with ThreadPoolExecutor(max_workers=3) as pool:checks=list(pool.map(verify,rows))
    report={'source_commit':commit,'source_dirty':dirty or bool(git('status','--porcelain')) or git('rev-parse','HEAD')!=commit,
            'checks':checks,'passed':len(checks)==6 and all(x['passed'] for x in checks),'full_layer_download':'not_run'}
    path=ROOT/'build/validation/component-sources.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
