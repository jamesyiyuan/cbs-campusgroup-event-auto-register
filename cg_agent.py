#!/usr/bin/env python3
"""CampusGroups event agent (Columbia Business School).

Watches https://groups.gsb.columbia.edu/events for newly published events,
pushes each one to your phone via ntfy, and registers you for a FREE ticket
only after you tap "Register" on the notification. Paid events are never
purchased: you get an "Open to pay" link and finish checkout yourself.

Commands:
  init                 create config.yaml with random ntfy topics + secret
  login                open a real browser window; you sign in with Duo once
  scan [--notify-existing]   one scan, print + push new events
  register EVENT_ID [--dry-run]   try to register for one event now
  run                  daemon: scan on a schedule + listen for approvals
  test-notify          send a test push
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CG_AGENT_CONFIG", HERE / "config.yaml"))


# ----------------------------------------------------------------- config ---
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"No config at {CONFIG_PATH}. Run: python cg_agent.py init")
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    cfg["state_dir"] = Path(os.path.expanduser(cfg.get("state_dir", "~/.cg-agent")))
    cfg["state_dir"].mkdir(parents=True, exist_ok=True)
    (cfg["state_dir"] / "shots").mkdir(exist_ok=True)
    cfg["base_url"] = cfg.get("base_url", "https://groups.gsb.columbia.edu").rstrip("/")
    return cfg


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ state ---
class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
              id TEXT PRIMARY KEY, title TEXT, date TEXT, price TEXT,
              grp TEXT, first_seen TEXT, notified INTEGER DEFAULT 0,
              reg_status TEXT);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS schedule(id TEXT PRIMARY KEY, at REAL);
            """
        )

    def schedule(self, eid: str, at: float) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO schedule(id,at) VALUES(?,?)", (eid, at))
            self.db.commit()

    def unschedule(self, eid: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM schedule WHERE id=?", (eid,))
            self.db.commit()

    def next_scheduled(self) -> tuple[str, float] | None:
        with self.lock:
            r = self.db.execute("SELECT id, at FROM schedule ORDER BY at LIMIT 1").fetchone()
        return (r[0], r[1]) if r else None

    def seen(self, eid: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone() is not None

    def add(self, ev: dict, notified: bool) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO events(id,title,date,price,grp,first_seen,notified) VALUES(?,?,?,?,?,?,?)",
                (ev["id"], ev["title"], f'{ev["date"]} {ev["time"]}', ev["price"], ev["group"],
                 datetime.now().isoformat(timespec="seconds"), int(notified)),
            )
            self.db.commit()

    def set_reg(self, eid: str, status: str) -> None:
        with self.lock:
            self.db.execute("UPDATE events SET reg_status=? WHERE id=?", (status, eid))
            self.db.commit()

    def get(self, k: str, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def put(self, k: str, v: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))
            self.db.commit()

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]


# ------------------------------------------------------------------- ntfy ---
def sign(cfg: dict, eid: str) -> str:
    return hmac.new(cfg["approval_secret"].encode(), eid.encode(), hashlib.sha256).hexdigest()[:20]


def ntfy_headers(cfg: dict) -> dict:
    tok = (cfg["ntfy"].get("token") or "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def push(cfg: dict, title: str, message: str, *, priority: int = 3, click: str | None = None,
         actions: list | None = None, tags: list | None = None) -> None:
    n = cfg["ntfy"]
    payload = {"topic": n["alert_topic"], "title": title[:250], "message": message[:3500],
               "priority": priority}
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions[:3]
    if tags:
        payload["tags"] = tags
    req = urllib.request.Request(n.get("server", "https://ntfy.sh").rstrip("/"),
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **ntfy_headers(cfg)})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # never let a push failure kill the agent
        log(f"ntfy push failed: {e}")


