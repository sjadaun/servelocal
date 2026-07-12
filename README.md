# ServeLocal — Meal Planner for Raspberry Pi Zero 2W

A small self-hosted meal planner: manage meals from a web page on any device
on your Wi-Fi, and see "what's next" (plus current weather) on a 240x240 SPI
screen attached to the Pi. The web UI is a 4-tab app (Next / Calendar / All
Meals / Add) with a dark/light toggle, and the whole thing is backed by a
versioned JSON REST API so a future Android/iOS app can talk to the same Pi
with zero backend changes.

## What's in this project

| File | Purpose |
|---|---|
| `app.py` | Flask HTTP server + REST API (the "backend") |
| `database.py` | SQLite schema + recurring-meal + per-day-exception scheduling logic |
| `display.py` | Standalone daemon that draws to the SPI screen, incl. weather |
| `push.py` | Web Push: VAPID keys, subscriptions, sending "start prep by" reminders |
| `templates/index.html`, `static/*` | The web UI |
| `servelocal_planner.service`, `servelocal_display.service` | systemd units to run both on boot |
| `install.sh` | Installs (or updates) everything above in one step -- see below |

The web server and the display daemon are **two separate processes** that
both read/write the same SQLite file, so the screen keeps refreshing
smoothly even while you're editing meals from your phone.

## 1. Enable SPI on the Pi

```bash
sudo raspi-config
# Interface Options -> SPI -> Enable -> reboot
```

## 2. Install (or update)

```bash
git clone <your-repo-or-copy-this-folder> servelocal
cd servelocal
sudo ./install.sh
```

This deploys everything to `/var/lib/servelocal`, creates a Python venv
there, installs the systemd units, and starts both services. **It's safe to
re-run** any time you pull new changes -- it updates the code in place and
never touches your existing `mealplanner.db`, so your meals and settings
survive updates.

By default it installs for the `pi` user; override with
`sudo SERVICE_USER=myuser INSTALL_DIR=/opt/servelocal ./install.sh` if you
need something different.

<details>
<summary>Prefer to do it by hand instead? (click to expand)</summary>

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv fonts-dejavu-core rsync
sudo mkdir -p /var/lib/servelocal
sudo cp -r app.py database.py display.py static templates requirements.txt version.txt /var/lib/servelocal/
cd /var/lib/servelocal
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt
sudo cp servelocal_planner.service servelocal_display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now servelocal_planner.service servelocal_display.service
```
</details>

## 3. Set your location for weather

Open `display.py` and set `LATITUDE` / `LONGITUDE` near the top (defaults to
Chennai). Weather comes from [Open-Meteo](https://open-meteo.com/), which is
free and needs no API key. It's refreshed every 15 minutes and cached, so it
won't hammer the API or need much bandwidth on a Pi Zero.

## 4. Confirm the display driver

This project talks to an **ST7789** driven 240x240 SPI panel directly over
raw SPI (`spidev`) with GPIO reset/DC/backlight lines via `gpiozero` -- no
vendor display library required. Wiring assumed:

```
VCC -> 3V3      DIN -> GPIO10 (MOSI)   CLK -> GPIO11 (SCLK)
GND -> GND      CS  -> GPIO8  (CE0)    DC  -> GPIO25
RST -> GPIO27   BL  -> GPIO24 (backlight control)
```

If your board's silkscreen uses different pins, edit the `PIN_*` constants
at the top of `display.py`. If it's a different driver chip entirely (e.g.
GC9A01, ST7735), only `init_display()` and `push_frame()` need to change —
everything else just paints onto a PIL `Image`.

## 5. Try it manually first (optional)

If you want to poke at things before running `install.sh`, or you're
iterating on the code:

```bash
python3 app.py        # in one terminal -> visit http://<pi-ip>:8080
python3 display.py    # in another terminal -> screen should light up
```

Add a meal or two from the web UI and confirm the screen updates within
~8 seconds, and the weather strip appears at the top within a few seconds
(needs internet access).

## 6. Boot behavior

`install.sh` already enables both services to start on boot, so normally
you don't need to do anything else here. A couple of things worth knowing:

- **The display starts as early as possible**, before it waits on the
  network or the web server, and shows a "ServeLocal / Starting…" splash
  screen immediately -- useful proof-of-life on a Pi with no monitor/desktop,
  since otherwise the screen would just stay blank for however long the rest
  of boot takes. It creates its own database if the web server hasn't
  gotten to it yet, so the order they start in doesn't matter.
- Check on things any time with:
  ```bash
  systemctl status servelocal_planner servelocal_display
  journalctl -u servelocal_planner -f
  journalctl -u servelocal_display -f
  ```

## Theme: one shared setting, not a per-browser preference

Light/Dark/Auto is a single setting stored on the Pi (`app_settings` table),
not something each browser remembers independently. Changing it from the
web UI changes the physical display too, and vice versa conceptually --
they both read the same value. "Auto" is resolved from the **server's**
clock (6 AM-7 PM = light, otherwise dark), not the browser's OS theme,
since the physical screen has no such concept to borrow from.

Two things that can make a theme change *look* delayed or wrong if you're
not expecting them:
- The physical display only redraws every `REFRESH_SECONDS` (8s by
  default) -- toggling the web UI and immediately checking the screen can
  catch the previous frame.
- Browsers cache static JS/CSS. The web UI's `<link>`/`<script>` tags are
  cache-busted using `version.txt`, so bumping that file after a code
  change forces browsers to fetch the new files instead of running stale
  cached ones.

## How scheduling works

Each meal you add is a "slot": a name, a category (breakfast / lunch /
dinner / snack / other), a time it should be ready by, optional
prep minutes, optional notes, and a repeat rule:

- **Just once** — happens only on the start date
- **Every day** — happens daily from the start date (optionally until an end date)
- **Specific weekdays** — e.g. weekday lunches, Mon–Fri only
- **Monthly** — happens once a month, on the same day-of-month as the start
  date (e.g. start it on the 15th and it repeats on the 15th every month;
  in shorter months it's clamped to the last day of that month)

A meal can also be marked **Going out to eat**, with a place name — this
works on top of any category (e.g. "Dinner — going out to Barbeque Nation"
on a specific Friday), and the screen shows a distinct "Eating Out" badge
with the place and "leave by" time instead of prep instructions.

### Editing a single occurrence (Calendar tab)

Recurring meals normally apply to every matching day, but you'll often want
to change just one day — e.g. skip Tuesday's dinner because you're
travelling, or push Thursday's lunch to a later time. The **Calendar** tab
lets you tap any day and, per meal on that day:
- **Edit** — override just that day's time, category, notes, or going-out
  details, without touching the rest of the series
- **Skip** — cancel just that occurrence
- **Revert / Restore** — remove the override or cancellation and fall back
  to the series default

Under the hood this is stored as a `meal_exceptions` row keyed to
`(meal, date)` — the recurring rule itself is never modified, so every other
occurrence is unaffected. The month grid shows a dot on any day with at
least one (non-skipped) meal planned.

## API reference (for the web UI today, and a mobile app later)

Base URL: `http://<pi-ip>:8080/api/v1`

