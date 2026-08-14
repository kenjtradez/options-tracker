#!/usr/bin/env python3
"""
Tiny local CORS proxy for the options levels tracker dashboard.

Run this alongside your `python -m http.server 8000` (in a SECOND terminal
window, leave both running). It listens on port 8001 and forwards any
request like:

    http://localhost:8001/proxy?url=<url-encoded target address>

to the real target server-side (where CORS doesn't apply), then returns
the response to your browser with permissive CORS headers added. This
avoids depending on flaky free public proxies like allorigins/corsproxy.io.

Special case: Yahoo Finance's options endpoint now requires a session
cookie and a security "crumb" token (an anti-bot measure). When the target
is a Yahoo Finance options URL, this proxy fetches that cookie+crumb once
(like a real browser session would) and attaches it automatically.

No third-party packages required - uses only Python's standard library.
"""

import urllib.request
import urllib.parse
import http.cookiejar
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8001
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

cookie_jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

_crumb_cache = {'crumb': None}


def get_yahoo_crumb():
    """Fetch a Yahoo Finance session cookie + crumb token once, cache it."""
    if _crumb_cache['crumb']:
        return _crumb_cache['crumb']
    try:
        req1 = urllib.request.Request('https://fc.yahoo.com', headers={'User-Agent': UA})
        opener.open(req1, timeout=10).read()
    except Exception:
        pass  # cookie priming best-effort; crumb fetch below may still work
    req2 = urllib.request.Request('https://query2.finance.yahoo.com/v1/test/getcrumb', headers={'User-Agent': UA})
    crumb = opener.open(req2, timeout=10).read().decode('utf-8').strip()
    _crumb_cache['crumb'] = crumb
    return crumb


def is_yahoo_options_url(url):
    return 'finance.yahoo.com' in url and '/v7/finance/options/' in url


def add_crumb(url, crumb):
    sep = '&' if '?' in url else '?'
    return url + sep + 'crumb=' + urllib.parse.quote(crumb)


class ProxyHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != '/proxy':
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(b'Use /proxy?url=<target>')
            return

        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get('url', [None])[0]
        if not target:
            self.send_response(400)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(b'Missing url parameter')
            return

        try:
            if is_yahoo_options_url(target):
                crumb = get_yahoo_crumb()
                target = add_crumb(target, crumb)
                req = urllib.request.Request(target, headers={'User-Agent': UA, 'Accept': 'application/json'})
                with opener.open(req, timeout=15) as resp:
                    body = resp.read()
                    content_type = resp.headers.get('Content-Type', 'application/json')
                    status = resp.status
            else:
                req = urllib.request.Request(target, headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read()
                    content_type = resp.headers.get('Content-Type', 'application/json')
                    status = resp.status

            self.send_response(status)
            self._send_cors_headers()
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(502)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(('{"error": "%s"}' % str(e).replace('"', "'")).encode('utf-8'))

    def log_message(self, format, *args):
        print('[proxy]', format % args)


if __name__ == '__main__':
    server = HTTPServer(('localhost', PORT), ProxyHandler)
    print('Local CORS proxy running at http://localhost:%d/proxy?url=<target>' % PORT)
    print('Leave this window open. Press Ctrl+C to stop.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
