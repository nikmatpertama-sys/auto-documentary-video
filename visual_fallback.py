"""API-free, topic-aware illustrated documentary fallback (Pillow + FFmpeg).

Creates explanatory 2D diagrams selected from scene semantics. It deliberately
avoids printing narration into the picture; the single ASS subtitle layer owns
spoken text. It is an illustration, never presented as archival footage.
"""
from __future__ import annotations

import hashlib
import math
import re
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1080, 1920
PALETTE = {"bg": (7, 18, 32), "panel": (10, 31, 48), "line": (76, 183, 190),
           "hot": (255, 112, 68), "light": (225, 241, 244), "muted": (151, 184, 194),
           "green": (118, 207, 155), "gold": (245, 194, 94)}


def _font(size: int, bold: bool = False):
    paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else
             "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _wrap(draw, text, font, width):
    words = re.sub(r"\s+", " ", str(text or "")).split()
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _line_arrow(d, start, end, fill, width=8):
    d.line((start, end), fill=fill, width=width)
    angle = math.atan2(end[1]-start[1], end[0]-start[0])
    size = 25
    p1 = (end[0]-size*math.cos(angle-.48), end[1]-size*math.sin(angle-.48))
    p2 = (end[0]-size*math.cos(angle+.48), end[1]-size*math.sin(angle+.48))
    d.polygon((end, p1, p2), fill=fill)


def _hydrothermal(d, t):
    # Ocean surface, water column, crust, chimney, hot plume and circulation.
    d.rectangle((90, 805, 990, 1110), fill=(8, 46, 68), outline=PALETTE["line"], width=3)
    d.line((90, 805, 990, 805), fill=(110, 204, 224), width=6)
    for x in range(150, 960, 105):
        d.arc((x, 835, x+70, 910), 190, 345, fill=(45, 120, 151), width=3)
    d.polygon([(90, 1110),(90, 1250),(990,1250),(990,1110),(780,1085),(600,1130),(400,1080),(220,1130)], fill=(77, 57, 48))
    d.polygon([(430,1115),(480,990),(510,940),(545,975),(565,1115)], fill=(55,55,58), outline=(190,165,137))
    d.polygon([(430,1115),(455,1020),(480,1005),(505,1115)], fill=(120,73,55))
    # Black-smoker plume
    for i in range(8):
        x = 520 + int(38*math.sin(t*1.7+i*.8)) + (i%3)*17
        y = 930-i*35
        r = 20 + (i%3)*8
        d.ellipse((x-r,y-r,x+r,y+r), fill=(38,48,55))
    _line_arrow(d,(250,1090),(440,1040),PALETTE["hot"],9)
    _line_arrow(d,(620,1080),(820,1150),PALETTE["hot"],9)
    d.text((125,845),"COLD SEAWATER",font=_font(27,True),fill=PALETTE["light"])
    d.text((620,1170),"HOT MINERAL-RICH FLUID",font=_font(23,True),fill=PALETTE["gold"])
    d.text((570,895),"MINERAL PLUME",font=_font(23,True),fill=PALETTE["light"])
    d.text((350,1275),"OCEANIC CRUST",font=_font(25,True),fill=PALETTE["muted"])


def _earth(d, t):
    cx, cy = 540, 1040
    d.ellipse((180,690,900,1410), fill=(35,92,133), outline=(126,206,222), width=5)
    d.pieslice((180,690,900,1410), 0, 360, fill=(35,92,133))
    d.ellipse((285,795,795,1305), fill=(197,126,73), outline=(246,192,112), width=4)
    d.ellipse((390,900,690,1200), fill=(244,174,81), outline=(255,220,150), width=4)
    d.ellipse((475,985,605,1115), fill=(255,220,145))
    for label, y, color in [("CRUST",735,PALETTE['light']),("MANTLE",850,(255,230,205)),("OUTER CORE",1020,(255,244,207)),("INNER CORE",1160,(255,250,224))]:
        d.text((cx,y),label,font=_font(24,True),fill=color,anchor="mm")
    _line_arrow(d,(820,780),(760,850),PALETTE['gold'],7)