def approve_action(cfg: dict, eid: str, label: str = "Register") -> dict:
    n = cfg["ntfy"]
    a = {"action": "http", "label": label, "method": "POST",
         "url": f'{n.get("server", "https://ntfy.sh").rstrip("/")}/{n["approve_topic"]}',
         "body": f"{eid}.{sign(cfg, eid)}", "clear": True}
    h = ntfy_headers(cfg)
    if h:
        a["headers"] = h
    return a


def listen_for_approvals(cfg: dict, state: State, q: "queue.Queue[str]", stop: threading.Event) -> None:
    """Stream the approve topic; verified event IDs go onto the queue."""
    n = cfg["ntfy"]
    base = f'{n.get("server", "https://ntfy.sh").rstrip("/")}/{n["approve_topic"]}/json'
    backoff = 5
    while not stop.is_set():
        since = state.get("ntfy_since")
        url = base + (f"?since={since}" if since else "")
        try:
            req = urllib.request.Request(url, headers=ntfy_headers(cfg))
            with urllib.request.urlopen(req, timeout=90) as resp:
                backoff = 5
                for raw in resp:
                    if stop.is_set():
                        return
                    msg = json.loads(raw)
                    if msg.get("event") != "message":
                        continue
                    state.put("ntfy_since", msg["id"])
                    body = (msg.get("message") or "").strip()
                    eid, _, sig = body.partition(".")
                    if eid.isdigit() and hmac.compare_digest(sig, sign(cfg, eid)):
                        log(f"approval received for event {eid}")
                        q.put(eid)
                    else:
                        log(f"ignored unsigned/invalid approval: {body[:40]!r}")
        except Exception as e:
            if not stop.is_set():
                log(f"approval listener reconnecting ({e.__class__.__name__}: {e})")
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)


# ---------------------------------------------------------------- browser ---
def storage_path(cfg: dict) -> Path:
    return cfg["state_dir"] / "storage_state.json"


def new_context(browser: Browser, cfg: dict) -> BrowserContext:
    sp = storage_path(cfg)
    if not sp.exists():
        sys.exit("Not logged in yet. Run: python cg_agent.py login")
    ctx = browser.new_context(storage_state=str(sp), viewport={"width": 1280, "height": 900})
    ctx.set_default_timeout(30_000)
    return ctx


def save_session(ctx: BrowserContext, cfg: dict) -> None:
    ctx.storage_state(path=str(storage_path(cfg)))  # keeps refreshed cookies


class LoggedOut(Exception):
    pass


def check_logged_in(page: Page, cfg: dict) -> None:
    host = urlparse(cfg["base_url"]).netloc
    u = urlparse(page.url)
    if u.netloc != host or "login" in u.path.lower() or page.locator("input[type=password]").count():
        raise LoggedOut(page.url)


# ---------------------------------------------------------------- scanning ---
LIST_JS = r"""
() => [...document.querySelectorAll('li[id^="event_"]')].map(li => {
  const lines = li.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  const link = [...li.querySelectorAll('a[href*="rsvp_boot"]')].find(a => a.innerText.trim());
  const dateLine = lines.find(l => /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), [A-Z][a-z]{2} \d{1,2}, \d{4}/.test(l)) || '';
  const di = lines.indexOf(dateLine);
  let ai = -1;
  lines.forEach((l, i) => { if (l === 'Register' || l === 'View') ai = i; });
  const txt = li.innerText;
  return {
    id: li.id.slice(6),
    title: link ? link.innerText.trim() : (lines[0] || ''),
    date: dateLine,
    time: di >= 0 && lines[di + 1] && !/^[A-Z]{3,4} \(GMT/.test(lines[di + 1]) ? lines[di + 1] : '',
    price: lines.find(l => /^(FREE|\$\d)/.test(l)) || '',
    group: ai >= 0 && lines[ai + 1] ? lines[ai + 1] : (lines[lines.length - 1] || ''),
    canRegister: lines.includes('Register'),
    waitlist: /Waiting List/i.test(txt),
    spots: (txt.match(/(\d+) Spots? Left/i) || [])[1] || null,
    soldOut: /Sold Out|Event Full|Registration Closed/i.test(txt),
    text: lines.join(' | ').slice(0, 600),
  };
})
"""


