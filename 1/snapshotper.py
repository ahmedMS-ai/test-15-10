# snapshotper.py
import re, hashlib, time, json, base64, mimetypes, os
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
import yaml
import tldextract
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

def ts_name(prefix="job"): return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
def sha16(s:str)->str: return hashlib.sha1(s.encode()).hexdigest()[:16]

def ext_from_ctype(ctype:str, urlpath:str):
    if not ctype: ctype = ""
    c = ctype.split(";")[0].strip().lower()
    if c == "text/css": return ".css"
    if c in ("application/javascript","text/javascript","application/x-javascript"): return ".js"
    if c.startswith("image/"):
        mapping = {"image/png":".png","image/jpeg":".jpg","image/gif":".gif","image/webp":".webp","image/svg+xml":".svg"}
        return mapping.get(c, Path(urlpath).suffix or ".img")
    if "font" in c or "woff" in c: return Path(urlpath).suffix or ".woff2"
    if c in ("application/json","text/json"): return ".json"
    return Path(urlpath).suffix or ""

def guess_mime(name:str):
    mime, _ = mimetypes.guess_type(name)
    if not mime and name.endswith(".js"): mime="application/javascript"
    if not mime and name.endswith(".css"): mime="text/css"
    return mime or "application/octet-stream"

DEFAULT_INTERACTIONS = {
    "max_clicks_per_page": 16,
    "selectors_force_click": [],           # e.g. ["#themeToggle", ".open-gallery"]
    "anchor_click": True,
    "theme_toggle_keywords": ["dark","light","theme","mode","night","day"],
    "open_modals_keywords": ["modal","popup","dialog","lightbox"],
}

