"""Run UI contracts in local Chrome; stamp exact source and explicit fixture scope."""
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def main():
    def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args],text=True).strip()
    commit=git('rev-parse','HEAD');dirty=bool(git('status','--porcelain'))
    frontend=ROOT/'platform-overlays/1.2.0/modeldeck-frontend'
    command=['npx.cmd' if os.name=='nt' else 'npx','playwright','test','tests-e2e/ops-120.spec.ts','--workers=1','--reporter=json']
    env=dict(os.environ,MODELDECK_BASE_URL='http://127.0.0.1:18713')
    result=subprocess.run(command,cwd=frontend,env=env,capture_output=True,text=True,encoding='utf-8')
    folder=ROOT/'build/validation';folder.mkdir(parents=True,exist_ok=True)
    (folder/'modeldeck-ui-playwright.json').write_text(result.stdout,encoding='utf-8')
    if result.returncode:print(result.stdout);print(result.stderr);raise SystemExit(result.returncode)
    stats=json.loads(result.stdout)['stats']
    screenshots=[]
    for p in (frontend/'test-results').rglob('*.png'):
        target=folder/p.name;shutil.copyfile(p,target);screenshots.append(target.name)
    report={'source_commit':commit,'source_dirty':dirty or bool(git('status','--porcelain')) or git('rev-parse','HEAD')!=commit,
            'tests':stats,'passed':stats['expected']==3 and stats['unexpected']==0 and stats['skipped']==0,
            'scope':'local Chrome UI contract tests with simulated API; hardware and Docker not used','screenshots':screenshots}
    (folder/'modeldeck-ui.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
