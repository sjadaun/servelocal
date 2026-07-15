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
import math
import database as db

try:
    import requests
except ImportError:
    requests = None

REFRESH_SECONDS = 1
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

# Many ST7789 panels render colors inverted (near-black shows as near-white
# and vice versa) unless the Display Inversion ON command is sent during
# init. This is extremely common and easy to miss if your test colors
# happen to still look plausible either way. If, after this change, colors
# still look wrong (or were fine before and are now wrong), flip this to
# False -- it's a single toggle, not a deeper code issue.
PANEL_INVERT_COLORS = True

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


def load_font(name, size):
    try:
        return ImageFont.truetype(FONT_DIR + name, size)
    except OSError:
        return ImageFont.load_default()


FONT_WEATHER = load_font("DejaVuSans-Bold.ttf", 13)
FONT_TIME = load_font("DejaVuSans-Bold.ttf", 16)
FONT_SMALL = load_font("DejaVuSans.ttf", 14)

# Two size presets for the "next meal" block. ROOMY is tried first so short
# content (the common case) fills the screen instead of leaving the bottom
# empty; COMPACT is the fallback that's guaranteed to fit even the longest
# realistic combination (2-line name + going-out place + prep + notes).
FONT_LABEL_COMPACT = load_font("DejaVuSans-Bold.ttf", 16)
FONT_LABEL_ROOMY = load_font("DejaVuSans-Bold.ttf", 19)
FONT_MEAL_COMPACT = load_font("DejaVuSans-Bold.ttf", 36)        # 1 line
FONT_MEAL_WRAP_COMPACT = load_font("DejaVuSans-Bold.ttf", 24)   # 2 lines
FONT_MEAL_ROOMY = load_font("DejaVuSans-Bold.ttf", 40)          # 1 line
FONT_MEAL_SPLASH = load_font("DejaVuSans-Bold.ttf", 30)          # 1 line
FONT_MEAL_WRAP_ROOMY = load_font("DejaVuSans-Bold.ttf", 27)     # 2 lines
FONT_TINY_COMPACT = load_font("DejaVuSans.ttf", 13)
FONT_TINY_ROOMY = load_font("DejaVuSans.ttf", 16)

# kept for any external reference; render_frame() now picks per-preset fonts
FONT_LABEL = FONT_LABEL_COMPACT
FONT_MEAL = FONT_MEAL_COMPACT
FONT_MEAL_WRAP = FONT_MEAL_WRAP_COMPACT
FONT_TINY = FONT_TINY_COMPACT

# The "next meal" block now renders between two fixed bars (top bar with
# time/wifi, bottom bar with meals-left/weather) rather than floating a
# divider+summary based on content height. See CONTENT_TOP/CONTENT_BOTTOM
# and the fit-check in render_frame().
PRESET_COMPACT = {
    "label_font": FONT_LABEL_COMPACT, "tiny_font": FONT_TINY_COMPACT,
    "meal_font": FONT_MEAL_COMPACT, "meal_line_h": 42,
    "meal_wrap_font": FONT_MEAL_WRAP_COMPACT, "meal_wrap_line_h": 30,
    "next_up_gap": 20, "meal_gap": 6, "ready_gap": 16, "detail_gap": 14,
}
PRESET_ROOMY = {
    "label_font": FONT_LABEL_ROOMY, "tiny_font": FONT_TINY_ROOMY,
    "meal_font": FONT_MEAL_ROOMY, "meal_line_h": 46,
    "meal_wrap_font": FONT_MEAL_WRAP_ROOMY, "meal_wrap_line_h": 33,
    "next_up_gap": 28, "meal_gap": 12, "ready_gap": 22, "detail_gap": 20,
}
FONT_LABEL_MICRO = load_font("DejaVuSans-Bold.ttf", 13)
FONT_MEAL_MICRO = load_font("DejaVuSans-Bold.ttf", 28)         # 1 line
FONT_MEAL_WRAP_MICRO = load_font("DejaVuSans-Bold.ttf", 19)    # 2 lines
FONT_TINY_MICRO = load_font("DejaVuSans.ttf", 11)

