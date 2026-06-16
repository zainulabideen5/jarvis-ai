"""Product / shopping research — "X store se Y ke prices batao" ka jawab CHAT
mein laata hai (vision se nahi — woh slow/brittle hai). Yeh GENERAL hai: kisi
bhi store pe chalta hai, kuch hardcode nahi:

    Layer 1  Shopify-style JSON  (/search/suggest.json, /products.json)
             — outfitters/khaadi/etc. jaise zyadatar PK fashion stores.
    Layer 2  Koi bhi site: page fetch karke JSON-LD (schema.org Product) +
             meta/price patterns nikaalo — yeh har platform pe milta hai
             (Google rich-results ke liye sab daalte hain).
    (Layer 3 — vision — caller ke paas fallback ke taur pe rehta hai.)

Store domain bhi brand-naam se KHUD resolve hota hai (brand.com.pk, brand.com…),
ya user agar poora URL de to wahi. Nothing store-specific.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from html import unescape

from app.core.logging import get_logger

log = get_logger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# brand -> resolved base URL (in-memory cache; learnt at runtime, not hardcoded)
_DOMAIN_CACHE: dict[str, str] = {}


def _fetch(url: str, timeout: int = 12) -> tuple[int, str, str]:
    """GET a URL with a browser UA. Returns (status, final_url, body)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(2_000_000).decode("utf-8", "replace")
        return r.status, r.geturl(), body


def _norm_price(val) -> str | None:
    """Make a clean numeric price string from whatever the source gave."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # keep digits + decimal; drop currency words/symbols/commas
    s = re.sub(r"[^\d.]", "", s.replace(",", ""))
    if not s or s == ".":
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f <= 0:
        return None
    return f"{f:,.0f}" if f == int(f) else f"{f:,.2f}"


def _candidate_domains(name: str) -> list[str]:
    """Brand naam ko sambhavit domains mein badlo — general, koi list nahi."""
    raw = name.strip().lower()
    if raw.startswith("http://") or raw.startswith("https://"):
        return [raw.rstrip("/")]
    if "." in raw and " " not in raw:                       # looks like a domain
        return [f"https://{raw.rstrip('/')}"]
    slug = re.sub(r"[^a-z0-9]", "", raw)                    # "h&m" -> "hm"
    if not slug:
        return []
    return [f"https://{slug}.com.pk", f"https://www.{slug}.com.pk",
            f"https://{slug}.pk", f"https://{slug}.com", f"https://www.{slug}.com"]


def resolve_store(name_or_url: str) -> str | None:
    """Brand/URL se ek live store base-URL nikaalo. Pehla jo respond kare."""
    key = name_or_url.strip().lower()
    if key in _DOMAIN_CACHE:
        return _DOMAIN_CACHE[key]
    for base in _candidate_domains(name_or_url):
        try:
            status, final, _ = _fetch(base, timeout=8)
            if status < 400:
                root = "{u.scheme}://{u.netloc}".format(u=urllib.parse.urlparse(final))
                _DOMAIN_CACHE[key] = root
                log.info("store_resolved", brand=key, url=root)
                return root
        except Exception:
            continue
    log.info("store_resolve_failed", brand=key)
    return None


# ---------------------------------------------------------------- Layer 1: Shopify
def _shopify_search(base: str, query: str, limit: int) -> list[dict]:
    out: list[dict] = []
    q = urllib.parse.quote(query)
    url = (f"{base}/search/suggest.json?q={q}"
           f"&resources[type]=product&resources[limit]={limit}")
    try:
        status, _, body = _fetch(url, timeout=12)
        if status >= 400:
            return out
        data = json.loads(body)
        prods = (data.get("resources", {}).get("results", {}) or {}).get("products", [])
        for p in prods:
            price = _norm_price(p.get("price"))
            if not price:
                continue
            href = p.get("url") or ""
            if href and href.startswith("/"):
                href = base + href
            out.append({"title": (p.get("title") or "").strip(),
                        "price": price, "url": href.split("?")[0]})
    except Exception as e:
        log.info("shopify_suggest_miss", err=str(e)[:100])
    return out[:limit]


def _shopify_products(base: str, query: str, limit: int) -> list[dict]:
    """products.json: search nahi karta, par list deta hai — query ke words se filter."""
    out: list[dict] = []
    try:
        status, _, body = _fetch(f"{base}/products.json?limit=250", timeout=12)
        if status >= 400:
            return out
        words = [w for w in re.split(r"\s+", query.lower()) if len(w) > 2]
        for p in json.loads(body).get("products", []):
            title = (p.get("title") or "").strip()
            hay = (title + " " + (p.get("product_type") or "") + " "
                   + " ".join(p.get("tags", []) if isinstance(p.get("tags"), list) else [])).lower()
            if words and not all(w in hay for w in words):
                continue
            variants = p.get("variants") or [{}]
            price = _norm_price(variants[0].get("price"))
            if not price:
                continue
            handle = p.get("handle")
            url = f"{base}/products/{handle}" if handle else base
            out.append({"title": title, "price": price, "url": url})
            if len(out) >= limit:
                break
    except Exception as e:
        log.info("shopify_products_miss", err=str(e)[:100])
    return out


# ------------------------------------------------- Layer 2: any site (JSON-LD etc.)
def _jsonld_products(html: str, base: str, limit: int) -> list[dict]:
    """schema.org Product markup — har e-com platform pe milta hai."""
    out: list[dict] = []
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S)

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(str(x).lower() == "product" for x in types):
            name = node.get("name")
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = _norm_price((offers or {}).get("price")) if isinstance(offers, dict) else None
            url = (node.get("url") or (offers or {}).get("url") or "")
            if isinstance(url, str) and url.startswith("/"):
                url = base + url
            if name and price:
                out.append({"title": unescape(str(name)).strip(),
                            "price": price, "url": url})
        for v in node.values():
            walk(v)

    for b in blocks:
        try:
            walk(json.loads(b.strip()))
        except Exception:
            continue
    # dedupe by title
    seen, uniq = set(), []
    for p in out:
        if p["title"].lower() in seen:
            continue
        seen.add(p["title"].lower())
        uniq.append(p)
    return uniq[:limit]


def _generic_search(base: str, query: str, limit: int) -> list[dict]:
    """Site ke search page se products — JSON-LD primary, phir bhi miss ho to []."""
    q = urllib.parse.quote(query)
    for path in (f"/search?q={q}", f"/?s={q}", f"/catalogsearch/result/?q={q}"):
        try:
            status, final, body = _fetch(base + path, timeout=14)
            if status >= 400:
                continue
            prods = _jsonld_products(body, base, limit)
            if prods:
                return prods
        except Exception:
            continue
    return []


def search_products(store: str, query: str, limit: int = 8) -> dict:
    """MAIN entry. Returns {ok, store_url, products:[{title,price,url}], method}.
    ok=False (with reason) agar kuch na mile — caller vision pe fall kare."""
    base = resolve_store(store)
    if not base:
        return {"ok": False, "reason": "store_not_found", "store": store}

    for method, fn in (("shopify_suggest", _shopify_search),
                       ("shopify_products", _shopify_products),
                       ("jsonld", _generic_search)):
        try:
            prods = fn(base, query, limit)
        except Exception as e:
            log.info("search_layer_err", method=method, err=str(e)[:100])
            prods = []
        if prods:
            log.info("products_found", store=base, method=method, n=len(prods))
            return {"ok": True, "store_url": base, "products": prods, "method": method}

    return {"ok": False, "reason": "no_products", "store_url": base}
