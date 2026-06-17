#!/usr/bin/env python3
"""
HLS Stream Proxy Server for MovieBox/Aoneroom Sports API
==========================================================

This proxy solves the IP-locked HLS token problem by:
1. Making API calls AND stream requests from the SAME server IP
2. Rewriting .m3u8 playlists to route segments through the proxy
3. Caching segments to reduce bandwidth

The key insight: The sign= token is bound to the IP that requested it.
So both the API call and the HLS stream must come from the same IP.

Usage:
    python hls_proxy_server.py
    
Environment Variables:
    API_BASE_URL - The sports API base URL (default: https://h5-api.aoneroom.com)
    PROXY_PORT   - Proxy server port (default: 8080)
    CACHE_DIR    - Directory for segment cache (default: ./hls_cache)
"""

import os
import sys
import time
import hashlib
import requests
import threading
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request

# Configuration
API_BASE_URL = os.environ.get("API_BASE_URL", "https://h5-api.aoneroom.com")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))
CACHE_DIR = os.environ.get("CACHE_DIR", "./hls_cache")

# Create cache directory
os.makedirs(CACHE_DIR, exist_ok=True)

# Session with persistent connections for keep-alive
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
})

# In-memory cache for .m3u8 playlists (they change frequently)
playlist_cache = {}
CACHE_TTL = 10  # Cache playlists for 10 seconds


def get_cache_path(url):
    """Generate a local cache file path for a URL."""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(CACHE_DIR, url_hash)


def fetch_url(url, headers=None, cache_enabled=True):
    """
    Fetch a URL through the proxy. Uses cache for segments,
    bypasses cache for playlists.
    """
    is_playlist = url.endswith('.m3u8') or 'playlist' in url
    
    # Check memory cache for playlists
    if is_playlist and url in playlist_cache:
        cached_time, cached_data = playlist_cache[url]
        if time.time() - cached_time < CACHE_TTL:
            print(f"[CACHE HIT] {url}")
            return cached_data
    
    # Check disk cache for segments
    cache_path = get_cache_path(url)
    if cache_enabled and not is_playlist and os.path.exists(cache_path):
        with open(cache_path, 'rb') as f:
            return f.read()
    
    # Fetch from origin
    print(f"[FETCH] {url}")
    try:
        req_headers = dict(session.headers)
        if headers:
            req_headers.update(headers)
        
        # For HLS streams, we need to forward the original query parameters
        response = session.get(url, headers=req_headers, timeout=30, stream=True)
        response.raise_for_status()
        data = response.content
        
        # Cache playlists in memory
        if is_playlist:
            playlist_cache[url] = (time.time(), data)
        # Cache segments on disk
        elif cache_enabled:
            with open(cache_path, 'wb') as f:
                f.write(data)
        
        return data
        
    except Exception as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        raise


def rewrite_playlist(playlist_content, base_url, proxy_host):
    """
    Rewrite an .m3u8 playlist to route all segments through the proxy.
    
    This is the key function - it takes the original playlist and changes
    all segment URLs to go through our proxy instead of directly to the CDN.
    """
    lines = playlist_content.decode('utf-8', errors='replace').split('\n')
    rewritten = []
    
    for line in lines:
        line = line.strip()
        
        # Skip empty lines and comments (except EXTINF)
        if not line or line.startswith('#EXT'):
            rewritten.append(line)
            continue
            
        # Skip comments that aren't directives
        if line.startswith('#'):
            rewritten.append(line)
            continue
        
        # This is a URL (segment or sub-playlist)
        if line.startswith('http'):
            original_url = line
        else:
            # Relative URL - resolve against base
            original_url = urljoin(base_url, line)
        
        # Route through proxy
        proxy_url = f"/proxy?url={urllib.parse.quote(original_url, safe='')}&base={urllib.parse.quote(base_url, safe='')}"
        rewritten.append(proxy_url)
    
    return '\n'.join(rewritten).encode('utf-8')


