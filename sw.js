/* Meridian service worker — a persistent, Google-Earth-style tile cache.
 *
 * The app is served from a STABLE origin (http://127.0.0.1:49731), so this Cache Storage survives app
 * restarts: once you've seen a map tile it's on disk forever and loads instantly next time — no re-download,
 * works even offline. We only ever cache the immutable map ASSETS (vector/raster tiles, glyphs, sprites,
 * flags); the live feed, the app HTML and the /clip proxy are never intercepted, so news stays fresh.
 */
const CACHE = "meridian-tiles-v2";
const MAX_ENTRIES = 5000;                 // cap the cache (~80-100MB); evict oldest beyond this

// Hostnames whose responses are static map assets — safe to serve cache-first (they don't change).
function isTileAsset(url){
  try{
    const h = new URL(url).hostname;
    return h.endsWith(".basemaps.cartocdn.com")   // CARTO vector/raster tiles, glyphs, sprite
        || h.endsWith(".arcgisonline.com")         // Esri hillshade / satellite / topo
        || h === "basemaps.cartocdn.com"
        || h === "flagcdn.com";                    // country flags
  }catch(e){ return false; }
}

self.addEventListener("install", function(){ self.skipWaiting(); });

self.addEventListener("activate", function(e){
  e.waitUntil((async function(){
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k.indexOf("meridian-tiles-") === 0 && k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();            // control already-open pages, so caching starts this session
  })());
});

// FIFO eviction: caches.keys() preserves insertion order, so the front is the oldest. This enumerates the
// WHOLE cache, so it must be AMORTISED — never run per-put (that thrashes Cache Storage and janks tile loads).
let _puts = 0, _trimming = false;
async function maybeTrim(cache){
  if(_trimming) return;
  if((++_puts % 250) !== 0) return;                 // only every ~250 cached tiles
  _trimming = true;
  try{
    const keys = await cache.keys();
    if(keys.length > MAX_ENTRIES){
      const drop = keys.slice(0, keys.length - MAX_ENTRIES + Math.floor(MAX_ENTRIES * 0.1));  // shave ~10%
      await Promise.all(drop.map(k => cache.delete(k)));
    }
  }catch(e){}
  finally{ _trimming = false; }
}

self.addEventListener("fetch", function(e){
  const req = e.request;
  if(req.method !== "GET" || !isTileAsset(req.url)) return;   // everything else: default browser handling (feed, HTML, api, clips)
  e.respondWith((async function(){
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req);
    if(hit) return hit;                                        // cache-first — instant, offline-capable
    try{
      const res = await fetch(req);
      // cache real 200s AND opaque no-cors responses (Esri rasters fetched via <img> are opaque); never errors
      if(res && (res.status === 200 || res.type === "opaque")){
        const copy = res.clone();
        e.waitUntil(cache.put(req, copy).then(function(){ return maybeTrim(cache); }).catch(function(){}));
      }
      return res;
    }catch(err){
      const any = await cache.match(req, {ignoreVary:true});
      if(any) return any;                                      // offline fallback
      throw err;
    }
  })());
});