def fetch_listing(ctx: BrowserContext, cfg: dict) -> list[dict]:
    page = ctx.new_page()
    try:
        page.goto(f'{cfg["base_url"]}/events', wait_until="domcontentloaded")
        check_logged_in(page, cfg)
        page.wait_for_selector('li[id^="event_"]', timeout=30_000)
        for _ in range(int(cfg.get("max_pages", 12)) - 1):
            before = page.locator('li[id^="event_"]').count()
            more = page.get_by_text("More", exact=True)
            if not more.count() or not more.first.is_visible():
                break
            more.first.click()
            try:
                page.wait_for_function(
                    f"document.querySelectorAll('li[id^=\"event_\"]').length > {before}", timeout=15_000)
            except Exception:
                break
            time.sleep(1.0)  # be gentle with the server
        events = page.evaluate(LIST_JS)
        save_session(ctx, cfg)
        return events
    finally:
        page.close()


def _rx_any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns or [])


def classify(ev: dict, cfg: dict) -> str | None:
    """Return None to skip silently, else 'watch' or 'normal'."""
    f = cfg.get("filters", {})
    if f.get("only_registrable", True) and not ev["canRegister"]:
        return None
    if _rx_any(f.get("exclude_groups", []), ev["group"]):
        return None
    if _rx_any(f.get("exclude_keywords", []), ev["title"]):
        return None
    hay = f'{ev["title"]} {ev["group"]} {ev["text"]}'
    return "watch" if _rx_any(f.get("watch_keywords", []), hay) else "normal"


def is_free_option(price: str) -> bool:
    return price.upper().startswith("FREE")


WINDOW_RX = re.compile(
    r"open from\s+([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})\s*\(at ([\d:]+ ?[AP]M)\)\s*to\s+"
    r"([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})\s*\(at ([\d:]+ ?[AP]M)\)", re.I)


def _parse_dt(d: str, t: str) -> datetime | None:
    d, t = d.replace(".", ""), t.upper().replace(" ", "")
    for df in ("%b %d, %Y", "%B %d, %Y"):
        for tf in ("%I:%M%p", "%I%p"):
            try:
                return datetime.strptime(f"{d} {t}", f"{df} {tf}")
            except ValueError:
                pass
    return None


def reg_window(text: str) -> tuple[datetime, datetime] | None:
    """Registration window shown on the event page, in local (New York) time."""
    m = WINDOW_RX.search(text)
    if not m:
        return None
    a, b = _parse_dt(m[1], m[2]), _parse_dt(m[3], m[4])
    return (a, b) if a and b else None


def event_window(ctx: BrowserContext, cfg: dict, eid: str) -> tuple[datetime, datetime] | None:
    page = ctx.new_page()
    try:
        page.goto(f'{cfg["base_url"]}/rsvp_boot?id={eid}', wait_until="domcontentloaded")
        check_logged_in(page, cfg)
        return reg_window(page.inner_text("body"))
    except LoggedOut:
        raise
    except Exception:
        return None
    finally:
        page.close()
        time.sleep(0.5)


def notify_event(cfg: dict, ev: dict, kind: str) -> None:
    url = f'{cfg["base_url"]}/rsvp_boot?id={ev["id"]}'
    status = []
    win = ev.get("window")
    if win and win[0] > datetime.now():
        status.append(f"Registration opens {win[0]:%a %b %-d, %-I:%M %p}; tap Register to auto-grab at opening")
    if ev["soldOut"]:
        status.append("sold out / closed")
    elif ev["waitlist"]:
        status.append("WAITLIST only")
    elif ev["spots"]:
        status.append(f'{ev["spots"]} spots left')
    price = ev["price"] or "price unknown"
    msg = "\n".join(x for x in [f'{ev["date"]}  {ev["time"]}'.strip(), ev["group"], price,
                                ", ".join(status)] if x)
    prio = int(cfg.get("filters", {}).get("watch_priority", 4) if kind == "watch"
               else cfg.get("filters", {}).get("normal_priority", 2))
    if is_free_option(ev["price"]) and not ev["soldOut"]:
        label = "Join waitlist" if ev["waitlist"] else "Register (free)"
        actions = [approve_action(cfg, ev["id"], label), {"action": "view", "label": "Open", "url": url}]
        title = f'FREE{" *" if kind == "watch" else ""} | {ev["title"]}'
    else:
        actions = [{"action": "view", "label": "Open to pay", "url": url}]
        title = f'PAID {price}{" *" if kind == "watch" else ""} | {ev["title"]}'
    push(cfg, title, msg, priority=prio, click=url, actions=actions)


