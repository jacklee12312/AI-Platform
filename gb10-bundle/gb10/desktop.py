"""Ubuntu desktop entry; authorize only the concrete installation command."""
import json
from pathlib import Path
import subprocess
import webbrowser


def main():
    if Path('/etc/ai-platform-gb10/config.json').exists():
        webbrowser.open('http://127.0.0.1:17780')
        return
    result = subprocess.run(['zenity', '--forms', '--title=AI 平台 GB10 安装', '--width=680',
        '--text=选择盒子上已有的模型与缓存目录；安装会校验两套权重并保留原有 Docker 和驱动。',
        '--add-entry=模型根目录（内含两套模型子目录）', '--add-entry=推理缓存目录', '--add-entry=Tiktoken 缓存目录', '--separator=|'],
        capture_output=True, text=True)
    if result.returncode:
        return
    values = result.stdout.strip().split('|')
    if len(values) != 3 or any(not Path(value).is_absolute() for value in values):
        subprocess.run(['zenity', '--error', '--text=请填写三个绝对目录路径。'])
        return
    command = ['pkexec', '/usr/bin/ai-platform-gb10', 'install', '--model-root', values[0],
               '--cache-root', values[1], '--tiktoken-root', values[2]]
    worker = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    progress = subprocess.Popen(['zenity', '--progress', '--pulsate', '--no-cancel', '--auto-close',
                                  '--title=正在安装 AI 平台 GB10', '--text=正在校验模型与部署平台，请稍候。'],
                                 stdin=subprocess.PIPE, text=True)
    output = []
    for line in worker.stdout:
        output.append(line)
        try:
            progress.stdin.write('# ' + line.strip() + '\n')
            progress.stdin.flush()
        except BrokenPipeError:
            pass
    code = worker.wait()
    try:
        progress.stdin.write('100\n')
        progress.stdin.close()
    except BrokenPipeError:
        pass
    progress.wait()
    subprocess.run(['zenity', '--info' if code == 0 else '--error', '--width=680',
                    '--text=' + (''.join(output)[-1800:] or '安装未完成，请检查系统授权。')])
    if not code:
        webbrowser.open('http://127.0.0.1:17780')


if __name__ == '__main__':
    main()
