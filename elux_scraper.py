"""
elux → Shopify Sync
====================
Scrapt shop.elux-licht.at, gleicht SKUs mit Shopify ab,
exportiert neue/fehlende Produkte nach Google Sheets.

Autor: luxomo.at
"""

import os
import re
import json
import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
import shopify
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ────────────────────────────────────────────────────────────────
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

# ─── Konfiguration ──────────────────────────────────────────────────────────
ELUX_BASE        = "https://shop.elux-licht.at/shop/pub"
SHOPIFY_SHOP     = os.environ["SHOPIFY_SHOP_URL"]          # z.B. luxomo.myshopify.com
SHOPIFY_TOKEN    = os.environ["SHOPIFY_ADMIN_TOKEN"]
SHEETS_ID        = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDS_JSON= os.environ.get("GOOGLE_CREDS_JSON", "google_creds.json")
REQUEST_DELAY    = 1.2   # Sekunden zwischen Requests (höfliches Scraping)

# Alle Haupt-Navigationspfade auf elux
ELUX_SECTIONS = [
    "/kategorie/technische-innenbeleuchtung",
    "/kategorie/technische-aussenbeleuchtung",
    "/kategorie/dekorative-beleuchtung",
    "/kategorie/leuchtmittel",
    "/kategorie/led-umruestungen-und-sanierungen",
    "/kategorie/zubehoer",
    "/neuheiten",
    "/restposten",
    "/abverkauf",
]

# ─── Datenstruktur ───────────────────────────────────────────────────────────
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

# ─── HTTP Session ─────────────────────────────────────────────────────────────
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-AT,de;q=0.9",
})

def get(url: str) -> Optional[BeautifulSoup]:
    """GET mit Retry-Logik und Rate-Limiting."""
    for attempt in range(3):
        try:
            time.sleep(REQUEST_DELAY)
            r = session.get(url, timeout=20)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning(f"Versuch {attempt+1} fehlgeschlagen für {url}: {e}")
            time.sleep(3 * (attempt + 1))
    log.error(f"Alle Versuche fehlgeschlagen: {url}")
    return None

# ─── Kategorie-Crawler ───────────────────────────────────────────────────────
def collect_product_urls(section_path: str) -> list[str]:
    """Sammelt alle Produkt-URLs aus einer Kategorie (inkl. Sub-Kategorien + Pagination)."""
    urls = []
    to_visit = [ELUX_BASE + section_path]
    visited = set()

    while to_visit:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)

        soup = get(url)
        if not soup:
            continue

        # Sub-Kategorien Links
        for a in soup.select(".category-list a, .subcategory a, nav.breadcrumb + div a"):
            href = a.get("href", "")
            if "/kategorie/" in href and href not in visited:
                full = href if href.startswith("http") else ELUX_BASE + href
                to_visit.append(full)

        # Produkt-Links auf dieser Seite
        for a in soup.select("a.product-item, .product-grid a, .product-list a, article a"):
            href = a.get("href", "")
            if href and "/produkt/" in href:
                full = href if href.startswith("http") else ELUX_BASE + href
                urls.append(full)

        # Nächste Seite (Pagination)
        next_page = soup.select_one("a.next, a[rel='next'], .pagination .next a")
        if next_page:
            href = next_page.get("href", "")
            if href and href not in visited:
                to_visit.append(href if href.startswith("http") else ELUX_BASE + href)

    log.info(f"  {section_path}: {len(urls)} Produkte gefunden")
    return list(set(urls))  # Duplikate entfernen