Every response has the same envelope:
```json
{ "success": true,  "data": { ... } }
{ "success": false, "error": "human-readable message" }
```
HTTP status codes are meaningful: `200` ok, `201` created, `404` not found,
`422` validation error, `500` server error. CORS is enabled (`flask-cors`).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/categories` | Valid category ids + labels |
| GET | `/meals` | List all recurring meal series |
| POST | `/meals` | Create a series |
| GET | `/meals/{id}` | Fetch one series |
| PUT / PATCH | `/meals/{id}` | Update a series (full / partial) |
| DELETE | `/meals/{id}` | Delete a series entirely |
| GET | `/today` | Today's occurrences, each with a `done` flag |
| GET | `/next` | The single soonest upcoming occurrence, or `null` |
| GET | `/theme` | `{ "mode": "light"/"dark"/"auto", "resolved": "light"/"dark" }` |
| PUT | `/theme` | Set the theme mode -- **shared** by the web UI and the physical display, see below |
| GET | `/calendar?year=&month=` | `{ "YYYY-MM-DD": count }` for that month |
| GET | `/calendar/{date}` | All occurrences on that date (incl. skipped ones) |
| PUT | `/meals/{id}/occurrences/{date}` | Override or skip (`cancelled:true`) a single occurrence |
| DELETE | `/meals/{id}/occurrences/{date}` | Revert a single occurrence to the series default |
| GET | `/push/vapid-public-key` | `{ "key": "..." }` -- needed by the browser to subscribe |
| POST | `/push/subscribe` | Body: the browser's `PushSubscription.toJSON()` |
| POST | `/push/unsubscribe` | Body: `{ "endpoint": "..." }` |

A meal series object looks like:
```json
{
  "id": 1,
  "name": "Family Dinner",
  "category": "dinner",
  "notes": "",
  "scheduled_time": "20:00",
  "prep_minutes": 20,
  "going_out": false,
  "going_out_place": "",
  "repeat_type": "weekly",
  "repeat_days": [0, 1, 2, 3, 4],
  "start_date": "2026-07-06",
  "end_date": null,
  "active": true,
  "created_at": "2026-07-06T09:00:00"
}
```
`repeat_days` is always a real JSON array (`0`=Monday..`6`=Sunday) and
`active`/`going_out` are real booleans, so a mobile client never has to
parse a CSV string.

An occurrence object (from `/today`, `/next`, `/calendar/{date}`) is the
effective, merged view for one specific date — it looks similar but adds
`when` (full ISO datetime), `slot_id` (the underlying series), and, for
calendar day views, `has_override` / `cancelled` flags.

Since this is meant for your home Wi-Fi rather than the public internet,
there's no auth layer. If you ever expose it beyond your LAN, put it behind
a reverse proxy with at least an API key or basic auth in front of it.

## Meal reminders (push notifications)

Tap the bell icon in the web UI to get a notification on your phone at
each meal's "start prep by" (or "leave by", for going-out meals) time.
This works per-device -- everyone in the household enables it separately
on their own phone.

**Android** (Chrome, Edge, Firefox): works from a normal bookmarked tab,
nothing extra needed.

**iPhone/iPad (Safari)**: Apple only allows web push for a site added to
the Home Screen (Share button -> "Add to Home Screen"), opened from that
icon, on iOS 16.4+. A regular Safari tab or bookmark cannot receive push
notifications there -- this is a platform restriction, not something this
app can work around. The bell button detects this and shows instructions
instead of silently failing.

Under the hood: this uses standard Web Push (VAPID), not any third-party
notification service. A keypair is generated automatically on first run
and stored at `vapid_private_key.pem` in the install directory -- back
this up if you care about not having to re-subscribe every device after a
reinstall (deleting it just means everyone re-taps the bell once). A
background thread inside the web server checks once a minute for meals
whose prep time just arrived and sends to every subscribed device; each
occurrence is only ever notified once, tracked in the `sent_notifications`
table.

## Ideas for later

- Add the Waveshare HAT's joystick/buttons to cycle between "next meal" and
  "full day view" on the physical screen.
- Sunrise/sunset or a simple weather icon glyph instead of text label.
- Swap SQLite for a synced backend if you want multiple Pis/screens.
