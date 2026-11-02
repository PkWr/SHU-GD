#!/usr/bin/env python3
"""
fetch-instagram.py — pulls recent posts from @shu_graphics via the official
Instagram API (Instagram Login, `instagram_business_basic`) into a review queue.

What it does:
  1. Loads the long-lived access token (env IG_TOKEN, or ~/.config/gd-showcase/instagram.json)
  2. Refreshes the token if it's more than 7 days old (tokens last 60 days; refreshing resets the clock)
  3. Fetches the latest posts: caption, type, image, permalink, timestamp (carousels → first image,
     videos/reels → cover thumbnail)
  4. Downloads each new post's image to media/ig/<post-id>.jpg — Instagram's image URLs expire,
     so they're never linked directly
  5. Adds posts not seen before to ../instagram.md → ## New, for the upkeep run to turn into entries

It never edits showcase.md. Safe to run repeatedly.

Usage:  python3 code/fetch-instagram.py [--limit 25] [--since YYYY-MM-DD] [--check]
        --set-token  prompts for a new token (hidden input) and saves it — use after generating one in Meta
        --check      verifies the token and prints the account name (no writes except token refresh)
"""

import argparse, json, os, re, sys, time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import HTTPError

HERE = os.path.dirname(os.path.abspath(__file__))
# Content (vault) is the master; the token lives outside the repo.
ROOT = os.environ.get('GD_SHOWCASE_CONTENT') or os.path.expanduser(
    '~/Vaults/2026-PkWr/70 Sandpit/Sandpit - GD Digest/Claude - GD Showcase')
SECRETS = os.path.expanduser('~/.config/gd-showcase/instagram.json')
QUEUE = os.path.join(ROOT, 'instagram.md')
MEDIA_DIR = os.path.join(ROOT, 'media', 'ig')
API = 'https://graph.instagram.com'
REFRESH_AFTER_DAYS = 7
FIELDS = ('id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,'
          'children{media_type,media_url,thumbnail_url}')

QUEUE_HEADER = """# GD Showcase — Instagram Queue

Posts pulled from [@shu_graphics](https://www.instagram.com/shu_graphics/) by `code/fetch-instagram.py`.
The upkeep run turns worthwhile posts into entries in `showcase.md`, then moves them to **Processed**.
Don't edit the `<!-- ig:… -->` markers — they stop posts being added twice.

---

## New

## Processed
"""


def die(msg):
    print(f'✗ {msg}'); sys.exit(1)


def get_json(url, params):
    try:
        with urlopen(Request(f'{url}?{urlencode(params)}' if params else url), timeout=20) as r:
            return json.load(r)
    except HTTPError as e:
        try: err = json.load(e).get('error', {})
        except Exception: err = {}
        die(f'Instagram API error {e.code}: {err.get("message", e.reason)}'
            + ('\n  → token expired or revoked: generate a new one (INSTAGRAM-SETUP.md, step 5)'
               if err.get('code') == 190 else ''))


# ── Token ────────────────────────────────────────────────
def load_token():
    if os.environ.get('IG_TOKEN'):
        return {'access_token': os.environ['IG_TOKEN'], 'saved_at': time.time(), 'from_env': True}
    if not os.path.isfile(SECRETS):
        die(f'No token. Put it in {SECRETS} as {{"access_token": "…"}} '
            'or set IG_TOKEN. See INSTAGRAM-SETUP.md.')
    with open(SECRETS) as f: tok = json.load(f)
    if not tok.get('access_token'): die('instagram.json has no access_token')
    tok.setdefault('saved_at', 0)
    return tok


def save_token(tok):
    os.makedirs(os.path.dirname(SECRETS), exist_ok=True)
    with open(SECRETS, 'w') as f: json.dump(tok, f, indent=2)
    try: os.chmod(SECRETS, 0o600)
    except OSError: pass


def maybe_refresh(tok):
    age_days = (time.time() - tok['saved_at']) / 86400
    if tok.get('from_env') or age_days < REFRESH_AFTER_DAYS:
        return tok
    r = get_json(f'{API}/refresh_access_token',
                 {'grant_type': 'ig_refresh_token', 'access_token': tok['access_token']})
    tok = {'access_token': r['access_token'], 'saved_at': time.time(),
           'expires_at': time.time() + int(r.get('expires_in', 0))}
    save_token(tok)
    days = int(r.get('expires_in', 0)) // 86400
    print(f'↻ token refreshed — valid for {days} days')
    return tok


