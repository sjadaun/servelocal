"""
display.py
Standalone daemon that reads the meal-planner DB directly (no HTTP hop, so it
stays fast and light on a Pi Zero 2W) and redraws the 240x240 SPI screen
every REFRESH_SECONDS. Also polls a free weather API on its own, slower
schedule and shows a small strip at the top of the screen.

Talks to an ST7789-driven 240x240 SPI IPS panel directly over raw SPI
(spidev) with GPIO reset/DC/backlight lines via gpiozero -- no vendor
display library required.

--- Wiring (default pins below, standard for Pi Zero 2W 40-pin header) ---
  VCC -> 3V3      DIN -> GPIO10 (MOSI)   CLK -> GPIO11 (SCLK)
  GND -> GND      CS  -> GPIO8  (CE0)    DC  -> GPIO25
  RST -> GPIO27   BL  -> GPIO24 (backlight control)
"""

import time
import datetime
from PIL import Image, ImageDraw, ImageFont
import spidev
from gpiozero import DigitalOutputDevice

import database as db

try:
    import requests
except ImportError:
    requests = None

REFRESH_SECONDS = 20
WEATHER_REFRESH_SECONDS = 15 * 60  # weather doesn't need to be near-real-time

# Set this to your location -- defaults to Chennai, IN. Get coordinates for
# your own city from https://open-meteo.com/en/docs#latlng (or just Google
# "<your city> latitude longitude").
LATITUDE = 13.0827
LONGITUDE = 80.2707

PIN_DC = 25
PIN_RST = 27
PIN_BL = 24
SPI_PORT = 0
SPI_CS = 0  # CE0
SPI_SPEED_HZ = 40_000_000

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


def load_font(name, size):
    try:
        return ImageFont.truetype(FONT_DIR + name, size)
    except OSError:
        return ImageFont.load_default()


FONT_WEATHER = load_font("DejaVuSans-Bold.ttf", 13)
FONT_TIME = load_font("DejaVuSans-Bold.ttf", 16)
FONT_LABEL = load_font("DejaVuSans-Bold.ttf", 16)
FONT_MEAL = load_font("DejaVuSans-Bold.ttf", 36)   # fits on one line
FONT_MEAL_WRAP = load_font("DejaVuSans-Bold.ttf", 24)  # used when it needs 2 lines
FONT_SMALL = load_font("DejaVuSans.ttf", 14)
FONT_TINY = load_font("DejaVuSans.ttf", 13)

# The theme mode (light / dark / auto) is a single setting shared with the
# web UI via the database -- see database.resolve_theme(). Each theme here
# supplies every color render_frame() needs.
THEMES = {
    "dark": {
        "bg": (18, 18, 22),
        "topbar_bg": (30, 60, 78),
        "topbar_text": (210, 230, 235),
        "text": (255, 255, 255),
        "muted": (150, 150, 150),
        "dim": (120, 120, 120),
        "detail": (200, 200, 200),
        "divider": (50, 50, 55),
        "amber": (230, 180, 90),
        "categories": {
            "breakfast": (240, 170, 60),
            "lunch": (110, 190, 110),
            "dinner": (110, 130, 220),
            "snack": (200, 160, 110),
            "other": (160, 160, 160),
        },
        "going_out": (230, 150, 80),
    },
    "light": {
        "bg": (247, 246, 241),
        "topbar_bg": (203, 227, 235),
        "topbar_text": (20, 55, 65),
        "text": (25, 22, 18),
        "muted": (110, 106, 98),
        "dim": (140, 136, 128),
        "detail": (70, 66, 60),
        "divider": (222, 218, 208),
        "amber": (170, 110, 15),
        "categories": {
            "breakfast": (190, 120, 20),
            "lunch": (50, 120, 55),
            "dinner": (60, 75, 170),
            "snack": (140, 95, 45),
            "other": (110, 108, 100),
        },
        "going_out": (185, 100, 30),
    },
}

# WMO weather codes (used by Open-Meteo) -> short label. Kept deliberately
# short since this shares a thin bar with the clock -- see render_frame()'s
# dynamic width-fit for the hard guarantee against overlap either way.
WEATHER_CODES = {
    0: "Clear", 1: "Clear", 2: "P.Cloudy", 3: "Cloudy",
    45: "Fog", 48: "Fog",
    51: "Drizzle", 53: "Drizzle", 55: "Drizzle+",
    61: "Rain", 63: "Rain", 65: "Rain+",
    71: "Snow", 73: "Snow", 75: "Snow+",
    80: "Showers", 81: "Showers", 82: "Showers+",
    95: "Storm", 96: "Storm", 99: "Storm+",
}

_weather_cache = {"data": None, "fetched_at": 0}


