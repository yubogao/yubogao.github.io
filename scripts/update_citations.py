"""Update only citation data and keyed badges from one public Scholar profile."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.walk()

    def text(self):
        return ''.join(c.text() if isinstance(c, Element) else c for c in self.children).strip()

    def has_class(self, name):
        return name in self.attrs.get('class', '').split()


class Document(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Element('document', {})
        self.stack = [self.root]
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def citation_number(text):
    text = text.strip()
    if not text:
        return 0
    if not re.fullmatch(r'\d[\d,]*\*?', text):
        raise ValueError('Unrecognized Scholar citation count')
    return int(text.rstrip('*').replace(',', ''))


def parse_profile(source, mapping, timestamp=None):
    doc = Document(source)
    nodes = list(doc.root.walk())
    name = next((n.text() for n in nodes if n.attrs.get('id') == 'gsc_prf_in'), None)
    if name != mapping['expected_name']:
        raise ValueError('Expected public Scholar profile is unavailable; preserving existing data')
    stats = next((n for n in nodes if n.attrs.get('id') == 'gsc_rsb_st'), None)
    counts = [n.text() for n in stats.walk() if n.has_class('gsc_rsb_std')] if stats else []
    if not counts:
        raise ValueError('Scholar statistics are missing; preserving existing data')
    observed = {}
    for row in [n for n in nodes if n.has_class('gsc_a_tr')]:
        title = next((n for n in row.walk() if n.has_class('gsc_a_at')), None)
        count = next((n for n in row.walk() if n.has_class('gsc_a_ac')), None)
        if title is None or count is None:
            raise ValueError('Incomplete Scholar paper row')
        params = parse_qs(urlparse(title.attrs.get('href', '')).query)
        paper_id = params.get('citation_for_view', [None])[0]
        if paper_id and paper_id.startswith(mapping['scholar_user'] + ':'):
            if paper_id in observed:
                raise ValueError('Duplicate Scholar paper ID')
            observed[paper_id] = {'title': title.text(), 'citations': citation_number(count.text())}
    if not observed:
        raise ValueError('No Scholar publications found; preserving existing data')
    papers = {}
    for paper in mapping['papers']:
        found = observed.get(paper['scholar_id'])
        if found is None:
            raise ValueError(f"Scholar entry missing for {paper['key']}; preserving existing data")
        papers[paper['key']] = {'scholar_id': paper['scholar_id'], 'citations': found['citations']}
    now = timestamp or datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    return {'schema_version': 1, 'scholar_user': mapping['scholar_user'],
            'source_url': profile_url(mapping), 'updated_at': now,
            'total_citations': citation_number(counts[0]), 'papers': papers}


def profile_url(mapping):
    user = mapping['scholar_user']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', user):
        raise ValueError('Invalid Scholar user ID')
    # Scholar robots.txt permits a user-first URL without cstart pagination.
    return f'https://scholar.google.com/citations?user={user}&hl=en&pagesize=100'


def render_citations(page, data):
    replaced = {}
    source_title = escape('Google Scholar · Updated ' + data['updated_at'], quote=True)
    pattern = re.compile(r'<span class="cite-pill"(?=[^>]*data-citation-key="(?P<key>[a-z0-9-]+)")[^>]*>.*?<span class="cite-number">\d+</span></span>', re.S)

    def replace(match):
        key = match['key']
        if key == 'total':
            count, label = data['total_citations'], 'Total citations'
        else:
            count, label = data['papers'][key]['citations'], 'Citations'
        original = match.group()
        original = re.sub(r'aria-label="[^"]*"', f'aria-label="{label}: {count}"', original, count=1)
        start_end = original.index('>')
        start = re.sub(r' title="[^"]*"', '', original[:start_end])
        original = start + f' title="{source_title}"' + original[start_end:]
        original = re.sub(r'(<span class="cite-number">)\d+(</span>)', rf'\g<1>{count}\g<2>', original, count=1)
        replaced[key] = replaced.get(key, 0) + 1
        return original

    page = pattern.sub(replace, page)
    expected = {'total': 1, **{k: 2 for k in data['papers']}}
    if replaced != expected:
        raise ValueError('Citation badges do not match both publication views; preserving existing page')
    date = datetime.fromisoformat(data['updated_at'].replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y/%m/%d')
    page, timestamps = re.subn(r'(<span id="citation-updated"[^>]*>)[^<]*(</span>)', rf'\g<1>Citations updated {date}\g<2>', page)
    if timestamps != 1:
        raise ValueError('Citation update timestamp is missing')
    return page


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(content, encoding='utf-8', newline='\n')
    temporary.replace(path)


def update(root, html_file=None):
    mapping = json.loads((root / 'data/citation-map.json').read_text(encoding='utf-8'))
    if html_file:
        source = Path(html_file).read_text(encoding='utf-8')
    else:
        request = Request(profile_url(mapping), headers={'User-Agent': 'Mozilla/5.0 (compatible; YuboGaoCitationUpdater/1.0)'})
        with urlopen(request, timeout=30) as response:
            source = response.read(2_000_001)
        if len(source) > 2_000_000:
            raise ValueError('Unexpectedly large Scholar response')
        source = source.decode('utf-8')
    data = parse_profile(source, mapping)
    # Validate the entire result before touching either cached data or the page.
    rendered = render_citations((root / 'index.html').read_text(encoding='utf-8'), data)
    atomic_write(root / 'data/citations.json', json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    atomic_write(root / 'index.html', rendered)
    print(f"Updated Google Scholar citations: total {data['total_citations']}; {len(data['papers'])} mapped papers")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--html-file', type=Path, help='Use a saved profile for offline validation')
    args = parser.parse_args()
    try:
        update(args.root, args.html_file)
    except Exception as error:
        print(f'Citation update failed: {error}. Last successful published counts are unchanged.', file=sys.stderr)
        sys.exit(1)