def scan(ctx: BrowserContext, cfg: dict, state: State, notify_existing: bool = False) -> int:
    events = fetch_listing(ctx, cfg)
    baseline = state.count() == 0 and not notify_existing
    max_push = int(cfg.get("filters", {}).get("max_pushes_per_scan", 10))
    new, pushed, overflow = 0, 0, []
    # watchlist matches first so they are never the ones that overflow
    ranked = sorted(events, key=lambda e: 0 if classify(e, cfg) == "watch" else 1)
    for ev in ranked:
        if not ev["id"] or state.seen(ev["id"]):
            continue
        new += 1
        kind = None if baseline else classify(ev, cfg)
        if kind and pushed < max_push:
            ev["window"] = event_window(ctx, cfg, ev["id"])
            notify_event(cfg, ev, kind)
            pushed += 1
            state.add(ev, notified=True)
            log(f'NEW [{kind}] {ev["id"]} {ev["price"] or "?":>12}  {ev["title"]}  ({ev["group"]})')
        else:
            if kind:
                overflow.append(ev)
            state.add(ev, notified=False)
    if overflow:
        lines = [f'- {e["title"]} ({e["price"] or "?"}, {e["date"]})' for e in overflow[:25]]
        push(cfg, f"{len(overflow)} more new events", "\n".join(lines), priority=2,
             click=f'{cfg["base_url"]}/events')
    if baseline:
        log(f"Baseline run: recorded {new} existing events without notifying.")
    else:
        log(f"Scan done: {len(events)} listed, {new} new, {pushed} pushed, {len(overflow)} in digest.")
    return new


# ------------------------------------------------------------ registration ---
TICKETS_JS = r"""
() => [...document.querySelectorAll('select[name^="ticket_"]')].map(s => {
  const row = s.closest('tr') || s.closest('.row') || s.parentElement;
  const t = (row ? row.innerText : '').replace(/\s+/g, ' ').trim();
  return {name: s.name, row: t.slice(0, 300),
          free: /\bFREE\b/i.test(t) || /\$\s?0(\.00)?\b/.test(t),
          options: [...s.options].map(o => o.value), visible: !!s.offsetParent};
})
"""
SUCCESS_RX = re.compile(
    r"you(?:'| a)re (?:now |successfully )?registered|registration (?:is )?(?:complete|confirmed|successful)"
    r"|thank you for (?:registering|your registration)|you(?:'| a)re on the wait(?:ing)?\s?list"
    r"|added to the wait(?:ing)?\s?list|cancel (?:my )?registration|unregister|you are going", re.I)
PAYMENT_SEL = ", ".join([
    "input[autocomplete^='cc-']", "input[name*='card' i]", "input[name*='cvv' i]",
    "input[name*='cvc' i]", "iframe[src*='stripe']", "iframe[src*='braintree']",
    "iframe[src*='cybersource']", "iframe[src*='authorize.net']", "iframe[src*='paypal']",
    "iframe[name*='card' i]", "iframe[title*='card' i]", "iframe[title*='payment' i]",
])
NEXT_BTN_RX = re.compile(r"^\s*(confirm|complete( registration)?|submit|finish|continue|register)\s*$", re.I)


