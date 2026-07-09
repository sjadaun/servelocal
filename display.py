"""
display.py
Standalone daemon that reads the meal-planner DB directly (no HTTP hop, so it
stays fast and light on a Pi Zero 2W) and redraws the 240x240 SPI screen
every REFRESH_SECONDS.

Assumes an ST7789-driven 240x240 SPI IPS panel, using the lightweight
`st7789` python library (pip install st7789). This is the same chip used on
most generic 1.3" 240x240 SPI boards sold as "Waveshare-compatible".

If your panel uses a different driver (e.g. GC9A01, ST7735), only the
`init_display()` function below needs to change -- everything else
(the drawing code) stays the same since it all just draws onto a PIL Image.

--- Wiring (default pins below, standard for Pi Zero 2W 40-pin header) ---
  VCC  -> 3V3
  GND  -> GND
  DIN  -> GPIO10 (MOSI)
  CLK  -> GPIO11 (SCLK)
  CS   -> GPIO8  (CE0)
  DC   -> GPIO25
  RST  -> GPIO27
  BL   -> GPIO24 (backlight, optional)
Adjust PIN_* constants below to match your actual board's silkscreen if
different.
"""

import time
import datetime
from PIL import Image, ImageDraw, ImageFont

import database as db

REFRESH_SECONDS = 20

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


FONT_TIME = load_font("DejaVuSans-Bold.ttf", 30)
FONT_LABEL = load_font("DejaVuSans-Bold.ttf", 16)
FONT_MEAL = load_font("DejaVuSans-Bold.ttf", 24)
FONT_SMALL = load_font("DejaVuSans.ttf", 15)
FONT_TINY = load_font("DejaVuSans.ttf", 13)

CATEGORY_COLORS = {
    "breakfast": (240, 170, 60),
    "lunch": (90, 170, 90),
    "dinner": (90, 110, 200),
    "school": (200, 90, 140),
    "snack": (180, 140, 90),
    "other": (140, 140, 140),
}


def init_display():
    import ST7789 as st7789
    disp = st7789.ST7789(
        port=SPI_PORT,
        cs=SPI_CS,
        dc=PIN_DC,
        rst=PIN_RST,
        backlight=PIN_BL,
        width=240,
        height=240,
        rotation=0,
        spi_speed_hz=SPI_SPEED_HZ,
    )
    disp.begin()
    return disp


def format_countdown(delta: datetime.timedelta) -> str:
    total_min = int(delta.total_seconds() // 60)
    if total_min < 0:
        return "now"
    h, m = divmod(total_min, 60)
    if h > 0:
        return f"in {h}h {m}m"
    return f"in {m}m"


def render_frame():
    img = Image.new("RGB", (240, 240), (18, 18, 22))
    d = ImageDraw.Draw(img)

    now = datetime.datetime.now()

    # -- header: current time --
    d.text((12, 10), now.strftime("%H:%M"), font=FONT_TIME, fill=(255, 255, 255))
    d.text((12, 46), now.strftime("%a, %d %b"), font=FONT_TINY, fill=(150, 150, 150))
    d.line((12, 68, 228, 68), fill=(50, 50, 55), width=1)

    nxt = db.get_next_meal(now)

    if nxt is None:
        d.text((12, 100), "No meals scheduled", font=FONT_SMALL, fill=(180, 180, 180))
    else:
        slot, when = nxt["slot"], nxt["when"]
        color = CATEGORY_COLORS.get(slot["category"], (140, 140, 140))

        d.text((12, 78), "NEXT UP", font=FONT_LABEL, fill=color)

        cat = slot["category"].upper()
        d.text((12, 100), cat, font=FONT_TINY, fill=color)

        meal_name = slot["name"]
        if len(meal_name) > 18:
            meal_name = meal_name[:17] + "…"
        d.text((12, 116), meal_name, font=FONT_MEAL, fill=(255, 255, 255))

        countdown = format_countdown(when - now)
        d.text((12, 148), f"ready by {slot['scheduled_time']} ({countdown})",
               font=FONT_TINY, fill=(200, 200, 200))

        y = 168
        if slot["prep_minutes"]:
            start_by = (when - datetime.timedelta(minutes=slot["prep_minutes"])).strftime("%H:%M")
            d.text((12, y), f"start prep by {start_by}", font=FONT_TINY, fill=(230, 180, 90))
            y += 18
        if slot["temperature"]:
            d.text((12, y), f"temp: {slot['temperature']}", font=FONT_TINY, fill=(230, 180, 90))
            y += 18
        if slot["notes"]:
            note = slot["notes"]
            if len(note) > 34:
                note = note[:33] + "…"
            d.text((12, y), note, font=FONT_TINY, fill=(160, 160, 160))

    d.line((12, 210, 228, 210), fill=(50, 50, 55), width=1)
    today = db.get_today_meals(now)
    remaining = [t for t in today if not t["done"]]
    summary = f"{len(remaining)} meal(s) left today" if remaining else "All done for today"
    d.text((12, 216), summary, font=FONT_TINY, fill=(120, 120, 120))

    return img


def main():
    disp = init_display()
    while True:
        try:
            frame = render_frame()
            disp.display(frame)
        except Exception as exc:  # keep the daemon alive across transient errors
            print(f"[display] render error: {exc}")
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
