"""把教务课表导出为 ICS 日历文件。

只读：复用 jwxt schedule 的解析结果，不碰任何选课或表单提交。

为什么逐周展开而不是用 RRULE：
教务的周次经常是断续的（1-8 周、1-16 周，还可能跨假期），且不同节次的课
周次不同。逐周生成独立 VEVENT 不需要日历端理解"教学周"，导入后所见即所得，
也避免 RRULE 在调休、停课时的错位。
"""

from .jwxt_schedule import schedule as fetch_schedule
from .timeslots import span_for_sections
from .result import success

WEEKDAY_INDEX = {
    "周一": 1,
    "周二": 2,
    "周三": 3,
    "周四": 4,
    "周五": 5,
    "周六": 6,
    "周日": 7,
}

DEFAULT_WEEKS = 20
DEFAULT_START = "2026-09-21"


def escape_text(value):
    """ICS 文本转义。"""
    text = str(value if value is not None else "")
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n")
    return text


def fold_line(line):
    """按 RFC 5545 折行：单行不超过 75 字节，续行以空格开头。"""
    if len(line.encode("utf-8")) <= 75:
        return line
    pieces = []
    current = b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > 74:
            pieces.append(current.decode("utf-8"))
            current = b" " + encoded
        else:
            current += encoded
    if current:
        pieces.append(current.decode("utf-8"))
    return "\r\n".join(pieces)


def date_add_days(iso_date, days):
    """在 ISO 日期上加减天数。"""
    import datetime

    base = datetime.date.fromisoformat(iso_date)
    return (base + datetime.timedelta(days=days)).isoformat()


def dedupe_items(items):
    """去掉课表网格重复给出的同一课程块。

    教务把跨大节的课在多个网格行重复列出（例如 [06-07-08节] 同时落在
    "第6~7节"与"第8~9节"两行），不去重会在日历里出现重复事件。
    """
    seen = set()
    result = []
    for item in items:
        key = (
            item.get("course_name", ""),
            item.get("day", ""),
            tuple(item.get("sections") or []),
            tuple(item.get("weeks") or []),
            item.get("room", ""),
            item.get("teacher", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def build_events(items, semester_start=DEFAULT_START, season="winter", total_weeks=DEFAULT_WEEKS):
    """课表条目 -> 事件列表（已按周展开并排序）。"""
    events = []
    for item in dedupe_items(items):
        weekday = WEEKDAY_INDEX.get(item.get("day", ""))
        if not weekday:
            continue
        sections = item.get("sections") or []
        span = span_for_sections(sections, season)
        if span is None:
            continue
        start_clock, end_clock = span
        weeks = item.get("weeks") or list(range(1, total_weeks + 1))
        for week in weeks:
            day = date_add_days(semester_start, (week - 1) * 7 + (weekday - 1))
            compact = day.replace("-", "")
            start = compact + "T" + start_clock.replace(":", "") + "00"
            end = compact + "T" + end_clock.replace(":", "") + "00"
            parts = []
            if item.get("teacher"):
                parts.append("教师：" + item["teacher"])
            parts.append("第 " + str(week) + " 周")
            if sections:
                parts.append("第 " + ",".join(str(value) for value in sections) + " 节")
            events.append(
                {
                    "uid": compact + "-" + start_clock.replace(":", "") + "-"
                    + str(abs(hash(item.get("course_name", "") + item.get("room", ""))) % (10 ** 12))
                    + "@easy-qfnu",
                    "start": start,
                    "end": end,
                    "summary": item.get("course_name", ""),
                    "location": item.get("room", ""),
                    "description": " ".join(parts),
                    "week": week,
                }
            )
    events.sort(key=lambda event: event["start"])
    return events


def render_ics(events, calendar_name="QFNU 课表"):
    """事件列表 -> ICS 文本。"""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//easy-qfnu-skill//QFNU Schedule//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + escape_text(calendar_name),
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for event in events:
        lines.extend(
            [
                "BEGIN:VEVENT",
                "UID:" + event["uid"],
                "DTSTAMP:" + event["start"] + "Z",
                "DTSTART;TZID=Asia/Shanghai:" + event["start"],
                "DTEND;TZID=Asia/Shanghai:" + event["end"],
                "SUMMARY:" + escape_text(event["summary"]),
                "LOCATION:" + escape_text(event["location"]),
                "DESCRIPTION:" + escape_text(event["description"]),
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


def export_ics(client, semester, week, mode, output, semester_start, season, name):
    """取课表并写出 ICS 文件。"""
    result = fetch_schedule(client, semester, week, mode)
    if not result.get("ok"):
        return result
    items = result.get("items") or []
    events = build_events(items, semester_start=semester_start, season=season)
    text = render_ics(events, calendar_name=name or "QFNU 课表")
    path = output
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except OSError as exc:
        return {
            "ok": False,
            "source": "jwxt",
            "error": "cannot write ics: " + str(exc),
            "hint": "检查输出路径是否可写",
        }
    return success(
        "jwxt",
        {
            "semester": result.get("semester", ""),
            "week": result.get("week", ""),
            "season": season,
            "semester_start": semester_start,
            "course_blocks": len(dedupe_items(items)),
            "events": len(events),
            "path": path,
            "url": result.get("url", ""),
        },
    )
