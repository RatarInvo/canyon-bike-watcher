import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright


PRODUCT_URL = os.getenv(
    "PRODUCT_URL",
    "https://www.canyon.com/sv-se/landsvaegscyklar/triathlon-cykel/speedmax/cfr/speedmax-cfr-di2/4525.html",
)

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

# Dessa texter betyder att produkten normalt inte går att köpa ännu.
UNAVAILABLE_MARKERS = [
    "kommer snart",
    "coming soon",
    "checka in igen",
    "check back",
    "registrera dig för att få ett meddelande",
    "registrera dig för att få en avisering",
    "notify me",
]

# Använd endast tydliga köpmarkörer här.
# "välj ramstorlek" och "välj färg och storlek" är borttagna
# eftersom de kan visas även när produkten inte går att köpa.
AVAILABLE_MARKERS = [
    "lägg i varukorg",
    "lägg i kundvagn",
    "lägg i korgen",
    "köp nu",
    "add to cart",
    "add to basket",
    "buy now",
    "välj ramstorlek",
    "välj färg och storlek",
]


def normalize_text(text: str) -> str:
    """Gör texten lättare att jämföra."""
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_state() -> dict:
    """Läser tidigare status från state.json."""
    if not STATE_FILE.exists():
        return {
            "available": False,
            "initialized": False,
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        return {
            "available": bool(state.get("available", False)),
            "initialized": bool(state.get("initialized", False)),
        }

    except json.JSONDecodeError:
        print("state.json kunde inte läsas. Börjar om från tom status.")

        return {
            "available": False,
            "initialized": False,
        }


def save_state(state: dict) -> None:
    """Skriver state atomiskt till state.json."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")

    temporary_file.replace(STATE_FILE)


def send_discord_message(message: str) -> None:
    """Skickar meddelande till Discord-webhooken."""
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


def get_matching_markers(text: str, markers: list[str]) -> list[str]:
    """Returnerar alla markörer som hittades i texten."""
    return [
        marker
        for marker in markers
        if marker in text
    ]


def get_visible_action_text(page) -> str:
    """
    Hämtar text från synliga knappar och länkar.
    Endast dessa används som köpindikator.
    """
    texts = []

    selectors = [
        "button:visible",
        "a:visible",
        "input[type='submit']:visible",
        "[role='button']:visible",
    ]

    for selector in selectors:
        try:
            texts.extend(
                page.locator(selector).all_inner_texts()
            )
        except Exception as error:
            print(
                f"Kunde inte läsa selector {selector!r}: {error}"
            )

    action_text = normalize_text(" ".join(texts))

    print(
        "Synlig knapp-/länktext: "
        f"{action_text[:2000]}"
    )

    return action_text


def close_cookie_dialog(page) -> None:
    """Försöker stänga en eventuell cookie-dialog."""
    cookie_selectors = [
        "button:has-text('Acceptera')",
        "button:has-text('Godkänn')",
        "button:has-text('Accept')",
        "button:has-text('Allow all')",
        "button:has-text('Tillåt alla')",
    ]

    for selector in cookie_selectors:
        try:
            locator = page.locator(selector).first

            if locator.is_visible():
                locator.click(timeout=3_000)
                print(f"Cookie-dialog stängdes med: {selector}")
                return

        except Exception:
            pass


def check_product(page) -> dict:
    """Öppnar produktsidan och kontrollerar tillgängligheten."""
    print(f"Öppnar: {PRODUCT_URL}")

    page.goto(
        PRODUCT_URL,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    # Canyon använder JavaScript för delar av produktsidan.
    # Vänta en kort stund utan att använda networkidle.
    page.wait_for_timeout(5_000)

    close_cookie_dialog(page)

    # Läs sidans synliga text.
    body_text = normalize_text(
        page.locator("body").inner_text()
    )

    # Läs synliga knappar och länkar.
    action_text = get_visible_action_text(page)

    matching_unavailable_markers = get_matching_markers(
        body_text,
        UNAVAILABLE_MARKERS,
    )

    matching_available_body_markers = get_matching_markers(
        body_text,
        AVAILABLE_MARKERS,
    )

    matching_available_action_markers = get_matching_markers(
        action_text,
        AVAILABLE_MARKERS,
    )

    unavailable = bool(matching_unavailable_markers)

    # Köpstatus baseras endast på tydlig köpmarkör i synliga
    # knappar eller länkar, inte på vanlig sidtext.
    has_available_action = bool(
        matching_available_action_markers
    )

    available = (
        not unavailable
        and has_available_action
    )

    print(
        "Unavailable-markörer som hittades: "
        f"{matching_unavailable_markers}"
    )

    print(
        "Available-markörer i vanlig sidtext: "
        f"{matching_available_body_markers}"
    )

    print(
        "Available-markörer i synliga knappar/länkar: "
        f"{matching_available_action_markers}"
    )

    print(f"Unavailable-text hittad: {unavailable}")
    print(
        "Köpmarkör hittad i synlig text: "
        f"{bool(matching_available_body_markers)}"
    )
    print(
        "Köpmarkör hittad i synliga knappar/länkar: "
        f"{has_available_action}"
    )

    return {
        "available": available,
        "unavailable": unavailable,
        "matching_unavailable_markers": (
            matching_unavailable_markers
        ),
        "matching_available_body_markers": (
            matching_available_body_markers
        ),
        "matching_available_action_markers": (
            matching_available_action_markers
        ),
    }


def run_discord_test() -> None:
    """Skickar ett testmeddelande när TEST_DISCORD=true."""
    send_discord_message(
        "✅ Test: Canyon watcher kan skicka Discord-notiser."
    )

    print("Testnotis skickad.")


def main() -> None:
    # Används bara vid test.
    # Lägg till TEST_DISCORD: "true" i workflowet tillfälligt.
    if os.getenv("TEST_DISCORD", "").lower() == "true":
        run_discord_test()
        return

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

            old_available = bool(
                state.get("available", False)
            )

            new_available = bool(
                result["available"]
            )

            initialized = bool(
                state.get("initialized", False)
            )

            print(f"Förra statusen: {old_available}")
            print(f"Nuvarande status: {new_available}")

            should_notify = new_available and (
                not initialized
                or not old_available
            )

            if should_notify:
                message = (
                    "🚨 Canyon-cykeln kan vara tillgänglig nu!\n\n"
                    f"{PRODUCT_URL}\n\n"
                    "Öppna sidan direkt och kontrollera "
                    "ramstorlek, lagerstatus och pris."
                )

                send_discord_message(message)
                print("Discord-notis skickad.")
            else:
                print("Ingen Discord-notis behövs.")

            new_state = {
                "available": new_available,
                "initialized": True,
            }

            state_changed = (
                state.get("available") != new_state["available"]
                or state.get("initialized") != new_state["initialized"]
            )

            save_state(new_state)

            if state_changed:
                print("State ändrades och sparades.")
            else:
                print("Produktstatus oförändrad.")

        finally:
            browser.close()


if __name__ == "__main__":
    main()