class UltraSnapshotter:
    """
    أوضاع:
      mode="static"       : DOM بعد التنفيذ + إزالة سكربتات SPA + أصول محلية
      mode="interactive"  : + كاش JSON (API) وحقن fetch/XHR patch
      mode="singlefile"   : ينتج HTML واحد مكتفٍ (يمكن دمجه مع static/interactive)
    """
    def __init__(self, out_dir:Path, mode="static",
                 wait_idle_ms=1600, scroll_passes=12, slow_mo=0,
                 same_domain_only=True, follow_subpages=True, max_pages=3,
                 interactions_yaml:Path|None=None):
        self.out = Path(out_dir); self.out.mkdir(parents=True, exist_ok=True)
        self.assets = self.out/"assets"; self.assets.mkdir(exist_ok=True)
        self.offdir = self.out/"_offline"; self.offdir.mkdir(exist_ok=True)

        self.mode = mode
        self.wait_idle_ms = wait_idle_ms
        self.scroll_passes = scroll_passes
        self.slow_mo = slow_mo
        self.same_domain_only = same_domain_only
        self.follow_subpages = follow_subpages
        self.max_pages = max_pages

        self.ctypes = {}
        self.responses = {}    # url -> bytes (assets)
        self.api_cache = {}     # url -> bytes (json/text)
        self.url2local = {}     # url -> relative (under out)
        self.actions = []
        self.visited = set()
        self.queue = []

        # تحميل تفاعلات مخصصة
        self.inter = DEFAULT_INTERACTIONS.copy()
        if interactions_yaml and Path(interactions_yaml).exists():
            with open(interactions_yaml, "r", encoding="utf-8") as f:
                y = yaml.safe_load(f) or {}
                self.inter.update(y)

    # ---------- helpers ----------
    def _norm(self, u:str): return urldefrag(u)[0].strip()
    def _same_reg_domain(self, a:str, b:str):
        ax, bx = tldextract.extract(a), tldextract.extract(b)
        return ax.registered_domain and ax.registered_domain == bx.registered_domain
    def _asset_name(self, url, ctype): 
        p = urlparse(url); return f"{sha16(url)}{ext_from_ctype(ctype, p.path)}"
    def _sleep_js(self, ms:int): return f"new Promise(r=>setTimeout(r,{ms}))"

    # ---------- playwright wiring ----------
    def _new_browser(self, pw):
        launch_args = ["--disable-web-security","--disable-features=IsolateOrigins,site-per-process","--disable-background-networking"]
        return pw.chromium.launch(headless=True, args=launch_args, slow_mo=self.slow_mo or 0)

    def _new_context(self, browser):
        # تعطيل Service Workers لأنها تكسر العرض الأوفلاين والكاش
        ctx = browser.new_context(ignore_https_errors=True, java_script_enabled=True, service_workers="block")
        # حجب بعض التحليلات الثقيلة لتسريع وكتم الضوضاء
        def route_block(route):
            url = route.request.url
            if any(x in url for x in ["googletagmanager","google-analytics","doubleclick","facebook","segment.io","hotjar"]):
                return route.abort()
            return route.continue_()
        ctx.route("**/*", route_block)
        return ctx

    # ---------- network capture ----------
    def _on_response(self, resp):
        try:
            url = self._norm(resp.url)
            ctype = (resp.headers or {}).get("content-type","") or ""
            self.ctypes[url] = ctype
            body = resp.body()
            if ("application/json" in ctype or "text/json" in ctype or (ctype.startswith("text/") and len(body) < 5_000_000)):
                if self.mode in ("interactive","singlefile"):
                    self.api_cache[url]=body
            if any(t in ctype for t in ["text/css","javascript","image/","font/","svg","application/font","woff"]) or url.endswith((".css",".js",".svg",".woff",".woff2",".ttf",".png",".jpg",".jpeg",".gif",".webp")):
                self.responses[url]=body
        except Exception:
            pass

    def _flush_assets(self):
        for u,b in self.responses.items():
            rel = self.url2local.get(u) or f"assets/{self._asset_name(u, self.ctypes.get(u,''))}"
            self.url2local[u]=rel
            p = self.out/rel; p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists(): p.write_bytes(b)

    # ---------- DOM processing ----------
    def _strip_spa_scripts(self, soup:BeautifulSoup):
        for s in soup.find_all("script"): s.decompose()
        for b in soup.find_all("base"): b.decompose()
        for l in soup.find_all("link"):
            for a in ["integrity","crossorigin","referrerpolicy"]:
                if l.has_attr(a): del l[a]

    def _collect_assets_map(self, soup:BeautifulSoup, base_url:str):
        res=set()
        def add(u):
            if not u or u.startswith("data:"): return
            absu = self._norm(urljoin(base_url, u)); res.add(absu)
        for tag,attr in [("link","href"),("script","src"),("img","src"),("source","src"),("video","poster")]:
            for el in soup.find_all(tag): add(el.get(attr))
        for style in soup.find_all("style"):
            if style.string:
                for m in re.findall(r'url\(([^)]+)\)', style.string):
                    u=m.strip("'\" "); add(u)
        for u in res:
            if u not in self.url2local:
                self.url2local[u] = f"assets/{self._asset_name(u, self.ctypes.get(u,''))}"

    def _rewrite_attrs(self, soup:BeautifulSoup, base_url:str):
        def mapu(u):
            absu = self._norm(urljoin(base_url, u))
            if absu in self.url2local: return self.url2local[absu]
            if u.startswith("/"): return u.lstrip("/")
            return u
        for tag,attr in [("link","href"),("script","src"),("img","src"),("source","src"),("video","poster")]:
            for el in soup.find_all(tag):
                u = el.get(attr)
                if not u: continue
                if u.startswith(("http://","https://","data:","mailto:","tel:","whatsapp:")):
                    absu = self._norm(urljoin(base_url, u))
                    if absu in self.url2local: el[attr] = self.url2local[absu]
                    continue
                el[attr] = mapu(u)
        # روابط داخلية
        for a in soup.find_all("a"):
            h = a.get("href")
            if not h: continue
            if h.startswith(("#","mailto:","tel:","whatsapp:")): continue
            if h.startswith("/"): a["href"]=h.lstrip("/")

    def _offline_utils_js(self):
        # util للتعامل مع أنكورز + theme toggles + حركة بسيطة
        return r"""
(function(){
  try { document.documentElement.style.scrollBehavior='smooth'; } catch(e){}
  function toggleDark(){
    const root = document.documentElement || document.body;
    root.classList.toggle('dark');
    const v = root.getAttribute('data-theme');
    if(v==='dark') root.setAttribute('data-theme','light');
    else if(v==='light') root.setAttribute('data-theme','dark');
  }
  const kw=/(dark|light|theme|mode|night|day)/i;
  document.querySelectorAll('button,[role=button],a,input[type=button],input[type=submit]').forEach(el=>{
    const sig=[el.id,el.className,el.getAttribute('aria-label'),el.innerText].join(' ');
    if(kw.test(sig)){
      el.addEventListener('click',(ev)=>{
        const isAnchor = el.tagName.toLowerCase()==='a' && (el.getAttribute('href')||'').startsWith('#');
        if(!isAnchor) ev.preventDefault();
        toggleDark();
      }, {capture:true});
    }
  });
})();
"""

    def _fetch_patch_js(self):
        # patch يخدم JSON من الكاش أثناء الأوفلاين
        return r"""
(function(){
  const manifestPath = (window.__OFFLINE_API_MANIFEST__ || "_offline/api_manifest.json");
  let MAP = {};
  const toKey = u => { try { return new URL(u, location.href).href.split('#')[0]; } catch(e){ return (u||'').toString(); } };
  fetch(manifestPath).then(r=>r.json()).then(j=>MAP=j||{}).catch(()=>{MAP={};});

  const _fetch = window.fetch.bind(window);
  window.fetch = async function(input, init){
    const url = (typeof input==='string') ? input : (input && input.url);
    const key = toKey(url);
    const local = MAP[key];
    const offline = (location.protocol==='file:' || !navigator.onLine);
    if(local && offline){
      const r = await fetch(local);
      return new Response(await r.blob(), {status:200, headers:r.headers});
    }
    try { return await _fetch(input, init); }
    catch(e){
      if(local){ const r = await fetch(local); return new Response(await r.blob(), {status:200, headers:r.headers}); }
      throw e;
    }
  };

  const _XHR = window.XMLHttpRequest;
  window.XMLHttpRequest = function(){
    const xhr = new _XHR();
    let _url = null;
    const _open = xhr.open;
    xhr.open = function(method, url){ _url = url; return _open.apply(xhr, arguments); };
    const _send = xhr.send;
    xhr.send = function(){
      const key = toKey(_url);
      const offline = (location.protocol==='file:' || !navigator.onLine);
      const rel = MAP[key];
      if(rel && offline){
        fetch(rel).then(r=>r.text()).then(txt=>{
          Object.defineProperty(xhr,'responseText',{value:txt});
          Object.defineProperty(xhr,'status',{value:200});
          xhr.onreadystatechange && xhr.onreadystatechange();
          xhr.onload && xhr.onload();
        });
      } else return _send.apply(xhr, arguments);
    };
    return xhr;
  };
})();
"""

    def _write_runtime_helpers(self):
        (self.offdir/"offline_utils.js").write_text(self._offline_utils_js(), encoding="utf-8")
        (self.offdir/"fetch_patch.js").write_text(self._fetch_patch_js(), encoding="utf-8")

    # ---------- interactions ----------
    def _discover_clickables_js(self, inter):
        kws = "|".join(re.escape(k) for k in self.inter["theme_toggle_keywords"])
        mks = "|".join(re.escape(k) for k in self.inter["open_modals_keywords"])
        return f"""
() => {{
  const els = [];
  const add = (el, why) => {{
    const r = el.getBoundingClientRect();
    if (r.width<1 || r.height<1) return;
    let sel=''; if(el.id) sel='#'+el.id;
    else {{
      let c=(el.className||'').toString().trim().split(/\\s+/).slice(0,3).join('.');
      sel=el.tagName.toLowerCase()+(c?'.'+c:'');
    }}
    els.push({{sel, why, tag: el.tagName.toLowerCase(), text:(el.innerText||'').slice(0,80), href: el.href||null}});
  }};
  const cands = Array.from(document.querySelectorAll('a,button,[role=button],[onclick],input[type=button],input[type=submit]'));
  cands.forEach(el=>add(el,'generic'));
  const kw=/(?:{kws})/i;
  const mk=/(?:{mks})/i;
  document.querySelectorAll('*').forEach(el=>{{
    const sig=[el.id,el.className,el.getAttribute('aria-label'),el.innerText].join(' ');
    if(kw.test(sig)) add(el,'theme-guess');
    if(mk.test(sig)) add(el,'modal-guess');
  }});
  return els.slice(0,200);
}}
"""

    def _attempt_interactions(self, page, base_url):
        max_clicks = int(self.inter.get("max_clicks_per_page", 16))
        # Forced selectors first
        for sel in self.inter.get("selectors_force_click", [])[:max_clicks]:
            try:
                if page.query_selector(sel):
                    page.click(sel, timeout=1500)
                    page.wait_for_timeout(self.wait_idle_ms)
                    max_clicks -= 1
                    self.actions.append({"url":base_url, "action":"forced-click", "selector":sel})
            except Exception as e:
                self.actions.append({"url":base_url,"action":"forced-click","selector":sel,"error":str(e)[:140]})

        # Discovered clickables
        try:
            els = page.evaluate(self._discover_clickables_js(self.inter))
        except Exception:
            els = []
        clicks = 0
        for el in els:
            if clicks >= max_clicks: break
            try:
                if el.get("href"):
                    href = self._norm(urljoin(base_url, el["href"]))
                    # in-page anchor
                    if href.startswith(base_url.split("#")[0]+"#") and self.inter.get("anchor_click", True):
                        # انقر الأنكور ذاته
                        page.evaluate("h=>{const a=document.querySelector(`a[href='${h}']`); if(a) a.click();}", el["href"])
                        page.wait_for_timeout(self.wait_idle_ms)
                        clicks += 1
                        self.actions.append({"url":base_url,"action":"anchor-click","href":el["href"]})
                    else:
                        if self.follow_subpages and self._same_reg_domain(href, base_url):
                            self.queue.append(href)
                            self.actions.append({"url":base_url,"action":"queue-subpage","href":href})
                        else:
                            self.actions.append({"url":base_url,"action":"skip-external","href":href})
                else:
                    sel = el.get("sel")
                    if sel and page.query_selector(sel):
                        page.click(sel, timeout=1500)
                        page.wait_for_timeout(self.wait_idle_ms)
                        clicks += 1
                        self.actions.append({"url":base_url,"action":"click","selector":sel})
            except Exception as e:
                self.actions.append({"url":base_url,"action":"click","error":str(e)[:140]})

    # ---------- single-file pack ----------
    def _embed_file_uri(self, path:Path):
        if not path.exists(): return None
        data = path.read_bytes()
        mime = guess_mime(path.name)
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"

    def _make_single_file(self, html_path:Path):
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        def process(tag, attr):
            for el in soup.find_all(tag):
                u = el.get(attr)
                if not u: continue
                if u.startswith(("data:","mailto:","tel:","whatsapp:","http://","https://")): continue
                p = (html_path.parent / u).resolve()
                uri = self._embed_file_uri(p)
                if uri: el[attr]=uri
        for tag, attr in [("link","href"),("script","src"),("img","src"),("source","src"),("video","poster")]:
            process(tag, attr)
        # inline url(...) inside <style>
        for style in soup.find_all("style"):
            if not style.string: continue
            def repl(m):
                u = m.group(1).strip('\'" ')
                if u.startswith(("data:","http://","https://","mailto:","tel:","whatsapp:")): return f"url({u})"
                p = (html_path.parent / u).resolve()
                uri = self._embed_file_uri(p)
                return f"url({uri})" if uri else f"url({u})"
            style.string = re.sub(r'url\(([^)]+)\)', repl, style.string)
        out_path = html_path.parent / "index_single.html"
        out_path.write_text(str(soup), encoding="utf-8")
        return str(out_path)

    # ---------- main ----------
    def run(self, start_url:str):
        start_url = self._norm(start_url)
        self.queue = [start_url]
        self._write_runtime_helpers()

        with sync_playwright() as pw:
            browser = self._new_browser(pw)
            ctx = self._new_context(browser)
            page = ctx.new_page()
            page.on("response", self._on_response)

            pages_done = 0
            while self.queue and pages_done < self.max_pages:
                url = self.queue.pop(0)
                if url in self.visited: continue
                if self.same_domain_only and not self._same_reg_domain(url, start_url): continue
                self.visited.add(url); pages_done += 1

                # Goto + مهلات مرنة + retries
                for attempt in range(3):
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                        # حاول networkidle لو ممكن
                        try: page.wait_for_load_state("networkidle", timeout=8000)
                        except PWTimeout: pass
                        break
                    except PWTimeout as e:
                        if attempt==2: raise
                        page.wait_for_timeout(1500)

                # lazy-load scrolling
                for _ in range(self.scroll_passes):
                    page.mouse.wheel(0, 1400)
                    page.wait_for_timeout(self.wait_idle_ms)

                # تفاعلات ذكية
                self._attempt_interactions(page, url)

                # snapshot DOM
                html = page.content()

                # flush assets
                self._flush_assets()

                # build HTML
                soup = BeautifulSoup(html, "html.parser")

                # منع rehydration لو static/singlefile
                if self.mode in ("static","singlefile"):
                    self._strip_spa_scripts(soup)

                # أجمع خريطة الأصول وأعد كتابة الروابط
                self._collect_assets_map(soup, url)
                self._rewrite_attrs(soup, url)

                head = soup.find("head") or soup
                # inject offline utils دائمًا
                util_tag = soup.new_tag("script"); util_tag["src"]="_offline/offline_utils.js"; head.append(util_tag)

                # interactive: inject fetch patch + manifest
                if self.mode in ("interactive","singlefile"):
                    manifest = self.offdir/"api_manifest.json"
                    # اكتب الكاش
                    api_map = {}
                    for u, body in self.api_cache.items():
                        name = self._asset_name(u, self.ctypes.get(u,".json"))
                        rel = f"assets/{name if name.endswith('.json') else name+'.json'}"
                        (self.out/rel).write_bytes(body)
                        api_map[u] = rel
                    manifest.write_text(json.dumps(api_map, indent=2, ensure_ascii=False), encoding="utf-8")

                    helper = soup.new_tag("script")
                    helper.string = "window.__OFFLINE_API_MANIFEST__ = '_offline/api_manifest.json';"
                    head.append(helper)
                    patch = soup.new_tag("script"); patch["src"] = "_offline/fetch_patch.js"; head.append(patch)

                # title
                if not soup.title:
                    t=soup.new_tag("title"); t.string="Offline Snapshot"; head.append(t)

                out_html = self.out/("index_offline.html" if pages_done==1 else f"page_{pages_done}.html")
                out_html.write_text(str(soup), encoding="utf-8")

            # لقطة شاشة للصفحة الأولى
            try: page.screenshot(path=str(self.out/"screenshot.png"), full_page=True)
            except Exception: pass

            # actions log
            (self.out/"actions_log.json").write_text(json.dumps(self.actions, ensure_ascii=False, indent=2), encoding="utf-8")

            browser.close()

        # single-file (اختياري)
        single_path = None
        if self.mode=="singlefile":
            single_path = self._make_single_file(self.out/"index_offline.html")

        return {"out_dir": str(self.out), "single_file": single_path}
