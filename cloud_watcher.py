from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
STATE_PATH = BASE / "state.json"
DIAG_DIR = BASE / "diagnostics"
DIAG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("premier-cloud-watcher")

WATCH_URL = "https://all.accor.com/booking/en/accor/hotel/B2Q9?dateIn=2026-12-25&compositions=2-4&stayplus=false&snu=false&hideHotelDetails=true&dateOut=2026-12-28"
HOTEL_NAME = "Premier Residences Phu Quoc Emerald Bay"
CHECK_IN = "2026-12-25"
CHECK_OUT = "2026-12-28"
GUESTS = "Adults 2 + Child 1 (age 4)"

UNAVAILABLE_MARKERS = [
    "this accommodation is unavailable on our site",
    "this hotel is unavailable on our site",
    "no availability",
    "no rooms available",
    "sold out",
    "unavailable for the selected dates",
    "we are sorry, there are no rooms available",
]
BLOCK_MARKERS = [
    "captcha",
    "access denied",
    "verify you are human",
    "unusual traffic",
    "temporarily unavailable",
    "service unavailable",
]
PRICE_PATTERNS = [
    re.compile(r"(?:KRW|₩)\s?[0-9][0-9,\.]*", re.I),
    re.compile(r"[0-9][0-9,\.]*\s?(?:KRW|₩)", re.I),
    re.compile(r"(?:VND|₫)\s?[0-9][0-9,\.]*", re.I),
    re.compile(r"[0-9][0-9,\.]*\s?(?:VND|₫)", re.I),
    re.compile(r"(?:USD|US\$|\$)\s?[0-9][0-9,\.]*", re.I),
]


