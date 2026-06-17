/**
 * Railway HLS Proxy - Express Server
 * ==================================
 * 
 * Deploy this to Railway to proxy HLS streams from the same IP.
 * The key principle: both API calls AND stream requests must come
 * from the same IP address for the signed token to work.
 * 
 * Environment Variables:
 *   PORT              - Server port (Railway sets this automatically)
 *   SPORTS_API_URL    - Your sports API endpoint
 *   API_AUTH_TOKEN    - Auth token for your sports API (if needed)
 * 
 * Deploy to Railway:
 *   1. Create new Railway project
 *   2. Add this file as the main server
 *   3. Add package.json with express dependency
 *   4. Deploy
 * 
 * API Flow:
 *   Client -> Your Railway Proxy -> h5-api.aoneroom.com (match list)
 *                                     |
 *                                     v
 *                              Stream URLs (signed for Railway IP)
 *                                     |
 *                                     v
 *   Client <- Your Railway Proxy <- HLS segments (same IP = valid)
 */

const express = require('express');
const axios = require('axios');
const cors = require('cors');
const NodeCache = require('node-cache');

const app = express();
const PORT = process.env.PORT || 3000;
const SPORTS_API_URL = process.env.SPORTS_API_URL || 'https://h5-api.aoneroom.com';

// Cache for playlists (short TTL since they update frequently)
const playlistCache = new NodeCache({ stdTTL: 10, checkperiod: 15 });

// HTTP client with keep-alive for connection reuse
const httpClient = axios.create({
  timeout: 30000,
  headers: {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
  },
  // Important: Use responseType 'arraybuffer' for binary data (video segments)
  responseType: 'arraybuffer',
  // Keep connections alive for better performance
  httpAgent: new (require('http').Agent)({ keepAlive: true }),
  httpsAgent: new (require('https').Agent)({ keepAlive: true }),
});

app.use(cors());
app.use(express.json());

/**
 * Health check endpoint
 */
app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    proxy_ip_working: 'Tokens signed for this Railway IP will work' 
  });
});

/**
 * Get live matches (proxies to your sports API)
 * 
 * This endpoint:
 * 1. Calls the sports API FROM Railway's IP
 * 2. Returns match data including stream URLs
 * 3. Stream URLs are signed for Railway's IP
 */
app.get('/api/matches', async (req, res) => {
  try {
    // Call your sports API from Railway's IP
    const response = await axios.get(`${SPORTS_API_URL}/wefeed-h5api-bff/live/match-list-v5?type=football&tab=all`, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Authorization': process.env.API_AUTH_TOKEN || '',
      },
      params: req.query,
    });
    
    res.json(response.data);
  } catch (error) {
    console.error('API Error:', error.message);
    res.status(502).json({ 
      error: 'Failed to fetch matches', 
      details: error.message 
    });
  }
});

/**
 * Proxy HLS stream
 * 
 * This is the KEY endpoint. It:
 * 1. Receives the original stream URL (with sign= token)
 * 2. Fetches the .m3u8 playlist FROM Railway (same IP = token valid)
 * 3. Rewrites segment URLs to go through this proxy
 * 4. Client plays from proxy, proxy fetches from origin
 * 
 * Query params:
 *   url - The original stream URL (with sign and t parameters)
 */
app.get('/proxy/stream', async (req, res) => {
  const streamUrl = req.query.url;
  
  if (!streamUrl) {
    return res.status(400).json({ error: 'Missing url parameter' });
  }
  
  console.log(`[STREAM] Proxying: ${streamUrl.substring(0, 100)}...`);
  
  try {
    // Check cache first
    const cached = playlistCache.get(streamUrl);
    if (cached) {
      console.log('[CACHE] Returning cached playlist');
      res.set('Content-Type', 'application/vnd.apple.mpegurl');
      return res.send(cached);
    }
    
    // Fetch the playlist - THIS IS THE KEY STEP
    // The token was signed for Railway's IP, so fetching FROM Railway works!
    const response = await httpClient.get(streamUrl);
    
    let playlistContent = response.data.toString('utf-8');
    
    // Rewrite the playlist to route segments through this proxy
    const baseUrl = streamUrl.split('?')[0]; // Remove query params for base
    const proxyBase = `${req.protocol}://${req.get('host')}/proxy/segment`;
    
    playlistContent = rewritePlaylist(playlistContent, baseUrl, proxyBase, streamUrl);
    
    // Cache the rewritten playlist
    playlistCache.set(streamUrl, playlistContent);
    
    // Send to client
    res.set('Content-Type', 'application/vnd.apple.mpegurl');
    res.set('Cache-Control', 'no-cache');
    res.send(playlistContent);
    
  } catch (error) {
    console.error(`[ERROR] Failed to proxy stream: ${error.message}`);
    res.status(502).json({ 
      error: 'Failed to fetch stream', 
      details: error.message,
      tip: 'The token may have expired. Request a fresh URL from the API.'
    });
  }
});

/**
 * Proxy individual segments
 * 
 * This handles .ts (transport stream) files and other media segments.
 * These don't require signing - only the playlist URL needs the token.
 */
