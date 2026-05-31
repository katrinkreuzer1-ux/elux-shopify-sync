"""
elux → Shopify Sync
====================
Scrapt shop.elux-licht.at, gleicht SKUs mit Shopify ab,
exportiert neue/fehlende Produkte nach Google Sheets.

FIX 30.05.2026: Sollux Lighting Produkte werden NICHT auf 0 gesetzt.
Schutz via Vendor "Sollux Lighting" ODER SKU-Prefix SL. / TH.
"""

import os
import re
import json
import time
import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("elux_sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

ELUX_BASE       = "https://shop.elux-licht.at/shop/pub"
SHOPIFY_SHOP    = os.environ["SHOPIFY_SHOP_URL"]
SHOPIFY_TOKEN   = os.environ["SHOPIFY_ADMIN_TOKEN"]
SHEETS_ID       = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDS    = os.environ.get("GOOGLE_CREDS_JSON", "google_creds.json")
REQUEST_DELAY   = 1.5

# Shopify REST API: Leaky Bucket mit max 40 Requests im Bucket,
# Auffüllrate = 2 Requests/Sekunde (Standard-Plan).
# 0.55s Pause = ~1.8 Req/Sek → sicher unter dem Limit, nie 429.
# Shopify Plus: 20 Req/Sek → könnte man auf 0.1s senken.
SHOPIFY_REQUEST_DELAY = 0.55

# Vendor-Namen die NIEMALS auf 0 gesetzt werden dürfen
PROTECTED_VENDORS = ["Sollux Lighting"]

# SKU-Prefixe die NIEMALS auf 0 gesetzt werden dürfen
PROTECTED_SKU_PREFIXES = ["SL.", "TH."]

# Alle Sub-Kategorieseiten — direkt von der Live-Seite geprüft
ELUX_CATEGORY_URLS = [
    # Technische Innenbeleuchtung (14 Sub-Kategorien)
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/deckenanbauleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/deckeneinbauleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/haengeleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/profile.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/downlights.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/wand-und-spiegelleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/lichtbandsysteme.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/stromschienen-und-strahler.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/tisch-und-stehleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/led-strips-treiber-und-zubehor.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/notleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/frw-nuplex-und-lichtbalken.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/steuerungen.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/interior/smart-home/casambi.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/interior/smart-home/eutrac.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/hallen-und-industrieleuchten.html",
    # Technische Außenbeleuchtung (5 Sub-Kategorien)
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/anbauleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/einbauleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/pollerleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/strahler.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/strassenleuchten.html",
    # Dekorative Beleuchtung (8 Sub-Kategorien)
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/hangeleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-beleuchtung/deckenleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/kronleuchter.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/wandleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/stehleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/tischleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/badezimmerleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/ventilatoren.html",
    # Leuchtmittel (7 Sub-Kategorien)
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-leuchtmittel-e27.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-leuchtmittel-e14.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-leuchtmittel-gu10.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/sonstige-leuchtmittel-230v.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-tubes.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-leuchtmittel-12v.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/speziallampen.html",
    # LED Umrüstungen (direkt, keine Sub-Kategorien)
    "https://shop.elux-licht.at/shop/pub/elux/produkte/led-umrustungen-und-sanierungen.html",
    # Zubehör (2 Sub-Kategorien)
    "https://shop.elux-licht.at/shop/pub/elux/produkte/zubehor/leuchtenzubehor.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/zubehor/pfahle-und-masten.html",
    # Neuheiten & Abverkauf
    "https://shop.elux-licht.at/shop/pub/elux/coming-soon.html",
    "https://shop.elux-licht.at/shop/pub/elux/derzeit-ist-keine-aktion-verfugbar.html",
]

@dataclass
class EluxVariant:
    sku: str
    name: str
    description_details: str = ""
    description_more: str = ""
    stock: int = 0
    price: str = ""
    dimensions: str = ""
    color: str = ""
    ip_rating: str = ""
    watt: str = ""
    category: str = ""
    product_url: str = ""
    image_url: str = ""
    image_urls: str = ""

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-AT,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def is_protected_sku(sku: str, vendor: str = "") -> bool:
    """
    Gibt True zurück wenn diese SKU/Variante NICHT auf 0 gesetzt werden darf.
    Schutz greift wenn:
      - Vendor in PROTECTED_VENDORS (z.B. "Sollux Lighting")
      - SKU beginnt mit einem Prefix aus PROTECTED_SKU_PREFIXES (z.B. SL. oder TH.)
    """
    if vendor and any(vendor.strip() == v for v in PROTECTED_VENDORS):
        return True
    sku_upper = sku.upper()
    if any(sku_upper.startswith(prefix.upper()) for prefix in PROTECTED_SKU_PREFIXES):
        return True
    return False


def get(url: str) -> Optional[BeautifulSoup]:
    for attempt in range(3):
        try:
            time.sleep(REQUEST_DELAY)
            r = session.get(url, timeout=25)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning(f"Versuch {attempt+1} fehlgeschlagen für {url}: {e}")
            time.sleep(4 * (attempt + 1))
    log.error(f"Alle Versuche fehlgeschlagen: {url}")
    return None


def collect_product_urls_from_category(category_url: str) -> list[str]:
    """Sammelt alle Produkt-URLs aus einer Kategorieseite (inkl. Pagination)."""
    urls = []
    page_url = category_url
    visited = set()

    while page_url and page_url not in visited:
        visited.add(page_url)
        soup = get(page_url)
        if not soup:
            break

        for a in soup.select("a.product-item-link, .product-item-info a, .product-item h2 a, li.product-item a.product-photo"):
            href = a.get("href", "")
            if href and "elux-licht.at" in href and href not in urls:
                if not any(x in href for x in ["customer", "cart", "wishlist", "search"]):
                    urls.append(href)

        next_a = soup.select_one("a.action.next, li.pages-item-next a, a[title='Nächste']")
        page_url = next_a.get("href") if next_a else None

    return list(dict.fromkeys(urls))


def parse_product(url: str, category: str) -> list[EluxVariant]:
    """Parsed eine Produktseite → alle Varianten mit SKU + Lagerstand."""
    soup = get(url)
    if not soup:
        return []

    product_name = ""
    for sel in ["h1.page-title span", "h1.page-title", "h1"]:
        el = soup.select_one(sel)
        if el:
            product_name = el.get_text(strip=True)
            break

    image_url = ""
    image_urls = ""
    all_images = []
    for img in soup.select(".gallery-placeholder img, .fotorama img, .product.media img, [data-gallery] img, .MagicSlideshow img"):
        src = img.get("src", img.get("data-src", img.get("data-original", "")))
        if src and "elux-licht.at" in src and src not in all_images:
            if not any(x in src for x in ["logo", "icon", "placeholder", "bright/"]):
                all_images.append(src)
    fotorama = soup.select_one("[data-gallery]")
    if fotorama:
        import json as _json
        try:
            gallery_data = _json.loads(fotorama.get("data-gallery", "[]"))
            for item in gallery_data:
                src = item.get("full", item.get("img", ""))
                if src and src not in all_images:
                    all_images.append(src)
        except Exception:
            pass
    if all_images:
        image_url = all_images[0]
        image_urls = " | ".join(all_images)

    price = ""
    price_el = soup.select_one(".price-box .price, [data-price-type='finalPrice'] .price")
    if price_el:
        price = price_el.get_text(strip=True)

    description_details = ""
    for sel in ["#product-attribute-specs-table", ".product.attribute.description .value",
                "#description .value", ".product-description", ".tab-content-item:first-child"]:
        el = soup.select_one(sel)
        if el:
            description_details = el.get_text(separator="\n", strip=True)
            break

    description_more = ""
    for sel in ["#additional", "#product-attribute-specs-table", ".additional-attributes"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True) != description_details:
            description_more = el.get_text(separator="\n", strip=True)
            break

    variants = []

    ausfuehrung = None
    for sel in ["#ausf-hrung", "#ausfuhrung", "#ausfuehrung", "[id*='ausfuhr']", "[id*='ausfuehr']"]:
        el = soup.select_one(sel)
        if el:
            ausfuehrung = el
            break

    if not ausfuehrung:
        for panel in soup.select(".tab-content-item, .data.item.content, [role='tabpanel']"):
            txt = panel.get_text()
            if "Lagerstand" in txt or re.search(r'\d{2,}-[A-Z0-9/]+', txt):
                ausfuehrung = panel
                break

    if ausfuehrung:
        text = ausfuehrung.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        current_sku = None
        current_desc_lines = []
        current_stock = 0

        for line in lines:
            sku_match = re.match(r'^(\d{2,}-[A-Z0-9][A-Z0-9/\-]+)', line)
            if sku_match:
                if current_sku:
                    desc = " ".join(current_desc_lines)
                    dim_m = re.search(r'(L=\s*\d+mm[^\n,]+)', desc)
                    col_m = re.search(r'(weiß|schwarz|grau|silber|chrom|gold|nickel)', desc, re.I)
                    ip_m = re.search(r'(IP\d{2})', desc)
                    w_m = re.search(r'(\d+(?:\.\d+)?\s*W)\b', desc)
                    variants.append(EluxVariant(
                        sku=current_sku,
                        name=product_name,
                        description_details=description_details,
                        description_more=description_more,
                        stock=current_stock,
                        price=price,
                        dimensions=dim_m.group(1) if dim_m else "",
                        color=col_m.group(1) if col_m else "",
                        ip_rating=ip_m.group(1) if ip_m else "",
                        watt=w_m.group(1) if w_m else "",
                        category=category,
                        product_url=url,
                        image_url=image_url,
                        image_urls=image_urls,
                    ))
                current_sku = sku_match.group(1)
                current_desc_lines = [line]
                current_stock = 0
                sm = re.search(r'Lagerstand[:\s]+(\d+)', line, re.I)
                if sm:
                    current_stock = int(sm.group(1))
            elif current_sku:
                current_desc_lines.append(line)
                sm = re.search(r'Lagerstand[:\s]+(\d+)', line, re.I)
                if sm:
                    current_stock = int(sm.group(1))

        if current_sku:
            desc = " ".join(current_desc_lines)
            dim_m = re.search(r'(L=\s*\d+mm[^\n,]+)', desc)
            col_m = re.search(r'(weiß|schwarz|grau|silber|chrom|gold|nickel)', desc, re.I)
            ip_m = re.search(r'(IP\d{2})', desc)
            w_m = re.search(r'(\d+(?:\.\d+)?\s*W)\b', desc)
            variants.append(EluxVariant(
                sku=current_sku,
                name=product_name,
                description_details=description_details,
                description_more=description_more,
                stock=current_stock,
                price=price,
                dimensions=dim_m.group(1) if dim_m else "",
                color=col_m.group(1) if col_m else "",
                ip_rating=ip_m.group(1) if ip_m else "",
                watt=w_m.group(1) if w_m else "",
                category=category,
                product_url=url,
                image_url=image_url,
                image_urls=image_urls,
            ))

    if not variants:
        for sel in [".product.attribute.sku .value", "[itemprop='sku']", ".sku .value"]:
            el = soup.select_one(sel)
            if el:
                sku = el.get_text(strip=True)
                if sku:
                    variants.append(EluxVariant(
                        sku=sku,
                        name=product_name,
                        description_details=description_details,
                        description_more=description_more,
                        stock=0,
                        price=price,
                        category=category,
                        product_url=url,
                        image_url=image_url,
                        image_urls=image_urls,
                    ))
                break

    return variants


def scrape_all() -> list[EluxVariant]:
    all_variants: list[EluxVariant] = []
    all_product_urls = []

    log.info("Sammle Produkt-URLs aus allen Kategorien...")
    for cat_url in ELUX_CATEGORY_URLS:
        category = cat_url.rstrip("/").rstrip(".html").split("/")[-1].replace("-", " ").title()
        log.info(f"  Kategorie: {category}")
        urls = collect_product_urls_from_category(cat_url)
        log.info(f"    → {len(urls)} Produkte")
        for u in urls:
            all_product_urls.append((u, category))

    seen = set()
    unique = []
    for u, c in all_product_urls:
        if u not in seen:
            seen.add(u)
            unique.append((u, c))

    log.info(f"\nGesamt {len(unique)} einzigartige Produktseiten — starte Parsing...")

    for i, (url, category) in enumerate(unique, 1):
        log.info(f"[{i}/{len(unique)}] {url}")
        vs = parse_product(url, category)
        all_variants.extend(vs)
        if vs:
            log.info(f"  → {len(vs)} Variante(n): {', '.join(v.sku for v in vs)}")
        else:
            log.warning(f"  → Keine Varianten gefunden")

    log.info(f"\nScraping fertig: {len(all_variants)} Varianten gesamt")
    return all_variants


def get_shopify_skus() -> tuple[dict, dict]:
    """
    Lädt alle Shopify-Varianten inkl. Vendor + inventory_management + published.
    Gibt zurück:
      - shopify_skus:     {sku: {...}} für Varianten-Abgleich
      - shopify_products: {product_id: {vendor, published, skus: [...]}} für publish/unpublish
    """
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    shopify_skus = {}
    shopify_products = {}
    page_url = f"https://{SHOPIFY_SHOP}/admin/api/2024-01/products.json?limit=250&fields=id,vendor,published_at,variants"

    while page_url:
        r = requests.get(page_url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        for product in data.get("products", []):
            vendor = product.get("vendor", "")
            product_id = product["id"]
            # published_at ist None wenn das Produkt versteckt ist
            is_published = product.get("published_at") is not None

            shopify_products[product_id] = {
                "vendor": vendor,
                "published": is_published,
                "skus": [],
            }

            for v in product.get("variants", []):
                if v.get("sku"):
                    sku = v["sku"].strip()
                    shopify_skus[sku] = {
                        "variant_id": v["id"],
                        "product_id": product_id,
                        "inventory_item_id": v["inventory_item_id"],
                        "stock": v["inventory_quantity"],
                        "title": v.get("title", ""),
                        "vendor": vendor,
                        "inventory_management": v.get("inventory_management"),
                    }
                    shopify_products[product_id]["skus"].append(sku)

        link = r.headers.get("Link", "")
        page_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                page_url = part.split(";")[0].strip().strip("<>")

    log.info(f"Shopify: {len(shopify_skus)} Varianten, {len(shopify_products)} Produkte geladen")
    return shopify_skus, shopify_products


def enable_inventory_tracking(variant_id: int, max_retries: int = 5) -> bool:
    """
    Aktiviert Inventory-Tracking für eine Variante (inventory_management = 'shopify').
    Muss aufgerufen werden BEVOR inventory_levels/set.json verwendet wird.
    Gibt True zurück wenn erfolgreich, False wenn fehlgeschlagen.
    """
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }

    # Vorbeugend warten – BEVOR der Request gesendet wird
    time.sleep(SHOPIFY_REQUEST_DELAY)

    for attempt in range(max_retries):
        r = requests.put(
            f"https://{SHOPIFY_SHOP}/admin/api/2024-01/variants/{variant_id}.json",
            json={"variant": {"id": variant_id, "inventory_management": "shopify"}},
            headers=headers, timeout=15,
        )

        if r.status_code == 200:
            return True

        elif r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", 10))
            log.warning(f"    429 Rate Limit (Tracking-Aktivierung) – warte {retry_after:.0f}s...")
            time.sleep(retry_after + 1)

        else:
            log.warning(f"    HTTP {r.status_code} beim Aktivieren von Tracking (Versuch {attempt+1}/{max_retries})")
            time.sleep(3 * (attempt + 1))

    log.error(f"    Tracking konnte nicht aktiviert werden für Variante {variant_id}")
    return False


def get_shopify_location_id() -> str:
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    r = requests.get(
        f"https://{SHOPIFY_SHOP}/admin/api/2024-01/locations.json",
        headers=headers, timeout=15
    )
    r.raise_for_status()
    locs = r.json().get("locations", [])
    if not locs:
        raise ValueError("Keine Shopify Location gefunden!")
    return str(locs[0]["id"])


def update_shopify_stock(inventory_item_id: int, quantity: int, location_id: str, max_retries: int = 5):
    """
    Setzt den Lagerstand in Shopify.
    - Wartet SHOPIFY_REQUEST_DELAY Sekunden VOR jedem Request (vorbeugend)
    - Bei 429 (Too Many Requests): wartet Retry-After Sekunden + nochmal versuchen
    - Bei 422 (Unprocessable Entity): überspringen, nicht retrien
    """
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }

    # Vorbeugend warten – BEVOR der Request gesendet wird
    # Shopify erlaubt 2 Req/Sek, wir bleiben mit 0.6s sicher darunter
    time.sleep(SHOPIFY_REQUEST_DELAY)

    for attempt in range(max_retries):
        r = requests.post(
            f"https://{SHOPIFY_SHOP}/admin/api/2024-01/inventory_levels/set.json",
            json={"location_id": location_id, "inventory_item_id": inventory_item_id, "available": quantity},
            headers=headers, timeout=15
        )

        if r.status_code == 200:
            return  # Erfolg

        elif r.status_code == 429:
            # Rate Limit erreicht → warten und nochmal
            retry_after = float(r.headers.get("Retry-After", 10))
            log.warning(f"    429 Rate Limit – warte {retry_after:.0f}s (Versuch {attempt+1}/{max_retries})...")
            time.sleep(retry_after + 1)

        elif r.status_code == 422:
            # Ungültige inventory_item_id → nicht retrien, einfach überspringen
            log.warning(f"    422 Unprocessable – inventory_item_id {inventory_item_id} ungültig, übersprungen.")
            return

        else:
            # Anderer Fehler → kurz warten und nochmal
            log.warning(f"    HTTP {r.status_code} – Versuch {attempt+1}/{max_retries}...")
            time.sleep(3 * (attempt + 1))

    # Alle Versuche fehlgeschlagen
    raise Exception(f"Shopify Update fehlgeschlagen nach {max_retries} Versuchen (inventory_item_id={inventory_item_id})")