@dataclass
class CheckResult:
    status: str
    body_text: str
    title: str = ""
    prices: tuple[str, ...] = ()
    note: str = ""


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_status": None, "available_repeat_sent": False, "failure_count": 0}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault("last_status", None)
    data.setdefault("available_repeat_sent", False)
    data.setdefault("failure_count", 0)
    return data


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_prices(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in PRICE_PATTERNS:
        for item in pattern.findall(text or ""):
            item = re.sub(r"\s+", " ", item).strip()
            if item not in found:
                found.append(item)
            if len(found) >= 5:
                return tuple(found)
    return tuple(found)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secret is missing")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "false"},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload)


def make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    for arg in [
        "--headless=new",
        "--window-size=1440,1400",
        "--lang=en-US",
        "--disable-notifications",
        "--disable-popup-blocking",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]:
        options.add_argument(arg)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(75)
    return driver


def dismiss_cookie_banners(driver) -> None:
    for selector in [
        "button#onetrust-accept-btn-handler",
        "button[data-testid='accept-all']",
        "button[aria-label*='Accept']",
    ]:
        try:
            for el in driver.find_elements("css selector", selector):
                if el.is_displayed() and el.is_enabled():
                    el.click()
                    time.sleep(1)
                    return
        except Exception:
            pass


def save_diagnostics(driver, label: str, body: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    try:
        driver.save_screenshot(str(DIAG_DIR / f"{stamp}_{label}.png"))
    except Exception:
        pass
    try:
        (DIAG_DIR / f"{stamp}_{label}.txt").write_text((body or "")[:50000], encoding="utf-8")
    except Exception:
        pass


def check_page(driver) -> CheckResult:
    try:
        driver.get(WATCH_URL)
        time.sleep(14)
        dismiss_cookie_banners(driver)
        time.sleep(2)
        body = driver.find_element("tag name", "body").text or ""
        title = driver.title or ""
        low = (title + "\n" + body).lower()
        prices = extract_prices(body)

        if any(marker in low for marker in BLOCK_MARKERS):
            save_diagnostics(driver, "blocked", body)
            return CheckResult("error", body, title, prices, "Block/CAPTCHA marker detected")
        if any(marker in low for marker in UNAVAILABLE_MARKERS):
            return CheckResult("unavailable", body, title, prices, "Unavailable marker detected")

        availability_words = [
            "choose your room",
            "select your room",
            "room",
            "rate",
            "member rate",
            "book",
            "booking",
            "breakfast",
            "flexible",
            "non-refundable",
        ]
        hits = sum(1 for word in availability_words if word in low)
        if prices and hits >= 2:
            save_diagnostics(driver, "available", body)
            return CheckResult("available", body, title, prices, f"price+booking signals ({hits})")

        save_diagnostics(driver, "ambiguous", body)
        return CheckResult("error", body, title, prices, f"Ambiguous page: signals={hits}, prices={len(prices)}")
    except Exception as exc:
        try:
            save_diagnostics(driver, "exception", "")
        except Exception:
            pass
        return CheckResult("error", "", "", (), f"{type(exc).__name__}: {exc}")



@dataclass
class CheckoutProbe:
    stage: str
    url: str
    note: str = ""


def safe_checkout_probe(driver) -> CheckoutProbe:
    """
    Advance only through obviously non-final navigation controls.
    Never fill personal/payment fields and never click payment/confirmation actions.
    This is a verification/preparation probe, not an order-placement routine.
    """
    stop_markers = [
        "credit card",
        "card number",
        "card details",
        "billing address",
        "pay now",
        "complete booking",
        "confirm booking",
        "confirm reservation",
        "finalise booking",
        "finalize booking",
    ]
    form_markers = [
        "guest details",
        "your details",
        "contact details",
        "personal information",
        "first name",
        "last name",
        "email address",
        "phone number",
    ]
    safe_words = [
        "select",
        "choose",
        "continue",
        "next",
        "view rates",
        "see rates",
        "show rates",
        "select rate",
        "choose rate",
        "select room",
        "choose room",
        "continue",
    ]
    blocked_words = [
        "pay",
        "confirm",
        "complete",
        "purchase",
        "submit",
        "reserve",
        "book now",
        "finalise",
        "finalize",
    ]

    try:
        for step in range(1, 5):
            body = driver.find_element("tag name", "body").text or ""
            low = body.lower()
            current_url = driver.current_url

            if any(marker in low for marker in stop_markers):
                save_diagnostics(driver, "checkout_payment_stop", body)
                return CheckoutProbe(
                    "payment_boundary",
                    current_url,
                    "결제/최종확정 단계가 감지되어 자동 진행을 중지했습니다.",
                )

            if any(marker in low for marker in form_markers):
                save_diagnostics(driver, "checkout_guest_details_stop", body)
                return CheckoutProbe(
                    "guest_details",
                    current_url,
                    "예약자 정보 입력 단계가 감지되어 자동 진행을 중지했습니다.",
                )

            candidates = []
            for selector in ["button", "a", "[role='button']"]:
                try:
                    candidates.extend(driver.find_elements("css selector", selector))
                except Exception:
                    pass

            clicked = False
            for el in candidates:
                try:
                    if not el.is_displayed() or not el.is_enabled():
                        continue
                    txt = re.sub(r"\s+", " ", (el.text or "")).strip().lower()
                    aria = (el.get_attribute("aria-label") or "").strip().lower()
                    label = f"{txt} {aria}".strip()
                    if not label:
                        continue
                    if any(word in label for word in blocked_words):
                        continue
                    if not any(word in label for word in safe_words):
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", el
                    )
                    time.sleep(0.5)
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    log.info("CHECKOUT_PROBE step=%s clicked=%r", step, label[:120])
                    time.sleep(5)
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                save_diagnostics(driver, "checkout_safe_stop", body)
                return CheckoutProbe(
                    "safe_stop",
                    current_url,
                    "안전하게 자동 클릭할 수 있는 다음 단계가 없어 여기서 중지했습니다.",
                )

        body = driver.find_element("tag name", "body").text or ""
        save_diagnostics(driver, "checkout_max_steps", body)
        return CheckoutProbe(
            "max_steps",
            driver.current_url,
            "안전 제한(최대 4단계)에 도달해 자동 진행을 중지했습니다.",
        )
    except Exception as exc:
        return CheckoutProbe(
            "probe_error",
            getattr(driver, "current_url", WATCH_URL),
            f"{type(exc).__name__}: {exc}",
        )


def checkout_probe_message(probe: CheckoutProbe) -> str:
    labels = {
        "payment_boundary": "✅ 결제/최종확정 직전 단계 감지",
        "guest_details": "✅ 예약자 정보 입력 단계까지 진입",
        "safe_stop": "ℹ️ 안전 자동진행 중지",
        "max_steps": "ℹ️ 안전 제한 도달",
        "probe_error": "⚠️ 결제직전 탐색 오류",
    }
    label = labels.get(probe.stage, probe.stage)
    return (
        f"{label}\n\n"
        f"상태: {probe.note}\n"
        f"현재 URL: {probe.url}\n\n"
        "※ 자동화는 개인정보/카드정보를 입력하지 않고, 결제·최종확정 버튼도 누르지 않습니다. "
        "GitHub의 브라우저 세션은 사용자 휴대폰/PC 브라우저와 공유되지 않으므로, "
        "이 URL이 동일한 예약 상태를 그대로 이어주지 않을 수 있습니다."
    )


def availability_message(result: CheckResult, repeat: bool = False) -> str:
    prices = ", ".join(result.prices) if result.prices else "페이지에서 직접 확인 필요"
    repeat_line = "\n⚠️ 아직 예약 가능 상태입니다. (1회 재알림)" if repeat else ""
    korea_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "🚨 프리미어 레지던스 객실 오픈 감지!\n\n"
        f"🏨 {HOTEL_NAME}\n"
        f"📅 {CHECK_IN} → {CHECK_OUT}\n"
        f"👨‍👩‍👧 {GUESTS}\n"
        f"💰 감지된 가격: {prices}\n"
        f"🕒 확인시각: {korea_time}"
        f"{repeat_line}\n\n"
        "👉 바로 예약:\n"
        f"{WATCH_URL}\n\n"
        "※ 재고/가격은 수시로 바뀔 수 있으니 최종 화면에서 확인하세요."
    )