def fetch_weather():
    """Cached weather fetch; returns dict or None if unavailable."""
    now = time.time()
    if _weather_cache["data"] and (now - _weather_cache["fetched_at"] < WEATHER_REFRESH_SECONDS):
        return _weather_cache["data"]
    if requests is None:
        return _weather_cache["data"]
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LATITUDE,
                "longitude": LONGITUDE,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code",
                "timezone": "auto",
            },
            timeout=8,
        )
        resp.raise_for_status()
        cur = resp.json()["current"]
        data = {
            "temp": round(cur["temperature_2m"]),
            "feels_like": round(cur["apparent_temperature"]),
            "humidity": round(cur["relative_humidity_2m"]),
            "label": WEATHER_CODES.get(cur["weather_code"], "—"),
        }
        _weather_cache["data"] = data
        _weather_cache["fetched_at"] = now
        return data
    except Exception as exc:
        print(f"[display] weather fetch failed: {exc}")
        return _weather_cache["data"]  # fall back to stale data if we have any


_spi = None
_rst_pin = None
_dc_pin = None
_bl_pin = None


def _send_command(cmd):
    _dc_pin.off()
    _spi.writebytes([cmd])


def _send_data(data):
    _dc_pin.on()
    _spi.writebytes([data])


def _send_data_buf(buf):
    _dc_pin.on()
    _spi.writebytes2(buf)


def init_display():
    """Bring up the SPI bus + GPIO control lines and run the ST7789 register
    init sequence. Call once before the render loop."""
    global _spi, _rst_pin, _dc_pin, _bl_pin

    _rst_pin = DigitalOutputDevice(PIN_RST, active_high=True, initial_value=False)
    _dc_pin = DigitalOutputDevice(PIN_DC, active_high=True, initial_value=False)
    _bl_pin = DigitalOutputDevice(PIN_BL, active_high=True, initial_value=True)  # backlight ON

    _spi = spidev.SpiDev()
    _spi.open(SPI_PORT, SPI_CS)
    _spi.max_speed_hz = SPI_SPEED_HZ
    _spi.mode = 0b00

    _rst_pin.off()
    time.sleep(0.1)
    _rst_pin.on()
    time.sleep(0.1)

    _send_command(0x11)  # Sleep Out
    time.sleep(0.12)

    _send_command(0x36)  # Memory Data Access Control (display orientation)
    _send_data(0x00)     # standard vertical mode

    _send_command(0x3A)  # Interface Pixel Format
    _send_data(0x05)     # 16-bit color (RGB565)

    _send_command(0x29)  # Display ON


def push_frame(image):
    """Convert a PIL image to RGB565 and stream it to the panel over SPI."""
    img = image.convert("RGB").resize((240, 240))
    img_data = list(img.getdata())
    buf = bytearray(240 * 240 * 2)

    idx = 0
    for r, g, b in img_data:
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        buf[idx] = (rgb565 >> 8) & 0xFF
        buf[idx + 1] = rgb565 & 0xFF
        idx += 2

    _send_command(0x2A)  # Column Address Set
    _send_data(0x00); _send_data(0x00); _send_data(0x00); _send_data(239)
    _send_command(0x2B)  # Row Address Set
    _send_data(0x00); _send_data(0x00); _send_data(0x00); _send_data(239)
    _send_command(0x2C)  # RAM Write
    _send_data_buf(buf)


def backlight_off():
    if _bl_pin:
        _bl_pin.off()


