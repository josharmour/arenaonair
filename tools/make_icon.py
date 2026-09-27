"""Render the ArenaOnAir app icon: an on-air broadcast mic in front of a fanned pair of cards.

~/.venvs/arenaonair/bin/python tools/make_icon.py [--out PATH] [--size N]
Drawn with QPainter (ui extra) on Apple's icon grid: an 824 px continuous-corner
body on a 1024 px canvas, so the macOS Dock shows it without a backing plate.
Writes src/arenaonair/assets/icon.png; `arenaonair install-app` builds the .icns.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QRadialGradient, QTransform)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src" / "arenaonair" / "assets" / "icon.png"


def squircle(rect: QRectF, r: float) -> QPainterPath:
    """Apple's continuous-corner rounded rect (the iOS 7 / Big Sur icon shape)."""
    # Offsets from each corner in units of r, walking from the edge into the corner.
    a, b, c, d, e, f = 1.52866483, 1.08849323, 0.86840689, 0.63149399, 0.07491100, 0.37282392
    g = 0.16906001
    x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
    p = QPainterPath(QPointF(x0 + a * r, y0))
    corners = (  # (corner point, x direction into the rect, y direction into the rect)
        ((x1, y0), -1, 1), ((x1, y1), -1, -1), ((x0, y1), 1, -1), ((x0, y0), 1, 1))
    for (cx, cy), sx, sy in corners:
        # Horizontal-edge corners (top-right, bottom-left) walk x first, then y.
        horizontal_first = (sx, sy) in ((-1, 1), (1, -1))

        def pt(u, v):
            if horizontal_first:
                return QPointF(cx + sx * u * r, cy + sy * v * r)
            return QPointF(cx + sx * v * r, cy + sy * u * r)
        p.lineTo(pt(a, 0))
        p.cubicTo(pt(b, 0), pt(c, 0), pt(d, e))
        p.cubicTo(pt(f, g), pt(g, f), pt(e, d))
        p.cubicTo(pt(0, c), pt(0, b), pt(0, a))
    p.closeSubpath()
    return p


def draw_card(p: QPainter, center: QPointF, angle: float, face: tuple[str, str], w=286.0, h=400.0):
    p.save()
    p.translate(center)
    p.rotate(angle)
    body = QRectF(-w / 2, -h / 2, w, h)
    # Soft contact shadow so the fanned cards read as separate objects.
    p.setPen(Qt.NoPen)
    for i in range(8):
        p.setBrush(QColor(0, 0, 0, 10))
        p.drawRoundedRect(body.adjusted(-i * 2, -i * 2 + 10, i * 2, i * 2 + 10), 26 + i, 26 + i)
    p.setBrush(QColor("#0d0b12"))
    p.drawRoundedRect(body, 24, 24)
    frame = body.adjusted(12, 12, -12, -12)
    gold = QLinearGradient(frame.topLeft(), frame.bottomRight())
    gold.setColorAt(0, QColor("#f7df9a"))
    gold.setColorAt(0.45, QColor("#d4a24c"))
    gold.setColorAt(1, QColor("#8a5a1c"))
    p.setBrush(gold)
    p.drawRoundedRect(frame, 14, 14)
    inner = frame.adjusted(10, 10, -10, -10)
    face_grad = QLinearGradient(inner.topLeft(), inner.bottomLeft())
    face_grad.setColorAt(0, QColor(face[0]))
    face_grad.setColorAt(1, QColor(face[1]))
    p.setBrush(face_grad)
    p.drawRoundedRect(inner, 8, 8)
    # Art window and text box, like a card frame.
    art = QRectF(inner.left() + 14, inner.top() + 44, inner.width() - 28, inner.height() * 0.46)
    p.setBrush(QColor(255, 255, 255, 46))
    p.drawRoundedRect(art, 6, 6)
    title = QRectF(inner.left() + 14, inner.top() + 12, inner.width() - 28, 22)
    p.setBrush(QColor(255, 255, 255, 70))
    p.drawRoundedRect(title, 5, 5)
    text = QRectF(inner.left() + 14, art.bottom() + 16, inner.width() - 28, inner.bottom() - art.bottom() - 30)
    p.setBrush(QColor(255, 255, 255, 38))
    p.drawRoundedRect(text, 6, 6)
    p.restore()


