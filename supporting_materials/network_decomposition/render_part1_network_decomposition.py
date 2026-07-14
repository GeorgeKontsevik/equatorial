from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tmp" / "part1_network_decomposition.png"

W, H = 2580, 980
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)

FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


F_TITLE = font(42, True)
F_H = font(27, True)
F = font(20)
F_SMALL = font(17)
F_LEGEND = font(26)

black = (25, 25, 25)
gray = (175, 175, 175)
light = (238, 242, 246)
red = (220, 95, 100)
red_fill = (255, 212, 212)
green = (78, 165, 92)
green_fill = (210, 238, 210)
supply_b = (18, 118, 73)
supply_b_fill = (205, 240, 222)
blue = (72, 126, 190)
blue_fill = (210, 228, 250)
yellow = (230, 184, 92)
orange = (230, 130, 48)
purple = (165, 105, 190)
teal = (0, 140, 137)
brown = (150, 110, 70)
env = (255, 247, 214)
precip_env = (222, 241, 255)
temp_env = (255, 232, 205)

N = {
    "1": (300, 0),
    "2": (180, 100),
    "3": (300, 180),
    "5": (80, 285),
    "6": (135, 205),
    "7": (430, 125),
    "8": (520, 220),
}
ROAD = [
    ("1", "7", "paved"),
    ("7", "3", "paved"),
    ("2", "6", "unpaved"),
    ("2", "3", "paved"),
    ("5", "3", "unpaved"),
    ("3", "8", "paved"),
    ("7", "8", "unpaved"),
]
TRANSPORT = [
    ("2", "3", "air"),
    ("3", "8", "air"),
    ("1", "7", "water"),
    ("7", "3", "water"),
    ("5", "8", "winter"),
]
ROAD_COLORS = {"paved": yellow, "unpaved": brown}
TRANSPORT_COLORS = {"air": red, "water": purple, "winter": teal}


def pts(x, y, s=0.52):
    return {k: (x + vx * s, y + vy * s) for k, (vx, vy) in N.items()}


def panel(x, y, w, h, title):
    d.rounded_rectangle((x, y, x + w, y + h), radius=16, fill="white", outline=(205, 205, 205), width=3)
    title = title.replace("спроса-предложения", "спроса-\nпредложения")
    d.multiline_text((x + 24, y + 22), title, font=F_H, fill=black, spacing=4)


def node(p, kind="plain", label=None, r=13, override=None):
    fill, outline = {
        "plain": ("white", black),
        "demand": (red_fill, red),
        "supply": (green_fill, green),
        "candidate": (blue_fill, blue),
    }[kind]
    if override:
        fill, outline = override
    d.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=fill, outline=outline, width=2)
    if label:
        bb = d.textbbox((0, 0), label, font=F_SMALL)
        d.text((p[0] - bb[2] / 2, p[1] - bb[3] / 2 - 1), label, font=F_SMALL, fill=black)