def set_product_published(product_id: int, published: bool, max_retries: int = 5) -> bool:
    """
    Setzt ein Produkt auf sichtbar (published=True) oder unsichtbar (published=False).
    Wird aufgerufen wenn:
      - Alle Varianten eines Produkts Lagerstand 0 haben → verstecken
      - Mindestens eine Variante > 0 hat → wieder sichtbar machen
    Sollux-Produkte werden NIE hier aufgerufen (Schutz in run_sync).
    """
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }

    time.sleep(SHOPIFY_REQUEST_DELAY)

    for attempt in range(max_retries):
        r = requests.put(
            f"https://{SHOPIFY_SHOP}/admin/api/2024-01/products/{product_id}.json",
            json={"product": {"id": product_id, "published": published}},
            headers=headers, timeout=15,
        )

        if r.status_code == 200:
            return True

        elif r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", 10))
            log.warning(f"    429 Rate Limit (published) – warte {retry_after:.0f}s...")
            time.sleep(retry_after + 1)

        else:
            log.warning(f"    HTTP {r.status_code} beim Setzen von published={published} (Versuch {attempt+1}/{max_retries})")
            time.sleep(3 * (attempt + 1))

    log.error(f"    published konnte nicht gesetzt werden für Produkt {product_id}")
    return False