PRESET_MICRO = {
    "label_font": FONT_LABEL_MICRO, "tiny_font": FONT_TINY_MICRO,
    "meal_font": FONT_MEAL_MICRO, "meal_line_h": 32,
    "meal_wrap_font": FONT_MEAL_WRAP_MICRO, "meal_wrap_line_h": 22,
    "next_up_gap": 16, "meal_gap": 4, "ready_gap": 13, "detail_gap": 11,
}
FONT_BRAND = load_font("DejaVuSans.ttf", 32)

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
        "accent": "#4CAF50",
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
        "accent": "#2E7D32",
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


WIFI_INTERFACE = "wlan0"


def get_wifi_status(interface=WIFI_INTERFACE):
    """Reads signal strength straight from /proc/net/wireless -- no
    subprocess, no extra dependency, effectively free to call every frame.
    Returns {'connected': bool, 'dbm': int|None, 'bars': 0-4}."""
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if not line.strip().startswith(interface):
                    continue
                fields = line.split(":", 1)[1].split()
                # fields: status, link_quality, signal_level(dBm), noise_level, ...
                dbm = float(fields[2])
                if dbm > 0:  # some drivers report as unsigned; normalize
                    dbm -= 256
                dbm = int(dbm)
                if dbm >= -50:
                    bars = 4
                elif dbm >= -60:
                    bars = 3
                elif dbm >= -70:
                    bars = 2
                elif dbm >= -80:
                    bars = 1
                else:
                    bars = 0
                return {"connected": True, "dbm": dbm, "bars": bars}
    except Exception as exc:
        print(f"[display] wifi status read failed: {exc}")
    return {"connected": False, "dbm": None, "bars": 0}


def draw_wifi_bars(d, right_x, cy, wifi, active_color, inactive_color):
    """Draws 4 signal bars (phone-style, increasing height left to right),
    right-aligned so right_x is the rightmost edge. Returns the left edge x
    so callers can position other text relative to it."""
    bar_w, gap, max_h = 4, 2, 16
    n_bars = 4
    total_w = n_bars * bar_w + (n_bars - 1) * gap
    left_x = right_x - total_w

    for i in range(n_bars):
        bar_h = max_h * (i + 1) // n_bars
        x0 = left_x + i * (bar_w + gap)
        x1 = x0 + bar_w
        y1 = cy + max_h // 2
        y0 = y1 - bar_h
        color = active_color if (wifi["connected"] and i < wifi["bars"]) else inactive_color
        d.rectangle((x0, y0, x1, y1), fill=color)

    if not wifi["connected"]:
        # small slash through the bars to make "disconnected" unambiguous
        d.line((left_x - 1, cy + max_h // 2 + 2, right_x + 1, cy - max_h // 2 - 2),
               fill=inactive_color, width=2)

    return left_x


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

    if PANEL_INVERT_COLORS:
        _send_command(0x21)  # Display Inversion ON

    _send_command(0x36)  # Memory Data Access Control (display orientation)
    _send_data(0x00)     # standard vertical mode

    _send_command(0x3A)  # Interface Pixel Format
    _send_data(0x05)     # 16-bit color (RGB565)

    _send_command(0x29)  # Display ON


def push_frame(image):
    """Convert a PIL image to RGB565 and stream it to the panel over SPI."""
    img = image.convert("RGB").resize((240, 240))
    if hasattr(img, "get_flattened_data"):  # newer Pillow; getdata() is deprecated
        img_data = img.get_flattened_data()
    else:
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


def draw_clock_icon(d, cx, cy, r, color):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=1)
    d.line((cx, cy, cx, cy - r * 0.6), fill=color, width=1)
    d.line((cx, cy, cx + r * 0.5, cy + r * 0.1), fill=color, width=1)


def draw_pot_icon(d, cx, cy, w, h, color):
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    d.rounded_rectangle((x0, y0, x1, y1), radius=2, outline=color, width=1)
    d.line((x0 - 3, y0 + 2, x0, y0 + 2), fill=color, width=1)
    d.line((x1, y0 + 2, x1 + 3, y0 + 2), fill=color, width=1)