# ─── Produkt-Parser ──────────────────────────────────────────────────────────
def parse_product(url: str, category: str) -> list[EluxVariant]:
    """
    Parsed eine Produktseite und gibt alle Varianten zurück.
    Jede Variante = eine SKU mit eigenem Lagerstand.
    """
    soup = get(url)
    if not soup:
        return []

    # Produktname
    product_name = ""
    for sel in ["h1.product-title", "h1", ".product-name h1"]:
        el = soup.select_one(sel)
        if el:
            product_name = el.get_text(strip=True)
            break

    # Bild-URL
    image_url = ""
    img = soup.select_one(".product-image img, .product-gallery img, img.main-image")
    if img:
        image_url = img.get("src", img.get("data-src", ""))
        if image_url and not image_url.startswith("http"):
            image_url = "https://shop.elux-licht.at" + image_url

    # Preis
    price = ""
    price_el = soup.select_one(".price, .product-price, [class*='price']")
    if price_el:
        price = price_el.get_text(strip=True)

    # ── Tab: Details ──────────────────────────────────────────────────────────
    description_details = ""
    # Versuche den Inhalt des Details-Tabs zu finden
    for sel in [
        "#details", "[id*='details']", ".tab-content #details",
        "div[data-tab='details']", ".tab-pane:first-child",
    ]:
        el = soup.select_one(sel)
        if el:
            description_details = el.get_text(separator="\n", strip=True)
            break
    # Fallback: Suche nach dem Tab-Button und nimm den nächsten Content-Block
    if not description_details:
        tabs = soup.select(".tab-content .tab-pane, .tabs-content > div")
        if tabs:
            description_details = tabs[0].get_text(separator="\n", strip=True)

    # ── Tab: Mehr Informationen ───────────────────────────────────────────────
    description_more = ""
    for sel in [
        "#mehr-informationen", "[id*='mehr']", "[id*='more']",
        "div[data-tab='mehr-informationen']",
    ]:
        el = soup.select_one(sel)
        if el:
            description_more = el.get_text(separator="\n", strip=True)
            break
    if not description_more:
        tabs = soup.select(".tab-content .tab-pane, .tabs-content > div")
        if len(tabs) > 1:
            description_more = tabs[1].get_text(separator="\n", strip=True)

    # ── Tab: Ausführung → Varianten + Lagerstand ─────────────────────────────
    variants = []

    # Suche den Ausführung-Tab-Inhalt
    ausfuehrung_content = None
    for sel in [
        "#ausfuehrung", "[id*='ausfuehrung']", "[id*='ausfuhrung']",
        "div[data-tab='ausfuehrung']",
    ]:
        el = soup.select_one(sel)
        if el:
            ausfuehrung_content = el
            break
    if not ausfuehrung_content:
        tabs = soup.select(".tab-content .tab-pane, .tabs-content > div")
        if len(tabs) > 2:
            ausfuehrung_content = tabs[2]

    if ausfuehrung_content:
        # Jede Variante ist typischerweise eine Zeile/Row mit SKU + Beschreibung
        # Beispiel: "85-TR32W/1245" in einer Zelle, "Lagerstand: 13" irgendwo in der Zeile
        rows = ausfuehrung_content.select("tr, .variant-row, .product-variant, li.variant")

        if rows:
            for row in rows:
                row_text = row.get_text(separator=" | ", strip=True)
                if not row_text.strip():
                    continue

                # SKU extrahieren — Muster: Zahlen + Buchstaben + Schrägstrich
                # Typisch bei elux: "85-TR32W/1245", "LED-123/AB", etc.
                sku_match = re.search(
                    r'\b(\d{2,}-[A-Z0-9]+[/\-][A-Z0-9]+(?:[/\-][A-Z0-9]+)*)\b',
                    row_text
                )
                if not sku_match:
                    # Fallback: erstes "Wort" das wie eine Artikelnummer aussieht
                    sku_match = re.search(r'\b([A-Z]{0,3}\d{2,}[A-Z0-9/\-]{3,})\b', row_text)
                if not sku_match:
                    continue

                sku = sku_match.group(1)

                # Lagerstand
                stock = 0
                stock_match = re.search(r'Lagerstand[:\s]+(\d+)', row_text, re.IGNORECASE)
                if stock_match:
                    stock = int(stock_match.group(1))

                # Maße
                dimensions = ""
                dim_match = re.search(r'(L=\s*\d+mm[^,\n]+)', row_text)
                if dim_match:
                    dimensions = dim_match.group(1).strip()

                # Farbe / IP
                color = ""
                color_match = re.search(r'(weiß|schwarz|grau|silber|chrom|gold|nickel)', row_text, re.IGNORECASE)
                if color_match:
                    color = color_match.group(1)

                ip_match = re.search(r'(IP\d{2})', row_text)
                ip_rating = ip_match.group(1) if ip_match else ""

                watt_match = re.search(r'(\d+(?:\.\d+)?)\s*W\b', row_text)
                watt = watt_match.group(0) if watt_match else ""

                # Varianten-spezifische Beschreibung (die Zeile selbst)
                variant_desc = row.get_text(separator=" ", strip=True)

                variants.append(EluxVariant(
                    sku=sku,
                    name=product_name,
                    description_details=description_details,
                    description_more=description_more,
                    stock=stock,
                    price=price,
                    dimensions=dimensions,
                    color=color,
                    ip_rating=ip_rating,
                    watt=watt,
                    category=category,
                    product_url=url,
                    image_url=image_url,
                ))
        else:
            # Kein strukturiertes Tabellen-Layout — freier Text-Fallback
            text = ausfuehrung_content.get_text(separator="\n", strip=True)
            # Jede Zeile die mit einer Artikelnummer beginnt
            for line in text.splitlines():
                sku_match = re.match(r'^(\d{2,}-[A-Z0-9/\-]+)', line.strip())
                if not sku_match:
                    continue
                sku = sku_match.group(1)
                stock = 0
                stock_match = re.search(r'Lagerstand[:\s]+(\d+)', line, re.IGNORECASE)
                if stock_match:
                    stock = int(stock_match.group(1))
                variants.append(EluxVariant(
                    sku=sku,
                    name=product_name,
                    description_details=description_details,
                    description_more=description_more,
                    stock=stock,
                    price=price,
                    category=category,
                    product_url=url,
                    image_url=image_url,
                ))

    # Wenn gar keine Varianten gefunden: Produkt mit Basis-SKU anlegen
    if not variants:
        sku_el = soup.select_one(".sku, .article-number, [class*='sku'], [class*='artikel']")
        if sku_el:
            sku_text = sku_el.get_text(strip=True)
            sku_match = re.search(r'[\w\-/]+', sku_text)
            if sku_match:
                stock = 0
                stock_el = soup.select_one(".stock, .lagerstand, [class*='stock']")
                if stock_el:
                    sm = re.search(r'\d+', stock_el.get_text())
                    stock = int(sm.group()) if sm else 0
                variants.append(EluxVariant(
                    sku=sku_match.group(),
                    name=product_name,
                    description_details=description_details,
                    description_more=description_more,
                    stock=stock,
                    price=price,
                    category=category,
                    product_url=url,
                    image_url=image_url,
                ))

    return variants

