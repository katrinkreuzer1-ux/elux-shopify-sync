"""
Test-Script: Aktualisiert NUR SKU D-244819 und zeigt genaue Fehler.
"""
import os, requests, json

SHOPIFY_SHOP_URL    = os.environ["SHOPIFY_SHOP_URL"]
SHOPIFY_CLIENT_ID   = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_API_VERSION = "2026-04"
TEST_SKU            = "D-244819"
TEST_NEW_STOCK      = 331

# Token holen
print("Hole Token...")
resp = requests.post(
    f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token",
    data={"grant_type": "client_credentials",
          "client_id": SHOPIFY_CLIENT_ID,
          "client_secret": SHOPIFY_CLIENT_SECRET},
    headers={"Content-Type": "application/x-www-form-urlencoded"}
)
token = resp.json()["access_token"]
print(f"Token: {token[:12]}...")

headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

# Location holen
resp = requests.get(f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/locations.json", headers=headers)
location_id = resp.json()["locations"][0]["id"]
print(f"Location ID: {location_id}")

# SKU in Shopify suchen
print(f"\nSuche SKU {TEST_SKU} in Shopify...")
page_url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/variants.json?limit=250"
found = None
page = 0
while page_url and not found:
    page += 1
    resp = requests.get(page_url, headers=headers)
    for v in resp.json().get("variants", []):
        if v.get("sku", "").strip() == TEST_SKU:
            found = v
            break
    link = resp.headers.get("Link", "")
    page_url = None
    for part in link.split(","):
        if 'rel="next"' in part:
            page_url = part.split(";")[0].strip().strip("<>")

if not found:
    print(f"SKU {TEST_SKU} NICHT gefunden in Shopify!")
else:
    print(f"Gefunden: variant_id={found['id']}, inventory_item_id={found['inventory_item_id']}")
    print(f"Aktueller Lagerstand: {found['inventory_quantity']}")
    print(f"inventory_management: {found.get('inventory_management')}")
    
    # Tracking aktivieren falls nötig
    if found.get("inventory_management") != "shopify":
        print("\nAktiviere Tracking...")
        resp = requests.put(
            f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/variants/{found['id']}.json",
            json={"variant": {"id": found['id'], "inventory_management": "shopify"}},
            headers=headers
        )
        print(f"Tracking Status: {resp.status_code}")
        print(resp.json())

    # Lagerstand setzen
    print(f"\nSetze Lagerstand auf {TEST_NEW_STOCK}...")
    resp = requests.post(
        f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/inventory_levels/set.json",
        json={"location_id": location_id,
              "inventory_item_id": found['inventory_item_id'],
              "available": TEST_NEW_STOCK},
        headers=headers
    )
    print(f"Status: {resp.status_code}")
    print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
