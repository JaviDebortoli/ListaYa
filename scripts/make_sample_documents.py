"""Generate the PDF and photo sample lists in samples/ferreteria (fictional data).

    python scripts/make_sample_documents.py

- lista_pintureria_oeste.pdf: a PDF with a text layer (text route).
- foto_corralon_san_martin.jpg: a slightly rotated, noisy photo of a printed list (vision route).
- escaneo_corralon_san_martin.pdf: the same photo as an image-only PDF (scanned PDF route).
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples" / "ferreteria"

OESTE_LINES = [
    ("PINTURERÍA OESTE - LISTA DE PRECIOS OCTUBRE 2026", 13),
    ("Precios + IVA. Consultar por cantidad.", 10),
    ("", 10),
    ("Código     Descripción                              Precio", 10),
    ("PO-101     Látex interior blanco 4 L                $ 22.990,00", 10),
    ("PO-102     Rodillo lana 22 cm                       $ 5.610,00", 10),
    ("PO-103     Pincel cerda N° 2                        $ 2.050,00", 10),
    ("PO-201     Canilla esférica 1/2\"                    $ 7.480,00", 10),
    ("PO-202     Teflón 3/4\" x 10 m                       $ 520,00", 10),
    ("PO-301     Silicona transparente 280 ml             $ 4.150,00", 10),
    ("PO-302     Pegamento de contacto 250 ml             $ 4.990,00", 10),
    ("PO-401     Esmalte sintético negro 1 L              $ 9.800,00", 10),
]

CORRALON_LINES = [
    "CORRALÓN SAN MARTÍN",
    "Lista de precios - Ferretería",
    "Precios con IVA incluido",
    "",
    "Clavo punta paris 2\" (x kg) ........ $ 4.828",
    "Clavo punta paris 2 1/2\" (x kg) .... $ 4.683",
    "Martillo carpintero 27 mm ......... $ 13.189",
    "Cinta métrica 5 m ................. $ 5.748",
    "Toma corriente doble .............. $ 3.194",
    "Destornillador plano 6 mm ......... $ 3.497",
    "Llave ajustable 8\" ................ $ 15.730",
]


def _pdf_text(text: str) -> bytes:
    encoded = text.encode("cp1252")
    return encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def text_pdf(lines: list[tuple[str, int]]) -> bytes:
    """A one-page PDF with a real text layer (Courier, WinAnsi encoding for Spanish accents)."""
    stream = b"BT /F1 10 Tf 50 780 Td 14 TL\n"
    for text, size in lines:
        stream += b"/F1 %d Tf (" % size + _pdf_text(text) + b") Tj T*\n"
    stream += b"ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
        b"/Contents 5 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("consola.ttf", "DejaVuSansMono.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def photo(lines: list[str]) -> Image.Image:
    """A printed list photographed slightly rotated, on greyish paper, with some noise and blur."""
    rng = random.Random(7)
    page = Image.new("RGB", (1100, 760), (236, 232, 222))
    draw = ImageDraw.Draw(page)
    title, body = _font(34), _font(27)
    y = 50
    for index, line in enumerate(lines):
        draw.text((60, y), line, fill=(35, 35, 40), font=title if index == 0 else body)
        y += 52 if index == 0 else 44
    for _ in range(9000):  # paper grain
        x, yy = rng.randrange(page.width), rng.randrange(page.height)
        shade = rng.randrange(170, 235)
        page.putpixel((x, yy), (shade, shade, shade - 8))
    table = Image.new("RGB", (1240, 900), (120, 105, 90))  # the table the paper lies on
    rotated = page.rotate(2.5, expand=True, fillcolor=(120, 105, 90), resample=Image.BICUBIC)
    table.paste(rotated, (40, 50))
    return table.filter(ImageFilter.GaussianBlur(0.8))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "lista_pintureria_oeste.pdf").write_bytes(text_pdf(OESTE_LINES))
    picture = photo(CORRALON_LINES)
    picture.save(OUT / "foto_corralon_san_martin.jpg", quality=82)
    picture.save(OUT / "escaneo_corralon_san_martin.pdf", resolution=110)
    print(f"written to {OUT}")


if __name__ == "__main__":
    main()