def draw_mic(p: QPainter, cx: float):
    head = QRectF(cx - 104, 262, 208, 318)
    # Stand: yoke arms, stem and weighted base.
    p.setPen(Qt.NoPen)
    metal = QLinearGradient(cx - 130, 0, cx + 130, 0)
    for t, col in ((0, "#5b6072"), (0.25, "#e9ebf2"), (0.5, "#a4a9b8"), (0.8, "#f6f7fb"), (1, "#5b6072")):
        metal.setColorAt(t, QColor(col))
    yoke = QPainterPath()
    yoke.moveTo(cx - 146, 420)
    yoke.cubicTo(cx - 146, 640, cx + 146, 640, cx + 146, 420)
    stroke = QPen(QBrush(metal), 30, Qt.SolidLine, Qt.RoundCap)
    p.setPen(stroke)
    p.setBrush(Qt.NoBrush)
    p.drawPath(yoke)
    p.setPen(Qt.NoPen)
    p.setBrush(metal)
    p.drawRoundedRect(QRectF(cx - 20, 580, 40, 160), 14, 14)
    base_shadow = QRadialGradient(QPointF(cx, 770), 190)
    base_shadow.setColorAt(0, QColor(0, 0, 0, 120))
    base_shadow.setColorAt(1, QColor(0, 0, 0, 0))
    p.setBrush(base_shadow)
    p.drawEllipse(QPointF(cx, 772), 200, 46)
    p.setBrush(metal)
    p.drawRoundedRect(QRectF(cx - 150, 728, 300, 40), 20, 20)
    # Head: a chrome capsule with a grille and a gold band.
    shadow = QRadialGradient(QPointF(cx, head.center().y() + 20), 220)
    shadow.setColorAt(0, QColor(0, 0, 0, 90))
    shadow.setColorAt(1, QColor(0, 0, 0, 0))
    p.setBrush(shadow)
    p.drawEllipse(QPointF(cx, head.center().y() + 24), 170, 210)
    chrome = QLinearGradient(head.left(), 0, head.right(), 0)
    for t, col in ((0, "#474c5e"), (0.18, "#dfe2ea"), (0.36, "#ffffff"), (0.62, "#9da3b4"),
                   (0.86, "#e7e9f0"), (1, "#474c5e")):
        chrome.setColorAt(t, QColor(col))
    capsule = QPainterPath()
    capsule.addRoundedRect(head, head.width() / 2, head.width() / 2)
    p.setBrush(chrome)
    p.drawPath(capsule)
    p.save()
    p.setClipPath(capsule)
    grille = QColor(40, 44, 58, 120)
    p.setBrush(grille)
    y = head.top() + 30
    while y < head.bottom() - 20:
        p.drawRoundedRect(QRectF(head.left() - 4, y, head.width() + 8, 9), 4, 4)
        y += 22
    band = QLinearGradient(head.left(), 0, head.right(), 0)
    for t, col in ((0, "#7a4a12"), (0.3, "#ffe39a"), (0.55, "#d9a441"), (0.85, "#fff0c2"), (1, "#7a4a12")):
        band.setColorAt(t, QColor(col))
    p.setBrush(band)
    p.drawRect(QRectF(head.left() - 2, head.center().y() + 8, head.width() + 4, 34))
    # Specular highlight down the left of the capsule.
    gloss = QLinearGradient(head.left(), 0, head.left() + 80, 0)
    gloss.setColorAt(0, QColor(255, 255, 255, 0))
    gloss.setColorAt(0.5, QColor(255, 255, 255, 90))
    gloss.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(gloss)
    p.drawRoundedRect(QRectF(head.left() + 26, head.top() + 26, 44, head.height() - 60), 22, 22)
    p.restore()
    p.setPen(QPen(QColor(20, 22, 32, 140), 3))
    p.setBrush(Qt.NoBrush)
    p.drawPath(capsule)
    # Pivot knobs where the yoke meets the head.
    p.setPen(Qt.NoPen)
    for side in (-1, 1):
        knob = QRadialGradient(QPointF(cx + side * 140, 414), 30)
        knob.setColorAt(0, QColor("#ffffff"))
        knob.setColorAt(0.5, QColor("#b8bcc9"))
        knob.setColorAt(1, QColor("#4b5063"))
        p.setBrush(knob)
        p.drawEllipse(QPointF(cx + side * 140, 420), 26, 26)
    return head


