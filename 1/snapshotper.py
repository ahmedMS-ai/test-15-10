import re, hashlib, time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def ts_name(prefix="offline"):
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"

def _sha16(s:str)->str: return hashlib.sha1(s.encode()).hexdigest()[:16]

def _ext_from_ctype(ctype:str, urlpath:str):
    c = (ctype or "").split(";")[0].strip().lower()
    if c == "text/css": return ".css"
    if c in ("application/javascript","text/javascript","application/x-javascript"): return ".js"
    if c.startswith("image/"):
        mapping = {"image/png":".png","image/jpeg":".jpg","image/gif":".gif","image/webp":".webp","image/svg+xml":".svg"}
        return mapping.get(c, Path(urlpath).suffix or ".img")
    if "font" in c: return Path(urlpath).suffix or ".woff2"
    return Path(urlpath).suffix or ""

class StaticSnapshotter:
    def __init__(self, out_dir:Path, wait_idle_ms=1600, scroll_passes=14):
        self.out = Path(out_dir); self.out.mkdir(parents=True, exist_ok=True)
        self.assets = self.out/"assets"; self.assets.mkdir(exist_ok=True)
        self.offdir = self.out/"_offline"; self.offdir.mkdir(exist_ok=True)
        self.wait_idle_ms = wait_idle_ms
        self.scroll_passes = scroll_passes
        self.ctypes = {}
        self.responses = {}
        self.url2local = {}

    def _norm(self, u:str): return urldefrag(u)[0].strip()
    def _asset_name(self, url, ctype):
        p = urlparse(url); return f"{_sha16(url)}{_ext_from_ctype(ctype, p.path)}"

    def _collect_map_assets(self, soup:BeautifulSoup, base_url:str):
        res=set()
        def add(u):
            if not u or u.startswith("data:"): return
            absu = self._norm(urljoin(base_url, u)); res.add(absu)
        for tag,attr in [("link","href"),("script","src"),("img","src"),("source","src"),("video","poster")]:
            for el in soup.find_all(tag): add(el.get(attr))
        for style in soup.find_all("style"):
            s = style.string or ""
            for m in re.findall(r'url\(([^)]+)\)', s):
                add(m.strip("'\" "))
            # دعم @import "file.css";
            for m in re.findall(r'@import\s+["\']([^"\']+)["\']', s):
                add(m.strip())
        for u in res:
            if u not in self.url2local:
                self.url2local[u] = f"assets/{self._asset_name(u, self.ctypes.get(u,''))}"

    def _strip_spa_scripts(self, soup:BeautifulSoup):
        for s in soup.find_all("script"): s.decompose()
        for b in soup.find_all("base"): b.decompose()
        for l in soup.find_all("link"):
            for a in ["integrity","crossorigin","referrerpolicy"]:
                if l.has_attr(a): del l[a]

    def _rewrite_attrs(self, soup:BeautifulSoup, base_url:str):
        def mapu(u):
            absu = self._norm(urljoin(base_url, u))
            return self.url2local.get(absu, u.lstrip("/") if u.startswith("/") else u)
        for tag,attr in [("link","href"),("script","src"),("img","src"),("source","src"),("video","poster")]:
            for el in soup.find_all(tag):
                u = el.get(attr); 
                if not u: continue
                if u.startswith(("http://","https://","data:","mailto:","tel:","whatsapp:")):
                    absu = self._norm(urljoin(base_url, u))
                    if absu in self.url2local: el[attr]=self.url2local[absu]
                    continue
                el[attr] = mapu(u)
        for a in soup.find_all("a"):
            h = a.get("href"); 
            if not h: continue
            if h.startswith(("#","mailto:","tel:","whatsapp:")): continue
            if h.startswith("/"): a["href"]=h.lstrip("/")

    def _write_offline_utils(self)->str:
        code = r"""
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
})();"""
        p = self.offdir/"offline_utils.js"; p.write_text(code, encoding="utf-8"); return "_offline/offline_utils.js"

    def run(self, url:str):
        from playwright.sync_api import Error as PWError
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-web-security"])
            ctx = browser.new_context(ignore_https_errors=True)
            page = ctx.new_page()

            def on_response(resp):
                try:
                    u = self._norm(resp.url)
                    ctype = (resp.headers or {}).get("content-type","") or ""
                    self.ctypes[u]=ctype
                    body = resp.body()
                    if any(t in ctype for t in ("text/css","javascript","image/","font/","svg","application/font","woff")):
                        self.responses[u]=body
                except Exception:
                    pass
            page.on("response", on_response)

            page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            # تمرير Lazy تكيُّفي: نتوقف إذا الارتفاع لم يتغير 3 مرات متتالية
            same_count = 0
            last_h = 0
            for i in range(self.scroll_passes):
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(self.wait_idle_ms)
                h = page.evaluate("() => document.body.scrollHeight || 0")
                if h == last_h:
                    same_count += 1
                    if same_count >= 3: break
                else:
                    same_count = 0
                last_h = h

            html = page.content()

            # حفظ الأصول (ما جمعناه من on_response)
            for u, body in self.responses.items():
                rel = self.url2local.get(u) or f"assets/{self._asset_name(u, self.ctypes.get(u,''))}"
                self.url2local[u]=rel
                p = self.out/rel; p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists(): p.write_bytes(body)

            soup = BeautifulSoup(html, "html.parser")
            self._strip_spa_scripts(soup)
            self._collect_map_assets(soup, url)
            self._rewrite_attrs(soup, url)

            util_rel = self._write_offline_utils()
            head = soup.find("head") or soup
            tag = soup.new_tag("script"); tag["src"]=util_rel; head.append(tag)
            if not soup.title:
                t=soup.new_tag("title"); t.string="Offline Snapshot"; head.append(t)

            (self.out/"index_offline.html").write_text(str(soup), encoding="utf-8")
            page.screenshot(path=str(self.out/"screenshot.png"), full_page=True)
            browser.close()

        return str(self.out)
