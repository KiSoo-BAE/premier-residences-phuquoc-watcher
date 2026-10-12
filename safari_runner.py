"""Live checks on a monotonic five-minute clock, with bounded batches."""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

END = datetime.fromisoformat("2026-12-26T08:30:00+07:00")
INTERVAL = 300
BATCH_SIZE = 6
CHECK_TIMEOUT = 240
STATE = Path("safari_status.json")
WORKFLOW = "safari-watch.yml"


def now():
    return datetime.now(timezone.utc)


def notice(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("MONITOR_NOTICE_FAILED missing existing secrets", flush=True)
        return
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": message}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            if not json.load(response).get("ok"):
                raise RuntimeError("delivery rejected")
        print("MONITOR_NOTICE_DELIVERED", flush=True)
    except Exception as exc:
        print("MONITOR_NOTICE_FAILED " + type(exc).__name__, flush=True)


def github(method, suffix, payload=None):
    repo = os.environ["GITHUB_REPOSITORY"]
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{suffix}",
        data=None if payload is None else json.dumps(payload).encode(),
        method=method,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    # Never log request/response headers or raw exception strings.
    with urllib.request.urlopen(request, timeout=25) as response:
        body = response.read()
        return json.loads(body) if body else {}


def has_active_worker(runs):
    return any(run.get("event") == "workflow_dispatch" and run.get("status") != "completed" for run in runs)


def control(mode):
    if now() >= END:
        github("PUT", f"actions/workflows/{WORKFLOW}/disable")
        print("WATCH_FINISHED workflow disabled after tour start", flush=True)
        return 0
    if mode == "ensure":
        runs = github("GET", f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=20")
        if has_active_worker(runs.get("workflow_runs", [])):
            print("WORKER_ALREADY_ACTIVE", flush=True)
            return 0
    for attempt in range(3):
        try:
            github("POST", f"actions/workflows/{WORKFLOW}/dispatches", {"ref": "main"})
            print("WORKER_DISPATCHED " + now().isoformat(), flush=True)
            return 0
        except Exception as exc:
            print(f"DISPATCH_FAILED attempt={attempt + 1} type={type(exc).__name__}", flush=True)
            if attempt < 2:
                time.sleep(5)
    notice("⚠️ 사파리 5분 감시의 다음 실행을 시작하지 못했습니다. 현재 감시가 끊길 수 있어 점검이 필요합니다.")
    return 1


def read_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def record_failed_check(start, why):
    state = read_state()
    state.update({"status": "error", "note": why,
                  "checked_at_utc": start.isoformat(),
                  "run_id": os.environ.get("GITHUB_RUN_ID"),
                  "consecutive_errors": state.get("consecutive_errors", 0) + 1})
    save_state(state)
    if state["consecutive_errors"] == 3:
        notice("⚠️ 사파리 감시가 3회 연속 실패했습니다. 현재 성인 6명 예약 가능 여부를 확인할 수 없습니다.")


def run_check():
    start = now()
    process = subprocess.Popen([sys.executable, "-u", "safari_watcher.py"], start_new_session=True)
    try:
        result = process.wait(timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        record_failed_check(start, "Single check exceeded 240-second limit")
        return 1
    state = read_state()
    try:
        written = datetime.fromisoformat(state["checked_at_utc"])
    except (ValueError, KeyError):
        written = None
    if written is None or written < start:
        record_failed_check(start, "Checker exited without a fresh result")
        return 1
    return result


def git(*args, check=True):
    result = subprocess.run(["git", *args], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError("git operation failed: " + args[0])
    return result.returncode


def persist():
    git("config", "user.name", "safari-watcher-bot")
    git("config", "user.email", "actions@users.noreply.github.com")
    git("add", "safari_status.json")
    if git("diff", "--cached", "--quiet", check=False) == 0:
        return
    git("commit", "-m", "chore: record five-minute safari check [skip ci]")
    for attempt in range(3):
        git("pull", "--rebase", "origin", "main")
        if git("push", "origin", "HEAD:main", check=False) == 0:
            print("STATUS_PERSISTED " + now().isoformat(), flush=True)
            return
        if attempt < 2:
            time.sleep(2)
    raise RuntimeError("Status push failed")


def loop():
    any_error = False
    persistence_warned = False
    for index in range(BATCH_SIZE):
        if now() >= END:
            break
        started = time.monotonic()
        print(f"CHECK_START {index + 1}/{BATCH_SIZE} {now().isoformat()}", flush=True)
        rc = run_check()
        state = read_state()
        state.update({"watch_mode": "continuous_5min", "batch_check_number": index + 1,
                      "next_check_due_utc": datetime.fromtimestamp(now().timestamp() + max(0, INTERVAL - (time.monotonic() - started)), timezone.utc).isoformat()})
        save_state(state)
        try:
            persist()
        except Exception as exc:
            print("STATUS_PERSIST_FAILED " + type(exc).__name__, flush=True)
            any_error = True
            if not persistence_warned:
                notice("⚠️ 사파리 조회는 진행 중이지만 최신 감시 기록 저장에 실패했습니다. 실행 기록 점검이 필요합니다.")
                persistence_warned = True
        print(f"CHECK_END {index + 1}/{BATCH_SIZE} rc={rc} {now().isoformat()}", flush=True)
        any_error |= bool(rc)
        if index + 1 < BATCH_SIZE:
            delay = max(0, INTERVAL - (time.monotonic() - started))
            delay = min(delay, max(0, (END - now()).total_seconds()))
            print(f"NEXT_CHECK_IN {delay:.1f} seconds", flush=True)
            time.sleep(delay)
    return int(any_error)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"
    try:
        raise SystemExit(loop() if mode == "loop" else control(mode))
    except Exception as exc:
        print("RUNNER_FAILED " + type(exc).__name__, flush=True)
        notice("⚠️ 사파리 감시 실행 중 오류가 발생했습니다. 자동 복구 및 실행 기록 점검이 필요합니다.")
        raise SystemExit(1)