# ─── Hauptscraper ─────────────────────────────────────────────────────────────
def scrape_all() -> list[EluxVariant]:
    """Scrapt alle Sektionen und gibt eine flache Liste aller Varianten zurück."""
    all_variants: list[EluxVariant] = []

    for section in ELUX_SECTIONS:
        log.info(f"Scrape Sektion: {section}")
        product_urls = collect_product_urls(section)
        category = section.strip("/").replace("-", " ").title()

        for i, url in enumerate(product_urls, 1):
            log.info(f"  [{i}/{len(product_urls)}] {url}")
            variants = parse_product(url, category)
            all_variants.extend(variants)
            if variants:
                skus = [v.sku for v in variants]
                log.info(f"    → {len(variants)} Variante(n): {', '.join(skus)}")

    log.info(f"\nScraping abgeschlossen: {len(all_variants)} Varianten total")
    return all_variants

# ─── Shopify ──────────────────────────────────────────────────────────────────
def get_shopify_skus() -> dict[str, dict]:
    """
    Lädt alle Varianten aus Shopify.
    Gibt dict zurück: { sku: { 'variant_id': ..., 'product_id': ..., 'inventory_item_id': ..., 'stock': ... } }
    """
    shopify.ShopifyResource.set_site(f"https://{SHOPIFY_SHOP}/admin/api/2024-01")
    shopify.ShopifyResource.activate_session(
        shopify.Session(SHOPIFY_SHOP, "2024-01", SHOPIFY_TOKEN)
    )

    result = {}
    page = shopify.Variant.find(limit=250)
    while page:
        for v in page:
            if v.sku:
                result[v.sku.strip()] = {
                    "variant_id": v.id,
                    "product_id": v.product_id,
                    "inventory_item_id": v.inventory_item_id,
                    "stock": v.inventory_quantity,
                    "title": v.title,
                }
        if page.has_next_page():
            page = page.next_page()
        else:
            break

    log.info(f"Shopify: {len(result)} Varianten geladen")
    return result

