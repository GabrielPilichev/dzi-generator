"""QR codes as SVG, generated in memory.

One function, one input: the text to encode. Nothing is written to disk, the
database or the vault, and the SVG is self-contained (one ``<path>``, no scripts,
styles, links or remote resources). The same text always yields the same bytes.
"""

from __future__ import annotations

import io

import segno

# Level M recovers about 15% damage; segno raises it for free when the symbol
# version (its size) stays the same, which helps a projected or printed code.
QR_ERROR_CORRECTION = "m"
# Quiet zone in modules: the ISO/IEC 18004 minimum for a full-size QR code.
QR_QUIET_ZONE_MODULES = 4
QR_MODULE_PIXELS = 8


def qr_svg(text: str) -> bytes:
    """``text`` as a black-on-white QR code: a standalone UTF-8 SVG document."""
    code = segno.make(text, error=QR_ERROR_CORRECTION, boost_error=True, micro=False)
    buffer = io.BytesIO()
    code.save(
        buffer,
        kind="svg",
        scale=QR_MODULE_PIXELS,
        border=QR_QUIET_ZONE_MODULES,
        dark="#000",
        light="#fff",  # the page is dark; a scanner needs the light background
        omitsize=True,  # a viewBox instead of a fixed size: scales on screen and in print
        xmldecl=True,
        svgns=True,
        svgclass=None,
        lineclass=None,
        nl=False,
    )
    return buffer.getvalue()
