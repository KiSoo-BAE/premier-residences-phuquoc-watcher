"""Read-only quantity check; never submit an order or bypass a disabled control."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

URL = "https://booking.vinwonders.com/ko-KRW/search?code=PQSAF&usingDate=26-12-2026&VinWonderTagGroupCode=Bestseller&tab=VZT"
PRODUCT = "[현장 추가 서비스] - 프리미엄 VIP 동물원 투어 - 빈펄 사파리 푸꾸옥"
SLOT = "8:30 - 10:00"
DATE = "2026-12-26"
TARGET = 6
END = datetime.fromisoformat("2026-12-26T08:30:00+07:00")
STATE = Path("safari_status.json")


def telegram(method, data=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing Telegram bot secret")
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=25)
        payload = response.json()
    except Exception:
        raise RuntimeError("Telegram transport error (credentials redacted)") from None
    if response.status_code != 200 or payload.get("ok") is not True:
        raise RuntimeError(f"Telegram request failed: HTTP {response.status_code}")
    return payload["result"]


def send(text):
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat:
        raise RuntimeError("Missing existing Telegram recipient secret")
    telegram("sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": "true"})
    print("TELEGRAM_DELIVERED", flush=True)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def check():
    options = Options()
    for arg in ["--headless=new", "--window-size=1440,1200", "--lang=ko-KR", "--no-sandbox", "--disable-dev-shm-usage"]:
        options.add_argument(arg)
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(100)
    stage = "open"
    try:
        driver.get(URL)
        wait = WebDriverWait(driver, 120, poll_frequency=1)
        stage = "product load"
        wait.until(lambda d: any(PRODUCT == el.text.strip() for el in d.find_elements(By.CSS_SELECTOR, ".item-card-v2 .title")))
        for button in driver.find_elements(By.TAG_NAME, "button"):
            if button.is_displayed() and button.text.strip() == "동의하지 않는다":
                button.click()
                break
        dates = driver.find_elements(By.CSS_SELECTOR, "input[title='2026-12-26']")
        if len(dates) != 1 or dates[0].get_attribute("value") != DATE:
            raise RuntimeError("Requested visit date not verified")
        cards = [el for el in driver.find_elements(By.CSS_SELECTOR, ".item-card-v2") if el.find_element(By.CSS_SELECTOR, ".title").text.strip() == PRODUCT]
        if len(cards) != 1:
            raise RuntimeError("Premium product not uniquely identified")
        cards[0].find_element(By.CSS_SELECTOR, ".choose").click()
        stage = "time slot load"
        # The site briefly shows a sold-out placeholder while fetching inventory.
        # Never classify that transient message as a completed check.
        slot = wait.until(lambda d: next((b for b in d.find_elements(By.CSS_SELECTOR, ".ticket-selector .options button") if b.is_displayed() and b.text.strip() == SLOT), None))
        if not slot.is_enabled():
            raise RuntimeError("Time slot disabled before selection; inventory unclear")
        slot.click()
        stage = "adult quantity"
        row = wait.until(lambda d: next((r for r in d.find_elements(By.CSS_SELECTOR, ".ticket-selector .ticket-row") if r.is_displayed() and r.find_element(By.CSS_SELECTOR, ".info-label").text.strip() == "성인"), None))
        def quantity():
            return int(row.find_element(By.CSS_SELECTOR, ".controls > span").text.strip())
        if quantity() != 0:
            raise RuntimeError("Unexpected initial quantity")
        for expected in range(1, TARGET + 1):
            plus = next(b for b in row.find_elements(By.CSS_SELECTOR, ".controls button") if b.text.strip() == "+")
            if not plus.is_enabled():
                return {"status": "unavailable", "max_selectable_observed": quantity(), "note": "Adult plus button disabled before six"}
            plus.click()
            WebDriverWait(driver, 15).until(lambda d: quantity() == expected)
        active = driver.find_elements(By.CSS_SELECTOR, ".ticket-selector .options button.active")
        titles = [el.text.strip() for el in driver.find_elements(By.CSS_SELECTOR, ".title-container .title") if el.is_displayed()]
        buy = driver.find_element(By.CSS_SELECTOR, ".ticket-selector button.buy-now")
        if titles != [PRODUCT] or SLOT not in [b.text.strip() for b in active] or quantity() != TARGET or not buy.is_enabled():
            raise RuntimeError("Final selection evidence incomplete")
        time.sleep(2)
        if quantity() != TARGET or not buy.is_enabled():
            raise RuntimeError("Selection did not remain valid")
        price = row.find_element(By.CSS_SELECTOR, ".prices").text.strip()
        return {"status": "available", "max_selectable_observed": TARGET, "price_per_adult_display": price, "note": "Six adults selected and purchase button enabled; no order submitted"}
    except Exception as exc:
        # Do not print remote driver / request exceptions that may contain URLs or tokens.
        return {"status": "error", "note": f"{stage}: {type(exc).__name__}"}
    finally:
        driver.quit()


def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    if now >= END:
        result = {"status": "expired", "note": "Tour start has passed"}
    else:
        result = check()
    previous = state.get("last_successful_status")
    state.update(result)
    state.update({"checked_at_utc": now.isoformat(), "visit_date": DATE, "slot_local": SLOT, "timezone": "Asia/Ho_Chi_Minh", "adults": TARGET, "booking_url": URL, "run_id": os.environ.get("GITHUB_RUN_ID")})
    try:
        if result["status"] in ("available", "unavailable"):
            if not state.get("telegram_verified"):
                telegram("getMe")
                telegram("getChat", {"chat_id": os.environ.get("TELEGRAM_CHAT_ID", "")})
                state["telegram_verified"] = True
                print("TELEGRAM_READ_PREFLIGHT_OK", flush=True)
            if result["status"] == "available" and previous != "available":
                send("🚨 빈펄 사파리 프리미엄 VIP투어 성인 6명 선택 가능!\n\n2026년 12월 26일\n08:30~10:00 (베트남 현지시간)\n성인 6명 선택 및 구매 버튼 활성 확인\n\n" + URL + "\n\n※ 자동 예약·결제는 하지 않았습니다. 재고는 변동되므로 최종 예약 화면에서 확인해 주세요.")
                state["last_alert_at_utc"] = now.isoformat()
                state["setup_sent"] = True
            elif not state.get("setup_sent"):
                send("✅ 사파리 텔레그램 연결 테스트 성공\n\n감시 조건: 2026/12/26 08:30~10:00 (베트남 현지시간), 프리미엄 VIP투어 성인 6명\n현재 화면: 성인 " + str(result.get("max_selectable_observed")) + "명까지 선택 가능\n6명 선택이 가능해지면 알립니다.\n\n※ 이 메시지는 예약 가능 알림이 아닌 연결 확인입니다.")
                state["setup_sent"] = True
            state["last_successful_status"] = result["status"]
            state["consecutive_errors"] = 0
        elif result["status"] == "error":
            state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
            if state["consecutive_errors"] == 3:
                send("⚠️ 사파리 예약 감시가 3회 연속 판독에 실패했습니다. 현재 성인 6명 예약 가능 여부를 확인할 수 없습니다. 감시 기록 점검이 필요합니다.")
    except Exception as exc:
        state["status"] = "error"
        state["note"] = f"Telegram delivery: {type(exc).__name__}"
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        print("RESULT " + json.dumps(state, ensure_ascii=False), flush=True)
        return 1
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    print("RESULT " + json.dumps(state, ensure_ascii=False), flush=True)
    return int(state["status"] == "error")


if __name__ == "__main__":
    raise SystemExit(main())