app.get('/proxy/segment', async (req, res) => {
  const segmentUrl = req.query.url;
  
  if (!segmentUrl) {
    return res.status(400).json({ error: 'Missing url parameter' });
  }
  
  try {
    // Fetch the segment (no auth needed for segments)
    const response = await httpClient.get(segmentUrl);
    
    // Determine content type
    const ext = segmentUrl.split('.').pop().split('?')[0];
    const contentType = ext === 'ts' ? 'video/mp2t' 
                      : ext === 'mp4' ? 'video/mp4'
                      : ext === 'm3u8' ? 'application/vnd.apple.mpegurl'
                      : 'application/octet-stream';
    
    res.set('Content-Type', contentType);
    res.set('Cache-Control', 'max-age=60'); // Cache segments longer
    res.send(response.data);
    
  } catch (error) {
    console.error(`[ERROR] Segment fetch failed: ${error.message}`);
    res.status(502).json({ error: 'Failed to fetch segment', details: error.message });
  }
});

/**
 * Direct proxy endpoint (catches all .m3u8 and .ts requests)
 * 
 * This provides a catch-all proxy for any HLS URL.
 */
app.get('/proxy', async (req, res) => {
  const targetUrl = req.query.url;
  
  if (!targetUrl) {
    return res.status(400).json({ error: 'Missing url parameter. Use ?url=<encoded_hls_url>' });
  }
  
  try {
    const isPlaylist = targetUrl.includes('.m3u8');
    
    if (isPlaylist) {
      // Check cache
      const cached = playlistCache.get(targetUrl);
      if (cached) {
        res.set('Content-Type', 'application/vnd.apple.mpegurl');
        return res.send(cached);
      }
    }
    
    // Fetch from origin
    const response = await httpClient.get(targetUrl);
    
    if (isPlaylist) {
      // Rewrite playlist
      const baseUrl = targetUrl.split('?')[0];
      const proxyBase = `${req.protocol}://${req.get('host')}/proxy/segment`;
      
      let playlistContent = response.data.toString('utf-8');
      playlistContent = rewritePlaylist(playlistContent, baseUrl, proxyBase, targetUrl);
      
      playlistCache.set(targetUrl, playlistContent);
      
      res.set('Content-Type', 'application/vnd.apple.mpegurl');
      res.set('Cache-Control', 'no-cache');
      res.send(playlistContent);
    } else {
      // Segment - pass through
      const ext = targetUrl.split('.').pop().split('?')[0];
      const contentType = ext === 'ts' ? 'video/mp2t' : 'application/octet-stream';
      
      res.set('Content-Type', contentType);
      res.set('Cache-Control', 'max-age=60');
      res.send(response.data);
    }
    
  } catch (error) {
    console.error(`[ERROR] Proxy error: ${error.message}`);
    res.status(502).json({ error: 'Proxy error', details: error.message });
  }
});

/**
 * Rewrite HLS playlist to route segments through proxy
 * 
 * @param {string} content - Original playlist content
 * @param {string} baseUrl - Base URL for resolving relative URLs
 * @param {string} proxyBase - Base URL for proxy endpoints
 * @param {string} originalUrl - The original stream URL (with query params)
 * @returns {string} - Rewritten playlist
 */
function rewritePlaylist(content, baseUrl, proxyBase, originalUrl) {
  const lines = content.split('\n');
  const rewritten = [];
  
  for (const line of lines) {
    const trimmed = line.trim();
    
    // Pass through HLS directives and comments
    if (!trimmed || trimmed.startsWith('#')) {
      rewritten.push(line);
      continue;
    }
    
    // This is a URL (segment or sub-playlist)
    let originalSegmentUrl;
    if (trimmed.startsWith('http')) {
      originalSegmentUrl = trimmed;
    } else {
      // Relative URL - resolve against the original URL's directory
      const baseDir = baseUrl.substring(0, baseUrl.lastIndexOf('/') + 1);
      originalSegmentUrl = baseDir + trimmed;
    }
    
    // Route through proxy, preserving the original sign= and t= params
    const proxyUrl = `/proxy?url=${encodeURIComponent(originalSegmentUrl)}`;
    rewritten.push(proxyUrl);
  }
  
  return rewritten.join('\n');
}

/**
 * Start the server
 */
app.listen(PORT, '0.0.0.0', () => {
  console.log(`
╔══════════════════════════════════════════════════════════╗
║           Railway HLS Stream Proxy                       ║
╠══════════════════════════════════════════════════════════╣
║  Server running on port ${PORT}                            ║
╠══════════════════════════════════════════════════════════╣
║  Endpoints:                                              ║
║    GET /health           - Health check                  ║
║    GET /api/matches      - Get live matches              ║
║    GET /proxy/stream     - Proxy HLS stream              ║
║    GET /proxy/segment    - Proxy media segment           ║
║    GET /proxy?url=       - Generic proxy                 ║
╠══════════════════════════════════════════════════════════╣
║  KEY PRINCIPLE:                                          ║
║  Tokens are IP-locked. This proxy ensures both the API   ║
║  call AND the stream request come from Railway's IP.     ║
╚══════════════════════════════════════════════════════════╝
  `);
});

module.exports = app;