def draw_cloud_sun_icon(d, cx, cy, r, sun_color, cloud_color):
    d.ellipse((cx - r * 0.3, cy - r * 1.1, cx + r * 1.3, cy + r * 0.3), fill=sun_color)
    d.ellipse((cx - r * 1.1, cy - r * 0.1, cx + r * 0.3, cy + r * 1.0), fill=cloud_color)
    d.ellipse((cx - r * 0.4, cy - r * 0.5, cx + r * 1.1, cy + r * 1.0), fill=cloud_color)


def draw_drop_icon(d, cx, cy, r, color):
    d.polygon([(cx, cy - r), (cx - r * 0.8, cy + r * 0.3), (cx + r * 0.8, cy + r * 0.3)], fill=color)
    d.ellipse((cx - r * 0.8, cy - r * 0.1, cx + r * 0.8, cy + r * 1.5), fill=color)


def draw_calendar_icon(d, cx, cy, w, h, color):
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    d.rounded_rectangle((x0, y0, x1, y1), radius=1, outline=color, width=1)
    d.line((x0, y0 + h * 0.32, x1, y0 + h * 0.32), fill=color, width=1)


def draw_chevron(d, cx, cy, size, color):
    d.line((cx - size * 0.3, cy - size * 0.5, cx + size * 0.3, cy), fill=color, width=2)
    d.line((cx + size * 0.3, cy, cx - size * 0.3, cy + size * 0.5), fill=color, width=2)


def draw_fork_knife_badge(d, cx, cy, r, badge_color, icon_color):
    """Small badge version of the fork/knife mark for inline use (the big
    ornate draw_icon() further down is sized for the splash screen)."""
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=badge_color)
    fx = cx - r * 0.35
    d.line((fx, cy - r * 0.5, fx, cy + r * 0.5), fill=icon_color, width=1)
    for dx in (-2, 0, 2):
        d.line((fx + dx, cy - r * 0.5, fx + dx, cy - r * 0.1), fill=icon_color, width=1)
    kx = cx + r * 0.35
    d.line((kx, cy - r * 0.5, kx, cy + r * 0.5), fill=icon_color, width=1)
    d.polygon([(kx, cy - r * 0.5), (kx + 3, cy - r * 0.1), (kx, cy)], fill=icon_color)


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


def fit_text(d, text, font, max_width):
    """Dynamic width-fit with an ellipsis, using the font's actual
    rendered width rather than a guessed character count. A fixed char
    count is only ever correct for ONE font size -- this codebase has hit
    that exact bug more than once (weather row, going-out place/notes)
    since font size varies by preset (ROOMY/COMPACT/MICRO)."""
    if d.textlength(text, font=font) <= max_width:
        return text
    while text and d.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


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


def fit_meal_name(d, name, max_width, preset):
    """Returns (lines, font, line_height) for the given size preset. Tries
    one line at the preset's big font first; if it doesn't fit, wraps to up
    to 2 lines at the preset's smaller font instead of just chopping it off."""
    if d.textlength(name, font=preset["meal_font"]) <= max_width:
        return [name], preset["meal_font"], preset["meal_line_h"]
    lines = _wrap_lines(d, name, preset["meal_wrap_font"], max_width, max_lines=2)
    return lines, preset["meal_wrap_font"], preset["meal_wrap_line_h"]


def _meal_block_height(d, meal, going_out, preset):
    """Dry-run the vertical space the 'next meal' block would need under
    this preset, without drawing anything -- used to decide whether ROOMY
    fits or COMPACT is needed instead."""
    lines, font, line_h = fit_meal_name(d, meal["name"], 216, preset)
    h = preset["next_up_gap"]
    h += line_h * len(lines) + preset["meal_gap"]
    h += preset["ready_gap"]
    if going_out and meal["going_out_place"]:
        h += preset["detail_gap"]
    if meal["prep_minutes"]:
        h += preset["detail_gap"]
    if meal["notes"]:
        h += preset["detail_gap"]
    return h, lines, font, line_h


