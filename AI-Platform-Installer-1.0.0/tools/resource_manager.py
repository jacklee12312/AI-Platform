"""Inspect sources, prefetch offline media, and configure an address without rebuilding."""
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'installer'))
from artifacts import load_catalog,source_config,plan_sources,BlobStore,validate_url
from common import atomic_json

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['plan','fetch','set-source']);p.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--output',type=Path);p.add_argument('--local-root',action='append',default=[]);p.add_argument('--offline',action='store_true');p.add_argument('--models',nargs='*');p.add_argument('--category',choices=['platform','docker','runtime','images','models']);p.add_argument('--base-url');p.add_argument('--allow-http',action='store_true');a=p.parse_args()
    config=source_config(a.bundle)
    if a.action=='set-source':
        if not a.category or not a.base_url:p.error('set-source 需要 --category 和 --base-url')
        validate_url(a.base_url,a.allow_http or config['allow_http'])
        config['base_urls'][a.category]=[a.base_url];config['allow_http']=a.allow_http or config['allow_http']
        atomic_json(a.bundle/'sources.json',config);print('来源已更新；发行清单、文件摘要和模型版本保持不变。');return
    catalog=load_catalog(a.bundle)
    if a.models is not None:
        unknown=set(a.models)-set(catalog['models'])
        if unknown:p.error('未知模型：'+', '.join(sorted(unknown)))
        catalog=dict(catalog,resources=[r for r in catalog['resources'] if r['category']!='models' or set(r['models'])&set(a.models)])
    config['local_roots'].extend(str(Path(r).resolve()) for r in a.local_root)
    if a.offline:config['mode']='local-only'
    cache=a.output.resolve() if a.output else a.bundle/'download-cache'
    plan=plan_sources(a.bundle,catalog,config,cache)
    if a.action=='plan':print(json.dumps(plan,ensure_ascii=False,indent=2));return
    if not a.output:p.error('fetch 需要 --output，指定独立的离线资源目录')
    if cache==a.bundle.resolve() or cache.is_relative_to(a.bundle.resolve()):p.error('离线资源目录必须在轻量安装器目录之外')
    if not plan['ready']:raise SystemExit('资源不完整；先运行 plan 查看缺失文件并补充来源。')
    last=[0]
    def progress(row,n,phase):
        if time.monotonic()-last[0]>1:print(phase+' '+row['path']+' '+str(n)+'/'+str(row['size']),flush=True);last[0]=time.monotonic()
    store=BlobStore(a.bundle,config,cache,progress)
    for row in catalog['resources']:store.ensure(row)
    atomic_json(cache/'resource-store.json',{'schema':1,'release_id':catalog['release_id'],'complete':a.models is None,'selected_models':a.models,'verified_files':len(catalog['resources'])},0o644)
    print('资源准备完成：'+str(cache)+'；可作为本地源交付，尚未安装任何系统服务。')

if __name__=='__main__':main()
