"""
elux → Shopify Sync
====================
Scrapt shop.elux-licht.at, gleicht SKUs mit Shopify ab,
exportiert neue/fehlende Produkte nach Google Sheets.
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
import shopify
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

# Alle bekannten Kategorieseiten (direkt von der Live-Seite)
ELUX_CATEGORY_URLS = [
    # Technische Innenbeleuchtung
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/deckenanbauleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/deckeneinbauleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/haengeleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/profile.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/downlights.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/wand-und-spiegelleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/tisch-und-stehleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/stromschienen-und-strahler.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/lichtbandsysteme.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/led-strips-treiber-und-zubehor.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/frw-nuplex-und-lichtbalken.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/hallen-und-industrieleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/notleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-innenbeleuchtung/steuerungen.html",
    # Technische Außenbeleuchtung
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung.html",
    # Dekorative Beleuchtung
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten.html",
    # Leuchtmittel
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel.html",
    # LED Umrüstungen
    "https://shop.elux-licht.at/shop/pub/elux/produkte/led-umrustungen-und-sanierungen.html",
    # Zubehör
    "https://shop.elux-licht.at/shop/pub/elux/produkte/zubehor.html",
    # Restposten
    "https://shop.elux-licht.at/shop/pub/elux/restposten.html",
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

        # Produkt-Links — Magento-typische Selektoren
        for a in soup.select("a.product-item-link, .product-item-info a, .product-item h2 a, li.product-item a.product-photo"):
            href = a.get("href", "")
            if href and "elux-licht.at" in href and href not in urls:
                # Nur echte Produktseiten (nicht Kategorien)
                if not any(x in href for x in ["/produkte/", "/kategorie/", "customer", "cart", "wishlist", "search"]):
                    urls.append(href)

        # Pagination — nächste Seite
        next_a = soup.select_one("a.action.next, li.pages-item-next a, a[title='Nächste']")
        page_url = next_a.get("href") if next_a else None

    return list(dict.fromkeys(urls))  # Reihenfolge erhalten, Duplikate weg

def parse_product(url: str, category: str) -> list[EluxVariant]:
    """Parsed eine Produktseite → alle Varianten mit SKU + Lagerstand."""
    soup = get(url)
    if not soup:
        return []

    # Produktname
    product_name = ""
    for sel in ["h1.page-title span", "h1.page-title", "h1"]:
        el = soup.select_one(sel)
        if el:
            product_name = el.get_text(strip=True)
            break

    # Bild
    image_url = ""
    img = soup.select_one(".gallery-placeholder img, .fotorama__img, .product.media img")
    if img:
        image_url = img.get("src", img.get("data-src", ""))

    # Preis
    price = ""
    price_el = soup.select_one(".price-box .price, [data-price-type='finalPrice'] .price")
    if price_el:
        price = price_el.get_text(strip=True)

    # Beschreibung Details-Tab
    description_details = ""
    for sel in ["#product-attribute-specs-table", ".product.attribute.description .value",
                "#description .value", ".product-description", ".tab-content-item:first-child"]:
        el = soup.select_one(sel)
        if el:
            description_details = el.get_text(separator="\n", strip=True)
            break

    # Mehr Informationen Tab
    description_more = ""
    for sel in ["#additional", "#product-attribute-specs-table", ".additional-attributes"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True) != description_details:
            description_more = el.get_text(separator="\n", strip=True)
            break

    # Ausführung Tab — enthält die Varianten mit SKU + Lagerstand
    variants = []

    # Suche nach dem Ausführungs-Tab-Inhalt
    ausfuehrung = None
    for sel in ["#ausf-hrung", "#ausfuhrung", "#ausfuehrung", "[id*='ausfuhr']", "[id*='ausfuehr']"]:
        el = soup.select_one(sel)
        if el:
            ausfuehrung = el
            break

    # Fallback: alle Tab-Panels durchsuchen
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
            # Zeile die mit SKU beginnt (z.B. "85-TR32W/1245")
            sku_match = re.match(r'^(\d{2,}-[A-Z0-9][A-Z0-9/\-]+)', line)
            if sku_match:
                # Vorherige Variante speichern
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
                    ))
                current_sku = sku_match.group(1)
                current_desc_lines = [line]
                current_stock = 0
                # Lagerstand direkt in dieser Zeile?
                sm = re.search(r'Lagerstand[:\s]+(\d+)', line, re.I)
                if sm:
                    current_stock = int(sm.group(1))

            elif current_sku:
                # Folgezeilen zur aktuellen Variante
                current_desc_lines.append(line)
                sm = re.search(r'Lagerstand[:\s]+(\d+)', line, re.I)
                if sm:
                    current_stock = int(sm.group(1))

        # Letzte Variante speichern
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
            ))

    # Wenn keine Varianten → Hauptprodukt mit SKU aus der Seite
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

    # Duplikate nach URL entfernen
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

def get_shopify_skus() -> dict:
    url = f"https://{SHOPIFY_SHOP}/admin/api/2024-01/variants.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    result = {}
    page_url = f"{url}?limit=250"

    while page_url:
        r = requests.get(page_url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        for v in data.get("variants", []):
            if v.get("sku"):
                result[v["sku"].strip()] = {
                    "variant_id": v["id"],
                    "product_id": v["product_id"],
                    "inventory_item_id": v["inventory_item_id"],
                    "stock": v["inventory_quantity"],
                    "title": v.get("title", ""),
                }
        # Link-Header für Pagination
        link = r.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        page_url = next_url

    log.info(f"Shopify: {len(result)} Varianten geladen")
    return result

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

def update_shopify_stock(inventory_item_id: int, quantity: int, location_id: str):
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }
    r = requests.post(
        f"https://{SHOPIFY_SHOP}/admin/api/2024-01/inventory_levels/set.json",
        json={"location_id": location_id, "inventory_item_id": inventory_item_id, "available": quantity},
        headers=headers, timeout=15
    )
    r.raise_for_status()

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

    rows = [["SKU","Name","Kategorie","Beschreibung Details","Beschreibung Mehr","Lagerstand","Preis","Maße","Farbe","IP","Watt","Bild-URL","Produkt-URL","Datum"]]
    for v in new_products:
        rows.append([v.sku, v.name, v.category, v.description_details[:500], v.description_more[:300],
                     v.stock, v.price, v.dimensions, v.color, v.ip_rating, v.watt, v.image_url, v.product_url, ts])
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
    shopify_skus = get_shopify_skus()
    location_id = get_shopify_location_id()

    log.info("\n[3/4] Abgleich...")
    updated, new_products, delisted, errors = [], [], [], []

    for sku, ev in elux_by_sku.items():
        if sku in shopify_skus:
            sv = shopify_skus[sku]
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

    for sku, sv in shopify_skus.items():
        if sku not in elux_by_sku:
            if sv["stock"] != 0:
                try:
                    update_shopify_stock(sv["inventory_item_id"], 0, location_id)
                    log.warning(f"  ⚠ {sku} → 0")
                except Exception as e:
                    log.error(f"  ✗ Auslistung {sku}: {e}")
            delisted.append({**sv, "sku": sku})

    log.info("\n[4/4] Google Sheets Export...")
    if new_products or delisted:
        export_to_sheets(new_products, delisted)

    log.info("\n" + "=" * 60)
    log.info(f"Aktualisiert:  {len(updated)}")
    log.info(f"Neu (Sheet A): {len(new_products)}")
    log.info(f"Ausgelistet:   {len(delisted)}")
    log.info(f"Fehler:        {len(errors)}")
    log.info("=" * 60)

if __name__ == "__main__":
    run_sync()