TOP_BAR_H = 30
MEALS_ROW_H = 30
WEATHER_ROW_H = 52
CONTENT_TOP = 38
CONTENT_BOTTOM = 240 - WEATHER_ROW_H - MEALS_ROW_H - 8


def render_frame():
    theme_mode = db.get_theme_mode()
    c = THEMES[db.resolve_theme(theme_mode)]

    img = Image.new("RGB", (240, 240), c["bg"])
    d = ImageDraw.Draw(img)
    now = datetime.datetime.now()

    # -- top bar: time (left) + wifi signal (right) --
    # (No battery/flashlight icons -- this hardware has neither a battery
    # nor a flashlight to report on; showing them would just be fake data.)
    d.rectangle((0, 0, 240, TOP_BAR_H), fill=c["topbar_bg"])
    time_str = format_time_12h(now)
    d.text((10, 6), time_str, font=FONT_TIME, fill=c["text"])

    wifi = get_wifi_status()
    draw_wifi_bars(d, 228, TOP_BAR_H // 2, wifi, c["topbar_text"], c["divider"])

    d.line((0, TOP_BAR_H, 240, TOP_BAR_H), fill=c["divider"], width=1)

    # -- next meal content --
    occ = db.get_next_meal(now)

    if occ is None:
        d.text((12, 90), "No meals scheduled", font=FONT_MEAL_WRAP_ROOMY, fill=c["muted"])
    else:
        meal, when = occ["meal"], occ["when"]
        going_out = bool(meal["going_out"])
        color = c["going_out"] if going_out else c["categories"].get(meal["category"], c["categories"]["other"])

        tag = "EATING OUT" if going_out else meal["category"].upper()

        def _tag_fits(preset):
            w = d.textlength(f"NEXT UP:  {tag}", font=preset["label_font"])
            return w <= 228 - 12

        # Try ROOMY first so short/typical content fills the screen with
        # bigger text instead of leaving space unused. Fall back to COMPACT,
        # and as a last resort MICRO, if this specific meal's content is
        # tall enough that a roomier size would run into the row below, OR
        # if "NEXT UP: EATING OUT" (the longest possible tag) wouldn't fit
        # on one line at that size.
        content_h, lines, meal_font, line_h = _meal_block_height(d, meal, going_out, PRESET_ROOMY)
        if CONTENT_TOP + content_h <= CONTENT_BOTTOM and _tag_fits(PRESET_ROOMY):
            preset = PRESET_ROOMY
        else:
            content_h, lines, meal_font, line_h = _meal_block_height(d, meal, going_out, PRESET_COMPACT)
            if CONTENT_TOP + content_h <= CONTENT_BOTTOM and _tag_fits(PRESET_COMPACT):
                preset = PRESET_COMPACT
            else:
                preset = PRESET_MICRO
                _, lines, meal_font, line_h = _meal_block_height(d, meal, going_out, preset)

        icon_r = 8 if preset is PRESET_ROOMY else (7 if preset is PRESET_COMPACT else 5)
        text_x = 32  # leaves room for the icon column at x=12..28

        y = CONTENT_TOP
        next_up_text = fit_text(d, f"NEXT UP:  {tag}", preset["label_font"], 228 - 12)
        d.text((12, y), next_up_text, font=preset["label_font"], fill=color)
        y += preset["next_up_gap"]

        for line in lines:
            d.text((12, y), line, font=meal_font, fill=c["text"])
            y += line_h
        y += preset["meal_gap"]

        countdown = format_countdown(when - now)
        label = "there by" if going_out else "ready by"
        ready_text = fit_text(d, f"{label} {meal['scheduled_time']} ({countdown})", preset["tiny_font"], 228 - text_x)
        draw_clock_icon(d, 18, y + icon_r, icon_r, c["detail"])
        d.text((text_x, y), ready_text, font=preset["tiny_font"], fill=c["detail"])
        y += preset["ready_gap"]

        if going_out and meal["going_out_place"]:
            prefix_w = d.textlength("@ ", font=preset["tiny_font"])
            place = fit_text(d, meal["going_out_place"], preset["tiny_font"], 228 - text_x - prefix_w)
            d.text((text_x, y), f"@ {place}", font=preset["tiny_font"], fill=c["amber"])
            y += preset["detail_gap"]
        if meal["prep_minutes"]:
            start_by = format_time_12h(when - datetime.timedelta(minutes=meal["prep_minutes"]))
            prep_label = "leave by" if going_out else "start prep by"
            prep_text = fit_text(d, f"{prep_label} {start_by}", preset["tiny_font"], 228 - text_x)
            draw_pot_icon(d, 18, y + icon_r - 1, 11, 8, c["amber"])
            d.text((text_x, y), prep_text, font=preset["tiny_font"], fill=c["amber"])
            y += preset["detail_gap"]
        if meal["notes"]:
            note = fit_text(d, meal["notes"], preset["tiny_font"], 228 - text_x)
            d.text((text_x, y), note, font=preset["tiny_font"], fill=c["muted"])
            y += preset["detail_gap"]

    # -- meals-left row: fork/knife badge + text + chevron --
    meals_row_y = CONTENT_BOTTOM + 8
    d.line((0, meals_row_y, 240, meals_row_y), fill=c["divider"], width=1)
    row_cy = meals_row_y + MEALS_ROW_H // 2

    today = db.get_today_meals(now)
    remaining = [t for t in today if not t["done"]]
    summary = f"{len(remaining)} meal(s) left today" if remaining else "All done for today"

    draw_fork_knife_badge(d, 24, row_cy, 11, c["going_out"], c["bg"])
    d.text((42, row_cy - 7), summary, font=FONT_WEATHER, fill=c["text"])
    draw_chevron(d, 226, row_cy, 12, c["dim"])

    # -- bottom row: weather | humidity | date, 3 segments --
    weather_row_y = meals_row_y + MEALS_ROW_H
    d.line((0, weather_row_y, 240, weather_row_y), fill=c["divider"], width=1)
    d.rectangle((0, weather_row_y, 240, 240), fill=c["topbar_bg"])

    weather = fetch_weather()
    # Unequal segments: weather (icon+temp+condition) needs the most room,
    # humidity (just "53%") needs the least. Equal thirds left long-but-
    # common condition labels like "P.Cloudy" truncated to "P.Clo…" even in
    # completely normal weather -- this matches the actual mockup's
    # proportions better too, where the weather segment reads visibly wider.
    seg1_w, seg2_w, seg3_w = 100, 62, 78
    seg1_end, seg2_end = seg1_w, seg1_w + seg2_w
    icon_cy = weather_row_y + 18
    val_y = weather_row_y + 8
    label_y = weather_row_y + 24

    # segment 1: condition + temperature (x: 0..100, text starts at 32)
    draw_cloud_sun_icon(d, 18, icon_cy, 6, (232, 201, 74), c["topbar_text"])
    temp_str = fit_text(d, f"{weather['temp']}°C" if weather else "--", FONT_WEATHER, seg1_end - 4 - 32)
    cond_str = fit_text(d, weather["label"] if weather else "N/A", FONT_TINY_COMPACT, seg1_end - 4 - 32)
    d.text((32, val_y), temp_str, font=FONT_WEATHER, fill=c["text"])
    d.text((32, label_y), cond_str, font=FONT_TINY_COMPACT, fill=c["topbar_text"])
    d.line((seg1_end, weather_row_y + 6, seg1_end, 240 - 6), fill=c["divider"], width=1)

    # segment 2: humidity (x: 100..162, text starts at seg1_end+20)
    draw_drop_icon(d, seg1_end + 12, icon_cy, 6, (90, 168, 216))
    hum_text_x = seg1_end + 22
    hum_max_w = seg2_end - 4 - hum_text_x
    hum_str = fit_text(d, f"{weather['humidity']}%" if weather else "--", FONT_WEATHER, hum_max_w)
    d.text((hum_text_x, val_y), hum_str, font=FONT_WEATHER, fill=c["text"])
    d.text((hum_text_x, label_y), fit_text(d, "Hum", FONT_TINY_COMPACT, hum_max_w), font=FONT_TINY_COMPACT, fill=c["topbar_text"])
    d.line((seg2_end, weather_row_y + 6, seg2_end, 240 - 6), fill=c["divider"], width=1)

    # segment 3: date (x: 162..240, text starts at seg2_end+22)
    draw_calendar_icon(d, seg2_end + 12, icon_cy, 12, 11, c["topbar_text"])
    date_text_x = seg2_end + 22
    date_max_w = 240 - 6 - date_text_x
    d.text((date_text_x, val_y), fit_text(d, now.strftime("%a"), FONT_WEATHER, date_max_w), font=FONT_WEATHER, fill=c["text"])
    d.text((date_text_x, label_y), fit_text(d, now.strftime("%-d %b"), FONT_TINY_COMPACT, date_max_w), font=FONT_TINY_COMPACT, fill=c["topbar_text"])

    return img

def draw_icon(draw, cx, cy, fork_x_offset=0, knife_x_offset=0, scale=1.0, 
              plate_color=(255, 255, 255), fork_color=(255, 255, 255), knife_color=(255, 255, 255)):
    """Draws a giant, multi-colored ServeLocal logo using primitive geometric shapes."""
    plate_radius = int(32 * scale)
    fork_h = int(60 * scale)
    knife_h = int(60 * scale)

    # 1. Draw Plate
    draw.ellipse(
        [cx - plate_radius, cy - plate_radius, cx + plate_radius, cy + plate_radius],
        fill=plate_color,
    )

    # 2. Draw Fork
    fx = cx - int(48 * scale) + fork_x_offset
    fy_top = cy - (fork_h // 2)
    fy_bot = cy + (fork_h // 2)
    draw.line([(fx, cy), (fx, fy_bot)], fill=fork_color, width=max(1, int(4 * scale)))
    draw.line([(fx - int(9 * scale), cy), (fx + int(9 * scale), cy)], fill=fork_color, width=max(1, int(4 * scale)))
    for prong_offset in [-8, 0, 8]:
        px = fx + int(prong_offset * scale)
        draw.line([(px, cy), (px, fy_top)], fill=fork_color, width=max(1, int(3 * scale)))

    # 3. Draw Knife
    kx = cx + int(48 * scale) + knife_x_offset
    ky_top = cy - (knife_h // 2)
    ky_bot = cy + (knife_h // 2)
    draw.line([(kx, ky_top), (kx, ky_bot)], fill=knife_color, width=max(1, int(4 * scale)))
    draw.polygon([(kx, ky_top), (kx + int(9 * scale), ky_top + int(14 * scale)), (kx, cy)], fill=knife_color)


def draw_glowing_border(draw, width, height, current_step, total_steps, base_color):
    """Draws a multi-layered, pulsating neon-glow border using sine-wave color shifts."""
    cycle_factor = (math.sin((current_step / total_steps) * math.pi * 2) + 1) / 2
    
    br, bg, bb = base_color
    
    # Layer 1: Inner Dim Glow (3 pixels deep)
    glow_r1 = int(br * (0.4 + cycle_factor * 0.3))
    glow_g1 = int(bg * (0.4 + cycle_factor * 0.3))
    glow_b1 = int(bb * (0.4 + cycle_factor * 0.3))
    draw.rectangle([(2, 2), (width - 3, height - 3)], outline=(glow_r1, glow_g1, glow_b1), width=1)

    # Layer 2: Main Vivid Light Frame (2 pixels deep)
    glow_r2 = int(br * (0.7 + cycle_factor * 0.3))
    glow_g2 = int(bg * (0.7 + cycle_factor * 0.3))
    glow_b2 = int(bb * (0.7 + cycle_factor * 0.3))
    draw.rectangle([(1, 1), (width - 2, height - 2)], outline=(glow_r2, glow_g2, glow_b2), width=1)
    
    # Layer 3: Ultra-Bright White/Neon Core Edge (1 pixel thick)
    core_r = min(255, int(br + (255 - br) * 0.4 * cycle_factor))
    core_g = min(255, int(bg + (255 - bg) * 0.4 * cycle_factor))
    core_b = min(255, int(bb + (255 - bb) * 0.4 * cycle_factor))
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=(core_r, core_g, core_b), width=1)


def run_splash_screen():
    """Renders the splash animation surrounded by an active, pulsing neon border frame."""
    print("[Display] Running massive splash intro with glowing border frame...")

    # Extract theme configurations
    bg_color = THEMES["dark"]["bg"]
    fork_color = THEMES["dark"]["categories"]["breakfast"]  # Amber tuple
    knife_color = THEMES["dark"]["categories"]["dinner"]    # Indigo Blue tuple
    plate_color = (255, 255, 255)
    
    glow_base_color = THEMES["dark"]["categories"]["breakfast"]

    # Canvas Setup
    w, h = 240, 240
    center_x, center_y = w // 2, h // 2 - 35
    global_step = 0  

    # --- PHASE 1: Slide In Utensils (30 Frames) ---
    steps_phase1 = 30
    for i in range(steps_phase1):
        img = Image.new("RGB", (w, h), color=bg_color)
        draw = ImageDraw.Draw(img)

        progress = i / (steps_phase1 - 1)
        easing = 1 - math.pow(1 - progress, 3)

        fork_offset = int(-140 * (1 - easing))
        knife_offset = int(140 * (1 - easing))

        draw_icon(
            draw, center_x, center_y,
            fork_x_offset=fork_offset, knife_x_offset=knife_offset,
            scale=1.0, plate_color=(0, 0, 0), fork_color=fork_color, knife_color=knife_color
        )

        draw_glowing_border(draw, w, h, global_step, 60, glow_base_color)
        global_step += 1

        push_frame(img)
        time.sleep(0.015)

    # --- PHASE 2: Plate Elastic Expansion (15 Frames) ---
    steps_phase2 = 15
    for i in range(steps_phase2):
        img = Image.new("RGB", (w, h), color=bg_color)
        draw = ImageDraw.Draw(img)

        progress = i / (steps_phase2 - 1)
        scale_factor = math.sin(progress * math.pi / 2)

        pr, pg, pb = plate_color
        current_plate_color = (int(pr * scale_factor), int(pg * scale_factor), int(pb * scale_factor))

        draw_icon(
            draw, center_x, center_y,
            fork_x_offset=0, knife_x_offset=0,
            scale=1.0, 
            plate_color=current_plate_color,
            fork_color=fork_color, knife_color=knife_color
        )

        draw_glowing_border(draw, w, h, global_step, 60, glow_base_color)
        global_step += 1

        push_frame(img)
        time.sleep(0.015)

    # --- PHASE 3: Text Fade & Persistent Glowing Loop (50 Frames) ---
    steps_phase3 = 50  
    brand_text = "ServeLocal"

    # FIXED: Correctly index the bounding box tuple coordinates (right - left) to get string width
    bbox_b = FONT_BRAND.getbbox(brand_text)
    text_width = bbox_b[2] - bbox_b[0]
    bx = (w - text_width) // 2
    by = center_y + 60  

    for i in range(steps_phase3):
        img = Image.new("RGB", (w, h), color=bg_color)
        draw = ImageDraw.Draw(img)

        text_progress = min(1.0, i / 25)
        alpha = int(255 * text_progress)

        draw_icon(
            draw, center_x, center_y,
            fork_x_offset=0, knife_x_offset=0,
            scale=1.0, plate_color=plate_color, fork_color=fork_color, knife_color=knife_color
        )

        draw.text((bx, by), brand_text, font=FONT_BRAND, fill=(alpha, alpha, alpha))

        draw_glowing_border(draw, w, h, global_step, 60, glow_base_color)
        global_step += 1

        push_frame(img)
        time.sleep(0.02)

    print("[Display] Colorful splash sequence with neon border complete.")

def main():
    init_display()
    run_splash_screen()

    # display.py can now start before servelocal_planner.service (it starts
    # as early as boot allows, to show this splash ASAP) -- init_db() is
    # idempotent (CREATE TABLE IF NOT EXISTS), so it's safe to call from
    # both services regardless of which one wins the race.
    try:
        db.init_db()
    except Exception as exc:
        print(f"[display] db init error: {exc}")
        run_splash_screen()

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