def get_sheets_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if os.path.exists(GOOGLE_CREDS):
        creds = Credentials.from_service_account_file(GOOGLE_CREDS, scopes=scopes)
    else:
        import json as _j
        creds = Credentials.from_service_account_info(
            _j.loads(os.environ["GOOGLE_CREDS_JSON_CONTENT"]), scopes=scopes
        )
    return gspread.authorize(creds)


def export_to_sheets(new_products: list, delisted: list):
    gc = get_sheets_client()
    sh = gc.open_by_key(SHEETS_ID)
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Tab A: Neue Produkte
    try:
        ws = sh.worksheet("Neue Produkte")
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Neue Produkte", 5000, 15)

    rows = [["SKU","Name","Kategorie","Beschreibung Details","Beschreibung Mehr","Lagerstand","Preis","Maße","Farbe","IP","Watt","Bild-URL (Haupt)","Alle Bild-URLs","Produkt-URL","Datum"]]
    for v in new_products:
        rows.append([v.sku, v.name, v.category, v.description_details[:500], v.description_more[:300],
                     v.stock, v.price, v.dimensions, v.color, v.ip_rating, v.watt, v.image_url, v.image_urls, v.product_url, ts])
    ws.update(rows, "A1")
    ws.format("A1:N1", {"textFormat": {"bold": True}})

    # Tab B: Nicht mehr lieferbar
    try:
        ws2 = sh.worksheet("Nicht mehr lieferbar")
        ws2.clear()
    except gspread.WorksheetNotFound:
        ws2 = sh.add_worksheet("Nicht mehr lieferbar", 2000, 7)

    rows2 = [["SKU","Name","Produkt-ID","Varianten-ID","Alter Bestand","Aktion","Datum"]]
    for p in delisted:
        rows2.append([p["sku"], p.get("title",""), p.get("product_id",""), p.get("variant_id",""),
                      p.get("stock",0), "Lagerstand auf 0 gesetzt", ts])
    ws2.update(rows2, "A1")
    ws2.format("A1:G1", {"textFormat": {"bold": True}})

    log.info(f"Sheets: {len(new_products)} neue + {len(delisted)} ausgelistete Produkte exportiert")


