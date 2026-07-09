# Meal Planner for Raspberry Pi 

A small self-hosted meal planner: manage meals from a web page on any device
on your Wi-Fi, and see "what's next" on a 240x240 SPI screen attached to the
Pi. The web UI is a 3-tab app (Next / All Meals / Add) with a dark/light
toggle, and the whole thing is backed by a versioned JSON REST API so a
future Android/iOS app can talk to the same Pi with zero backend changes.

## What's in this project

| File | Purpose |
|---|---|
| `app.py` | Flask HTTP server + REST API (the "backend") |
| `database.py` | SQLite schema + repeat-rule scheduling logic |
| `display.py` | Standalone daemon that draws to the SPI screen |
| `templates/index.html`, `static/*` | The web UI |
| `meal_planner.service`, `meal_display.service` | systemd units to run both on boot |

The web server and the display daemon are **two separate processes** that
both read/write the same SQLite file. That way the screen keeps refreshing
smoothly even while you're editing meals from your phone.

## 1. Enable SPI on the Pi

```bash
sudo raspi-config
# Interface Options -> SPI -> Enable -> reboot
```

## 2. Install dependencies

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv fonts-dejavu
cd /home/pi
git clone <your-repo-or-copy-this-folder> meal-planner-pi
cd meal-planner-pi
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> If you used a venv, update the `ExecStart` lines in the two `.service`
> files to point at `venv/bin/python3` instead of `/usr/bin/python3`.

## 3. Confirm the display driver

This project assumes an **ST7789** driven 240x240 SPI panel (the most common
chip for these boards) using the `st7789` pip package. Wiring assumed:

```
VCC -> 3V3      DIN -> GPIO10 (MOSI)   CLK -> GPIO11 (SCLK)
GND -> GND      CS  -> GPIO8  (CE0)    DC  -> GPIO25
RST -> GPIO27   BL  -> GPIO24 (optional backlight control)
```

If your board's silkscreen uses different pins, edit the `PIN_*` constants
at the top of `display.py`. If it turns out to be a different driver chip
entirely (e.g. GC9A01, ST7735), only the `init_display()` function in
`display.py` needs to change — everything else (all the drawing code) works
on any driver since it just paints onto a PIL `Image` and hands it off.

Quick test before wiring into the full project:
```python
import ST7789 as st7789
disp = st7789.ST7789(port=0, cs=0, dc=25, rst=27, backlight=24, width=240, height=240)
disp.begin()
```
If that errors out, search for your exact board model + "python driver" —
you'll just need to swap that block.

## 4. Try it manually first

```bash
python3 app.py        # in one terminal -> visit http://<pi-ip>:8080
python3 display.py    # in another terminal -> screen should light up
```

Add a meal or two from the web UI and confirm the screen updates within
~20 seconds.

## 5. Run both on boot

```bash
sudo cp meal_planner.service meal_display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meal_planner.service
sudo systemctl enable --now meal_display.service

# check logs if something's off:
journalctl -u meal_planner -f
journalctl -u meal_display -f
```

## API reference (for the web UI today, and a mobile app later)

Base URL: `http://<pi-ip>:8080/api/v1`

Every response has the same envelope, so client code can handle it generically:
```json
{ "success": true,  "data": { ... } }
{ "success": false, "error": "human-readable message" }
```
HTTP status codes are meaningful: `200` ok, `201` created, `404` not found,
`422` validation error, `500` server error. CORS is enabled (`flask-cors`),
so it can also be called from a webview or a different origin during
development.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check, returns server time |
| GET | `/categories` | Valid category ids + display labels |
| GET | `/meals` | List all meals |
| POST | `/meals` | Create a meal, returns the created object |
| GET | `/meals/{id}` | Fetch one meal |
| PUT | `/meals/{id}` | Full update (send the complete object) |
| PATCH | `/meals/{id}` | Partial update (send only changed fields) |
| DELETE | `/meals/{id}` | Delete a meal |
| GET | `/today` | Today's meals in time order, each with a `done` flag |
| GET | `/next` | The single soonest upcoming meal, or `null` |

A meal object looks like:
```json
{
  "id": 1,
  "name": "School Lunchbox",
  "category": "school",
  "notes": "Sandwich + fruit",
  "scheduled_time": "08:00",
  "prep_minutes": 20,
  "temperature": "",
  "repeat_type": "weekly",
  "repeat_days": [0, 1, 2, 3, 4],
  "start_date": "2026-07-06",
  "end_date": null,
  "active": true,
  "created_at": "2026-07-06T09:00:00"
}
```
`repeat_days` is `0`=Monday..`6`=Sunday, always a real JSON array (never a
CSV string), and `active` is a real boolean -- both were picked specifically
so a mobile client doesn't have to do any string-parsing.

Since this is meant for your home Wi-Fi rather than the public internet,
there's no auth layer. If you ever expose it beyond your LAN (e.g. via a
tunnel so a phone app works away from home), put it behind a reverse proxy
with at minimum an API key or basic auth in front of it.

## How scheduling works

Each meal you add is a "slot": a name, a category (breakfast / lunch /
dinner / school / snack / other), a time it should be **ready by**, optional
prep-time and temperature, and a repeat rule:

- **Just once** — happens only on the start date
- **Every day** — happens daily from the start date (optionally until an end date)
- **Specific weekdays** — e.g. "school lunch" on Mon–Fri only

The screen always shows the single soonest upcoming meal across all active
slots, a countdown, when to start prepping (scheduled time minus prep
minutes), temperature if set, and a count of what's left today.

## Ideas for later

- Add the Waveshare HAT's joystick/buttons to cycle between "next meal" and
  "full day view" — `st7789`-based HATs typically expose these as plain GPIO
  inputs you can poll with `RPi.GPIO` inside `display.py`'s main loop.
- Add a "mark as done early" button/endpoint if you cook ahead of schedule.
- Swap SQLite for a synced backend if you want multiple Pis/screens.