# ── Posts ────────────────────────────────────────────────
def fetch_posts(token, limit, since):
    posts, url = [], f'{API}/me/media'
    params = {'fields': FIELDS, 'limit': min(limit, 50), 'access_token': token}
    while url and len(posts) < limit:
        data = get_json(url, params)
        for p in data.get('data', []):
            if since and p['timestamp'][:10] < since: return posts
            posts.append(p)
        url, params = data.get('paging', {}).get('next'), None  # next URL carries its own params
    return posts[:limit]


def image_url(p):
    if p['media_type'] == 'CAROUSEL_ALBUM':
        kids = p.get('children', {}).get('data', [])
        if kids: p = kids[0]
    if p.get('media_type') == 'VIDEO':
        return p.get('thumbnail_url')
    return p.get('media_url')


def download(url, dest):
    if os.path.isfile(dest): return True
    if not url: return False
    try:
        with urlopen(Request(url, headers={'User-Agent': 'GDShowcase/1.0'}), timeout=30) as r:
            data = r.read()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(data)); im.thumbnail((1600, 1600))
            if im.mode not in ('RGB', 'L'): im = im.convert('RGB')
            im.save(dest, 'JPEG', quality=85, optimize=True)
        except ImportError:
            with open(dest, 'wb') as f: f.write(data)
        return True
    except Exception as e:
        print(f'  !  image download failed for {os.path.basename(dest)}: {e}')
        return False


# ── Queue ────────────────────────────────────────────────
def queue_block(p, img_rel):
    ts = datetime.strptime(p['timestamp'][:19], '%Y-%m-%dT%H:%M:%S')
    caption = (p.get('caption') or '').strip()
    quoted = '\n'.join('> ' + l if l.strip() else '>' for l in caption.splitlines()) or '> (no caption)'
    kind = {'CAROUSEL_ALBUM': 'carousel', 'VIDEO': 'video/reel', 'IMAGE': 'image'}.get(p['media_type'], p['media_type'].lower())
    return (f"<!-- ig:{p['id']} -->\n"
            f"### {ts:%d %m·%y} · {kind} · [post]({p['permalink']})\n"
            f"*Image: {img_rel or '(download failed)'}*\n\n{quoted}\n\n---\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=25)
    ap.add_argument('--since', help='only posts on/after YYYY-MM-DD')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--set-token', action='store_true')
    a = ap.parse_args()

    if a.set_token:
        import getpass
        t = getpass.getpass('Paste the Instagram access token (input hidden): ').strip()
        if not t: die('no token entered')
        save_token({'access_token': t, 'saved_at': time.time()})
        print(f'✓ saved to {SECRETS} — now run with --check')
        return

    tok = maybe_refresh(load_token())
    if a.check:
        me = get_json(f'{API}/me', {'fields': 'username,account_type,media_count', 'access_token': tok['access_token']})
        print(f"✓ connected as @{me.get('username')} · {me.get('account_type')} · {me.get('media_count')} posts")
        return

    posts = fetch_posts(tok['access_token'], a.limit, a.since)
    queue = open(QUEUE, encoding='utf-8').read() if os.path.isfile(QUEUE) else QUEUE_HEADER
    seen = set(re.findall(r'<!-- ig:(\d+) -->', queue))
    new = [p for p in posts if p['id'] not in seen]
    print(f'Fetched {len(posts)} posts · {len(new)} new')

    blocks = []
    for p in sorted(new, key=lambda p: p['timestamp'], reverse=True):
        dest = os.path.join(MEDIA_DIR, f"{p['id']}.jpg")
        ok = download(image_url(p), dest)
        blocks.append(queue_block(p, os.path.relpath(dest, ROOT) if ok else ''))
        print(f"  +  {p['timestamp'][:10]}  {(p.get('caption') or '')[:60].replace(chr(10), ' ')}")

    if blocks:
        queue = queue.replace('## New\n', '## New\n\n' + '\n'.join(blocks), 1)
        with open(QUEUE, 'w', encoding='utf-8') as f: f.write(queue)
        print(f'✓ {len(blocks)} posts added to instagram.md')


if __name__ == '__main__':
    main()
