"""Загрузка только стабильного релиза своего репозитория с привязкой к commit SHA."""
import ast
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import time
import urllib.request

REPO = 'mrkoster1988/Shop-VPN-'
API = 'https://api.github.com/repos/' + REPO
TAG = re.compile(r'v(0|[1-9][0-9]{0,5})\.([0-9]{3})')
SHA = re.compile(r'[a-f0-9]{40}')


def version(tag):
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError('invalid_version')
    return tuple(map(int, match.groups()))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('redirect_refused')


def download(url, limit, target=None):
    # URL строит только этот модуль; прокси окружения и перенаправления выключены.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent':'Pushok-Host-Updater', 'Accept':'application/vnd.github+json'})
    start = time.monotonic()
    with opener.open(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError('source_unavailable')
        raw = bytearray()
        while True:
            part = response.read(65536)
            if not part:
                break
            raw.extend(part)
            if len(raw) > limit or time.monotonic() - start > 120:
                raise ValueError('source_limit')
    if target:
        with Path(target).open('xb') as stream:
            stream.write(raw)
        return None
    return json.loads(raw)


def resolve(tag):
    version(tag)
    release = download(API + '/releases/tags/' + tag, 256 * 1024)
    if (release.get('tag_name') != tag or release.get('draft') is not False
            or release.get('prerelease') is not False or not release.get('published_at')):
        raise ValueError('not_stable_release')
    obj = download(API + '/git/ref/tags/' + tag, 16384)['object']
    for _ in range(5):
        if not SHA.fullmatch(obj.get('sha', '')):
            raise ValueError('invalid_commit')
        if obj.get('type') == 'commit':
            return obj['sha']
        if obj.get('type') != 'tag':
            break
        obj = download(API + '/git/tags/' + obj['sha'], 16384)['object']
    raise ValueError('invalid_commit')


def extract(archive, destination):
    """Ни ссылок, ни перезаписи файлов, ни extractall с путями из архива."""
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    seen, top, size = set(), set(), 0
    with tarfile.open(archive, mode='r:gz') as source:
        for index, member in enumerate(source):
            path = PurePosixPath(member.name)
            size += member.size
            if (index >= 2000 or size > 64 * 1024 * 1024 or path.is_absolute()
                    or '..' in path.parts or not path.parts or member.name in seen
                    or any(ord(ch) < 32 for ch in member.name)
                    or not (member.isfile() or member.isdir())):
                raise ValueError('invalid_archive')
            seen.add(member.name)
            top.add(path.parts[0])
            if len(top) != 1:
                raise ValueError('invalid_archive_root')
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with source.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 65536)
                target.chmod(0o600)
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError('invalid_archive_root')
    return roots[0]


def check_version(source, tag):
    for name in ('scripts/install.py', 'scripts/upgrade.py'):
        tree = ast.parse((source / name).read_text())
        values = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets)]
        if values != [tag]:
            raise ValueError('source_version_mismatch')
    for name in ('Dockerfile', 'app/main.py', 'scripts/backup.py'):
        if not (source / name).is_file():
            raise ValueError('incomplete_source')


def fetch(tag, work):
    commit = resolve(tag)
    work.mkdir(mode=0o700)
    archive = work / 'source.tar.gz'
    download('https://codeload.github.com/' + REPO + '/tar.gz/' + commit, 32 * 1024 * 1024, archive)
    source = extract(archive, work / 'unpacked')
    check_version(source, tag)
    archive.unlink()
    return source, commit
