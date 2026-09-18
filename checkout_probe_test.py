from __future__ import annotations

import time
from urllib.parse import urlencode

import cloud_watcher as cw

TEST_DATES = [
    ("2026-10-05", "2026-10-06"),
    ("2026-10-12", "2026-10-13"),
    ("2026-11-02", "2026-11-03"),
    ("2027-01-12", "2027-01-13"),
]

BASE_URL = "https://all.accor.com/booking/en/accor/hotel/B2Q9"


def make_url(date_in: str, date_out: str) -> str:
    return BASE_URL + "?" + urlencode(
        {
            "dateIn": date_in,
            "compositions": "2-4",
            "stayplus": "false",
            "snu": "false",
            "hideHotelDetails": "true",
            "dateOut": date_out,
        }
    )


def main() -> int:
    found_available = False
    summaries = []

    for date_in, date_out in TEST_DATES:
        url = make_url(date_in, date_out)
        driver = None
        try:
            driver = cw.make_driver()
            cw.WATCH_URL = url
            result = cw.check_page(driver)
            line = f"{date_in} -> {date_out}: {result.status} ({result.note})"
            print(line, flush=True)
            summaries.append(line)

            if result.status != "available":
                continue

            found_available = True
            probe = cw.safe_checkout_probe(driver)
            print(
                f"CHECKOUT_PROBE stage={probe.stage} url={probe.url} note={probe.note}",
                flush=True,
            )
            cw.send_telegram(
                "🧪 결제직전 자동진입 테스트\n\n"
                f"테스트 날짜: {date_in} → {date_out}\n"
                f"객실 감지: available\n"
                f"도달 단계: {probe.stage}\n"
                f"상태: {probe.note}\n"
                f"현재 URL: {probe.url}\n\n"
                "※ 테스트용 다른 날짜입니다. 개인정보/카드정보 입력 및 결제·최종확정은 하지 않았습니다."
            )
            return 0
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
        time.sleep(2)

    if not found_available:
        cw.send_telegram(
            "🧪 결제직전 자동진입 테스트 결과\n\n"
            "테스트 후보 날짜에서 예약 가능한 객실을 찾지 못해 실제 예약단계 진입까지는 테스트하지 못했습니다.\n\n"
            + "\n".join(summaries)
            + "\n\n기존 실시간 감시는 그대로 유지됩니다."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