def _space(d, t):
    cx,cy=540,1030
    d.ellipse((cx-125,cy-125,cx+125,cy+125),fill=(242,177,73),outline=(255,225,150),width=5)
    for radius, color, phase in [(270,(113,190,221),0),(390,(91,145,205),1.7)]:
        d.ellipse((cx-radius,cy-radius*.55,cx+radius,cy+radius*.55),outline=color,width=4)
        a=t*0.8+phase
        x=cx+radius*math.cos(a); y=cy+radius*.55*math.sin(a)
        d.ellipse((x-28,y-28,x+28,y+28),fill=color,outline=PALETTE['light'],width=3)
    for i in range(45):
        x=(i*193+83)%900+90; y=(i*317+120)%620+700
        d.ellipse((x,y,x+4,y+4),fill=(170,203,221))
    d.text((540,1400),"ORBITAL MOTION",font=_font(25,True),fill=PALETTE['muted'],anchor='mm')


def _bacteria(d, t):
    # Distinct rod-shaped cells, flagella and magnified callouts.
    for i in range(11):
        x=210+(i*137)%670; y=850+(i*83)%380
        angle=math.sin(t+i)*.22
        d.rounded_rectangle((x-55,y-24,x+55,y+24),radius=24,fill=(91,185,145),outline=(190,245,194),width=4)
        d.arc((x+45,y-40,x+125,y+55),180,320,fill=(170,222,180),width=3)
        for k in range(3):
            d.ellipse((x-25+k*22,y-5,x-15+k*22,y+5),fill=(30,92,79))
    d.ellipse((285,730,795,1240),outline=PALETTE['line'],width=5)
    d.text((540,1320),"MICROBIAL LIFE",font=_font(27,True),fill=PALETTE['green'],anchor='mm')


def _volcano(d, t):
    d.polygon([(150,1230),(410,830),(520,1010),(680,740),(940,1230)],fill=(83,76,73),outline=(186,164,143))
    d.polygon([(520,1010),(680,740),(940,1230),(650,1230)],fill=(105,72,59))
    d.polygon([(620,860),(680,740),(735,870),(700,900)],fill=(255,125,57))
    d.line((680,760,680,1210),fill=(255,117,57),width=25)
    for i in range(8):
        x=670+int(math.sin(t+i)*i*12); y=710-i*37
        d.ellipse((x-18,y-18,x+18,y+18),fill=(100,104,105))
    d.text((540,1300),"MAGMA CHAMBER  →  ERUPTION",font=_font(23,True),fill=PALETTE['gold'],anchor='mm')


def _animal(d, t):
    # Neutral mammal anatomy silhouette with labeled respiratory/circulatory systems.
    d.ellipse((260,850,790,1160),fill=(105,145,150),outline=(207,230,224),width=5)
    d.ellipse((700,790,890,960),fill=(105,145,150),outline=(207,230,224),width=5)
    d.polygon([(845,820),(930,850),(860,900)],fill=(105,145,150))
    for x in (360,680):
        d.line((x,1100,x-25,1300),fill=(207,230,224),width=28)
        d.line((x+30,1100,x+55,1300),fill=(207,230,224),width=28)
    d.arc((410,880,660,1120),0,360,fill=(255,145,110),width=12)
    d.text((540,1390),"ANATOMICAL OVERVIEW",font=_font(25,True),fill=PALETTE['muted'],anchor='mm')


def _climate(d, t):
    d.ellipse((730,760,900,930),fill=(246,190,93),outline=(255,225,150),width=5)
    d.arc((200,800,760,1380),190,350,fill=(107,189,209),width=28)
    d.arc((250,850,710,1320),190,350,fill=(107,189,209),width=18)
    for i in range(5):
        x=280+i*110
        _line_arrow(d,(x,1150),(x+25,950),PALETTE['hot'],7)
    d.text((540,1390),"ATMOSPHERIC HEAT FLOW",font=_font(25,True),fill=PALETTE['gold'],anchor='mm')



