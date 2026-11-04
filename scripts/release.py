"""Publish changelog releases. Uses only Python stdlib and the GitHub CLI."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

VERSION = r'(0|[1-9][0-9]*)\.([0-9]{3})'
TAG = re.compile(r'v' + VERSION + r'$')
HEADER = re.compile(r'^## (?:\[v?(' + VERSION + r')\]|v?(' + VERSION + r'))(?:\s+.*)?$')


def version(value):
    match = TAG.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def sections(text):
    """Read H2 version sections, ignoring headings inside Markdown code fences."""
    result, heading, body, fence = [], None, [], None
    for line in text.splitlines():
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        if fence is None and line.startswith('## '):
            if heading is not None:
                result.append((heading, '\n'.join(body).strip()))
            heading, body = line, []
        else:
            body.append(line)
    if heading is not None:
        result.append((heading, '\n'.join(body).strip()))
    entries = []
    for heading, body in result:
        if heading in ('## [Unreleased]', '## Unreleased', '## [Не выпущено]'):
            continue
        match = HEADER.fullmatch(heading)
        if not match:
            raise ValueError(f'Invalid changelog heading: {heading}')
        number = match[1] or match[4]
        if not body:
            raise ValueError(f'Empty changelog entry: {number}')
        entries.append(('v' + number, heading + '\n\n' + body))
    if not entries:
        raise ValueError('CHANGELOG.md contains no versions')
    keys = [version(tag) for tag, _ in entries]
    if len(set(keys)) != len(keys) or keys != sorted(keys, reverse=True):
        raise ValueError('Changelog versions must be unique and ordered newest first')
    return entries


def plan(text, releases):
    entries = sections(text)
    published = [version(r.get('tag_name', '')) for r in releases
                 if not r.get('draft') and not r.get('prerelease')]
    baseline = max((v for v in published if v is not None), default=(-1, -1))
    pending = [(tag, body) for tag, body in entries if version(tag) > baseline]
    if not pending:
        return None
    tag = pending[0][0]
    if any(r.get('tag_name') == tag for r in releases):
        raise ValueError(f'{tag} already exists as a draft or prerelease; resolve it before publishing')
    title = tag if len(pending) == 1 else f'Сводный релиз: изменения с {pending[-1][0]} по {tag}'
    return {'tag_name': tag, 'name': title,
            'body': '\n\n'.join(body for _, body in pending),
            'draft': False, 'prerelease': False, 'make_latest': 'true'}


def github(args, payload=None):
    result = subprocess.run(['gh', 'api', *args],
                            input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('GitHub API failed: ' + result.stderr.strip())
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('Releases may only run from main')
    repo = os.environ['GITHUB_REPOSITORY']
    sha = os.environ['GITHUB_SHA']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Invalid repository or commit SHA')
    pages = github(['--paginate', '--slurp', f'repos/{repo}/releases?per_page=100'])
    releases = [release for page in pages for release in page]
    payload = plan(Path('CHANGELOG.md').read_text(encoding='utf-8'), releases)
    if payload is None:
        print('No new changelog versions; nothing to publish.')
        return
    payload['target_commitish'] = sha
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    result = github(['--method', 'POST', f'repos/{repo}/releases', '--input', '-'], payload)
    print('Published: ' + result['html_url'])
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write(f"Published [{payload['tag_name']}]({result['html_url']})\n")


if __name__ == '__main__':
    main()
