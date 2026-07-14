from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "equatorial_network_decomposition.png"

W, H = 2580, 960
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)

FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


F_TITLE = font(38, True)
F_SUBTITLE = font(22)
F_PANEL = font(25, True)
F_H = font(20, True)
F = font(18)
F_SMALL = font(15)
F_TINY = font(13)
F_TAG = font(14, True)
F_LEGEND = font(18)

INK = (29, 34, 40)
MUTED = (93, 103, 113)
BORDER = (200, 207, 214)
LIGHT = (245, 247, 249)
BLUE = (47, 123, 201)
BLUE_LIGHT = (221, 238, 252)
CORAL = (220, 92, 94)
CORAL_LIGHT = (255, 219, 218)
GREEN = (42, 145, 86)
GREEN_LIGHT = (213, 241, 225)
GOLD = (225, 172, 69)
BROWN = (145, 101, 59)
ORANGE = (231, 126, 43)
ORANGE_LIGHT = (255, 234, 211)
RED = (218, 79, 83)
RED_LIGHT = (255, 224, 225)
GRAY = (164, 173, 181)
GRAY_LIGHT = (238, 241, 244)
PURPLE = (130, 97, 180)
PURPLE_LIGHT = (235, 224, 250)


def text_center(box, text, fnt, fill=INK, spacing=4):
    x1, y1, x2, y2 = box
    bb = d.multiline_textbbox((0, 0), text, font=fnt, spacing=spacing, align="center")
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.multiline_text(((x1 + x2 - tw) / 2, (y1 + y2 - th) / 2), text, font=fnt, fill=fill, spacing=spacing, align="center")


def rounded(box, fill="white", outline=BORDER, width=2, radius=14):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def panel(x, y, w, h, number, title):
    rounded((x, y, x + w, y + h), width=3, radius=18)
    d.ellipse((x + 22, y + 19, x + 60, y + 57), fill=INK)
    text_center((x + 22, y + 19, x + 60, y + 57), str(number), font(19, True), fill="white")
    d.text((x + 74, y + 23), title, font=F_PANEL, fill=INK)


def tag(x, y, label, fill, outline, text_color=INK):
    bb = d.textbbox((0, 0), label, font=F_TAG)
    w = bb[2] - bb[0] + 24
    rounded((x, y, x + w, y + 28), fill=fill, outline=outline, radius=14)
    text_center((x, y, x + w, y + 28), label, F_TAG, text_color)
    return w


def arrow(a, b, color=BLUE, width=4, dashed=False, head=11):
    ax, ay = a
    bx, by = b
    angle = math.atan2(by - ay, bx - ax)
    end = (bx - head * math.cos(angle), by - head * math.sin(angle))
    if dashed:
        length = math.hypot(end[0] - ax, end[1] - ay)
        ux, uy = (end[0] - ax) / length, (end[1] - ay) / length
        pos = 0
        while pos < length:
            seg_end = min(pos + 11, length)
            d.line((ax + ux * pos, ay + uy * pos, ax + ux * seg_end, ay + uy * seg_end), fill=color, width=width)
            pos += 19
    else:
        d.line((ax, ay, *end), fill=color, width=width)
    d.polygon([
        (bx, by),
        (bx - head * math.cos(angle - 0.48), by - head * math.sin(angle - 0.48)),
        (bx - head * math.cos(angle + 0.48), by - head * math.sin(angle + 0.48)),
    ], fill=color)


def line(a, b, color=INK, width=3, dashed=False):
    if not dashed:
        d.line((*a, *b), fill=color, width=width)
        return
    ax, ay = a
    bx, by = b
    length = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / length, (by - ay) / length
    pos = 0
    while pos < length:
        seg_end = min(pos + 10, length)
        d.line((ax + ux * pos, ay + uy * pos, ax + ux * seg_end, ay + uy * seg_end), fill=color, width=width)
        pos += 17


def node(p, kind="road", r=13):
    fill, outline = {
        "road": ("white", INK),
        "origin": (CORAL_LIGHT, CORAL),
        "origin_a": (CORAL_LIGHT, CORAL),
        "origin_b": (PURPLE_LIGHT, PURPLE),
        "destination": (GREEN_LIGHT, GREEN),
    }[kind]
    x, y = p
    d.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline=outline, width=2)


def xmark(p, color=RED, size=9):
    x, y = p
    d.line((x - size, y - size, x + size, y + size), fill=color, width=4)
    d.line((x - size, y + size, x + size, y - size), fill=color, width=4)


