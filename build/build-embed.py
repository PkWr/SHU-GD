#!/usr/bin/env python3
"""
build-embed.py — generates index.html from template.html + showcase.md.

Same pipeline as GD Digest, plus local media:
  1. Parses showcase.md entries (### DD MM·YY · Type · Title)
  2. Entries with *Image: media/xyz.jpg* → local file resized (≤1200px) and inlined as a data URI
  3. Entries with no Image line → og:image fetched from the entry's first link (generic/logo images skipped)
  4. Injects the augmented markdown + build date at the BUILD_INJECT marker in template.html
  5. *Video: / *Video Small:* files → copied to code/assets/ (published alongside);
     *Poster:* → inlined as a data URI so it paints instantly
  6. Writes index.html (self-contained apart from code/assets/ videos) (no external dependencies except remote og:images)

Usage: python3 code/build-embed.py            (from anywhere)
       python3 code/build-embed.py --no-fetch (skip og:image fetching; offline builds)
"""

import base64, io, json, mimetypes, os, re, shutil, sys
from datetime import date
from urllib.request import urlopen, Request
from urllib.parse import urlparse
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# Content (vault) is the master; this script only reads it.
ROOT = os.environ.get('GD_SHOWCASE_CONTENT') or os.path.expanduser(
    '~/Vaults/2026-PkWr/70 Sandpit/Sandpit - GD Digest/Claude - GD Showcase')
SRC_HTML = os.path.join(HERE, 'template.html')
SRC_MD   = os.path.join(ROOT, 'showcase.md')
OUT_HTML = os.path.join(REPO, 'index.html')
ASSETS   = os.path.join(REPO, 'assets')   # published next to index.html (videos are too big to inline)
TIMEOUT, WORKERS, MAX_PX = 8, 12, 1200
SEP = ' · '

SKIP_RE = re.compile('|'.join([
    r'/logo[s\-_]', r'[\-_]logo\.', r'/icon[s\-_]', r'[\-_]icon\.', r'/favicon',
    r'placeholder', r'default[\-_]image', r'og[\-_]default', r'social[\-_]default', r'fallback',
]), re.IGNORECASE)


class OGParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.image = None; self._in_head = True
    def handle_starttag(self, tag, attrs):
        if not self._in_head: return
        if tag == 'body': self._in_head = False; return
        if tag != 'meta': return
        d = dict(attrs); prop = d.get('property', '') or d.get('name', '')
        if prop in ('og:image', 'og:image:url', 'twitter:image') and not self.image:
            self.image = (d.get('content') or '').strip()


def fetch_og_image(url):
    try:
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; GDShowcaseBot/1.0)', 'Accept': 'text/html'})
        with urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(65536).decode('utf-8', errors='replace')
        p = OGParser(); p.feed(raw); img = p.image
        if not img: return url, None
        if img.startswith('//'): img = 'https:' + img
        elif img.startswith('/'):
            u = urlparse(url); img = f'{u.scheme}://{u.netloc}{img}'
        return url, (None if SKIP_RE.search(img) else img)
    except Exception:
        return url, None


def local_to_data_uri(rel):
    """Resize a local image (path relative to project root) and return a data URI, or None."""
    path = os.path.join(ROOT, rel)
    if not os.path.isfile(path):
        print(f'  !  missing local image: {rel}'); return None
    try:
        from PIL import Image
        im = Image.open(path); im.thumbnail((MAX_PX, MAX_PX))
        if im.mode not in ('RGB', 'L'): im = im.convert('RGB')
        buf = io.BytesIO(); im.save(buf, 'JPEG', quality=80, optimize=True, progressive=True)
        data, mime = buf.getvalue(), 'image/jpeg'
    except ImportError:
        with open(path, 'rb') as f: data = f.read()
        mime = mimetypes.guess_type(path)[0] or 'image/jpeg'
        print('  (Pillow not installed — inlining original size)')
    return f'data:{mime};base64,' + base64.b64encode(data).decode()


