"""
elux → Shopify Sync
====================
Scrapt shop.elux-licht.at, gleicht SKUs mit Shopify ab,
exportiert neue/fehlende Produkte nach Google Sheets.

FIXES 01.06.2026:
- Layout A/B Erkennung: Lagerstand oben vs. im Ausführungs-Tab
- extract_sku() mit Leerzeichen (85-NOVA S 18/830B, 54-DIFF FAMOSA PL ANTHRAZIT)
- Beschreibung Details / Mehr Informationen korrekt getrennt
- Sollux-Schutz (Vendor + SL./TH. Prefix)
- Tracking-Aktivierung vor jedem Lagerstand-Update
- Rate-Limit Schutz (0.55s vorbeugend + 429 Retry)
- Verstecken deaktiviert, Wieder-Aktivieren aktiv
- Tab C: Alle Elux Produkte (SKU, Name, Lagerstand)
- Node.js 24 in sync.yml
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

ELUX_BASE             = "https://shop.elux-licht.at/shop/pub"
SHOPIFY_SHOP          = os.environ["SHOPIFY_SHOP_URL"]
SHOPIFY_TOKEN         = os.environ["SHOPIFY_ADMIN_TOKEN"]
SHEETS_ID             = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDS          = os.environ.get("GOOGLE_CREDS_JSON", "google_creds.json")
REQUEST_DELAY         = 1.0  # Reduziert von 1.5s → ~60 Min/Run statt ~90 Min
SHOPIFY_REQUEST_DELAY = 0.55  # 1.8 Req/Sek → unter Shopify Limit von 2/Sek

PROTECTED_VENDORS     = ["Sollux Lighting"]
PROTECTED_SKU_PREFIXES = ["SL.", "TH."]

# SKUs die immer extra gesucht werden (unabhängig vom Kategorien-Scraping)
# Hier SKUs eintragen die nicht in den Kategorien gefunden werden
EXTRA_SEARCH_SKUS = [
    # 54-DIFF FAMOSA Ersatzdiffuser
    "54-DIFF FAMOSA PL ANTHRAZIT",
    "54-DIFF FAMOSA PL SCHWARZ",
    "54-DIFF FAMOSA PL SILBER",
    "54-DIFF FAMOSA PL BRAUN",
    "54-DIFF FAMOSA WL ANTHRAZIT",
    "54-DIFF FAMOSA WL SCHWARZ",
    "54-DIFF FAMOSA WL SILBER",
    "54-DIFF FAMOSA WL BRAUN",
    # 85-VS Versus Anbauleuchten
    "85-VS60-10/4/IP54",
    "85-VS90-12,5/4/IP54",
    "85-VS120-19/4/IP54",
    "85-VS150-28/4/IP54",
    "85-VS60-10/3/IP54",
    "85-VS90-12,5/3/IP54",
    "85-VS120-19/3/IP54",
    "85-VS150-28/3/IP54",
    # EUTRAC SX Stromschienen
    "SX 1 1209-2",
    "SX 0 1217-2",
    "SX 1 1212-2",
    "SX 1 1213-2",
    "SX 1 1212-6",
    "SX 1 1213-6",
    "SX 1 1214-2",
    "SX 1 1214-6",
    "SX 1 1215-2",
    "SX 1 1215-6",
    # 88-N Zubehör
    "88-N68196",
    "88-N68195",
    "88-N68202",
    "88-N68201",
    "88-N68218",
]

# Stoppwörter für SKU-Erkennung (Zeilen OHNE "Lagerstand")
SKU_STOP_WORDS_NO_LAGER = {
    "LED", "Wand", "Decke", "Anbaul", "Einbaul", "Hange",
    "Strahler", "Leuchte", "Profil", "Lampe", "Treiber",
    "Preis", "Mehr", "Details", "inkl", "ohne", "mit",
    # Farben NUR als Kleinbuchstaben stoppen - Großbuchstaben können SKU-Teil sein
    # z.B. 54-DIFF FAMOSA PL ANTHRAZIT, 54-DIFF FAMOSA PL SCHWARZ
    "weiß", "grau", "silber", "chrom", "gold", "nickel", "braun", "beige",
}

ELUX_CATEGORY_URLS = [
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
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/anbauleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/einbauleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/pollerleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/strahler.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/technische-aussenbeleuchtung/strassenleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/hangeleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-beleuchtung/deckenleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/kronleuchter.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/wandleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/stehleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/tischleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/badezimmerleuchten.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/dekorative-leuchten/ventilatoren.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-leuchtmittel-e27.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-leuchtmittel-e14.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-leuchtmittel-gu10.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/sonstige-leuchtmittel-230v.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-tubes.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/led-leuchtmittel-12v.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/leuchtmittel/speziallampen.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/led-umrustungen-und-sanierungen.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/zubehor/leuchtenzubehor.html",
    "https://shop.elux-licht.at/shop/pub/elux/produkte/zubehor/pfahle-und-masten.html",
    "https://shop.elux-licht.at/shop/pub/elux/coming-soon.html",
    "https://shop.elux-licht.at/shop/pub/elux/derzeit-ist-keine-aktion-verfugbar.html",
    # Einzelprodukte die in keiner Kategorie gelistet sind
    "https://shop.elux-licht.at/shop/pub/elux/einbaustr-panno-led-8w-230v-3000k-850nlm-weiss.html",
    "https://shop.elux-licht.at/shop/pub/elux/beril-m-l.html",
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
    """True wenn diese SKU/Variante niemals verändert werden darf."""
    if vendor and any(vendor.strip() == v for v in PROTECTED_VENDORS):
        return True
    sku_upper = sku.upper()
    return any(sku_upper.startswith(p.upper()) for p in PROTECTED_SKU_PREFIXES)


def extract_sku(line: str) -> str:
    """
    Extrahiert SKU aus einer Zeile des Ausführungs-Tabs.
    Zwei Modi:
    - Mit "Lagerstand" in Zeile: alles vor Lagerstand nehmen, bereinigen
    - Ohne "Lagerstand": Stoppwörter verwenden
    Erlaubt Leerzeichen in SKU (z.B. 85-NOVA S 18/830B, 54-DIFF FAMOSA PL ANTHRAZIT).
    """
    # Bereinige "→ X" am Ende (kommt manchmal in Tab-Zeilen vor)
    line = re.sub(r'\s*→\s*\d+\s*$', '', line).strip()
    m = re.match(r'^\d{2,}-[A-Za-z0-9][A-Za-z0-9/\-\.,]*', line)  # Komma erlaubt (z.B. 85-VS90-12,5/4/IP54)
    if not m:
        return ""
    sku_core = m.group(0)
    sku = sku_core
    rest = line[len(sku_core):]
    # Kern mit / oder . gilt als "komplex" → reine Großwörter danach stoppen
    core_is_complex = bool(re.search(r'[/.]', sku_core[3:]))
    has_lagerstand = "Lagerstand" in line

    if has_lagerstand:
        lager_pos = rest.find(" Lagerstand")
        candidate = rest[:lager_pos].strip() if lager_pos >= 0 else rest.strip()
        for part in candidate.split():
            if re.search(r'[äöüÄÖÜß]', part): break
            if re.match(r'^\d{5,}$', part): break  # 4-stellig erlaubt (z.B. 20-8486 1250)
            if re.match(r'^[A-Z][a-z]{3,}', part): break
            if re.match(r'^[a-z]{4,}$', part): break
            if re.match(r'^[A-Z]{4,}$', part) and core_is_complex: break
            sku += " " + part
    else:
        for part in rest.split():
            if part in SKU_STOP_WORDS_NO_LAGER: break
            if any(part.startswith(sw) for sw in {"LED", "Anbaul", "Einbaul", "Leuchte"}): break
            if re.search(r'[äöüÄÖÜß]', part): break
            if re.match(r'^\d{5,}$', part): break  # 4-stellig erlaubt (z.B. 20-8486 1250)
            if re.match(r'^[A-Z][a-z]{3,}', part): break
            if re.match(r'^[a-z]{4,}$', part): break
            if re.match(r'^[A-Z]{4,}$', part) and core_is_complex: break
            # Erlaubt: kurz ODER Sonderzeichen ODER reines GROSSWORT (wenn Kern nicht complex)
            if len(part) <= 5 or re.search(r'[0-9/\-\.,]', part) or re.match(r'^[A-Z]+$', part):
                sku += " " + part
            else:
                break
    return sku.strip()


def get_soup(url: str) -> Optional[BeautifulSoup]:
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
    urls = []
    page_url = category_url
    visited: set = set()
    while page_url and page_url not in visited:
        visited.add(page_url)
        soup = get_soup(page_url)
        if not soup:
            break
        for a in soup.select(
            "a.product-item-link, .product-item-info a, "
            ".product-item h2 a, li.product-item a.product-photo"
        ):
            href = a.get("href", "")
            if href and "elux-licht.at" in href and href not in urls:
                if not any(x in href for x in ["customer", "cart", "wishlist", "search"]):
                    urls.append(href)
        next_a = soup.select_one("a.action.next, li.pages-item-next a, a[title='Nächste']")
        page_url = next_a.get("href") if next_a else None
    return list(dict.fromkeys(urls))


def parse_product(url: str, category: str) -> list[EluxVariant]:
    """
    Parsed eine Produktseite → alle Varianten mit SKU + Lagerstand.

    Layout A: "Alle Lagerstände siehe unten: Ausführung"
              → Lagerstand steht im Ausführungs-Tab pro Variante

    Layout B: Kein solcher Hinweis
              → Lagerstand steht oben rechts auf der Seite
              → Im Tab steht SKU ohne Lagerstand
              → Fallback: Lagerstand von oben lesen
    """
    soup = get_soup(url)
    if not soup:
        return []

    page_text = soup.get_text(separator="\n")

    # ── Layout erkennen ───────────────────────────────────────────
    is_layout_a = "Alle Lagerstände siehe unten" in page_text

    # ── Produktname ───────────────────────────────────────────────
    product_name = ""
    for sel in ["h1.page-title span", "h1.page-title", "h1"]:
        el = soup.select_one(sel)
        if el:
            product_name = el.get_text(strip=True)
            break

    # ── Bilder ───────────────────────────────────────────────────
    image_url = ""
    image_urls = ""
    all_images: list = []
    for img in soup.select(
        ".gallery-placeholder img, .fotorama img, "
        ".product.media img, [data-gallery] img, .MagicSlideshow img"
    ):
        src = img.get("src", img.get("data-src", img.get("data-original", "")))
        if src and "elux-licht.at" in src and src not in all_images:
            if not any(x in src for x in ["logo", "icon", "placeholder", "bright/"]):
                all_images.append(src)
    fotorama = soup.select_one("[data-gallery]")
    if fotorama:
        import json as _json
        try:
            for item in _json.loads(fotorama.get("data-gallery", "[]")):
                src = item.get("full", item.get("img", ""))
                if src and src not in all_images:
                    all_images.append(src)
        except Exception:
            pass
    if all_images:
        image_url = all_images[0]
        image_urls = " | ".join(all_images)

    # ── Preis ─────────────────────────────────────────────────────
    price = ""
    price_el = soup.select_one(".price-box .price, [data-price-type='finalPrice'] .price")
    if price_el:
        price = price_el.get_text(strip=True)

    # ── Tab "Details" → description_details ──────────────────────
    description_details = ""
    for sel in [
        "#description .value", "#description",
        ".product.attribute.description .value",
        ".product.attribute.description",
    ]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(separator="\n", strip=True)
            if txt and "Mehr Informationen" not in txt[:40]:
                description_details = txt
                break
    if not description_details:
        for panel in soup.select(".data.item.content, [role='tabpanel'], .tab-content-item"):
            txt = panel.get_text(separator="\n", strip=True)
            if txt and "Mehr Informationen" not in txt[:40] and len(txt) > 20:
                if not re.search(r'\d{2,}-[A-Za-z0-9]', txt[:100]):
                    description_details = txt
                    break

    # ── Tab "Mehr Informationen" → description_more ───────────────
    description_more = ""
    for sel in ["#additional", ".additional-attributes", ".product-attributes"]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(separator="\n", strip=True)
            if txt and txt != description_details:
                description_more = txt
                break

    # ── Lagerstand oben (Fallback für alle Layouts) ──────────────
    # Wird verwendet wenn Tab keinen Lagerstand liefert
    # Auch Layout A Produkte können Lagerstand nur oben haben (z.B. 54-11106PK/W/W)
    top_stock = 0
    m_top = re.search(r'Lagerstand[:\s]+(\d+)\s*Stk', page_text, re.I)
    if m_top:
        top_stock = int(m_top.group(1))

    # ── Ausführungs-Tab → Varianten ───────────────────────────────
    variants: list[EluxVariant] = []
    ausfuehrung = None
    for sel in ["#ausf-hrung", "#ausfuhrung", "#ausfuehrung",
                "[id*='ausfuhr']", "[id*='ausfuehr']"]:
        el = soup.select_one(sel)
        if el:
            ausfuehrung = el
            break
    if not ausfuehrung:
        for panel in soup.select(".tab-content-item, .data.item.content, [role='tabpanel']"):
            txt = panel.get_text()
            if "Lagerstand" in txt or re.search(r'\d{2,}-[A-Za-z0-9]', txt):
                ausfuehrung = panel
                break

    if ausfuehrung:
        text = ausfuehrung.get_text(separator="\n", strip=True)
        lines_list = [l.strip() for l in text.splitlines() if l.strip()]

        current_sku = None
        current_desc_lines: list = []
        current_stock = 0

        def save_variant():
            if not current_sku:
                return
            desc = " ".join(current_desc_lines)
            dim_m = re.search(r'(L=\s*\d+mm[^\n,]+)', desc)
            col_m = re.search(r'(weiß|schwarz|grau|silber|chrom|gold|nickel)', desc, re.I)
            ip_m  = re.search(r'(IP\d{2})', desc)
            w_m   = re.search(r'(\d+(?:\.\d+)?\s*W)\b', desc)
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

        for line in lines_list:
            sku = extract_sku(line)
            if sku:
                save_variant()
                current_sku = sku
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

        save_variant()

    # ── Fallback: Lagerstand von oben der Seite ──────────────────
    # Wenn Tab keinen Lagerstand hat (alle stock=0) UND oben steht ein Lagerstand
    # Gilt für alle Layouts, auch wenn mehrere Varianten vorhanden
    if top_stock > 0 and all(v.stock == 0 for v in variants):
        if len(variants) == 1:
            # Einzelne Variante → direkt setzen
            log.info(f"    Fallback (1 Variante): stock={top_stock} von oben der Seite")
            variants[0].stock = top_stock
        else:
            # Mehrere Varianten → Gesamtlagerstand bekannt aber nicht pro Variante
            # Setze alle Varianten auf -1 als Signal "nicht auf 0 setzen"
            # In run_sync(): wenn elux stock == -1 → Shopify-Wert behalten
            log.info(f"    Fallback ({len(variants)} Varianten): top_stock={top_stock} – Shopify-Wert behalten")
            for v in variants:
                v.stock = -1  # -1 = "unbekannt, Shopify-Wert behalten"

    # ── Fallback: SKU direkt von der Seite ────────────────────────
    if not variants:
        for sel in [".product.attribute.sku .value", "[itemprop='sku']", ".sku .value"]:
            el = soup.select_one(sel)
            if el:
                sku = el.get_text(strip=True)
                if sku:
                    stock = top_stock  # Fallback für alle Layouts
                    variants.append(EluxVariant(
                        sku=sku, name=product_name,
                        description_details=description_details,
                        description_more=description_more,
                        stock=stock, price=price, category=category,
                        product_url=url, image_url=image_url, image_urls=image_urls,
                    ))
                break

    return variants


def scrape_all() -> list[EluxVariant]:
    all_variants: list[EluxVariant] = []
    all_product_urls: list = []

    log.info("Sammle Produkt-URLs aus allen Kategorien...")
    for cat_url in ELUX_CATEGORY_URLS:
        category = cat_url.rstrip("/").rstrip(".html").split("/")[-1].replace("-", " ").title()
        log.info(f"  Kategorie: {category}")
        urls = collect_product_urls_from_category(cat_url)
        log.info(f"    → {len(urls)} Produkte")
        for u in urls:
            all_product_urls.append((u, category))

    seen: set = set()
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



def search_elux_product(sku: str) -> Optional[str]:
    """
    Sucht eine SKU auf Elux und gibt die Produkt-URL zurück.
    Wird verwendet für SKUs die nicht in den Kategorien gefunden wurden.
    """
    import urllib.parse
    search_url = (
        f"https://shop.elux-licht.at/shop/pub/catalogsearch/result/"
        f"?q={urllib.parse.quote(sku)}"
    )
    soup = get_soup(search_url)
    if not soup:
        return None
    # Erstes Produkt aus Suchergebnis
    for sel in ["a.product-item-link", ".product-item-info a", ".product-item h2 a"]:
        a = soup.select_one(sel)
        if a and a.get("href"):
            return a.get("href")
    return None

def get_shopify_skus() -> tuple[dict, dict]:
    """
    Lädt alle Shopify-Varianten: aktive + Entwürfe + Archivierte.
    status=any ist kein gültiger Wert → 3 separate Requests nötig.
    """
    headers  = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    shopify_skus: dict     = {}
    shopify_products: dict = {}

    def load_by_status(status: str):
        page_url = (
            f"https://{SHOPIFY_SHOP}/admin/api/2024-01/products.json"
            f"?limit=250&fields=id,vendor,published_at,variants&status={status}"
        )
        count = 0
        while page_url:
            r = requests.get(page_url, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            for product in data.get("products", []):
                vendor     = product.get("vendor", "")
                product_id = product["id"]
                is_published = product.get("published_at") is not None
                if product_id not in shopify_products:
                    shopify_products[product_id] = {
                        "vendor": vendor, "published": is_published, "skus": []
                    }
                for v in product.get("variants", []):
                    if v.get("sku"):
                        sku = v["sku"].strip()
                        shopify_skus[sku] = {
                            "variant_id":           v["id"],
                            "product_id":           product_id,
                            "inventory_item_id":    v["inventory_item_id"],
                            "stock":                v["inventory_quantity"],
                            "title":                v.get("title", ""),
                            "vendor":               vendor,
                            "inventory_management": v.get("inventory_management"),
                        }
                        shopify_products[product_id]["skus"].append(sku)
                        count += 1
            link = r.headers.get("Link", "")
            page_url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    page_url = part.split(";")[0].strip().strip("<>")
        log.info(f"  Status={status}: {count} Varianten")

    load_by_status("active")
    load_by_status("draft")

    log.info(f"Shopify: {len(shopify_skus)} Varianten, {len(shopify_products)} Produkte geladen")
    return shopify_skus, shopify_products


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


def enable_inventory_tracking(variant_id: int, max_retries: int = 5) -> bool:
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"}
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
            wait = float(r.headers.get("Retry-After", 10))
            log.warning(f"    429 Rate Limit (Tracking) – warte {wait:.0f}s...")
            time.sleep(wait + 1)
        else:
            log.warning(f"    HTTP {r.status_code} beim Tracking-Aktivieren (Versuch {attempt+1})")
            time.sleep(3 * (attempt + 1))
    log.error(f"    Tracking konnte nicht aktiviert werden für Variante {variant_id}")
    return False


def update_shopify_stock(inventory_item_id: int, quantity: int,
                         location_id: str, max_retries: int = 5):
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"}
    time.sleep(SHOPIFY_REQUEST_DELAY)
    for attempt in range(max_retries):
        r = requests.post(
            f"https://{SHOPIFY_SHOP}/admin/api/2024-01/inventory_levels/set.json",
            json={"location_id": location_id,
                  "inventory_item_id": inventory_item_id,
                  "available": quantity},
            headers=headers, timeout=15,
        )
        if r.status_code == 200:
            return
        elif r.status_code == 429:
            wait = float(r.headers.get("Retry-After", 10))
            log.warning(f"    429 Rate Limit – warte {wait:.0f}s (Versuch {attempt+1})...")
            time.sleep(wait + 1)
        elif r.status_code == 422:
            log.warning(f"    422 Unprocessable – inventory_item_id {inventory_item_id} ungültig, übersprungen.")
            return
        else:
            log.warning(f"    HTTP {r.status_code} – Versuch {attempt+1}/{max_retries}...")
            time.sleep(3 * (attempt + 1))
    raise Exception(f"Shopify Update fehlgeschlagen nach {max_retries} Versuchen (id={inventory_item_id})")


def set_product_published(product_id: int, published: bool, max_retries: int = 5) -> bool:
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"}
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
            wait = float(r.headers.get("Retry-After", 10))
            log.warning(f"    429 Rate Limit (published) – warte {wait:.0f}s...")
            time.sleep(wait + 1)
        else:
            log.warning(f"    HTTP {r.status_code} beim Setzen published={published} (Versuch {attempt+1})")
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


def export_to_sheets(new_products: list, delisted: list, all_elux: list):
    gc = get_sheets_client()
    sh = gc.open_by_key(SHEETS_ID)
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Tab A: Neue Produkte
    try:
        ws = sh.worksheet("Neue Produkte")
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Neue Produkte", 5000, 15)
    rows = [["SKU","Name","Kategorie","Beschreibung Details","Beschreibung Mehr",
             "Lagerstand","Preis","Maße","Farbe","IP","Watt",
             "Bild-URL (Haupt)","Alle Bild-URLs","Produkt-URL","Datum"]]
    for v in new_products:
        rows.append([v.sku, v.name, v.category,
                     v.description_details[:500], v.description_more[:300],
                     v.stock, v.price, v.dimensions, v.color,
                     v.ip_rating, v.watt, v.image_url, v.image_urls,
                     v.product_url, ts])
    ws.update(rows, "A1")
    ws.format("A1:O1", {"textFormat": {"bold": True}})

    # Tab B: Nicht mehr lieferbar
    try:
        ws2 = sh.worksheet("Nicht mehr lieferbar")
        ws2.clear()
    except gspread.WorksheetNotFound:
        ws2 = sh.add_worksheet("Nicht mehr lieferbar", 2000, 7)
    rows2 = [["SKU","Name","Produkt-ID","Varianten-ID","Alter Bestand","Aktion","Datum"]]
    for p in delisted:
        rows2.append([p["sku"], p.get("title",""), p.get("product_id",""),
                      p.get("variant_id",""), p.get("stock",0),
                      "Lagerstand auf 0 gesetzt", ts])
    ws2.update(rows2, "A1")
    ws2.format("A1:G1", {"textFormat": {"bold": True}})

    # Tab C: Alle Elux Produkte (SKU + Name + Lagerstand)
    try:
        ws3 = sh.worksheet("Alle Elux Produkte")
        ws3.clear()
    except gspread.WorksheetNotFound:
        ws3 = sh.add_worksheet("Alle Elux Produkte", 6000, 5)
    rows3 = [["SKU","Name","Kategorie","Lagerstand","Produkt-URL"]]
    for v in sorted(all_elux, key=lambda x: x.sku):
        rows3.append([v.sku, v.name, v.category, v.stock, v.product_url])
    ws3.update(rows3, "A1")
    ws3.format("A1:E1", {"textFormat": {"bold": True}})

    log.info(
        f"Sheets: {len(new_products)} neue + {len(delisted)} ausgelistete + "
        f"{len(all_elux)} gesamt exportiert"
    )


def run_sync():
    log.info("=" * 60)
    log.info(f"elux → Shopify Sync: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    log.info("=" * 60)

    log.info("\n[1/5] Scrape elux...")
    elux_variants = scrape_all()
    elux_by_sku   = {v.sku: v for v in elux_variants}

    with open("elux_products_raw.json", "w", encoding="utf-8") as f:
        json.dump([vars(v) for v in elux_variants], f, ensure_ascii=False, indent=2)

    log.info("\n[2/5] Lade Shopify...")
    shopify_skus, shopify_products = get_shopify_skus()
    location_id = get_shopify_location_id()

    log.info("\n[3/5] Abgleich...")
    updated          : list = []
    new_products     : list = []
    delisted         : list = []
    errors           : list = []
    skipped_protected: list = []
    tracking_enabled : list = []

    for sku, ev in elux_by_sku.items():
        if sku in shopify_skus:
            sv = shopify_skus[sku]
            if sv.get("inventory_management") != "shopify":
                log.info(f"  🔧 Tracking aktivieren für {sku}...")
                ok = enable_inventory_tracking(sv["variant_id"])
                if not ok:
                    log.error(f"  ✗ Tracking konnte nicht aktiviert werden: {sku}")
                    errors.append(sku)
                    continue
                tracking_enabled.append(sku)
                time.sleep(1)
            # stock == -1 bedeutet: Lagerstand unbekannt (mehrere Varianten, kein Tab-Lagerstand)
            # → Shopify-Wert behalten, NICHT auf 0 setzen
            if ev.stock == -1:
                log.info(f"  ⏭ {sku}: Lagerstand unbekannt → Shopify-Wert {sv['stock']} behalten")
            elif sv["stock"] != ev.stock:
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
            vendor = sv.get("vendor", "")
            if is_protected_sku(sku, vendor):
                log.info(f"  🔒 Geschützt (nicht auf 0): {sku} [{vendor}]")
                skipped_protected.append(sku)
                continue
            if sv["stock"] != 0:
                try:
                    update_shopify_stock(sv["inventory_item_id"], 0, location_id)
                    log.warning(f"  ⚠ {sku} → 0")
                except Exception as e:
                    log.error(f"  ✗ Auslistung {sku}: {e}")
            delisted.append({**sv, "sku": sku})

    # ── Extra SKUs suchen + EXTRA_SEARCH_SKUS Liste ──────────────
    log.info("\n[4/5] Extra SKUs suchen...")
    searched = 0

    # Kombiniere: EXTRA_SEARCH_SKUS + alle Shopify-SKUs die nicht gescrapt wurden
    extra_skus = list(EXTRA_SEARCH_SKUS)
    for sku in shopify_skus:
        if sku not in elux_by_sku and not is_protected_sku(sku, shopify_skus[sku].get("vendor","")) and sku not in extra_skus:
            extra_skus.append(sku)

    for sku in extra_skus:
        sv = shopify_skus.get(sku)
        if not sv:
            log.info(f"  ⚠ {sku}: nicht in Shopify → übersprungen")
            continue
        if sku in elux_by_sku and sku not in EXTRA_SEARCH_SKUS:
            continue
        # Suche auf Elux
        product_url = search_elux_product(sku)
        if product_url:
            log.info(f"  🔍 Gefunden via Suche: {sku} → {product_url.split('/')[-1]}")
            category = "Suche"
            vs = parse_product(product_url, category)
            # Finde die passende Variante
            for v in vs:
                if v.sku == sku:
                    elux_by_sku[sku] = v
                    searched += 1
                    # Sofort Lagerstand setzen wenn abweichend
                    if sv.get("inventory_management") != "shopify":
                        log.info(f"  🔧 Tracking aktivieren für {sku}...")
                        ok = enable_inventory_tracking(sv["variant_id"])
                        if ok:
                            tracking_enabled.append(sku)
                            import time as _t; _t.sleep(1)
                    if sv["stock"] != v.stock and v.stock != -1:
                        try:
                            update_shopify_stock(sv["inventory_item_id"], v.stock, location_id)
                            log.info(f"  ✓ {sku}: {sv['stock']} → {v.stock}")
                            updated.append(sku)
                        except Exception as e:
                            log.error(f"  ✗ {sku}: {e}")
                            errors.append(sku)
                    break
    log.info(f"  Gefunden via Suche: {searched} SKUs")

    log.info("\n[5/5] Produkt-Sichtbarkeit prüfen...")
    restored_products: list = []
    for product_id, pdata in shopify_products.items():
        vendor = pdata.get("vendor", "")
        if any(vendor.strip() == v for v in PROTECTED_VENDORS):
            continue
        if pdata["skus"] and all(is_protected_sku(s) for s in pdata["skus"]):
            continue
        total_stock = sum(
            elux_by_sku[s].stock if s in elux_by_sku
            else shopify_skus.get(s, {}).get("stock", 0)
            for s in pdata["skus"]
        )
        # Verstecken ist deaktiviert – wird via Shopify Workflow gesteuert
        # Wieder aktivieren wenn Produkt auf Entwurf aber Lagerstand > 0
        if total_stock > 0 and not pdata["published"]:
            ok = set_product_published(product_id, True)
            if ok:
                log.info(f"  ✅ Wieder sichtbar: Produkt {product_id} [{vendor}]")
                restored_products.append(product_id)

    log.info("\n[5/5] Google Sheets Export...")
    export_to_sheets(new_products, delisted, elux_variants)

    log.info("\n" + "=" * 60)
    log.info(f"Aktualisiert:        {len(updated)}")
    log.info(f"Tracking aktiviert:  {len(tracking_enabled)}")
    log.info(f"Neu (Sheet A):       {len(new_products)}")
    log.info(f"Ausgelistet:         {len(delisted)}")
    log.info(f"Geschützt (Sollux):  {len(skipped_protected)}")
    log.info(f"Wieder sichtbar:     {len(restored_products)}")
    log.info(f"Fehler:              {len(errors)}")
    log.info("=" * 60)


if __name__ == "__main__":
    run_sync()