def update_shopify_stock(inventory_item_id: int, quantity: int, location_id: str):
    """Aktualisiert den Lagerstand einer Variante in Shopify."""
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }
    url = f"https://{SHOPIFY_SHOP}/admin/api/2024-01/inventory_levels/set.json"
    payload = {
        "location_id": location_id,
        "inventory_item_id": inventory_item_id,
        "available": quantity,
    }
    r = requests.post(url, json=payload, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

def get_shopify_location_id() -> str:
    """Gibt die erste Location-ID des Shopify-Shops zurück."""
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    r = requests.get(
        f"https://{SHOPIFY_SHOP}/admin/api/2024-01/locations.json",
        headers=headers, timeout=15
    )
    r.raise_for_status()
    locations = r.json().get("locations", [])
    if not locations:
        raise ValueError("Keine Shopify Location gefunden!")
    return str(locations[0]["id"])

# ─── Google Sheets ────────────────────────────────────────────────────────────
def get_sheets_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if os.path.exists(GOOGLE_CREDS_JSON):
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_JSON, scopes=scopes)
    else:
        # GitHub Actions: JSON als Env-Variable
        import json as _json
        creds_data = _json.loads(os.environ["GOOGLE_CREDS_JSON_CONTENT"])
        creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    return gspread.authorize(creds)

def export_to_sheets(
    new_products: list[EluxVariant],
    delisted_products: list[dict],
):
    """
    Tab A "Neue Produkte" — in elux aber nicht in Shopify.
    Tab B "Nicht mehr lieferbar" — in Shopify aber nicht in elux (Bestand auf 0 gesetzt).
    """
    gc = get_sheets_client()
    sh = gc.open_by_key(SHEETS_ID)
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")

    # ── Tab A: Neue Produkte ──────────────────────────────────────────────────
    try:
        ws_new = sh.worksheet("Neue Produkte")
        ws_new.clear()
    except gspread.WorksheetNotFound:
        ws_new = sh.add_worksheet("Neue Produkte", rows=5000, cols=20)

    headers_new = [
        "SKU", "Produktname", "Kategorie", "Beschreibung (Details)",
        "Beschreibung (Mehr Info)", "Lagerstand (elux)", "Preis",
        "Maße", "Farbe", "IP-Schutz", "Watt", "Bild-URL", "Produkt-URL",
        "Zuletzt aktualisiert",
    ]
    rows_new = [headers_new]
    for v in new_products:
        rows_new.append([
            v.sku, v.name, v.category,
            v.description_details[:500],   # Shopify-freundliche Länge
            v.description_more[:300],
            v.stock, v.price, v.dimensions,
            v.color, v.ip_rating, v.watt,
            v.image_url, v.product_url, ts,
        ])
    ws_new.update(rows_new, "A1")
    # Header fett
    ws_new.format("A1:N1", {"textFormat": {"bold": True}})
    log.info(f"Google Sheets Tab A: {len(new_products)} neue Produkte exportiert")

    # ── Tab B: Nicht mehr lieferbar ───────────────────────────────────────────
    try:
        ws_del = sh.worksheet("Nicht mehr lieferbar")
        ws_del.clear()
    except gspread.WorksheetNotFound:
        ws_del = sh.add_worksheet("Nicht mehr lieferbar", rows=2000, cols=10)

    headers_del = [
        "SKU", "Shopify Produktname", "Shopify Produkt-ID",
        "Shopify Varianten-ID", "Alter Lagerstand", "Aktion", "Datum",
    ]
    rows_del = [headers_del]
    for p in delisted_products:
        rows_del.append([
            p["sku"], p.get("title",""), p.get("product_id",""),
            p.get("variant_id",""), p.get("stock", 0),
            "Lagerstand auf 0 gesetzt", ts,
        ])
    ws_del.update(rows_del, "A1")
    ws_del.format("A1:G1", {"textFormat": {"bold": True}})
    log.info(f"Google Sheets Tab B: {len(delisted_products)} ausgelistete Produkte exportiert")

