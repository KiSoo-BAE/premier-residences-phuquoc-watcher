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
    Prefer a breakfast-included rate, add one room to the basket, then click Continue.
    Never fill personal/payment fields and never click payment/final-confirmation actions.
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

    try:
        body = driver.find_element("tag name", "body").text or ""
        low = body.lower()

        # Prefer a visible rate card that explicitly includes breakfast.
        breakfast_clicked = False
        cards = driver.find_elements(
            "xpath",
            "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'breakfast included')]",
        )
        for card in cards:
            try:
                if not card.is_displayed():
                    continue
                # Walk upward to a compact rate-card ancestor containing a room-selection control.
                node = card
                for _ in range(6):
                    buttons = node.find_elements(
                        "xpath",
                        ".//*[self::button or self::a or @role='button']",
                    )
                    for btn in buttons:
                        if not btn.is_displayed() or not btn.is_enabled():
                            continue
                        label = re.sub(
                            r"\\s+",
                            " ",
                            ((btn.text or "") + " " + (btn.get_attribute("aria-label") or "")),
                        ).strip().lower()
                        if "choose this room" in label or "select" in label or "choose" in label:
                            driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});", btn
                            )
                            time.sleep(0.5)
                            driver.execute_script("arguments[0].click();", btn)
                            log.info("CHECKOUT_PROBE selected breakfast-included rate: %r", label[:120])
                            breakfast_clicked = True
                            time.sleep(5)
                            break
                    if breakfast_clicked:
                        break
                    try:
                        node = node.find_element("xpath", "..")
                    except Exception:
                        break
                if breakfast_clicked:
                    break
            except Exception:
                continue

        # If Accor has already preselected a breakfast rate, do not keep clicking room buttons.
        if not breakfast_clicked and "breakfast included" not in low:
            save_diagnostics(driver, "checkout_no_breakfast_rate", body)
            return CheckoutProbe(
                "safe_stop",
                driver.current_url,
                "조식 포함 요금을 안전하게 식별하지 못해 자동 진행을 중지했습니다.",
            )

        body = driver.find_element("tag name", "body").text or ""
        low = body.lower()
        if any(marker in low for marker in stop_markers):
            save_diagnostics(driver, "checkout_payment_stop", body)
            return CheckoutProbe(
                "payment_boundary",
                driver.current_url,
                "결제/최종확정 단계가 감지되어 자동 진행을 중지했습니다.",
            )
        if any(marker in low for marker in form_markers):
            save_diagnostics(driver, "checkout_guest_details_stop", body)
            return CheckoutProbe(
                "guest_details",
                driver.current_url,
                "예약자 정보 입력 단계가 감지되어 자동 진행을 중지했습니다.",
            )

        # After the room/rate is in the basket, Continue is the only safe forward action we want.
        # Accor renders the sticky bottom Continue control in a way that may not
        # expose it as a normal button/a element. Search the whole DOM by visible
        # text first, then fall back to JS text matching, while still only allowing
        # the exact safe label "Continue".
        continue_btn = None
        xpath_candidates = [
            "//*[normalize-space(text())='Continue']",
            "//*[normalize-space(.)='Continue']",
            "//*[@aria-label='Continue']",
            "//*[@title='Continue']",
        ]
        for xp in xpath_candidates:
            try:
                for el in driver.find_elements("xpath", xp):
                    if el.is_displayed():
                        continue_btn = el
                        break
                if continue_btn is not None:
                    break
            except Exception:
                continue

        if continue_btn is None:
            try:
                continue_btn = driver.execute_script(
                    """
                    const all = Array.from(document.querySelectorAll('button,a,[role="button"],div,span'));
                    const exact = all.filter(el => (el.innerText || el.textContent || '').trim() === 'Continue');
                    return exact.find(el => {
                      const r = el.getBoundingClientRect();
                      const s = getComputedStyle(el);
                      return r.width > 20 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none';
                    }) || null;
                    """
                )
            except Exception:
                continue_btn = None

        if continue_btn is None:
            save_diagnostics(driver, "checkout_continue_missing", body)
            return CheckoutProbe(
                "safe_stop",
                driver.current_url,
                "조식 포함 객실은 선택됐지만 고정 하단 Continue 버튼을 찾지 못해 중지했습니다.",
            )

        # If the text node itself is not clickable, climb to the nearest clickable ancestor.
        try:
            clickable = driver.execute_script(
                """
                let el = arguments[0];
                for (let i=0; el && i<6; i++, el=el.parentElement) {
                  const tag = (el.tagName || '').toLowerCase();
                  const role = el.getAttribute && el.getAttribute('role');
                  if (tag === 'button' || tag === 'a' || role === 'button' || typeof el.onclick === 'function') return el;
                }
                return arguments[0];
                """,
                continue_btn,
            )
            if clickable is not None:
                continue_btn = clickable
        except Exception:
            pass

        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", continue_btn
        )
        time.sleep(0.7)

        before_url = driver.current_url
        before_body = driver.find_element("tag name", "body").text or ""
        advanced = False
        click_methods = ["native", "actions", "javascript"]

        for method in click_methods:
            try:
                if method == "native":
                    continue_btn.click()
                elif method == "actions":
                    from selenium.webdriver.common.action_chains import ActionChains
                    ActionChains(driver).move_to_element(continue_btn).pause(0.3).click().perform()
                else:
                    driver.execute_script("arguments[0].click();", continue_btn)

                log.info("CHECKOUT_PROBE Continue attempt method=%s", method)

                # Success means real navigation/state change, not merely a click call returning.
                for _ in range(16):
                    time.sleep(0.5)
                    now_url = driver.current_url
                    now_body = driver.find_element("tag name", "body").text or ""
                    now_low = now_body.lower()
                    if (
                        now_url != before_url
                        or any(marker in now_low for marker in form_markers)
                        or any(marker in now_low for marker in stop_markers)
                        or ("continue" not in now_low and now_body != before_body)
                    ):
                        advanced = True
                        break
                if advanced:
                    log.info(
                        "CHECKOUT_PROBE Continue verified advanced method=%s before=%s after=%s",
                        method, before_url, driver.current_url
                    )
                    break

                # Re-resolve the sticky Continue control because Accor may replace DOM nodes.
                refreshed = None
                for xp in xpath_candidates:
                    for el in driver.find_elements("xpath", xp):
                        try:
                            if el.is_displayed():
                                refreshed = el
                                break
                        except Exception:
                            pass
                    if refreshed is not None:
                        break
                if refreshed is not None:
                    continue_btn = refreshed
            except Exception as exc:
                log.warning("CHECKOUT_PROBE Continue method=%s failed: %s", method, exc)

        if not advanced:
            body = driver.find_element("tag name", "body").text or ""
            save_diagnostics(driver, "checkout_continue_no_transition", body)
            return CheckoutProbe(
                "safe_stop",
                driver.current_url,
                "Continue 클릭을 3가지 방식으로 시도했지만 실제 다음 화면 전환이 확인되지 않아 중지했습니다.",
            )

        time.sleep(3)
        body = driver.find_element("tag name", "body").text or ""
        low = body.lower()
        if any(marker in low for marker in stop_markers):
            save_diagnostics(driver, "checkout_payment_stop", body)
            return CheckoutProbe(
                "payment_boundary",
                driver.current_url,
                "결제/최종확정 단계가 감지되어 자동 진행을 중지했습니다.",
            )
        if any(marker in low for marker in form_markers):
            save_diagnostics(driver, "checkout_guest_details_stop", body)
            return CheckoutProbe(
                "guest_details",
                driver.current_url,
                "조식 포함 요금으로 예약자 정보 입력 단계까지 진입했습니다.",
            )

        save_diagnostics(driver, "checkout_after_continue", body)
        return CheckoutProbe(
            "safe_stop",
            driver.current_url,
            "조식 포함 요금으로 Continue까지 진행했으며 다음 단계는 안전 확인을 위해 중지했습니다.",
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