def main() -> int:
    state = load_state()
    driver = None
    result = None
    try:
        driver = make_driver()
        result = check_page(driver)

        last = state.get("last_status")
        log.info(
            "RESULT=%s | last=%s | note=%s | prices=%s | title=%s",
            result.status,
            last,
            result.note,
            result.prices,
            result.title,
        )

        if result.status == "available":
            state["failure_count"] = 0
            if last != "available":
                state["available_repeat_sent"] = False

                # Alert first so checkout probing can never delay the primary availability alert.
                send_telegram(availability_message(result, repeat=False))
                log.info("Telegram availability alert sent")

                probe = safe_checkout_probe(driver)
                log.info(
                    "CHECKOUT_PROBE stage=%s | url=%s | note=%s",
                    probe.stage,
                    probe.url,
                    probe.note,
                )
                try:
                    send_telegram(checkout_probe_message(probe))
                    log.info("Telegram checkout-probe result sent")
                except Exception:
                    log.exception("Failed to send checkout-probe Telegram message")

            elif not state.get("available_repeat_sent", False):
                send_telegram(availability_message(result, repeat=True))
                state["available_repeat_sent"] = True
                log.info("Telegram one-time repeat alert sent")

        elif result.status == "unavailable":
            state["failure_count"] = 0
            state["available_repeat_sent"] = False
            if last == "available":
                send_telegram(
                    "ℹ️ Premier Residences 객실이 다시 예약 불가 상태로 바뀌었습니다. 계속 감시합니다."
                )

        else:
            state["failure_count"] = int(state.get("failure_count", 0)) + 1
            if state["failure_count"] == 3:
                send_telegram(
                    "⚠️ Premier Residences 감시 중 3회 연속 페이지 판독 오류가 발생했습니다. "
                    "GitHub Actions 로그를 확인해 주세요. 감시는 계속됩니다."
                )

        state["last_status"] = result.status
        state["last_note"] = result.note
        save_state(state)
        return 0

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