def _shot(page: Page, cfg: dict, eid: str, step: str) -> str:
    p = cfg["state_dir"] / "shots" / f"{eid}-{datetime.now():%m%d-%H%M%S}-{step}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
    except Exception:
        pass
    return str(p)


def _needs_payment(page: Page) -> bool:
    if page.locator(PAYMENT_SEL).count():
        return True
    body = page.inner_text("body")
    return bool(re.search(r"card number|billing address|payment method|checkout", body, re.I))


def _empty_required_fields(page: Page) -> int:
    return page.evaluate(r"""() => [...document.querySelectorAll(
        'input[required], textarea[required], select[required]')]
        .filter(e => e.offsetParent && e.type !== 'hidden' &&
                     (e.type === 'checkbox' || e.type === 'radio' ? !e.checked : !e.value)).length""")


def register(ctx: BrowserContext, cfg: dict, eid: str, dry_run: bool) -> tuple[str, str]:
    """Returns (status, detail). Never submits anything that asks for payment."""
    url = f'{cfg["base_url"]}/rsvp_boot?id={eid}'
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        check_logged_in(page, cfg)
        page.wait_for_load_state("networkidle", timeout=20_000)
        title = (page.locator("h1, h2").first.inner_text() if page.locator("h1, h2").count() else eid).strip()
        body = page.inner_text("body")
        if SUCCESS_RX.search(body):
            return "already", f"{title}: already registered"
        win = reg_window(body)
        tickets = page.evaluate(TICKETS_JS)
        if win and not tickets:
            if datetime.now() > win[1]:
                return "closed", f"{title}: registration closed {win[1]:%b %-d %-I:%M %p}"
            # Not open yet (or server clock slightly behind ours): caller schedules / retries.
            return "scheduled", f"{win[0].timestamp()}|{title}: registration opens {win[0]:%a %b %-d %-I:%M %p}"
        if not tickets:
            return "manual", f"{title}: no ticket selector (closed, full, or a different flow). {_shot(page, cfg, eid, 'noform')}"
        free = [t for t in tickets if t["free"] and t["visible"]
                and any(o not in ("", "0") for o in t["options"])]
        if not free:
            return "paid", f"{title}: no free ticket; pay yourself: {url}"
        pick = free[0]
        others = [t for t in tickets if t["name"] != pick["name"] and t["visible"]]
        if any("0" not in t["options"] and "" not in t["options"] for t in others):
            # Can't deselect a paid ticket type, so submitting could add a paid ticket.
            return "manual", f"{title}: mixed free/paid tickets that can't be deselected; pick yourself: {url}"
        qty = str(cfg.get("registration", {}).get("quantity", 1))
        for t in tickets:
            sel = page.locator(f'select[name="{t["name"]}"]')
            if t["name"] == pick["name"]:
                sel.select_option(qty if qty in t["options"] else next(o for o in t["options"] if o not in ("", "0")))
            elif t in others:
                sel.select_option("0" if "0" in t["options"] else "")
        if dry_run:
            return "dry-run", f'{title}: would register "{pick["row"]}". {_shot(page, cfg, eid, "dryrun")}'

        # On CBS CampusGroups the Register control is <a href="javascript:submitRegistrationForm()">
        btn = page.locator('a[href*="submitRegistrationForm"]')
        form = page.locator("form[name=register_form]")
        if not btn.count():
            btn = page.get_by_role("button", name=re.compile(r"^\s*register\s*$", re.I))
        if not btn.count():
            btn = form.locator("input[type=submit], button[type=submit]")
        if not btn.count():
            return "manual", f"{title}: Register button not found. {_shot(page, cfg, eid, 'nobtn')}"
        btn.first.click()

        for step in range(3):  # follow up to 3 confirmation screens
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            time.sleep(1.5)
            if _needs_payment(page):
                return "paid", f"{title}: reached a payment step; finish yourself: {page.url}"
            body = page.inner_text("body")
            if SUCCESS_RX.search(body):
                shot = _shot(page, cfg, eid, "done")
                save_session(ctx, cfg)
                return "registered", f"{title}: registered. {shot}"
            if _empty_required_fields(page):
                return "manual", f"{title}: form has required questions; finish here: {page.url} {_shot(page, cfg, eid, 'questions')}"
            nxt = page.get_by_role("button", name=NEXT_BTN_RX)
            if not nxt.count():
                nxt = page.get_by_role("link", name=NEXT_BTN_RX)
            if not nxt.count():
                break
            nxt.first.click()
        return "unknown", f"{title}: submitted but no confirmation detected; please check {url} {_shot(page, cfg, eid, 'unknown')}"
    finally:
        page.close()