# ─── Haupt-Sync-Logik ─────────────────────────────────────────────────────────
def run_sync():
    log.info("=" * 60)
    log.info(f"elux → Shopify Sync gestartet: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    log.info("=" * 60)

    # 1. elux scrapen
    log.info("\n[1/4] Scrape elux...")
    elux_variants = scrape_all()
    elux_by_sku = {v.sku: v for v in elux_variants}

    # Zwischenspeichern (für Debugging)
    with open("elux_products_raw.json", "w", encoding="utf-8") as f:
        json.dump(
            [vars(v) for v in elux_variants], f,
            ensure_ascii=False, indent=2
        )
    log.info(f"Zwischenstand gespeichert: elux_products_raw.json")

    # 2. Shopify laden
    log.info("\n[2/4] Lade Shopify-Daten...")
    shopify_skus = get_shopify_skus()
    location_id = get_shopify_location_id()
    log.info(f"Shopify Location-ID: {location_id}")

    # 3. Abgleich
    log.info("\n[3/4] Starte Abgleich...")
    updated = []
    new_products = []
    delisted = []
    errors = []

    # Gematchte SKUs → Lagerstand updaten
    for sku, elux_v in elux_by_sku.items():
        if sku in shopify_skus:
            shopify_v = shopify_skus[sku]
            if shopify_v["stock"] != elux_v.stock:
                try:
                    update_shopify_stock(
                        shopify_v["inventory_item_id"],
                        elux_v.stock,
                        location_id,
                    )
                    log.info(
                        f"  ✓ Update: {sku} → "
                        f"{shopify_v['stock']} → {elux_v.stock} Stk"
                    )
                    updated.append(sku)
                except Exception as e:
                    log.error(f"  ✗ Fehler bei {sku}: {e}")
                    errors.append({"sku": sku, "error": str(e)})
            else:
                log.info(f"  = Unverändert: {sku} ({elux_v.stock} Stk)")
        else:
            # Neu — noch nicht in Shopify
            new_products.append(elux_v)

    # SKUs in Shopify die nicht mehr bei elux sind → Lagerstand = 0
    for sku, shopify_v in shopify_skus.items():
        if sku not in elux_by_sku:
            if shopify_v["stock"] != 0:
                try:
                    update_shopify_stock(
                        shopify_v["inventory_item_id"],
                        0,
                        location_id,
                    )
                    log.warning(
                        f"  ⚠ Ausgelistet: {sku} → Lagerstand auf 0"
                    )
                except Exception as e:
                    log.error(f"  ✗ Fehler bei Auslistung {sku}: {e}")
            delisted.append({
                "sku": sku,
                "title": shopify_v.get("title", ""),
                "product_id": shopify_v["product_id"],
                "variant_id": shopify_v["variant_id"],
                "stock": shopify_v["stock"],
            })

    # 4. Google Sheets Export
    log.info("\n[4/4] Exportiere nach Google Sheets...")
    if new_products or delisted:
        export_to_sheets(new_products, delisted)
    else:
        log.info("  Nichts zu exportieren.")

    # ── Zusammenfassung ───────────────────────────────────────────────────────
    log.info("\n" + "=" * 60)
    log.info("ZUSAMMENFASSUNG")
    log.info(f"  elux Varianten gesamt:      {len(elux_variants)}")
    log.info(f"  Shopify Varianten gesamt:   {len(shopify_skus)}")
    log.info(f"  ✓ Aktualisiert:             {len(updated)}")
    log.info(f"  + Neu (→ Sheet Tab A):      {len(new_products)}")
    log.info(f"  ⚠ Ausgelistet (→ Tab B):    {len(delisted)}")
    log.info(f"  ✗ Fehler:                   {len(errors)}")
    log.info("=" * 60)

    return {
        "updated": len(updated),
        "new": len(new_products),
        "delisted": len(delisted),
        "errors": len(errors),
    }

if __name__ == "__main__":
    run_sync()