def _rainforest_undergrowth(d, t):
    """Forest-floor ecosystem: roots, ferns, orchid blooms and small insects."""
    # Layered soil and broad understory leaves.
    d.polygon([(105,1260),(240,1190),(390,1245),(560,1180),(740,1245),(930,1190),(975,1510),(105,1510)],
              fill=(36,75,56), outline=(90,145,101))
    for x, top, width in [(245,850,95),(830,820,110),(510,960,70)]:
        d.line((x,top+170,x,1370),fill=(118,91,58),width=max(18,width//5))
    # Ferns made from curved fronds.
    for base_x, base_y, scale in [(260,1220,1.0),(760,1280,.85),(490,1340,.7)]:
        for side in (-1,1):
            pts=[]
            for j in range(9):
                x=base_x+side*j*22*scale
                y=base_y-j*38*scale
                pts.append((int(x),int(y)))
                d.ellipse((x-34*scale,y-15*scale,x+8*scale,y+13*scale),
                          fill=(62,145,91),outline=(132,199,135),width=2)
            d.line(pts,fill=(172,207,135),width=5)
    # Orchid bloom with five petals and a contrasting center.
    ox, oy = 535, 1000
    for angle in (-90,-18,54,126,198):
        a=math.radians(angle)
        x=ox+78*math.cos(a); y=oy+78*math.sin(a)
        d.ellipse((x-57,y-35,x+57,y+35),fill=(178,133,202),outline=(235,206,246),width=4)
    d.ellipse((ox-34,oy-28,ox+34,oy+28),fill=(245,195,103),outline=(255,230,170),width=3)
    # Exposed root network and a small pollinator.
    for j in range(5):
        d.arc((390+j*12,1130+j*12,710-j*10,1450-j*9),20,155,fill=(190,143,91),width=5)
    d.ellipse((770,1030,810,1070),fill=(241,190,82),outline=(255,230,170),width=3)
    d.line((790,1030,790,1070),fill=(50,65,43),width=3)
    for dx,dy in [(-30,-18),(30,-18),(-30,18),(30,18)]:
        d.ellipse((790+dx-14,1050+dy-10,790+dx+14,1050+dy+10),outline=(238,231,190),width=3)
    d.text((540,1450),"ORCHIDS  •  FERNS  •  POLLINATORS",font=_font(22,True),fill=PALETTE['light'],anchor='mm')


def _rainforest_jaguar(d, t):
    """A recognizable spotted big-cat silhouette with a leafcutter trail."""
    # Forest silhouettes in the background.
    for x,h in [(170,250),(330,340),(790,290),(930,220)]:
        d.rectangle((x-18,980,x+18,1370),fill=(48,94,66))
        d.ellipse((x-105,880-h//3,x+105,1090),fill=(35,105,69),outline=(80,151,99),width=3)
    # Tail, body, head, legs and ears.
    d.arc((185,915,480,1220),190,340,fill=(222,165,82),width=34)
    d.ellipse((300,930,770,1190),fill=(222,165,82),outline=(255,218,150),width=5)
    d.ellipse((690,890,875,1065),fill=(222,165,82),outline=(255,218,150),width=5)
    d.polygon([(720,910),(730,840),(785,900)],fill=(222,165,82),outline=(255,218,150))
    d.polygon([(815,905),(865,850),(865,950)],fill=(222,165,82),outline=(255,218,150))
    d.polygon([(850,990),(940,1018),(855,1040)],fill=(216,155,78),outline=(255,218,150))
    for x in (390,650,730):
        d.rounded_rectangle((x,1120,x+58,1350),radius=24,fill=(207,151,78),outline=(255,218,150),width=4)
    # Distinct dark rosettes across body.
    for x,y in [(380,995),(470,1080),(560,980),(640,1080),(720,1000),(790,965),(520,1140)]:
        d.ellipse((x-22,y-18,x+22,y+18),outline=(57,46,34),width=7)
        d.ellipse((x-5,y-4,x+5,y+4),fill=(57,46,34))
    # Leafcutter ants carrying leaves along the bottom edge.
    for x in range(260,820,135):
        d.ellipse((x,1390,x+42,1422),fill=(42,34,29))
        for lx in (x+8,x+32):
            d.line((lx,1410,lx-12,1435),fill=(42,34,29),width=4)
            d.line((lx,1410,lx+13,1434),fill=(42,34,29),width=4)
        d.line((x+20,1394,x+34,1370),fill=(42,34,29),width=3)
        d.polygon([(x+25,1365),(x+62,1342),(x+50,1383)],fill=(98,178,94),outline=(170,220,146))
    d.text((540,1475),"JAGUAR HABITAT  •  LEAFCUTTER ANTS",font=_font(21,True),fill=PALETTE['light'],anchor='mm')


def _rainforest_river(d, t):
    """Aerial rainforest canopy with a winding river and moisture transport."""
    # Dense canopy texture.
    for row in range(7):
        for col in range(8):
            x=130+col*118+(row%2)*42
            y=780+row*92
            r=45+(col*row)%20
            colr=[(34,105,70),(43,126,80),(56,143,91),(30,91,65)][(row+col)%4]
            d.ellipse((x-r,y-r,x+r,y+r),fill=colr,outline=(102,171,112),width=2)
    river=[(245,760),(360,860),(620,920),(720,1040),(580,1140),(390,1210),(340,1330),(520,1450)]
    d.line(river,fill=(31,91,123),width=126,joint='curve')
    d.line(river,fill=(85,177,205),width=76,joint='curve')
    d.line(river,fill=(157,217,225),width=5,joint='curve')
    # Water vapor rising toward cloud layer.
    d.ellipse((195,690,425,790),fill=(188,215,222),outline=(236,246,245),width=3)
    d.ellipse((310,650,520,785),fill=(203,225,229),outline=(236,246,245),width=3)
    for x in (320,460,640):
        _line_arrow(d,(x,850),(x+35,725),PALETTE['gold'],6)
    d.text((540,1480),"FOREST MOISTURE  →  FLYING RIVERS",font=_font(22,True),fill=PALETTE['light'],anchor='mm')


def _rainforest_drought(d, t):
    """Dry channel, cracked soil and stressed trees during drought."""
    d.ellipse((740,760,900,920),fill=(235,166,77),outline=(255,220,155),width=5)
    for a in range(0,360,45):
        x1=820+105*math.cos(math.radians(a)); y1=840+105*math.sin(math.radians(a))
        x2=820+150*math.cos(math.radians(a)); y2=840+150*math.sin(math.radians(a))
        d.line((x1,y1,x2,y2),fill=(222,154,82),width=7)
    d.polygon([(80,1120),(260,1060),(400,1115),(590,1050),(790,1130),(1000,1070),(1000,1510),(80,1510)],
              fill=(112,83,58),outline=(180,136,90))
    # Cracks radiating across the dry ground.
    cracks=[[(160,1170),(220,1230),(190,1300),(270,1370)],[(420,1130),(385,1210),(450,1270),(420,1380)],
            [(700,1150),(650,1220),(720,1300),(680,1410)],[(875,1160),(820,1240),(900,1320)]]
    for pts in cracks: d.line(pts,fill=(56,45,35),width=9)
    # Bare trees.
    for x in (220,850):
        d.line((x,1120,x-18,890),fill=(83,62,43),width=24)
        d.line((x-12,1000,x-120,920),fill=(83,62,43),width=15)
        d.line((x-4,970,x+90,880),fill=(83,62,43),width=14)
        for lx,ly in [(x-130,900),(x+80,860),(x-20,850)]:
            d.ellipse((lx-34,ly-20,lx+32,ly+24),fill=(119,103,63))
    d.text((540,1475),"DRY SEASON  •  WATER STRESS",font=_font(23,True),fill=PALETTE['gold'],anchor='mm')


def _rainforest_leaf(d, t):
    """Leaf anatomy, branching veins and water-vapor release."""
    leaf=[(540,775),(745,865),(860,1065),(730,1280),(540,1435),(350,1280),(220,1065),(335,865)]
    d.polygon(leaf,fill=(56,145,91),outline=(160,220,156))
    d.line((540,1430,540,805),fill=(194,222,147),width=13)
    for y,dx in [(900,150),(990,220),(1080,255),(1170,220),(1260,150)]:
        d.line((540,y,540-dx,y-95),fill=(170,218,145),width=8)
        d.line((540,y,540+dx,y-95),fill=(170,218,145),width=8)
    # Droplets and rising vapor arrows.
    for x,y in [(385,910),(695,1010),(430,1190),(650,1260)]:
        d.ellipse((x-13,y-20,x+13,y+20),fill=(145,211,226),outline=(220,245,245),width=3)
    for x in (365,540,715):
        _line_arrow(d,(x,830),(x+int((x-540)*.12),720),PALETTE['gold'],6)
    d.ellipse((770,1320,960,1500),fill=(12,42,55),outline=PALETTE['line'],width=4)
    for x in (820,865,910):
        d.ellipse((x,1390,x+18,1410),fill=(116,207,155))
    d.text((540,1490),"TRANSPIRATION  •  WATER VAPOR",font=_font(21,True),fill=PALETTE['light'],anchor='mm')


def _rainforest_mode(scene):
    # Let the concrete scene query decide first; incidental words in narration
    # (e.g. "insects" while describing orchids) must not hijack the visual.
    query = " ".join(str(scene.get(k, "")) for k in ("visual_query", "title")).lower()
    if re.search(r"\b(jaguar|leafcutter|ant|insect)\w*\b", query):
        return "jaguar"
    if re.search(r"\b(drought|dry|cracked|parched)\w*\b", query):
        return "drought"
    if re.search(r"\b(river|flying river|stream|waterway)\w*\b", query):
        return "river"
    if re.search(r"\b(leaf|leaves|transpiration|stomata|vapor)\w*\b", query):
        return "leaf"
    return "undergrowth"

def _generic_process(d, t, title):
    # Explicit explanatory process layout, not random geometry.
    labels = ["INPUT", "PROCESS", "OUTCOME"]
    y = [830, 1040, 1250]
    colors = [PALETTE['line'], PALETTE['gold'], PALETTE['green']]
    for i, (label, yy, color) in enumerate(zip(labels,y,colors)):
        d.rounded_rectangle((190,yy-65,890,yy+65),radius=24,fill=(12,43,58),outline=color,width=5)
        d.text((540,yy),label,font=_font(37,True),fill=PALETTE['light'],anchor='mm')
        if i < 2:
            _line_arrow(d,(540,yy+75),(540,yy+145),color,7)
    d.text((540,1450),"CONCEPTUAL PROCESS DIAGRAM",font=_font(22,True),fill=PALETTE['muted'],anchor='mm')


def _category(scene, topic):
    """Classify from scene-local cues first; use whole-topic cues only as fallback.

    Word boundaries are intentional: e.g. 'planetary' must not trigger 'planet'.
    """
    local = " ".join(str(scene.get(k, "")) for k in ("visual_query", "title", "narration")).lower()
    global_topic = str(topic or "").lower()

    def has(text, terms):
        return any(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?:s|es)?(?![a-z0-9])", text)
                   for term in terms)

    # Specific rainforest scene handling takes priority over broad documentary context.
    if has(local, ("rainforest", "amazon", "tropical forest", "forest floor", "rain forest")):
        return "rainforest"

    groups = [
        ("hydrothermal", ("hydrothermal", "black smoker", "ocean vent", "deep sea vent")),
        ("earth", ("earth core", "mantle", "tectonic", "earth layer", "crust")),
        ("space", ("planet", "solar system", "orbit", "astronomy", "asteroid", "moon", "space")),
        ("bacteria", ("bacteria", "bacterium", "microbe", "microbial", "microscopic organism")),
        ("volcano", ("volcano", "eruption", "magma", "lava")),
        ("animal", ("animal", "mammal", "whale", "shark", "insect", "bird", "dinosaur", "human body", "anatomy", "jaguar")),
        ("climate", ("climate", "global warming", "greenhouse", "atmosphere", "heat trapping")),
    ]
    for category, terms in groups:
        if has(local, terms):
            return category
    # Global title/topic is considered only after local scene semantics.
    for category, terms in groups:
        if has(global_topic, terms):
            return category
    return "process"


def create_fallback_video(scene: dict, scene_number: int, output: str | Path,
                          duration: float, topic: str = "") -> Path:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    seed = hashlib.sha256((topic + str(scene.get("visual_query", "")) +
                           str(scene.get("narration", ""))).encode()).digest()
    image = Image.new("RGB", (W,H), PALETTE['bg'])
    px = image.load()
    for y in range(H):
        blend = y/(H-1)
        col = tuple(int(PALETTE['bg'][c]*(1-blend)+PALETTE['panel'][c]*blend) for c in range(3))
        for x in range(W): px[x,y] = col
    # restrained atmospheric light
    glow = Image.new("RGBA",(W,H),(0,0,0,0)); gd=ImageDraw.Draw(glow)
    gd.ellipse((100,620,980,1500),fill=(31,111,128,30)); glow=glow.filter(ImageFilter.GaussianBlur(90))
    image=Image.alpha_composite(image.convert("RGBA"),glow)
    d=ImageDraw.Draw(image)
    d.text((76,125),f"FIELD NOTES  /  SCENE {scene_number:02d}",font=_font(26,True),fill=(*PALETTE['line'],255))
    d.line((76,190,1004,190),fill=(*PALETTE['line'],180),width=3)
    title=str(scene.get("title") or scene.get("visual_query") or topic or "DOCUMENTARY EXPLAINER")
    title=re.sub(r"[\r\n]+"," ",title).strip()[:100]
    tf=_font(57,True); lines=_wrap(d,title.upper(),tf,920)[:2]
    yy=265
    for line in lines:
        d.text((76,yy),line,font=tf,fill=(*PALETTE['light'],255),stroke_width=1,stroke_fill=(0,0,0,120)); yy+=75
    d.rounded_rectangle((76,650,1004,1515),radius=32,fill=(5,19,31,220),outline=(*PALETTE['line'],150),width=3)
    category=_category(scene,topic)
    if category == "rainforest":
        rainforest_drawers = {
            "undergrowth": _rainforest_undergrowth,
            "jaguar": _rainforest_jaguar,
            "river": _rainforest_river,
            "drought": _rainforest_drought,
            "leaf": _rainforest_leaf,
        }
        rainforest_drawers[_rainforest_mode(scene)](d, 0)
    else:
        drawers={"hydrothermal":_hydrothermal,"earth":_earth,"space":_space,"bacteria":_bacteria,
                 "volcano":_volcano,"animal":_animal,"climate":_climate}
        drawers.get(category,lambda draw, t:_generic_process(draw,t,title))(d,0)
    d.text((78,1610),category.replace("_"," ").upper()+"  •  ORIGINAL ILLUSTRATION",font=_font(23,True),fill=(*PALETTE['line'],255))
    d.text((78,1680),"SCHEMATIC EXPLANATION — NOT ARCHIVAL FOOTAGE",font=_font(20),fill=(*PALETTE['muted'],255))
    png=output.with_suffix('.png'); image.convert('RGB').save(png,quality=95)
    safe_duration=max(1.0,float(duration)); frames=max(30,int(math.ceil(safe_duration*30)))
    vf=(f"scale=1200:2134:flags=lanczos,zoompan=z='min(1.055,1+0.00018*on)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps=30,"
        "eq=contrast=1.04:saturation=1.08,format=yuv420p")
    cmd=["ffmpeg","-y","-loop","1","-i",str(png),"-t",f"{safe_duration:.3f}","-vf",vf,
         "-an","-c:v","libx264","-preset","medium","-crf","19","-r","30",
         "-pix_fmt","yuv420p","-movflags","+faststart",str(output)]
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=600)
    if result.returncode or not output.exists() or output.stat().st_size<1000:
        raise RuntimeError("Local visual fallback render failed: "+result.stderr[-1500:])
    return output
