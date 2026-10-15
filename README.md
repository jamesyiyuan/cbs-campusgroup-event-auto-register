# CampusGroups event agent

Watches CBS CampusGroups for newly published events and pushes each one to your phone.
Tap **Register (free)** on the notification and the agent registers you within seconds.
Paid events only get an **Open to pay** link; the agent never touches payment.

```
 your Mac (launchd)                          ntfy.sh                    your phone
 ┌─────────────────────────┐   new event   ┌──────────────┐  push   ┌──────────────┐
 │ scan /events every 10m  │ ────────────▶ │ alert topic  │ ──────▶ │ [Register]   │
 │ (headless Chromium,     │               └──────────────┘         │ [Open]       │
 │  your saved Duo session)│   approval    ┌──────────────┐  tap    └──────┬───────┘
 │ register free ticket ◀──┼───────────────│ approve topic│ ◀──────────────┘
 └─────────────────────────┘ (HMAC-signed) └──────────────┘
```

## Setup (about 10 minutes, on your Mac)

```bash
cd cg-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python cg_agent.py init          # writes config.yaml with private topic names
# phone: install "ntfy" (iOS/Android), subscribe to the alert topic it printed
python cg_agent.py test-notify

python cg_agent.py login         # browser opens: UNI + Duo, wait for Events list
python cg_agent.py scan          # first run only records today's events (baseline)
```

Then test registration safely, on a free event you actually want:

```bash
python cg_agent.py register 130534 --dry-run   # fills the form, screenshots, does NOT submit
# check the PNG in ~/.cg-agent/shots/
```

When the dry run looks right, set `registration.dry_run: false` in `config.yaml`, try one real
`register <id>`, confirm it shows up under *My Upcoming Events*, then start the daemon:

```bash
chmod +x install_launchd.sh && ./install_launchd.sh
tail -f ~/.cg-agent/agent.log
```

Event ID = the number in `rsvp_boot?id=NNNNNN` on any event page.

## What happens on each notification

| Event | Buttons | On tap |
|---|---|---|
| FREE, or FREE tier available | Register (free) / Open | Picks the free ticket, qty 1, submits, follows up to 3 confirm screens, pushes the result |
| Waitlist only | Join waitlist / Open | Same flow; CampusGroups puts you on the waitlist |
| Paid only | Open to pay | Opens the event page; you pay yourself |
| Registration not open yet | Register (free) / Open | **Pre-approve.** The push says when it opens; tap Register and the agent schedules itself, fires at the opening minute, and retries every 2s for ~1 min |

The agent stops and sends you a link instead of guessing when: a payment field appears, the form has
required custom questions, the Register button is missing, or it can't see a confirmation. A screenshot
of every outcome lands in `~/.cg-agent/shots/`.

Watch-list matches (`filters.watch_keywords`) get a sound alert and a `*`; everything else is a silent
push; beyond 10 per scan they are bundled into one digest.

## Known limits (read these)

- **Duo session expiry.** The agent reuses the cookies from `login`. When CampusGroups/Columbia SSO
  expires them, you get a "re-login needed" push; run `python cg_agent.py login` and it resumes on
  its own. How often that happens is untested; expect days, not months.
- **Mac must be awake.** launchd pauses during sleep. Approvals tapped while asleep are queued by
  ntfy (about 12 hours) and processed on wake, which may be too late for hot events.
- **Registration flow is only partly verified.** The listing parser, ticket selector, paid detection and
  the `submitRegistrationForm()` Register link were checked against the live site, and the full
  scan → push → approve → register loop was tested end-to-end against a local mock. The screens *after*
  clicking Register on the real site were not, because that would actually register you. That's why
  `dry_run` defaults to true. If your first real run reports `unknown`, the screenshot shows why.
- **"New" = not seen before** in the first `max_pages` pages of the date-sorted list (~300 events,
  several weeks out). An event posted for a date further out appears once it enters that window.
- **Topic names are secrets.** Anyone with the alert topic can read your alerts; approvals are
  HMAC-signed, so knowing the approve topic alone can't register you. For more privacy, reserve the
  topics with a free ntfy account and set `ntfy.token`.
- **Policy.** Automated access may conflict with CampusGroups' terms or Columbia IT policy. This design
  keeps you as the decision-maker for every registration and polls lightly, but the risk is yours to judge.

## Commands

```
python cg_agent.py init | login | scan [--notify-existing] | register ID [--dry-run] | run | test-notify
```