def handle_registration(ctx: BrowserContext, cfg: dict, state: State, eid: str, dry_run: bool,
                        at_opening: bool = False) -> None:
    """at_opening=True: we were woken at the scheduled open time; retry briefly if not open yet."""
    attempts = 25 if at_opening else 1
    for i in range(attempts):
        try:
            status, detail = register(ctx, cfg, eid, dry_run)
        except LoggedOut:
            raise
        except Exception as e:
            status, detail = "error", f"event {eid}: {e.__class__.__name__}: {e}"
            traceback.print_exc()
        if status != "scheduled" or not at_opening:
            break
        time.sleep(2)  # server clock may lag ours by a few seconds
    if status == "scheduled":
        ts, _, detail = detail.partition("|")
        if at_opening:  # still not open after ~1 min of retries
            state.unschedule(eid)
            status, detail = "manual", f"{detail} - still not open after retries; check {cfg['base_url']}/rsvp_boot?id={eid}"
        else:
            state.schedule(eid, float(ts))
            state.set_reg(eid, "scheduled")
            log(f"register {eid}: scheduled - {detail}")
            push(cfg, "Scheduled to auto-register", f"{detail}. Keep your Mac awake around then.",
                 priority=3, click=f'{cfg["base_url"]}/rsvp_boot?id={eid}')
            return
    state.unschedule(eid)
    state.set_reg(eid, status)
    log(f"register {eid}: {status} - {detail}")
    url = f'{cfg["base_url"]}/rsvp_boot?id={eid}'
    prio = 3 if status in ("registered", "already", "dry-run") else 4
    push(cfg, f"Registration: {status}", detail, priority=prio, click=url)


# ---------------------------------------------------------------- commands ---
def cmd_init(_args) -> None:
    if CONFIG_PATH.exists():
        sys.exit(f"{CONFIG_PATH} already exists; edit it directly.")
    text = (HERE / "config.example.yaml").read_text()
    tag = secrets.token_hex(6)
    text = (text.replace("cg-alerts-CHANGEME", f"cg-alerts-{tag}")
                .replace("cg-approve-CHANGEME", f"cg-approve-{secrets.token_hex(8)}")
                .replace("approval_secret: CHANGEME", f"approval_secret: {secrets.token_hex(24)}"))
    CONFIG_PATH.write_text(text)
    cfg = yaml.safe_load(text)
    print(f"Wrote {CONFIG_PATH}\n\nIn the ntfy phone app, subscribe to topic:\n  {cfg['ntfy']['alert_topic']}\n"
          "Keep both topic names private: anyone who knows them can read alerts.")


def cmd_login(_args) -> None:
    cfg = load_config()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        sp = storage_path(cfg)
        ctx = browser.new_context(storage_state=str(sp) if sp.exists() else None)
        page = ctx.new_page()
        page.goto(f'{cfg["base_url"]}/events')
        print("A browser window opened. Sign in with your UNI + Duo until you see the Events list.")
        try:
            page.wait_for_selector('li[id^="event_"]', timeout=300_000)
        except Exception:
            input("Press Enter here once the Events list is visible... ")
        save_session(ctx, cfg)
        print(f"Session saved to {sp}")
        browser.close()