def format_countdown(delta: datetime.timedelta) -> str:
    total_min = int(delta.total_seconds() // 60)
    if total_min < 0:
        return "now"
    h, m = divmod(total_min, 60)
    if h > 0:
        return f"in {h}h {m}m"
    return f"in {m}m"


def format_time_12h(t: datetime.datetime) -> str:
    """'11:24 AM' / '9:05 PM' -- no leading zero on the hour."""
    return t.strftime("%I:%M %p").lstrip("0")


def _wrap_lines(d, text, font, max_width, max_lines=2):
    """Greedy word-wrap into at most max_lines, ellipsizing whatever doesn't
    fit (including hard-truncating a single word wider than max_width)."""
    words = text.split()
    lines = []
    i = 0
    for _ in range(max_lines):
        if i >= len(words):
            break
        current = words[i]
        i += 1
        while i < len(words):
            candidate = f"{current} {words[i]}"
            if d.textlength(candidate, font=font) <= max_width:
                current = candidate
                i += 1
            else:
                break
        lines.append(current)

    truncated = i < len(words)
    if lines:
        while d.textlength(lines[-1], font=font) > max_width and len(lines[-1]) > 1:
            lines[-1] = lines[-1][:-1]
            truncated = True
        if truncated:
            last = lines[-1]
            while d.textlength(last + "…", font=font) > max_width and len(last) > 1:
                last = last[:-1].rstrip()
            lines[-1] = last.rstrip() + "…"
    return lines


def fit_meal_name(d, name, max_width):
    """Returns (lines, font, line_height). Tries one line at the big font
    first; if it doesn't fit, wraps to up to 2 lines at a smaller font
    instead of just chopping the name off."""
    if d.textlength(name, font=FONT_MEAL) <= max_width:
        return [name], FONT_MEAL, 42
    lines = _wrap_lines(d, name, FONT_MEAL_WRAP, max_width, max_lines=2)
    return lines, FONT_MEAL_WRAP, 30


def render_frame():
    theme_mode = db.get_theme_mode()
    c = THEMES[db.resolve_theme(theme_mode)]

    img = Image.new("RGB", (240, 240), c["bg"])
    d = ImageDraw.Draw(img)
    now = datetime.datetime.now()

    # -- combined time + weather bar --
    d.rectangle((0, 0, 240, 30), fill=c["topbar_bg"])
    time_str = format_time_12h(now)
    d.text((10, 6), time_str, font=FONT_TIME, fill=c["text"])
    time_end_x = 10 + d.textlength(time_str, font=FONT_TIME)

    weather = fetch_weather()
    weather_text = (f"{weather['label']} {weather['temp']}°C {weather['humidity']}%"
                     if weather else "Weather N/A")
    max_weather_w = 230 - (time_end_x + 10)  # never let it crowd the time
    truncated = False
    while d.textlength(weather_text, font=FONT_WEATHER) > max_weather_w and len(weather_text) > 1:
        weather_text = weather_text[:-1]
        truncated = True
    if truncated:
        weather_text = weather_text.rstrip() + "…"
    w = d.textlength(weather_text, font=FONT_WEATHER)
    d.text((230 - w, 8), weather_text, font=FONT_WEATHER, fill=c["topbar_text"])

    d.line((12, 42, 228, 42), fill=c["divider"], width=1)

    occ = db.get_next_meal(now)

    if occ is None:
        d.text((12, 66), "No meals scheduled", font=FONT_SMALL, fill=c["muted"])
        divider_y, summary_y = 182, 202
    else:
        meal, when = occ["meal"], occ["when"]
        going_out = bool(meal["going_out"])
        color = c["going_out"] if going_out else c["categories"].get(meal["category"], c["categories"]["other"])

        y = 52
        tag = "EATING OUT" if going_out else meal["category"].upper()
        d.text((12, y), f"NEXT UP:  {tag}", font=FONT_LABEL, fill=color)
        y += 24

        lines, meal_font, line_h = fit_meal_name(d, meal["name"], 216)
        for line in lines:
            d.text((12, y), line, font=meal_font, fill=c["text"])
            y += line_h
        y += 8

        countdown = format_countdown(when - now)
        label = "there by" if going_out else "ready by"
        d.text((12, y), f"{label} {meal['scheduled_time']} ({countdown})",
               font=FONT_TINY, fill=c["detail"])
        y += 18

        if going_out and meal["going_out_place"]:
            place = meal["going_out_place"]
            if len(place) > 30:
                place = place[:29] + "…"
            d.text((12, y), f"@ {place}", font=FONT_TINY, fill=c["amber"])
            y += 16
        if meal["prep_minutes"]:
            start_by = format_time_12h(when - datetime.timedelta(minutes=meal["prep_minutes"]))
            prep_label = "leave by" if going_out else "start prep by"
            d.text((12, y), f"{prep_label} {start_by}", font=FONT_TINY, fill=c["amber"])
            y += 16
        if meal["notes"]:
            note = meal["notes"]
            if len(note) > 34:
                note = note[:33] + "…"
            d.text((12, y), note, font=FONT_TINY, fill=c["muted"])
            y += 16

        divider_y, summary_y = y + 8, y + 24

    d.line((12, divider_y, 228, divider_y), fill=c["divider"], width=1)
    today = db.get_today_meals(now)
    remaining = [t for t in today if not t["done"]]
    summary = f"{len(remaining)} meal(s) left today" if remaining else "All done for today"
    d.text((12, summary_y), summary, font=FONT_TINY, fill=c["dim"])

    return img


def main():
    init_display()
    try:
        while True:
            try:
                frame = render_frame()
                push_frame(frame)
            except Exception as exc:  # keep the daemon alive across transient errors
                print(f"[display] render error: {exc}")
            time.sleep(REFRESH_SECONDS)
    except KeyboardInterrupt:
        print("\n[display] interrupted, turning off backlight")
        backlight_off()


if __name__ == "__main__":
    main()