def network(x, y, scale=1.0, affected=False, closed=False, route="baseline"):
    p = {
        "c1": (x + 15 * scale, y + 150 * scale),
        "c2": (x + 60 * scale, y + 35 * scale),
        "n1": (x + 145 * scale, y + 115 * scale),
        "n2": (x + 240 * scale, y + 60 * scale),
        "n3": (x + 275 * scale, y + 155 * scale),
        "city": (x + 365 * scale, y + 112 * scale),
        "port": (x + 370 * scale, y + 10 * scale),
        "airport": (x + 380 * scale, y + 200 * scale),
    }
    roads = [
        ("c1", "n1", "unpaved"), ("c2", "n1", "unpaved"),
        ("n1", "n2", "paved"), ("n1", "n3", "unpaved"),
        ("n2", "city", "paved"), ("n2", "port", "paved"),
        ("n3", "city", "paved"), ("n3", "airport", "paved"),
    ]
    slow_edge = tuple(sorted(("n1", "n3")))
    closed_edge = tuple(sorted(("n1", "n2")))
    for u, v, surface in roads:
        edge = tuple(sorted((u, v)))
        color = GOLD if surface == "paved" else BROWN
        width = max(2, round(4 * scale))
        if affected and edge == slow_edge:
            color, width = ORANGE, max(4, round(7 * scale))
        if closed and edge == closed_edge:
            color = GRAY
        line(p[u], p[v], color, width)
        if closed and edge == closed_edge:
            xmark(((p[u][0] + p[v][0]) / 2, (p[u][1] + p[v][1]) / 2), size=max(6, round(8 * scale)))
    for key in ("n1", "n2", "n3"):
        node(p[key], "road", max(8, round(12 * scale)))
    node(p["c1"], "origin_a", max(9, round(14 * scale)))
    node(p["c2"], "origin_b", max(9, round(14 * scale)))
    for key in ("city", "port", "airport"):
        node(p[key], "destination", max(9, round(14 * scale)))
    routes = {
        "baseline": ["c1", "n1", "n2", "city"],
        "wet": ["c1", "n1", "n3", "city"],
        "none": [],
    }
    path = routes[route]
    for u, v in zip(path, path[1:]):
        arrow(p[u], p[v], GREEN if route == "baseline" else ORANGE, max(2, round(3 * scale)), head=max(7, round(9 * scale)))
    return p


def layer_card(x, y, w, title, lines, fill, outline, active=False):
    rounded((x, y, x + w, y + 105), fill=fill, outline=outline, radius=12)
    d.text((x + 18, y + 14), title, font=F_H, fill=INK)
    d.multiline_text((x + 18, y + 47), lines, font=F_SMALL, fill=MUTED, spacing=4)
    if active:
        tag(x + w - 90, y + 12, "ACTIVE", BLUE_LIGHT, BLUE, BLUE)


def road_sample(x, y, color, title, detail):
    line((x, y + 28), (x + 100, y + 28), color, 7)
    d.text((x + 120, y + 6), title, font=F_H, fill=INK)
    d.text((x + 120, y + 34), detail, font=F_SMALL, fill=MUTED)


d.text((55, 35), "Equatorial crop accessibility as a weather-perturbed road-network problem", font=F_TITLE, fill=INK)
d.text((55, 84), "From crop origins and logistics destinations to weekly edge costs, routes, and accessibility outcomes", font=F_SUBTITLE, fill=MUTED)

Y, PH = 135, 710
panel(45, Y, 500, PH, 1, "Crop-specific network")
panel(575, Y, 570, PH, 2, "Environmental forcing")
panel(1175, Y, 500, PH, 3, "Road-edge response")
panel(1705, Y, 400, PH, 4, "Weekly routing")
panel(2135, Y, 400, PH, 5, "Accessibility outcomes")

# 1. Network entities
tag(75, 205, "STATIC SPATIAL INPUTS", GRAY_LIGHT, BORDER, MUTED)
p = network(85, 300, 0.92)
d.text((p["c1"][0] - 5, p["c1"][1] + 22), "crop A", font=F_TINY, fill=CORAL)
d.text((p["c2"][0] - 8, p["c2"][1] - 30), "crop B", font=F_TINY, fill=PURPLE)
d.text((76, 532), "Origins", font=F_H, fill=INK)
node((89, 574), "origin_a", 13)
d.text((112, 558), "crop A clusters", font=F_SMALL, fill=INK)
node((252, 574), "origin_b", 13)
d.text((275, 558), "crop B clusters", font=F_SMALL, fill=INK)
d.text((76, 610), "Destinations", font=F_H, fill=INK)
node((89, 650), "destination", 14)
d.text((115, 634), "cities / logistics hubs", font=F, fill=INK)
d.text((115, 660), "ports and airports", font=F_SMALL, fill=MUTED)
road_sample(76, 700, GOLD, "Paved roads", "lower weather sensitivity")
road_sample(76, 770, BROWN, "Unpaved-like roads", "unpaved / unknown / synthetic")

# 2. Environmental forcing
tag(605, 205, "DATA PIPELINE", BLUE_LIGHT, BLUE, BLUE)
layer_card(605, 250, 510, "Precipitation", "ERA5 hourly observations → weekly forcing", BLUE_LIGHT, BLUE, active=True)
arrow((860, 355), (860, 390), BLUE, 4)
rounded((680, 390, 1040, 452), fill="white", outline=BLUE, width=3, radius=12)
text_center((680, 390, 1040, 452), "weekly precipitation metric", F_H, BLUE)