def _with_ctx(cfg, fn):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = new_context(browser, cfg)
        try:
            return fn(ctx)
        except LoggedOut:
            sys.exit("Session expired. Run: python cg_agent.py login")
        finally:
            browser.close()


def cmd_scan(args) -> None:
    cfg = load_config()
    state = State(cfg["state_dir"] / "state.db")
    _with_ctx(cfg, lambda ctx: scan(ctx, cfg, state, args.notify_existing))


def cmd_register(args) -> None:
    cfg = load_config()
    state = State(cfg["state_dir"] / "state.db")
    dry = args.dry_run or cfg.get("registration", {}).get("dry_run", False)
    _with_ctx(cfg, lambda ctx: handle_registration(ctx, cfg, state, str(args.event_id), dry))


def cmd_test_notify(_args) -> None:
    cfg = load_config()
    push(cfg, "CampusGroups agent: test", "If you can read this, alerts work.", priority=3)
    print("Sent. Check your phone.")


def cmd_run(_args) -> None:
    cfg = load_config()
    state = State(cfg["state_dir"] / "state.db")
    interval = max(3, int(cfg.get("scan_interval_minutes", 10))) * 60
    dry = bool(cfg.get("registration", {}).get("dry_run", False))
    q: "queue.Queue[str]" = queue.Queue()
    stop = threading.Event()
    threading.Thread(target=listen_for_approvals, args=(cfg, state, q, stop), daemon=True).start()
    log(f"Agent running. Scan every {interval // 60} min. dry_run={dry}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx, ctx_mtime = None, 0.0
        next_scan, logged_out_since = 0.0, None
        try:
            while True:
                sp = storage_path(cfg)
                mtime = sp.stat().st_mtime if sp.exists() else 0
                if ctx is None or (logged_out_since and mtime > ctx_mtime):
                    if ctx:
                        ctx.close()
                    ctx, ctx_mtime = new_context(browser, cfg), mtime
                    if logged_out_since:
                        log("New session detected; resuming.")
                        push(cfg, "CampusGroups agent resumed", "Login refreshed.", priority=2)
                    logged_out_since = None

                if logged_out_since:
                    time.sleep(30)  # wait for `cg_agent.py login` to refresh the session file
                    continue
                try:
                    soon = state.next_scheduled()
                    if soon and soon[1] - time.time() < 180:
                        next_scan = max(next_scan, soon[1] + 30)  # never let a scan delay an opening
                    if time.time() >= next_scan:
                        try:
                            scan(ctx, cfg, state)
                        except LoggedOut:
                            raise
                        except Exception as e:
                            log(f"scan error: {e}")
                        next_scan = time.time() + interval
                        ctx_mtime = storage_path(cfg).stat().st_mtime
                    sched = state.next_scheduled()
                    if sched and sched[1] <= time.time():
                        log(f"registration window opening for {sched[0]}")
                        handle_registration(ctx, cfg, state, sched[0], dry, at_opening=True)
                        ctx_mtime = storage_path(cfg).stat().st_mtime
                        continue
                    wake = min(next_scan, sched[1] if sched else next_scan)
                    try:
                        eid = q.get(timeout=max(0.2, wake - time.time()))
                    except queue.Empty:
                        continue
                    handle_registration(ctx, cfg, state, eid, dry)
                    ctx_mtime = storage_path(cfg).stat().st_mtime
                except LoggedOut:
                    logged_out_since = time.time()
                    log("Session expired; waiting for re-login.")
                    push(cfg, "CampusGroups: re-login needed",
                         "Duo session expired. On your Mac run:  python cg_agent.py login  "
                         "(pending approvals will be retried by tapping Register again).", priority=4)
        finally:
            stop.set()
            browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    s = sub.add_parser("scan")
    s.add_argument("--notify-existing", action="store_true",
                   help="on the very first run, push current events instead of silently baselining")
    s.set_defaults(fn=cmd_scan)
    r = sub.add_parser("register")
    r.add_argument("event_id")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_register)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("test-notify").set_defaults(fn=cmd_test_notify)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
