"""Calendar lunar: săptămâni luni–duminică, sejururi pe intervale [sosire, plecare)."""
import calendar
from datetime import date, timedelta
from hashlib import sha256
from html import escape

PALETTE = ["#2864b4", "#a03291", "#197456", "#b05325", "#7252b8", "#ad394c"]


def property_color(property_id):
    return PALETTE[int(sha256(property_id.encode()).hexdigest()[:8], 16) % len(PALETTE)]


def month_html(year, month, properties, bookings, daily, today):
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    by_id = {p["id"]: p for p in properties}
    rates = {d["day"]: d for d in daily}
    parts = ['''<style>
    .sp-calendar{border:1px solid #cbd5e1;border-radius:8px;overflow:hidden;color:#183047;background:#fff}
    .sp-weekdays,.sp-dates,.sp-rates{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}
    .sp-weekdays div{padding:10px;text-align:center;font-weight:600;background:#f3f6fa}
    .sp-week{position:relative;border-top:1px solid #cbd5e1;min-height:130px;padding-bottom:9px;
      background:repeating-linear-gradient(to right,transparent 0,transparent calc(100% / 7 - 1px),#e2e8f0 calc(100% / 7 - 1px),#e2e8f0 calc(100% / 7))}
    .sp-date{padding:5px 9px;text-align:right;font-size:13px;background:#f8fafc;border-bottom:1px solid #e2e8f0}
    .sp-date.outside{color:#94a3b8}.sp-date.today{background:#fff4aa;font-weight:700}
    .sp-event{margin:3px 2px 0;padding:4px 7px;color:white;border-radius:3px;font-size:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;min-width:0}
    .sp-event-row{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}
    .sp-rates div{padding:5px;font-size:11px;text-align:right;color:#64748b;overflow:hidden}
    .sp-legend{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0}.sp-legend span{font-size:13px}
    </style><div class="sp-legend">''']
    for prop in properties:
        parts.append(f'<span><b style="color:{property_color(prop["id"])}">■</b> {escape(prop["name"])}</span>')
    parts.append('</div><div class="sp-calendar"><div class="sp-weekdays">')
    parts.extend(f"<div>{day}</div>" for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    parts.append("</div>")
    for week in weeks:
        parts.append('<div class="sp-week"><div class="sp-dates">')
        for day in week:
            classes = "sp-date" + (" outside" if day.month != month else "") + (" today" if day == today else "")
            label = day.strftime("%b %d") if day.day == 1 else str(day.day)
            parts.append(f'<div class="{classes}">{label}</div>')
        parts.append('</div><div class="sp-rates">')
        for day in week:
            info = rates.get(day.isoformat())
            label = f'{info["price"]:g} {info["currency"]} · ' + ("Available" if info["available"] else "Occupied / blocked") if info else ""
            parts.append(f'<div>{escape(label)}</div>')
        parts.append("</div>")
        for booking in sorted(bookings, key=lambda b: (b["check_in"], b["id"])):
            if booking["property_id"] not in by_id or booking["status"] == "cancelled":
                continue
            start, end = date.fromisoformat(booking["check_in"]), date.fromisoformat(booking["check_out"])
            left, right = max(start, week[0]), min(end, week[-1] + timedelta(days=1))
            if left >= right:
                continue
            col, span = (left-week[0]).days+1, (right-left).days
            title = f'{booking["guest_name"]} · {by_id[booking["property_id"]]["name"]} · {start} → {end}'
            label = ("‹ " if start < week[0] else "") + f'{booking["guest_name"]} ({booking["guest_count"]})' + (" ›" if end > week[-1]+timedelta(days=1) else "")
            parts.append(f'<div class="sp-event-row"><div class="sp-event" title="{escape(title, quote=True)}" style="grid-column:{col}/span {span};background:{property_color(booking["property_id"])}">{escape(label)}</div></div>')
        parts.append("</div>")
    return "".join(parts) + "</div>"
