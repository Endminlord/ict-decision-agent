"""Bounded web retrieval with provider fallback and explicit degraded states.
No model-generated links are presented as retrieved evidence. Cache is caller-
owned (no module-level mutable state); optional disk cache is explicitly supplied.
"""
import hashlib
import html.parser
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


def _url(value):
    if value.startswith('//'):
        value = 'https:' + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.hostname and parsed.hostname.endswith('duckduckgo.com'):
        value = urllib.parse.parse_qs(parsed.query).get('uddg', [value])[0]
        parsed = urllib.parse.urlsplit(value)
    return value if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username else None


class _DuckParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.current = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and 'result__a' in attrs.get('class', '').split():
            self.current = _url(attrs.get('href', ''))
            self.text = []

    def handle_data(self, data):
        if self.current:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current:
            self.results.append({'title': ''.join(self.text).strip(), 'url': self.current, 'summary': ''})
            self.current = None


def _fetch(url, timeout):
    request = urllib.request.Request(url, headers={'User-Agent': 'ProjectResearch/1.7'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('response_too_large')
    return raw.decode('utf-8', errors='replace')


def _parse(provider, text):
    if provider == 'duckduckgo':
        parser = _DuckParser()
        parser.feed(text)
        return parser.results
    # Avoid entity expansion in untrusted XML even with bounded response size.
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ValueError('unsafe_xml')
    root = ET.fromstring(text)
    results = []
    for item in root.findall('.//item'):
        link = _url(item.findtext('link') or '')
        if link:
            results.append({'title': item.findtext('title') or link, 'url': link,
                            'summary': item.findtext('description') or ''})
    return results


def searcher_agent(query, llm=None, *, cache=None, cache_dir=None, ttl=3600, timeout=5, fetcher=None, clock=None):
    report = {'query': query if isinstance(query, str) else '', 'results': [], 'stub': False,
              'degraded': False, 'errors': [], 'cache_hit': False, 'source': None}
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        report.update(stub=True, degraded=True, reason='invalid_query')
        return {'searcher': report}
    query = query.strip()
    key = hashlib.sha256(query.encode('utf-8')).hexdigest()
    now = (clock or time.time)()
    cache = cache if cache is not None else {}
    path = Path(cache_dir) / (key + '.json') if cache_dir else None
    entry = cache.get(key)
    if entry is None and path:
        try:
            if not path.is_symlink() and path.stat().st_size <= 1024 * 1024:
                entry = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    if isinstance(entry, dict) and isinstance(entry.get('created'), (int, float)) and 0 <= now - entry['created'] < ttl:
        results = entry.get('results')
        if isinstance(results, list) and results and all(isinstance(r, dict) and _url(r.get('url', '')) for r in results):
            report.update(results=results[:5], source=entry.get('source'), cache_hit=True)
            return {'searcher': report}
    fetcher = fetcher or _fetch
    q = urllib.parse.quote(query)
    providers = [('duckduckgo', 'https://html.duckduckgo.com/html/?q=' + q),
                 ('bing_rss', 'https://www.bing.com/search?format=rss&q=' + q)]
    for provider, url in providers:
        try:
            text = fetcher(url, timeout)
            found, seen = [], set()
            for item in _parse(provider, text):
                if item['url'] not in seen:
                    seen.add(item['url'])
                    found.append(item)
            if not found:
                raise ValueError('no_parseable_results')
            report.update(results=found[:5], source=provider)
            entry = {'created': now, 'source': provider, 'results': found[:5]}
            cache[key] = entry
            if path:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    fd, tmp = tempfile.mkstemp(prefix='.search-', dir=path.parent)
                    try:
                        with os.fdopen(fd, 'w', encoding='utf-8') as f:
                            json.dump(entry, f, ensure_ascii=False)
                        os.replace(tmp, path)
                    finally:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                except OSError as exc:
                    report['errors'].append({'provider': 'cache', 'reason': type(exc).__name__})
            report['degraded'] = bool(report['errors'])
            return {'searcher': report}
        except Exception as exc:
            report['errors'].append({'provider': provider, 'reason': type(exc).__name__ + ': ' + str(exc)[:160]})
    report.update(degraded=True, reason='all_providers_failed')
    return {'searcher': report}
