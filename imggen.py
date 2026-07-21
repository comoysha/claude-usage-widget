#!/usr/bin/env python3
"""Render the menubar icon and the dropdown panel as PNGs (iOS 16 / macOS style).

Drawn at 2x for retina; SwiftBar auto-fits the menubar image to the bar height.
Colors are semantic on the usage-vs-time pace difference. No tokens involved.
"""
import glob
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent


def _find_pingfang():
    """Resolve PingFang.ttc. Modern macOS ships it as an on-demand font asset
    under AssetsV2 (hashed path), so glob for it; fall back to legacy locations."""
    candidates = glob.glob(
        "/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData/PingFang.ttc"
    ) + [
        "/System/Library/Fonts/PingFang.ttc",
        "/Library/Fonts/PingFang.ttc",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return "/System/Library/Fonts/Hiragino Sans GB.ttc"  # last-resort fallback


# fonts
F_MONO = "/System/Library/Fonts/SFNSMono.ttf"     # numbers (no jitter)
F_CJK = _find_pingfang()                           # PingFang SC = macOS system 中文字体
# PingFang.ttc face indices: SC Regular=3, SC Medium=7 (Hiragino fallback: 0/2)
_IS_PINGFANG = F_CJK.endswith("PingFang.ttc")
CJK_W3, CJK_W6 = (3, 7) if _IS_PINGFANG else (0, 2)

# palette (iOS system colors)
OK    = (52, 199, 89)    # green  跟上/有余量
AHEAD = (255, 159, 10)   # orange 超前
HOT   = (255, 59, 48)    # red    烧超前很多
TRACK = (120, 120, 128, 64)   # iOS track fill (systemFill-ish)
INK   = (60, 60, 67)     # label ink (light-mode popover)
SUBTLE = (142, 142, 147) # secondary label
# menubar-safe neutral: reads on both light and dark menu bars
MB_NEUTRAL = (150, 150, 155)
DPI = (144, 144)         # tag PNGs as @2x so SwiftBar renders at correct point size


def _font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def pace_color(used, time_pct):
    if time_pct is None:
        return SUBTLE
    d = used - time_pct
    if d > 15:
        return HOT
    if d > 5:
        return AHEAD
    return OK


def _rounded_bar(draw, box, pct, fill, track=TRACK, radius=None):
    x0, y0, x1, y1 = box
    h = y1 - y0
    r = radius if radius is not None else h // 2
    draw.rounded_rectangle(box, radius=r, fill=track)
    frac = max(0.0, min(1.0, pct / 100.0))
    fw = int((x1 - x0) * frac)
    if fw >= 2 * r:  # only draw fill wide enough to round
        draw.rounded_rectangle((x0, y0, x0 + fw, y1), radius=r, fill=fill)
    elif fw > 0:
        draw.rounded_rectangle((x0, y0, x0 + max(fw, 2), y1), radius=min(r, fw // 2 or 1), fill=fill)


def menubar_icon(used, time_pct, output="menubar.png"):
    """Two stacked rows: line1 = time%, line2 = usage(token)%. Returns PNG path.

    Saved @2x (dpi 144) so SwiftBar renders it crisp at ~22pt menubar height.
    Colors are theme-safe (read on both light and dark menu bars).
    """
    S = 2  # 2x for retina; dpi tag makes SwiftBar treat it as points
    H = 22 * S
    # label (PingFang SC) and number (SF Mono) share the same size + baseline so
    # each row reads as one horizontally-aligned line.
    fnum = _font(F_CJK, 9 * S, index=CJK_W6)
    flab = _font(F_CJK, 9 * S, index=CJK_W6)
    col = pace_color(used, time_pct)

    t_txt = "--" if time_pct is None else "%d%%" % round(time_pct)
    u_txt = "%d%%" % round(used)

    # measure to size the canvas tightly (dynamic width)
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lab_w = max(tmp.textlength("时", font=flab), tmp.textlength("用", font=flab))
    num_w = max(tmp.textlength(t_txt, font=fnum), tmp.textlength(u_txt, font=fnum))
    gap = 3 * S
    W = int(lab_w + gap + num_w + 3 * S)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lab_x = 0
    num_x = lab_w + gap
    # anchor="ls" = draw from the left-baseline, so label and number sit on the
    # SAME baseline per row (horizontal alignment). Two evenly-spaced rows.
    base1, base2 = 9 * S, 20 * S
    d.text((lab_x, base1), "时", font=flab, fill=MB_NEUTRAL, anchor="ls")
    d.text((num_x, base1), t_txt, font=fnum, fill=MB_NEUTRAL, anchor="ls")
    d.text((lab_x, base2), "用", font=flab, fill=MB_NEUTRAL, anchor="ls")
    d.text((num_x, base2), u_txt, font=fnum, fill=col, anchor="ls")

    out = HERE / output
    img.save(out, dpi=DPI)
    return out


def combined_menubar_icon(claude_five, claude_seven, codex_weekly):
    """Three compact window columns, each with time% on row one and usage% on row two."""
    S = 2
    font = _font(F_CJK, 7 * S, index=CJK_W6)
    rows = [("C5h", claude_five), ("C7d", claude_seven), ("X7d", codex_weekly)]
    W, H = 111 * S, 22 * S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Dashed separators keep the three windows visually distinct without adding
    # much weight to the compact menubar icon.
    for sep_x in (37 * S, 74 * S):
        for y in range(2 * S, 22 * S, 5 * S):
            d.line((sep_x, y, sep_x, min(y + 2 * S, 21 * S)),
                   fill=MB_NEUTRAL, width=S)
    for i, (name, stats) in enumerate(rows):
        used, elapsed = stats
        x = (3 + i * 37) * S
        elapsed_txt = "--" if elapsed is None else "%d%%" % round(elapsed)
        used_txt = "--" if used is None else "%d%%" % round(used)
        col = pace_color(used or 0, elapsed)
        d.text((x, 8 * S), name + " " + elapsed_txt, font=font, fill=MB_NEUTRAL, anchor="ls")
        d.text((x, 20 * S), name + " " + used_txt, font=font, fill=col, anchor="ls")
    out = HERE / "menubar.png"
    img.save(out, dpi=DPI)
    return out


def panel(five, seven, service=None, output="panel.png", only_weekly=False):
    """iOS-style panel with two window sections, each with 时间 / 用量 bars
    and its own pace verdict, separated by a hairline divider.

    five / seven = {"used": float|None, "time": float|None, "rem": str,
                    "ucol": (r,g,b), "verdict": str, "vcol": (r,g,b)}
    """
    S = 2
    W = 268 * S
    pad = 18 * S
    bar_h = 11 * S
    row_dy = 42 * S           # vertical distance between the two bar rows
    fw_sec = _font(F_CJK, 12 * S, index=CJK_W6)   # section header (PingFang Medium)
    fw_lab = _font(F_CJK, 12 * S, index=CJK_W3)   # 时间/用量 labels
    fw_num = _font(F_CJK, 14 * S, index=CJK_W6)   # percentages (match CJK font)
    fw_small = _font(F_CJK, 10 * S, index=CJK_W3) # countdown
    fw_verdict = _font(F_CJK, 11 * S, index=CJK_W6)  # per-window pace verdict

    # per-section vertical budget: header + two bar rows + verdict line
    sec_h = 22 * S + row_dy + 6 * S + bar_h + 20 * S
    gap = 34 * S               # space between the two sections (holds a divider)
    title_h = 28 * S if service else 0
    H = pad + 6 * S + title_h + (sec_h if only_weekly else sec_h + gap + sec_h) + pad
    img = Image.new("RGBA", (int(W), int(H)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def bar_row(y, label, pct, fill):
        # label + right-aligned number share a baseline; track sits below
        d.text((pad, y), label, font=fw_lab, fill=INK, anchor="ls")
        num = "--" if pct is None else "%d%%" % round(pct)
        d.text((W - pad, y), num, font=fw_num, fill=fill, anchor="rs")
        by = y + 6 * S
        _rounded_bar(d, (pad, by, W - pad, by + bar_h), pct or 0, fill)

    def section(y_top, title, win):
        # header: window name (left) + reset countdown (right)
        d.text((pad, y_top), title, font=fw_sec, fill=INK, anchor="ls")
        rt = "重置 " + win["rem"]
        d.text((W - pad, y_top), rt, font=fw_small, fill=SUBTLE, anchor="rs")
        b1 = y_top + 22 * S
        bar_row(b1, "时间", win["time"], SUBTLE)
        b2 = b1 + row_dy
        bar_row(b2, "用量", win["used"], win["ucol"])
        # this window's own verdict, right under its usage bar
        vy = b2 + 6 * S + bar_h + 16 * S
        d.text((pad, vy), win["verdict"], font=fw_verdict, fill=win["vcol"], anchor="ls")
        return y_top + sec_h

    y = pad + 6 * S
    if service:
        d.text((pad, y), service, font=fw_sec, fill=INK, anchor="ls")
        y += title_h
    if only_weekly:
        section(y, "7 天窗口", seven)
    else:
        y = section(y, "5 小时窗口", five)
        # hairline divider centered in the gap between the two sections
        div_y = y + gap // 2
        d.line((pad, div_y, W - pad, div_y), fill=(198, 198, 200, 255), width=S)
        section(y + gap, "7 天窗口", seven)

    # crop to actual content + margins. SwiftBar's dropdown reserves space on
    # the RIGHT of every row for the "SwiftBar ›" disclosure arrow, which makes
    # a symmetric image look shifted left. Compensate with extra left margin so
    # the content appears centered inside the popover.
    bbox = img.getbbox()
    if bbox:
        m = 6 * S            # base margin (top / right / bottom)
        m_left = 28 * S      # left margin: offsets the disclosure-arrow reserve
        x0, y0, x1, y1 = bbox
        img = img.crop((max(0, x0 - m), max(0, y0 - m),
                        min(img.width, x1 + m), min(img.height, y1 + m)))
        # widen on the left so the content sits centered in SwiftBar's popover
        pad_l = m_left - m
        if pad_l > 0:
            canvas = Image.new("RGBA", (img.width + pad_l, img.height), (0, 0, 0, 0))
            canvas.paste(img, (pad_l, 0))
            img = canvas

    out = HERE / output
    img.save(out, dpi=DPI)
    return out


if __name__ == "__main__":
    # mock preview (silent; helper module, not a SwiftBar plugin)
    menubar_icon(27, 98)
    panel({"used": 56, "time": 66, "rem": "1h42m", "ucol": pace_color(56, 66),
           "verdict": "↓ 落后 · 有余量", "vcol": pace_color(56, 66)},
          {"used": 16, "time": 5, "rem": "6d14h", "ucol": pace_color(16, 5),
           "verdict": "↑ 略超前 · 省着点", "vcol": pace_color(16, 5)})