def run_sync():
    log.info("=" * 60)
    log.info(f"elux → Shopify Sync: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    log.info("=" * 60)

    log.info("\n[1/4] Scrape elux...")
    elux_variants = scrape_all()
    elux_by_sku = {v.sku: v for v in elux_variants}

    with open("elux_products_raw.json", "w", encoding="utf-8") as f:
        json.dump([vars(v) for v in elux_variants], f, ensure_ascii=False, indent=2)

    log.info("\n[2/4] Lade Shopify...")
    shopify_skus, shopify_products = get_shopify_skus()
    location_id = get_shopify_location_id()

    log.info("\n[3/4] Abgleich...")
    updated, new_products, delisted, errors, skipped_protected, tracking_enabled = [], [], [], [], [], []
    hidden_products, restored_products = [], []

    # Elux-SKUs → Shopify aktualisieren
    for sku, ev in elux_by_sku.items():
        if sku in shopify_skus:
            sv = shopify_skus[sku]

            # Tracking aktivieren falls nötig
            if sv.get("inventory_management") != "shopify":
                log.info(f"  🔧 Tracking aktivieren für {sku}...")
                ok = enable_inventory_tracking(sv["variant_id"])
                if not ok:
                    log.error(f"  ✗ Tracking konnte nicht aktiviert werden: {sku}")
                    errors.append(sku)
                    continue
                tracking_enabled.append(sku)
                # Nach Aktivierung kurz warten damit Shopify den Status übernimmt
                time.sleep(1)

            if sv["stock"] != ev.stock:
                try:
                    update_shopify_stock(sv["inventory_item_id"], ev.stock, location_id)
                    log.info(f"  ✓ {sku}: {sv['stock']} → {ev.stock}")
                    updated.append(sku)
                except Exception as e:
                    log.error(f"  ✗ {sku}: {e}")
                    errors.append(sku)
        else:
            new_products.append(ev)

    # Shopify-SKUs die nicht in Elux sind → auf 0 setzen
    # ABER: Geschützte Lieferanten (Sollux Lighting etc.) NIEMALS anfassen!
    for sku, sv in shopify_skus.items():
        if sku not in elux_by_sku:
            vendor = sv.get("vendor", "")

            # ── SCHUTZ ──────────────────────────────────────────────────
            if is_protected_sku(sku, vendor):
                log.info(f"  🔒 Geschützt (nicht auf 0): {sku} [{vendor}]")
                skipped_protected.append(sku)
                continue
            # ────────────────────────────────────────────────────────────

            if sv["stock"] != 0:
                try:
                    update_shopify_stock(sv["inventory_item_id"], 0, location_id)
                    log.warning(f"  ⚠ {sku} → 0")
                except Exception as e:
                    log.error(f"  ✗ Auslistung {sku}: {e}")
            delisted.append({**sv, "sku": sku})
            # Hinweis: Sollux-Produkte landen nie hier (continue oben)

    log.info("\n[4/4] Produkt-Sichtbarkeit prüfen...")
    # Alle Elux-Produkte: alle Varianten = 0 → verstecken / mind. 1 > 0 → zeigen
    # Sollux-Produkte werden NIEMALS angefasst!
    for product_id, pdata in shopify_products.items():
        vendor = pdata.get("vendor", "")

        # Sollux komplett überspringen
        if any(vendor.strip() == v for v in PROTECTED_VENDORS):
            continue
        if pdata["skus"] and all(is_protected_sku(s) for s in pdata["skus"]):
            continue

        # Gesamtlagerstand des Produkts berechnen (aktualisierte Werte)
        total_stock = 0
        for sku in pdata["skus"]:
            if sku in elux_by_sku:
                total_stock += elux_by_sku[sku].stock
            elif sku in shopify_skus:
                total_stock += shopify_skus[sku]["stock"]

        currently_published = pdata["published"]

        if total_stock == 0 and currently_published:
            ok = set_product_published(product_id, False)
            if ok:
                log.info(f"  👁 Versteckt (alle 0): Produkt {product_id} [{vendor}]")
                hidden_products.append(product_id)
            else:
                log.error(f"  ✗ Konnte Produkt {product_id} nicht verstecken")

        elif total_stock > 0 and not currently_published:
            ok = set_product_published(product_id, True)
            if ok:
                log.info(f"  ✅ Wieder sichtbar: Produkt {product_id} [{vendor}]")
                restored_products.append(product_id)
            else:
                log.error(f"  ✗ Konnte Produkt {product_id} nicht aktivieren")

    log.info("\n[5/5] Google Sheets Export...")
    if new_products or delisted:
        export_to_sheets(new_products, delisted)

    log.info("\n" + "=" * 60)
    log.info(f"Aktualisiert:        {len(updated)}")
    log.info(f"Tracking aktiviert:  {len(tracking_enabled)}")
    log.info(f"Neu (Sheet A):       {len(new_products)}")
    log.info(f"Ausgelistet:         {len(delisted)}")
    log.info(f"Geschützt (Sollux):  {len(skipped_protected)}")
    log.info(f"Versteckt (alle 0):  {len(hidden_products)}")
    log.info(f"Wieder sichtbar:     {len(restored_products)}")
    log.info(f"Fehler:              {len(errors)}")
    log.info("=" * 60)


if __name__ == "__main__":
    run_sync()