def curved(a, b, color, width=4, bend=0.15):
    ax, ay = a
    bx, by = b
    mx, my = (ax + bx) / 2, (ay + by) / 2
    dx, dy = bx - ax, by - ay
    cx, cy = mx - dy * bend, my + dx * bend
    line = []
    for i in range(30):
        t = i / 29
        line.append(((1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t**2 * bx, (1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t**2 * by))
    d.line(line, fill=color, width=width)


def curved_arrow(a, b, color, width=4, bend=0.22):
    ax, ay = a
    bx, by = b
    ang = math.atan2(by - ay, bx - ax)
    cut = 15
    ax += cut * math.cos(ang)
    ay += cut * math.sin(ang)
    bx -= cut * math.cos(ang)
    by -= cut * math.sin(ang)
    mx, my = (ax + bx) / 2, (ay + by) / 2
    dx, dy = bx - ax, by - ay
    cx, cy = mx - dy * bend, my + dx * bend
    line = []
    for i in range(30):
        t = i / 29
        line.append(((1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t**2 * bx, (1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t**2 * by))
    d.line(line, fill=color, width=width)
    x0, y0 = line[-2]
    x1, y1 = line[-1]
    head = math.atan2(y1 - y0, x1 - x0)
    size = 10
    d.polygon([(x1, y1), (x1 - size * math.cos(head - 0.45), y1 - size * math.sin(head - 0.45)), (x1 - size * math.cos(head + 0.45), y1 - size * math.sin(head + 0.45))], fill=color)


def arrow(a, b, color, width=4):
    ax, ay = a
    bx, by = b
    ang = math.atan2(by - ay, bx - ax)
    cut = 15
    a = (ax + cut * math.cos(ang), ay + cut * math.sin(ang))
    b = (bx - cut * math.cos(ang), by - cut * math.sin(ang))
    d.line((*a, *b), fill=color, width=width)
    size = 10
    d.polygon([b, (b[0] - size * math.cos(ang - 0.45), b[1] - size * math.sin(ang - 0.45)), (b[0] - size * math.cos(ang + 0.45), b[1] - size * math.sin(ang + 0.45))], fill=color)


def offset_arrow(a, b, color, offset=0, width=4):
    ax, ay = a
    bx, by = b
    ang = math.atan2(by - ay, bx - ax)
    nx = -math.sin(ang) * offset
    ny = math.cos(ang) * offset
    arrow((ax + nx, ay + ny), (bx + nx, by + ny), color, width)


def loop_arrow(p, color):
    x, y = p
    d.arc((x - 22, y - 22, x + 22, y + 22), 35, 330, fill=color, width=4)
    d.polygon([(x + 18, y - 9), (x + 7, y - 13), (x + 15, y - 20)], fill=color)


def graph(x, y, roads=None, transports=None, supply=(), demand=("1", "5"), plain=False, flows=(), muted_roads=False, show_mixed=True, supply_override=None):
    p = pts(x, y)
    roads = ROAD if roads is None else roads
    transports = TRANSPORT if transports is None else transports
    for u, v, typ in roads:
        d.line((*p[u], *p[v]), fill=black if muted_roads else ROAD_COLORS.get(typ, gray), width=2 if muted_roads else 4)
    for u, v, typ in transports:
        curved(p[u], p[v], TRANSPORT_COLORS[typ], width=4)
    for k in N:
        if plain:
            kind = "plain"
        elif k in demand:
            kind = "demand"
        elif k in supply:
            kind = "supply"
        elif show_mixed and k == "3":
            kind = "candidate"
        else:
            kind = "plain"
        node(p[k], kind, override=supply_override if kind == "supply" else None)
    for path, color in flows:
        for u, v in zip(path, path[1:]):
            arrow(p[u], p[v], color, 4)
    return p


def env_stack(x, y, labels=True, step=50, width=220):
    for i, (name, fill) in enumerate([("осадки", precip_env), ("температура", temp_env), ("др. факторы", env)]):
        yy = y + i * step
        d.polygon([(x, yy + 32), (x + 36, yy), (x + width, yy), (x + width - 36, yy + 32)], fill=fill, outline=(205, 215, 225))
        if labels:
            d.text((x + width + 15, yy + 3), name, font=F, fill=black)


def xmark(p):
    x, y = p
    d.line((x - 8, y - 8, x + 8, y + 8), fill=red, width=4)
    d.line((x - 8, y + 8, x + 8, y - 8), fill=red, width=4)


def damaged_graph(x, y, roads=None, transports=None, broken=()):
    p = pts(x, y)
    broken = {tuple(sorted((u, v))) for u, v in broken}
    roads = [] if roads is None else roads
    transports = [] if transports is None else transports
    for u, v, typ in roads:
        is_broken = tuple(sorted((u, v))) in broken
        d.line((*p[u], *p[v]), fill=gray if is_broken else ROAD_COLORS[typ], width=4)
        if is_broken:
            xmark(((p[u][0] + p[v][0]) / 2, (p[u][1] + p[v][1]) / 2))
    for u, v, typ in transports:
        is_broken = tuple(sorted((u, v))) in broken
        curved(p[u], p[v], gray if is_broken else TRANSPORT_COLORS[typ], width=4)
        if is_broken:
            xmark(((p[u][0] + p[v][0]) / 2, (p[u][1] + p[v][1]) / 2))
    for k in N:
        node(p[k], "plain")


def state_graph(x, y, broken=(), flows=None, new_winter=(), extra_supply=()):
    p = pts(x, y, s=0.46)
    broken = {tuple(sorted((u, v))) for u, v in broken}
    if flows is None:
        flows = [(("1", "7", "3"), green), (("5", "3", "8"), supply_b)]
    for u, v, _ in ROAD:
        is_broken = tuple(sorted((u, v))) in broken
        d.line((*p[u], *p[v]), fill=gray if is_broken else black, width=3 if is_broken else 2)
        if is_broken:
            xmark(((p[u][0] + p[v][0]) / 2, (p[u][1] + p[v][1]) / 2))
    for u, v in new_winter:
        curved(p[u], p[v], teal, width=4, bend=0.22)
    for k in N:
        if k == "3" or k in extra_supply:
            node(p[k], "supply")
            continue
        elif k in {"1", "5"}:
            kind = "demand"
        elif k == "8":
            node(p[k], "supply", override=(supply_b_fill, supply_b))
            continue
        else:
            kind = "plain"
        node(p[k], kind, r=13)
    for path, color in flows:
        for u, v in zip(path, path[1:]):
            arrow(p[u], p[v], color, 3)
    return p


def lightning_arrow(x, y):
    pts = [(x, y), (x - 10, y + 34), (x + 4, y + 34), (x - 8, y + 70)]
    d.line(pts, fill=red, width=5, joint="curve")
    d.polygon([(x - 8, y + 70), (x - 18, y + 55), (x + 2, y + 59)], fill=red)


d.text((50, 38), "Разложение сети на слои спроса-предложения, дорог и транспорта", font=F_TITLE, fill=black)

panel(55, 125, 390, 700, "1. Сложная сеть")
panel(470, 125, 390, 700, "2. Слои спроса-предложения")
panel(885, 125, 390, 700, "3. Слои ребер")
panel(1300, 125, 390, 700, "4. Внешние слои")
panel(1715, 125, 390, 700, "5. Разрушенные слои")
panel(2130, 125, 390, 700, "6. Состояния t1-t3")

graph(95, 315, supply=("3", "8"), flows=[(("1", "7", "3"), green), (("5", "3", "8"), supply_b)])
d.text((100, 700), "в одном графе смешаны:", font=F, fill=black)
d.text((100, 730), "спрос / предложение,", font=F, fill=black)
d.text((100, 758), "дороги, транспорт", font=F, fill=black)

p_supply_a = graph(510, 215, roads=ROAD, transports=(), supply=("3",), demand=("1", "5"), flows=[(("1", "7", "3"), green)], muted_roads=True, show_mixed=False)
loop_arrow(p_supply_a["5"], green)
d.text((510, 395), "тип предложения A", font=F, fill=green)
p_supply_b = graph(510, 500, roads=ROAD, transports=(), supply=("8",), demand=("1", "5"), flows=[(("5", "3", "8"), supply_b)], muted_roads=True, show_mixed=False, supply_override=(supply_b_fill, supply_b))
loop_arrow(p_supply_b["1"], supply_b)
d.text((510, 680), "тип предложения B", font=F, fill=supply_b)

graph(925, 195, roads=[e for e in ROAD if e[2] == "paved"], transports=(), supply=(), demand=(), plain=True)
d.text((925, 370), "дорога с покрытием", font=F, fill=yellow)
graph(925, 405, roads=[e for e in ROAD if e[2] == "unpaved"], transports=(), supply=(), demand=(), plain=True)
d.text((925, 580), "грунтовая дорога", font=F, fill=brown)
graph(925, 615, roads=[], transports=TRANSPORT, supply=(), demand=(), plain=True)
d.text((925, 790), "виды транспорта", font=F, fill=purple)

env_stack(1313, 250)
graph(1340, 500, roads=ROAD, transports=TRANSPORT, supply=(), demand=(), plain=True)
lightning_arrow(1495, 390)
d.text((1340, 765), "внешние слои меняют доступность", font=F, fill=black)
d.text((1340, 795), "ребер разных типов", font=F, fill=black)

env_stack(1780, 205, labels=False, step=38, width=260)
lightning_arrow(1910, 320)
damaged_graph(1755, 430, roads=[e for e in ROAD if e[2] == "unpaved"], broken=[("5", "3"), ("7", "8")])
d.text((1755, 600), "грунтовая дорога", font=F, fill=brown)
damaged_graph(1755, 625, transports=TRANSPORT, broken=[("3", "8"), ("5", "8")])
d.text((1755, 795), "виды транспорта", font=F, fill=purple)

p_t1 = state_graph(2185, 220, broken=[])
loop_arrow(p_t1["1"], green)
loop_arrow(p_t1["5"], supply_b)
d.text((2160, 195), "t1", font=F, fill=black)
p_t2 = state_graph(2185, 440, broken=[("7", "3")], flows=[(("5", "3", "8"), supply_b)])
loop_arrow(p_t2["1"], green)
loop_arrow(p_t2["5"], supply_b)
offset_arrow(p_t2["1"], p_t2["7"], green, 0, 3)
offset_arrow(p_t2["7"], p_t2["8"], green, 0, 3)
offset_arrow(p_t2["8"], p_t2["3"], green, -8, 3)
offset_arrow(p_t2["3"], p_t2["5"], green, -8, 3)
d.text((2160, 415), "t2", font=F, fill=black)
p_t3 = state_graph(2185, 660, broken=[("7", "3"), ("5", "3"), ("3", "8")], flows=[])
loop_arrow(p_t3["1"], green)
loop_arrow(p_t3["5"], supply_b)
curved_arrow(p_t3["5"], p_t3["8"], teal, 3)
d.text((2160, 635), "t3", font=F, fill=black)

node((80, 900), "demand", r=18)
d.text((115, 882), "спрос", font=F_LEGEND, fill=black)
node((255, 900), "supply", r=18)
d.text((290, 882), "предложение", font=F_LEGEND, fill=black)
d.line((520, 900, 590, 900), fill=yellow, width=8)
d.text((610, 882), "с покрытием", font=F_LEGEND, fill=black)
d.line((770, 900, 840, 900), fill=brown, width=8)
d.text((860, 882), "грунтовая", font=F_LEGEND, fill=black)
d.line((1020, 900, 1090, 900), fill=red, width=8)
d.text((1110, 882), "авиация", font=F_LEGEND, fill=black)
d.line((1305, 900, 1375, 900), fill=purple, width=8)
d.text((1395, 882), "водный транспорт", font=F_LEGEND, fill=black)
d.line((1685, 900, 1755, 900), fill=teal, width=8)
d.text((1775, 882), "зимник", font=F_LEGEND, fill=black)
d.line((2000, 900, 2090, 900), fill=black, width=9)
d.polygon([(2112, 900), (2086, 888), (2086, 912)], fill=black)
d.text((2130, 882), "поток", font=F_LEGEND, fill=black)

OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(OUT)
print(OUT)
