#!/usr/bin/env bash
set -euo pipefail
# A fixed release prevents downloading a moving main branch during installation.
VERSION=v0.050
main() {
  local archive_version="$VERSION"
  command -v curl >/dev/null || { echo 'Нужен curl.' >&2; exit 1; }
  command -v python3 >/dev/null || { echo 'Нужен Python 3: apt-get update && apt-get install -y python3' >&2; exit 1; }
  stage=$(mktemp -d)
  trap 'rm -rf -- "$stage"' EXIT
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --max-time 120 --max-filesize 10485760 \
    "https://github.com/mrkoster1988/Shop-VPN-/archive/refs/tags/${archive_version}.tar.gz" -o "$stage/source.tar.gz"
  python3 - "$stage" <<'PY'
import pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1])
with tarfile.open(root/'source.tar.gz') as archive:
    members=archive.getmembers()
    if len(members)>2000 or sum(m.size for m in members)>32*1024*1024:
        raise SystemExit('Архив слишком большой')
    for m in members:
        p=pathlib.PurePosixPath(m.name)
        if p.is_absolute() or '..' in p.parts or not (m.isfile() or m.isdir()):
            raise SystemExit('Небезопасная структура архива')
    archive.extractall(root/'source',members=members)
roots=list((root/'source').iterdir())
if len(roots)!=1 or not (roots[0]/'scripts/install.py').is_file():
    raise SystemExit('Не найден установщик в архиве')
(root/'source-path').write_text(str(roots[0]))
PY
  local source
  source=$(cat "$stage/source-path")
  local script=install.py
  local args=("$@")
  if [[ "${args[0]:-}" == '--upgrade' ]]; then
    script=upgrade.py
    args=("${args[@]:1}")
  fi
  if [[ $EUID -eq 0 || " ${*} " == *' --plan '* ]]; then
    python3 "$source/scripts/$script" --source "$source" "${args[@]}"
  else
    sudo python3 "$source/scripts/$script" --source "$source" "${args[@]}"
  fi
}
main "$@"