def draw_on_air(p: QPainter, cx: float):
    """The lit ON AIR sign above the mic."""
    sign = QRectF(cx - 150, 128, 300, 92)
    halo = QRadialGradient(sign.center(), 230)
    halo.setColorAt(0, QColor(255, 60, 50, 150))
    halo.setColorAt(1, QColor(255, 40, 40, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(halo)
    p.drawEllipse(sign.center(), 240, 150)
    p.setBrush(QColor("#3a0907"))
    p.drawRoundedRect(sign.adjusted(-8, -8, 8, 8), 30, 30)
    lamp = QLinearGradient(sign.topLeft(), sign.bottomLeft())
    lamp.setColorAt(0, QColor("#ff8a7a"))
    lamp.setColorAt(0.45, QColor("#f2332a"))
    lamp.setColorAt(1, QColor("#a9120c"))
    p.setBrush(lamp)
    p.drawRoundedRect(sign, 24, 24)
    font = QFont("Avenir Next Condensed")
    font.setPixelSize(64)
    font.setWeight(QFont.Black)
    font.setLetterSpacing(QFont.AbsoluteSpacing, 4)
    p.setFont(font)
    p.setPen(QColor(90, 0, 0, 150))
    p.drawText(sign.translated(0, 4), Qt.AlignCenter, "ON AIR")
    p.setPen(QColor("#fff4ec"))
    p.drawText(sign, Qt.AlignCenter, "ON AIR")


def render(size: int = 1024) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    p.setTransform(QTransform.fromScale(size / 1024, size / 1024))
    body_rect = QRectF(100, 100, 824, 824)
    body = squircle(body_rect, 185.4)
    # Apple-grid drop shadow, built from stacked translucent bodies.
    p.setPen(Qt.NoPen)
    for i in range(10):
        p.setBrush(QColor(0, 0, 0, 5))
        p.drawPath(squircle(body_rect.adjusted(-i, -i + 10, i, i + 10), 185.4 + i))
    bg = QLinearGradient(0, 100, 0, 924)
    bg.setColorAt(0, QColor("#2c2a6e"))
    bg.setColorAt(0.55, QColor("#17153f"))
    bg.setColorAt(1, QColor("#0b0a1f"))
    p.setBrush(bg)
    p.drawPath(body)
    p.save()
    p.setClipPath(body)
    glow = QRadialGradient(QPointF(512, 470), 420)
    glow.setColorAt(0, QColor(255, 138, 61, 120))
    glow.setColorAt(0.55, QColor(255, 110, 40, 30))
    glow.setColorAt(1, QColor(255, 110, 40, 0))
    p.setBrush(glow)
    p.drawRect(body_rect)
    p.translate(0, 30)  # optical centre: the sign's glow carries visual weight up top
    draw_card(p, QPointF(372, 470), -17, ("#e2553b", "#8f1f1a"))
    draw_card(p, QPointF(652, 470), 17, ("#3b82e2", "#1b3f8f"))
    draw_mic(p, 512)
    draw_on_air(p, 512)
    p.translate(0, -30)
    # Top sheen and a hairline edge, as on Apple's own icons.
    sheen = QLinearGradient(0, 100, 0, 520)
    sheen.setColorAt(0, QColor(255, 255, 255, 36))
    sheen.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(sheen)
    p.drawRect(body_rect)
    p.restore()
    p.setPen(QPen(QColor(255, 255, 255, 40), 2))
    p.setBrush(Qt.NoBrush)
    p.drawPath(squircle(body_rect.adjusted(1, 1, -1, -1), 184.4))
    p.end()
    return image


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--size", type=int, default=1024)
    args = parser.parse_args(argv)
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 (QPainter needs one)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if not render(args.size).save(str(args.out), "PNG"):
        print(f"could not write {args.out}")
        return 1
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
