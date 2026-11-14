const encoder = new TextEncoder();
const instant = (value) => new Date(value).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
const day = (value) => new Date(value).toISOString().slice(0, 10).replaceAll("-", "");
const escape = (value) => String(value ?? "").replace(/\\/g, "\\\\")
  .replace(/\r\n|\n|\r/g, "\\n").replace(/[;,]/g, "\\$&");
function fold(line) {
  let result = "", width = 0;
  for (const char of line) {
    const size = encoder.encode(char).length;
    if (width + size > 75) { result += "\r\n "; width = 1; }
    result += char;
    width += size;
  }
  return result;
}

export function calendarEvents(item, now) {
  if (item.archived) return [];
  if (Number.isFinite(item.startDay) && Number.isFinite(item.endDay) && item.end > now) {
    const next = new Date(item.endDay);
    next.setUTCDate(next.getUTCDate() + 1);
    return [{ start: day(item.startDay), end: day(next), allDay: true }];
  }
  const windows = item.windows?.length ? item.windows : [item];
  return windows.filter(({ start, end }) => Number.isFinite(start)
    && (Number.isFinite(end) ? end > now && end > start : start > now && !item.kind))
    .map(({ start, end }) => ({ start: instant(start), end: Number.isFinite(end) ? instant(end) : null }));
}

export function buildCalendar(item, now = Date.now()) {
  const events = calendarEvents(item, now);
  if (!events.length) return null;
  return ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Relocant Media//Belgrade Map//RU",
    "CALSCALE:GREGORIAN", ...events.flatMap(({ start, end, allDay }) => {
      const type = allDay ? ";VALUE=DATE" : "";
      return ["BEGIN:VEVENT", "UID:" + item.id + "-" + start + "@relocant-media",
        "DTSTAMP:" + instant(now), "DTSTART" + type + ":" + start,
        ...(end ? ["DTEND" + type + ":" + end] : []),
        "SUMMARY:" + escape(item.title), "DESCRIPTION:" + escape(item.reportedScope || item.venue || ""),
        "URL:" + item.source, "TRANSP:TRANSPARENT", "BEGIN:VALARM", "TRIGGER:-P1D",
        "ACTION:DISPLAY", "DESCRIPTION:" + escape(item.title), "END:VALARM", "END:VEVENT"];
    }), "END:VCALENDAR", ""].map(fold).join("\r\n");
}

export function calendarLink(item, now) {
  const contents = buildCalendar(item, now);
  if (!contents) return null;
  const a = document.createElement("a");
  a.href = "#";
  a.textContent = "В календарь ↓";
  a.addEventListener("click", (event) => {
    event.preventDefault();
    const file = new File([contents], "belgrade.ics", { type: "text/calendar" });
    if (navigator.canShare?.({ files: [file] })) {
      navigator.share({ files: [file], title: item.title }).catch((error) => {
        if (error.name !== "AbortError") download(file);
      });
    } else {
      download(file);
    }
  });
  return a;
}

function download(file) {
  const anchor = document.createElement("a");
  const url = URL.createObjectURL(file);
  anchor.href = url;
  anchor.download = file.name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
