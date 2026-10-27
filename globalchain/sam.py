"""SAM 3.1 output parsing and evidence cropping.

SAM 3.1 on the Meta Model API returns one text line per frame:

    <0f>0<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..|><|mask;x=0;y=0;data=H,W,~...|>,2<|box;...|>...

Box corners are source pixels; w/h repeat the source frame size. Both tokens carry an
optional c= confidence field when include_confidence is set. We only need the
boxes to crop evidence regions, so masks are kept as opaque payloads.
"""

import base64
import hashlib
import io
import re
from dataclasses import dataclass

import httpx
from PIL import Image

_FRAME = re.compile(r"<(\d+)f>")
_RECORD = re.compile(
    r"(\d+)<\|box;x1=(-?\d+);y1=(-?\d+);x2=(-?\d+);y2=(-?\d+);w=(\d+);h=(\d+)(?:;c=[\d.]+)?\|>"
    r"<\|mask;x=0;y=0;(?:c=[\d.]+;)?data=(\d+),(\d+),([~!])(.*?)\|>"
)


@dataclass
class SamDetection:
    object_id: int
    x1: int
    y1: int
    x2: int
    y2: int
    frame_w: int
    frame_h: int

    @property
    def area(self) -> int:
        return max(0, self.x2 - self.x1) * max(0, self.y2 - self.y1)


def parse_sam_output(text: str) -> list[SamDetection]:
    """Parse every detection in SAM 3.1 output text (all frames)."""
    detections = []
    for line in text.splitlines():
        if not _FRAME.match(line.strip()):
            continue
        for m in _RECORD.finditer(line):
            oid, x1, y1, x2, y2, w, h = (int(v) for v in m.groups()[:7])
            detections.append(SamDetection(oid, x1, y1, x2, y2, w, h))
    return detections


async def load_image(url: str, client: httpx.AsyncClient) -> bytes:
    if url.startswith("data:"):
        _, _, payload = url.partition(",")
        return base64.b64decode(payload)
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def to_data_url(image_bytes: bytes, max_side: int = 1536) -> str:
    """Re-encode as JPEG (bounded size) so every model sees the same pixels we crop from."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def crop_region(image_data_url: str, det: SamDetection, pad: float = 0.12, max_side: int = 512) -> tuple[str, str]:
    """Crop a padded detection box. Returns (jpeg data URL, sha256 of the crop bytes)."""
    _, _, payload = image_data_url.partition(",")
    img = Image.open(io.BytesIO(base64.b64decode(payload))).convert("RGB")
    # SAM reports boxes in its source frame; rescale in case the sizes differ.
    sx, sy = img.width / det.frame_w, img.height / det.frame_h
    bw, bh = (det.x2 - det.x1) * sx, (det.y2 - det.y1) * sy
    left = max(0, int(det.x1 * sx - bw * pad))
    top = max(0, int(det.y1 * sy - bh * pad))
    right = min(img.width, int(det.x2 * sx + bw * pad))
    bottom = min(img.height, int(det.y2 * sy + bh * pad))
    crop = img.crop((left, top, max(right, left + 1), max(bottom, top + 1)))
    crop.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=90)
    data = buf.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode(), hashlib.sha256(data).hexdigest()
