import hashlib
import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


PRODUCT_URL = os.getenv(
    "PRODUCT_URL",
    "https://www.canyon.com/sv-se/landsvaegscyklar/triathlon-cykel/speedmax/cf-slx/speedmax-cf-slx-8-di2/4520.html",
)

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

# Dessa texter betyder normalt att produkten inte kan köpas ännu.
UNAVAILABLE_MARKERS = [
    "kommer snart",
    "coming soon",
    "checka in igen",
    "check back",
    "registrera dig för att få ett meddelande",
    "notify me",
]

# Dessa texter indikerar att köpknapp eller varukorg kan vara tillgänglig.
AVAILABLE_MARKERS = [
    "lägg i varukorg",
    "lägg i kundvagn",
    "köp nu",
    "add to cart",
    "add to basket",
    "buy now",
    "välj ramstorlek",
    "välj färg och storlek",
]


def normalize_text(text: str) -> str:
    """
    Gör texten lättare att jämföra:
    - små bokstäver
    - ersätter flera mellanslag med ett
    """
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def calculate_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "available": False,
            "initialized": False,
            "last_text_hash": None,
            "last_checked_at": None,
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError:
        print("state.json kunde inte läsas. Börjar om från tom status.")
        return {
            "available": False,
            "initialized": False,
            "last_text_hash": None,
            "last_checked_at": None,
        }


def save_state(state: dict) -> None:
    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")

    temporary_file.replace(STATE_FILE)


def send_discord_message(message: str) -> None:
    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": message,
            "allowed_mentions": {
                "parse": []
            },
        },
        timeout=20,
    )

    response.raise_for_status()


def contains_any(text: str, markers: list[str]) -> bool:
    return any(marker in text for marker in markers)


def get_visible_action_text(page) -> str:
    """
    Hämtar text från synliga knappar och länkar.
    Detta minskar risken att en dold eller generell text
    felaktigt tolkas som en köpknapp.
    """
    texts = []

    for selector in [
        "button:visible",
        "a:visible",
        "input[type='submit']:visible",
        "[role='button']:visible",
    ]:
        try:
            texts.extend(page.locator(selector).all_inner_texts())
        except Exception:
            pass

    return normalize_text(" ".join(texts))


def check_product(page) -> dict:
    print(f"Öppnar: {PRODUCT_URL}")

    page.goto(
        PRODUCT_URL,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    # Ge sidan tid att ladda JavaScript och produktdata.
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightTimeoutError:
        print("networkidle nåddes inte. Fortsätter ändå.")

    # Försök stänga cookie-dialog om den finns.
    cookie_selectors = [
        "button:has-text('Acceptera')",
        "button:has-text('Godkänn')",
        "button:has-text('Accept')",
        "button:has-text('Allow all')",
    ]

    for selector in cookie_selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1_000):
                locator.click(timeout=3_000)
                print("Cookie-dialog stängdes.")
                break
        except Exception:
            pass

    # Läs synlig text från hela sidan.
    body_text = normalize_text(page.locator("body").inner_text())

    # Läs enbart synliga knappar/länkar separat.
    action_text = get_visible_action_text(page)

    unavailable = contains_any(body_text, UNAVAILABLE_MARKERS)

    # Köpmarkörer kontrolleras i både sidtext och synliga actions.
    has_available_marker_in_body = contains_any(
        body_text,
        AVAILABLE_MARKERS,
    )

    has_available_action = contains_any(
        action_text,
        AVAILABLE_MARKERS,
    )

    # Kräver en köpmarkör och att sidan inte samtidigt
    # innehåller "kommer snart"-text.
    available = (
        not unavailable
        and (
            has_available_marker_in_body
            or has_available_action
        )
    )

    title = page.title()
    text_hash = calculate_hash(body_text)

    return {
        "available": available,
        "title": title,
        "text_hash": text_hash,
        "unavailable": unavailable,
        "has_available_marker_in_body": has_available_marker_in_body,
        "has_available_action": has_available_action,
    }


def main() -> None:
    
    state = load_state()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )

        context = browser.new_context(
            locale="sv-SE",
            timezone_id="Europe/Stockholm",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        try:
            result = check_product(page)

            old_available = bool(state.get("available", False))
            new_available = bool(result["available"])
            initialized = bool(state.get("initialized", False))

            print(f"Förra statusen: {old_available}")
            print(f"Nuvarande status: {new_available}")
            print(f"Unavailable-text hittad: {result['unavailable']}")
            print(
                "Köpmarkör hittad i synlig text: "
                f"{result['has_available_marker_in_body']}"
            )
            print(
                "Köpmarkör hittad i synliga knappar/länkar: "
                f"{result['has_available_action']}"
            )

            should_notify = new_available and (
                not initialized or not old_available
            )

            if should_notify:
                message = (
                    "🚨 Canyon-cykeln kan vara tillgänglig nu!\n\n"
                    f"{PRODUCT_URL}\n\n"
                    "Öppna sidan direkt och kontrollera ramstorlek, "
                    "lagerstatus och pris."
                )

                send_discord_message(message)
                print("Discord-notis skickad.")
            else:
                print("Ingen Discord-notis behövs.")

            state = {
                "available": new_available,
                "initialized": True,
            }

            save_state(state)

            if old_available != new_available or not initialized:
                print("State ändrades och sparades.")
            else:
                print("Produktstatus oförändrad.")

            state = {
                "available": new_available,
                "initialized": True,
            }

            save_state(state)

            if old_available != new_available:
                print("Produktstatus ändrades. state.json uppdaterad.")
            else:
                print("Produktstatus oförändrad. Ingen ny state-ändring.")

        finally:
            browser.close()


if __name__ == "__main__":
    main()