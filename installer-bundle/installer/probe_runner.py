"""Root probe console filter; the unmodified vendor binary keeps private file logs."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

SECRET_KEY=r'(?:bindActivateToken|[A-Za-z_]*(?:password|passwd|pwd|secret|secretKey|privateKey)|access[_-]?(?:token|key)|refresh[_-]?token|authorization|token)'
PATTERN=re.compile(r'(?i)(["\']?\b'+SECRET_KEY+r'["\']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|[^,}\]\r\n]+)')


def redact_line(line, values=()):
    for value in values:
        if len(value)>=4:
            line=line.replace(value,'<redacted>')
            line=line.replace(json.dumps(value,ensure_ascii=False)[1:-1],'<redacted>')
    return PATTERN.sub(lambda match:match[1]+'"<redacted>"',line)


def secret_values(path):
    text=Path(path).read_text('utf-8')
    values=[]
    try:
        value=json.loads(text)
        def walk(data):
            if isinstance(data,dict):
                for key,item in data.items():
                    if re.fullmatch(SECRET_KEY,key,re.I) and isinstance(item,str) and item:values.append(item)
                    else:walk(item)
        walk(value)
    except ValueError:
        for match in re.finditer(r'(?im)^\s*('+SECRET_KEY+r'):\s*(.+)$',text):
            raw=match[2].strip()
            try:value,_=json.JSONDecoder().raw_decode(raw)
            except ValueError:value=raw.split(' #',1)[0].strip(" '\"")
            if isinstance(value,str) and value:values.append(value)
    return values


def main():
    if len(sys.argv)!=2:raise SystemExit('Expected the probe configuration path')
    os.umask(0o077)
    values=secret_values(sys.argv[1])
    process=subprocess.Popen(['/usr/local/bin/data-probe',sys.argv[1]],stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1)
    def stop(signum,frame):
        if process.poll() is None:process.send_signal(signum)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        for line in process.stdout:print(redact_line(line,values),end='',flush=True)
    finally:
        process.stdout.close()
        if process.poll() is None:process.terminate()
    return process.wait()


if __name__=='__main__':raise SystemExit(main())
