"""Keep signed YiQiBan metadata in distributions; acquire the payload at install time."""
from pathlib import Path
import shutil


def copy_bundle(source, destination):
    source = Path(source)
    defaults = shutil.ignore_patterns('__pycache__', '*.pyc', 'installer-manifest.json', 'SHA256SUMS')
    def ignore(directory, names):
        omitted = set(defaults(directory, names))
        if Path(directory) == source / 'yiqiban' and (source / 'yiqiban/source.json').is_file():
            omitted.add('files')
        return omitted
    shutil.copytree(source, destination, ignore=ignore)