class HLSProxyHandler(BaseHTTPRequestHandler):
    """HTTP request handler that proxies HLS streams."""
    
    def log_message(self, format, *args):
        """Override to add timestamps."""
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} - {format % args}")
    
    def do_GET(self):
        """Handle GET requests."""
        try:
            if self.path.startswith('/proxy'):
                self.handle_proxy()
            elif self.path.startswith('/stream'):
                self.handle_stream_request()
            elif self.path == '/health':
                self.handle_health()
            else:
                self.send_error(404, "Not Found")
                
        except Exception as e:
            print(f"[ERROR] {e}")
            self.send_error(500, str(e))
    
    def handle_health(self):
        """Health check endpoint."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    
    def handle_stream_request(self):
        """
        Main endpoint for stream requests.
        
        Usage: GET /stream?api_url=<API_ENDPOINT>&match_id=<ID>
        
        This endpoint:
        1. Calls your sports API to get the stream URL
        2. Fetches the .m3u8 playlist (using proxy's IP - so token works)
        3. Rewrites the playlist to route segments through the proxy
        4. Returns the rewritten playlist to the client
        """
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        # Get the stream URL from params (or your API)
        stream_url = params.get('url', [''])[0]
        
        if not stream_url:
            self.send_error(400, "Missing 'url' parameter")
            return
        
        print(f"[STREAM] Request for: {stream_url}")
        
        try:
            # Fetch the playlist (token is valid because same IP)
            playlist_data = fetch_url(stream_url)
            
            # Determine content type
            content_type = 'application/vnd.apple.mpegurl'
            
            # Check if this is a master playlist (contains multiple variants)
            playlist_text = playlist_data.decode('utf-8', errors='replace')
            is_master = '#EXT-X-STREAM-INF' in playlist_text
            
            if is_master:
                # Master playlist - rewrite sub-playlist URLs
                proxy_host = f"{self.headers.get('Host', 'localhost:' + str(PROXY_PORT))}"
                rewritten = rewrite_playlist(playlist_data, stream_url, proxy_host)
                
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(rewritten)
            else:
                # Media playlist - rewrite segment URLs
                proxy_host = f"{self.headers.get('Host', 'localhost:' + str(PROXY_PORT))}"
                rewritten = rewrite_playlist(playlist_data, stream_url, proxy_host)
                
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(rewritten)
                
        except Exception as e:
            print(f"[ERROR] Stream handling failed: {e}")
            self.send_error(502, f"Bad Gateway: {e}")
    
    def handle_proxy(self):
        """
        Generic proxy endpoint for any URL.
        
        Usage: GET /proxy?url=<ENCODED_URL>&base=<ENCODED_BASE>
        
        Proxies the request to the target URL, caching segments
        and handling playlists appropriately.
        """
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        target_url = params.get('url', [''])[0]
        base_url = params.get('base', [''])[0]
        
        if not target_url:
            self.send_error(400, "Missing 'url' parameter")
            return
        
        try:
            # Determine if this is a playlist or segment
            is_playlist = target_url.endswith('.m3u8') or '.m3u8' in target_url
            
            # Fetch the content
            data = fetch_url(target_url)
            
            # Determine content type
            if is_playlist:
                content_type = 'application/vnd.apple.mpegurl'
            elif target_url.endswith('.ts'):
                content_type = 'video/mp2t'
            elif target_url.endswith('.mp4'):
                content_type = 'video/mp4'
            else:
                content_type = 'application/octet-stream'
            
            # If it's a playlist, rewrite URLs
            if is_playlist:
                proxy_host = f"{self.headers.get('Host', 'localhost:' + str(PROXY_PORT))}"
                data = rewrite_playlist(data, target_url, proxy_host)
            
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache' if is_playlist else 'max-age=30')
            self.end_headers()
            self.wfile.write(data)
            
        except Exception as e:
            print(f"[ERROR] Proxy failed for {target_url}: {e}")
            self.send_error(502, f"Proxy Error: {e}")
    
    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()


class ThreadedHTTPServer(HTTPServer):
    """Handle requests in a separate thread."""
    daemon_threads = True
    
    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
    
    def process_request(self, request, client_address):
        thread = threading.Thread(target=self.process_request_thread,
                                   args=(request, client_address))
        thread.daemon = self.daemon_threads
        thread.start()


def main():
    """Start the HLS proxy server."""
    server = ThreadedHTTPServer(('0.0.0.0', PROXY_PORT), HLSProxyHandler)
    
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║           HLS Stream Proxy Server                                ║
╠══════════════════════════════════════════════════════════════════╣
║  Port:        {PROXY_PORT}                                              ║
║  Cache Dir:   {CACHE_DIR:<50} ║
║  API Base:    {API_BASE_URL:<50} ║
╠══════════════════════════════════════════════════════════════════╣
║  Endpoints:                                                      ║
║    GET /health          - Health check                           ║
║    GET /stream?url=     - Proxy an HLS stream                    ║
║    GET /proxy?url=      - Generic URL proxy                      ║
╠══════════════════════════════════════════════════════════════════╣
║  Usage Example:                                                  ║
║    /stream?url=https://live-pull.aisports.mobi/.../playlist.m3u8 ║
╚══════════════════════════════════════════════════════════════════╝
    """)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Server stopped")
        server.shutdown()


if __name__ == '__main__':
    main()
