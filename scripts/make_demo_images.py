"""Draw placeholder listing photos for the demo. Replace them with real photos before recording."""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "globalchain" / "static" / "demo"

BAGS = {
    "tabby-a-1": ("#6b4a3a", "#c9a45c", "COACH", True),
    "tabby-a-2": ("#6b4a3a", "#c9a45c", "COACH", True),
    "tabby-b-1": ("#5a4636", "#d8c27a", "C0ACH", False),
    "tabby-b-2": ("#5a4636", "#d8c27a", "C0ACH", False),
    "jetset-c-1": ("#4a3528", "#c0c0c0", "MK", True),
    "tabby-d-1": ("#2f2a28", "#c9a45c", "COACH", True),
}


def draw(name: str, body: str, metal: str, text: str, neat: bool) -> None:
    img = Image.new("RGB", (900, 900), "#efe9e1")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((150, 300, 750, 760), radius=40, fill=body)  # bag body
    d.arc((300, 120, 600, 420), 180, 360, fill=body, width=26)  # handle
    d.rounded_rectangle((370, 420, 530, 500), radius=14, fill=metal)  # clasp
    d.text((420, 448), text, fill="#3a2a1e")
    d.rectangle((560, 600, 700, 690), fill="#8a6a50", outline="#3a2a1e", width=3)  # creed patch
    d.text((575, 630), f"{text} No. 1941", fill="#efe2cc")
    for i, x in enumerate(range(170, 740, 18)):  # stitching along the top edge
        jitter = 0 if neat else (i * 7) % 9 - 4
        d.line((x, 322 + jitter, x + 10 + jitter, 322 + jitter), fill="#e8d9c0", width=3)
    img.save(OUT / f"{name}.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, spec in BAGS.items():
        draw(name, *spec)
    print(f"wrote {len(BAGS)} images to {OUT}")