def prepare_hero_media(md):
    """Copy *Video:* files to code/assets/ and inline *Poster:*; returns md with rewritten paths."""
    out = []
    for line in md.split('\n'):
        m = re.match(r'^\*(Video|Video Small|Poster):\s*(\S+)\*$', line.strip())
        if m and not re.match(r'^(https?:|data:)', m.group(2)):
            key, rel = m.groups(); src = os.path.join(ROOT, rel)
            if not os.path.isfile(src):
                print(f'  !  missing {key.lower()}: {rel}')
            elif key == 'Poster':
                uri = local_to_data_uri(rel)
                if uri: line = f'*Poster: {uri}*'; print(f'  ▣  poster {rel}')
            else:
                os.makedirs(ASSETS, exist_ok=True)
                dest = os.path.join(ASSETS, os.path.basename(rel))
                if not (os.path.isfile(dest) and os.path.getsize(dest) == os.path.getsize(src)):
                    shutil.copyfile(src, dest)
                line = f'*{key}: assets/{os.path.basename(rel)}*'
                print(f'  ▶  {key.lower()} {rel} ({os.path.getsize(src) / 1e6:.1f} MB)')
        out.append(line)
    return '\n'.join(out)


def parse_entries(md):
    entries, cur = [], None
    for line in md.split('\n'):
        s = line.strip()
        if s == '---': cur = None; continue
        if s.startswith('### ') and len(s[4:].split(SEP)) >= 3:
            cur = {'title': SEP.join(s[4:].split(SEP)[2:]).strip(), 'first_url': None, 'image': None}
            entries.append(cur); continue
        if not cur: continue
        m = re.match(r'^\*Image:\s*(\S+)\*$', s)
        if m: cur['image'] = m.group(1); continue
        if cur['first_url'] is None and not s.startswith('*Type:'):
            m = re.search(r'\[[^\]]+\]\((https?://[^)\s]+)\)', s)
            if m: cur['first_url'] = m.group(1)
    return entries


def augment(md, fetched, local):
    """Insert fetched og:images after *Type:* lines; swap local image paths for data URIs."""
    out, title = [], None
    for line in md.split('\n'):
        s = line.strip()
        if s.startswith('### ') and len(s[4:].split(SEP)) >= 3:
            title = SEP.join(s[4:].split(SEP)[2:]).strip()
        m = re.match(r'^\*Image:\s*(\S+)\*$', s)
        if m and m.group(1) in local:
            line = f'*Image: {local[m.group(1)]}*'
        out.append(line)
        if title and s.startswith('*Type:') and title in fetched:
            out.append(f'*Image: {fetched.pop(title)}*')
    return '\n'.join(out)


def main():
    no_fetch = '--no-fetch' in sys.argv
    html = open(SRC_HTML, encoding='utf-8').read()
    # Inline the self-hosted fonts referenced as url("fonts/…woff2")
    def font_uri(m):
        fp = os.path.join(HERE, 'fonts', m.group(1))
        if not os.path.isfile(fp): print(f'  !  missing font {m.group(1)}'); return m.group(0)
        return 'url("data:font/woff2;base64,' + base64.b64encode(open(fp, 'rb').read()).decode() + '")'
    html = re.sub(r'url\("fonts/([^"]+\.woff2)"\)', font_uri, html)
    md = prepare_hero_media(open(SRC_MD, encoding='utf-8').read())
    entries = parse_entries(md)
    print(f'Found {len(entries)} entries')

    # Local images
    local = {}
    for e in entries:
        img = e['image']
        if img and not re.match(r'^(https?:|data:)', img) and img not in local:
            uri = local_to_data_uri(img)
            if uri: local[img] = uri; print(f'  ▣  {img}')

    # og:images for entries with no image
    fetched = {}
    todo = [(e['title'], e['first_url']) for e in entries if not e['image'] and e['first_url']]
    if todo and not no_fetch:
        print(f'Fetching og:images for {len(todo)} entries…')
        by_url = {}
        for t, u in todo: by_url.setdefault(u, []).append(t)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for fut in as_completed([pool.submit(fetch_og_image, u) for u in by_url]):
                u, img = fut.result()
                for t in by_url[u]:
                    if img: fetched[t] = img
                    print(f'  {"✓" if img else "–"}  {t[:60]}')

    n_remote = len(fetched)
    md_aug = augment(md, fetched, local)
    stamp = date.today().strftime('%-d %b %Y') if os.name != 'nt' else date.today().strftime('%d %b %Y')

    start = html.index('\n// ── BUILD_INJECT ──')
    end = html.index('</script>', start)
    inject = f'\nconst INLINE_MD = {json.dumps(md_aug)};\ninitShowcase(INLINE_MD, {json.dumps(stamp)});\n'
    embed = html[:start] + inject + html[end:]
    with open(OUT_HTML, 'w', encoding='utf-8') as f: f.write(embed)
    print(f'\n✓ index.html written ({len(embed):,} chars) · {len(local)} local, {n_remote} remote images')


if __name__ == '__main__':
    main()
