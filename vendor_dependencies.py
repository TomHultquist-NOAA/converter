#!/usr/bin/env python3
"""Install self-hosted MapLibre 5.8.0 and *all* autumnplot-gl 4.1.1 dist assets.

Run this script on a machine with outbound Internet access, NOT on WCOSS2/RZDM.
Uses Python 3 standard library only.
"""
from pathlib import Path
from urllib.request import Request, urlopen
from io import BytesIO
import json
import tarfile

ROOT = Path(__file__).resolve().parent / 'vendor'
ROOT.mkdir(exist_ok=True)
USER_AGENT = 'OMD-EVU-Autumnplot/7.4'
MAPLIBRE = {
    'maplibre-gl-csp.js': 'https://cdn.jsdelivr.net/npm/maplibre-gl@5.8.0/dist/maplibre-gl-csp.js',
    'maplibre-gl-csp-worker.js': 'https://cdn.jsdelivr.net/npm/maplibre-gl@5.8.0/dist/maplibre-gl-csp-worker.js',
    'maplibre-gl.css': 'https://cdn.jsdelivr.net/npm/maplibre-gl@5.8.0/dist/maplibre-gl.css',
}
APGL_PACKAGE = 'https://registry.npmjs.org/autumnplot-gl/-/autumnplot-gl-4.1.1.tgz'

def download(url):
    with urlopen(Request(url, headers={'User-Agent': USER_AGENT}), timeout=180) as response:
        return response.read()

def check_file(name, data):
    if len(data) < 64 or data[:50].lstrip().lower().startswith((b'<html', b'<!doctype')):
        raise RuntimeError(f'Invalid download: {name}')
    return data

# The MapLibre CSP worker is a separate script and MUST be co-hosted.
for name, url in MAPLIBRE.items():
    print('Downloading MapLibre:', name, flush=True)
    data = check_file(name, download(url))
    (ROOT/name).write_bytes(data)
    print(' ', len(data), 'bytes', flush=True)

print('Downloading full autumnplot-gl npm distribution…', flush=True)
tarball = download(APGL_PACKAGE)
files = {}
with tarfile.open(fileobj=BytesIO(tarball), mode='r:gz') as archive:
    for member in archive.getmembers():
        if not member.isfile() or not member.name.startswith('package/dist/'):
            continue
        rel = member.name[len('package/dist/'):]
        # Avoid paths outside vendor, and exclude unnecessary source maps.
        if not rel or '/' in rel or '..' in rel or rel.endswith('.map'):
            continue
        if not rel.endswith(('.js', '.wasm')):
            continue
        with archive.extractfile(member) as f:
            files[rel] = check_file(rel, f.read())
if 'autumnplot-gl.js' not in files:
    raise RuntimeError('NPM package lacks dist/autumnplot-gl.js. Check npm version/package.')
workers = sorted(name for name in files if name != 'autumnplot-gl.js' and name.endswith('.js'))
if not workers:
    raise RuntimeError('No autumnplot-gl worker bundle found in npm dist/. Refusing incomplete installation.')
for name, data in sorted(files.items()):
    (ROOT/name).write_bytes(data)
    print(' autumnplot asset:',name,len(data),'bytes',flush=True)
(ROOT/'autumnplot-assets.json').write_text(json.dumps({'version':'4.1.1','files':sorted(files),'worker_candidates':workers},indent=2)+'\n')
print(f'COMPLETE: {len(files)} autumnplot-gl assets, including {len(workers)} worker JS file(s).')
print('Upload vendor/ as a whole to your RZDM web viewer directory.')
