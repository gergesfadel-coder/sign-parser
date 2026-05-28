from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
import ezdxf
import fitz  # PyMuPDF
import io, os, math, re

app = FastAPI(title="Sign Shape Parser")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

SECRET = os.getenv("PARSER_SECRET", "dev-secret")


def verify(secret: str | None):
    if secret != SECRET:
        raise HTTPException(status_code=401, detail="Unauthorised")


# ── DXF ──────────────────────────────────────────────────────────────────────

def dxf_to_svg(data: bytes) -> dict:
    doc = ezdxf.read(io.StringIO(data.decode("utf-8", errors="replace")))
    msp = doc.modelspace()

    paths, min_x, min_y, max_x, max_y = [], None, None, None, None

    def upd(x, y):
        nonlocal min_x, min_y, max_x, max_y
        min_x = x if min_x is None else min(min_x, x)
        min_y = y if min_y is None else min(min_y, y)
        max_x = x if max_x is None else max(max_x, x)
        max_y = y if max_y is None else max(max_y, y)

    for e in msp:
        if e.dxftype() == "LINE":
            s, end = e.dxf.start, e.dxf.end
            upd(s.x, s.y); upd(end.x, end.y)
            paths.append(f"M {s.x},{-s.y} L {end.x},{-end.y}")
        elif e.dxftype() == "LWPOLYLINE":
            pts = list(e.get_points())
            if not pts: continue
            d = f"M {pts[0][0]},{-pts[0][1]}"
            for p in pts[1:]:
                d += f" L {p[0]},{-p[1]}"
                upd(p[0], p[1])
            if e.closed: d += " Z"
            upd(pts[0][0], pts[0][1])
            paths.append(d)
        elif e.dxftype() == "CIRCLE":
            cx, cy, r = e.dxf.center.x, e.dxf.center.y, e.dxf.radius
            upd(cx - r, cy - r); upd(cx + r, cy + r)
            paths.append(
                f"M {cx-r},{-cy} A {r},{r} 0 1,0 {cx+r},{-cy} A {r},{r} 0 1,0 {cx-r},{-cy} Z"
            )
        elif e.dxftype() == "ARC":
            cx, cy, r = e.dxf.center.x, e.dxf.center.y, e.dxf.radius
            a1 = math.radians(e.dxf.start_angle)
            a2 = math.radians(e.dxf.end_angle)
            x1 = cx + r * math.cos(a1); y1 = cy + r * math.sin(a1)
            x2 = cx + r * math.cos(a2); y2 = cy + r * math.sin(a2)
            large = 1 if (e.dxf.end_angle - e.dxf.start_angle) % 360 > 180 else 0
            upd(x1, y1); upd(x2, y2)
            paths.append(f"M {x1},{-y1} A {r},{r} 0 {large},1 {x2},{-y2}")

    if min_x is None:
        raise HTTPException(status_code=422, detail="No drawable entities found in DXF")

    w = max_x - min_x
    h = max_y - min_y
    svg_path = " ".join(paths)

    return {
        "svgPath": svg_path,
        "widthMm":  round(w, 2),
        "heightMm": round(h, 2),
        "viewBox":  f"{min_x} {-max_y} {w} {h}",
    }


# ── PDF ──────────────────────────────────────────────────────────────────────

def pdf_to_svg(data: bytes) -> dict:
    doc = fitz.open(stream=data, filetype="pdf")
    page = doc[0]
    paths, min_x, min_y, max_x, max_y = [], None, None, None, None

    def upd(x, y):
        nonlocal min_x, min_y, max_x, max_y
        min_x = x if min_x is None else min(min_x, x)
        min_y = y if min_y is None else min(min_y, y)
        max_x = x if max_x is None else max(max_x, x)
        max_y = y if max_y is None else max(max_y, y)

    for path in page.get_drawings():
        items = path.get("items", [])
        if not items: continue
        d = ""
        for item in items:
            t = item[0]
            if t == "m":
                d += f"M {item[1].x},{item[1].y} "; upd(item[1].x, item[1].y)
            elif t == "l":
                d += f"L {item[1].x},{item[1].y} "; upd(item[1].x, item[1].y)
            elif t == "c":
                d += f"C {item[1].x},{item[1].y} {item[2].x},{item[2].y} {item[3].x},{item[3].y} "
                upd(item[3].x, item[3].y)
            elif t == "re":
                r = item[1]
                d += f"M {r.x0},{r.y0} L {r.x1},{r.y0} L {r.x1},{r.y1} L {r.x0},{r.y1} Z "
                upd(r.x0, r.y0); upd(r.x1, r.y1)
        if path.get("closePath"): d += "Z"
        if d.strip(): paths.append(d.strip())

    if min_x is None:
        raise HTTPException(status_code=422, detail="No path data found in PDF")

    # Convert from PDF points to mm (1 pt = 0.352778 mm)
    PT_TO_MM = 0.352778
    w_mm = (max_x - min_x) * PT_TO_MM
    h_mm = (max_y - min_y) * PT_TO_MM

    return {
        "svgPath":  " ".join(paths),
        "widthMm":  round(w_mm, 2),
        "heightMm": round(h_mm, 2),
        "viewBox":  f"{min_x} {min_y} {max_x - min_x} {max_y - min_y}",
    }


# ── ROUTES ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parse/dxf")
async def parse_dxf(
    file: UploadFile = File(...),
    x_parser_secret: str | None = Header(default=None),
):
    verify(x_parser_secret)
    data = await file.read()
    return dxf_to_svg(data)


@app.post("/parse/pdf")
async def parse_pdf(
    file: UploadFile = File(...),
    x_parser_secret: str | None = Header(default=None),
):
    verify(x_parser_secret)
    data = await file.read()
    return pdf_to_svg(data)