tag(605, 495, "CONTEXT / MODEL EXTENSIONS", GRAY_LIGHT, BORDER, MUTED)
layer_card(605, 540, 245, "Flood hazard", "derived hazard layer", GRAY_LIGHT, BORDER)
layer_card(870, 540, 245, "Atmosphere", "temperature · winds\nsand / dust winds", GRAY_LIGHT, BORDER)
layer_card(605, 700, 510, "Land surface", "moisture / dryness · terrain slope · drainage context", GRAY_LIGHT, BORDER)
arrow((730, 700), (730, 645), GRAY, 3, dashed=True, head=9)

# 3. Road-edge response
tag(1205, 205, "SURFACE-SENSITIVE", ORANGE_LIGHT, ORANGE, ORANGE)
d.text((1205, 255), "The same weekly forcing produces", font=F, fill=INK)
d.text((1205, 282), "different costs by road surface.", font=F, fill=INK)
road_sample(1205, 330, GOLD, "Paved", "smaller speed penalty")
road_sample(1205, 410, BROWN, "Unpaved-like", "larger speed penalty")
arrow((1425, 490), (1425, 535), BLUE, 4)
rounded((1205, 535, 1645, 650), fill=ORANGE_LIGHT, outline=ORANGE, width=3, radius=14)
text_center((1205, 545, 1645, 592), "ACTIVE EDGE RESPONSE", F_TAG, ORANGE)
text_center((1225, 588, 1625, 638), "speed multiplier → weekly travel time", F_H, INK)
arrow((1425, 650), (1425, 700), GRAY, 3, dashed=True)
rounded((1205, 700, 1645, 815), fill=RED_LIGHT, outline=RED, width=2, radius=14)
text_center((1205, 710, 1645, 757), "SCENARIO EXTENSION", F_TAG, RED)
text_center((1225, 752, 1625, 803), "link available ↔ unavailable", F_H, INK)

# 4. Weekly routing
tag(1735, 205, "WEEKLY STATES", BLUE_LIGHT, BLUE, BLUE)
d.text((1735, 255), "Baseline graph", font=F_H, fill=INK)
network(1745, 290, 0.72, route="baseline")
d.text((1735, 475), "Weather-perturbed graph", font=F_H, fill=INK)
network(1745, 510, 0.72, affected=True, route="wet")
rounded((1740, 738, 2070, 820), fill=LIGHT, outline=BORDER, radius=12)
text_center((1740, 744, 2070, 780), "A* SHORTEST PATH", F_TAG, INK)
text_center((1755, 778, 2055, 812), "for every crop–destination OD pair", F_SMALL, MUTED)

# 5. Accessibility outcomes
tag(2165, 205, "INTERPRETABLE OUTPUTS", GREEN_LIGHT, GREEN, GREEN)
outcomes = [
    ("Δ travel time", "minutes above OD baseline", ORANGE_LIGHT, ORANGE),
    ("Route change", "same OD pair, different path", BLUE_LIGHT, BLUE),
    ("Crop spoilage risk", "delivery delays increase post-harvest loss", RED_LIGHT, RED),
]
for i, (title, detail, fill, outline) in enumerate(outcomes):
    yy = 270 + i * 125
    rounded((2165, yy, 2505, yy + 100), fill=fill, outline=outline, radius=12)
    d.text((2190, yy + 17), title, font=F_H, fill=INK)
    d.text((2190, yy + 55), detail, font=F_SMALL, fill=MUTED)
rounded((2165, 660, 2505, 815), fill=LIGHT, outline=BORDER, radius=12)
d.text((2190, 680), "Comparison dimensions", font=F_H, fill=INK)
d.multiline_text((2190, 720), "country × crop × destination type\nweek × duration × intensity × exposure", font=F_SMALL, fill=MUTED, spacing=10)

# Main causal arrows between panels
for x1, x2 in [(545, 575), (1145, 1175), (1675, 1705), (2105, 2135)]:
    arrow((x1 + 3, 520), (x2 - 3, 520), BLUE, 5, head=12)

# Footer legend and scope note
line((65, 905), (125, 905), BLUE, 5)
d.text((140, 892), "data pipeline", font=F_LEGEND, fill=INK)
line((400, 905), (460, 905), GRAY, 4, dashed=True)
d.text((475, 892), "context / extension", font=F_LEGEND, fill=INK)
node((690, 905), "origin_a", 15)
node((715, 905), "origin_b", 15)
d.text((745, 892), "crop-specific origins", font=F_LEGEND, fill=INK)
node((955, 905), "destination", 15)
d.text((985, 892), "logistics destination", font=F_LEGEND, fill=INK)
line((1245, 905), (1305, 905), GOLD, 7)
d.text((1320, 892), "paved", font=F_LEGEND, fill=INK)
line((1445, 905), (1505, 905), BROWN, 7)
d.text((1520, 892), "unpaved-like", font=F_LEGEND, fill=INK)
line((1715, 905), (1775, 905), ORANGE, 7)
d.text((1790, 892), "reduced speed", font=F_LEGEND, fill=INK)
OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(OUT)
print(OUT)
