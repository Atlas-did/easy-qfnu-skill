"""只读学期课表。GET /jsxsd/xskb/xskb_list.do，不提交选课。

强智课表页的每个格子（td）里成对出现两个 div：

- ``div.kbcontent1`` 可见层：课程名 / 周次 / 教室
- ``div.kbcontent``  隐藏层：课程名 / **教师** / 周次+**精确节次** / 教室

两者 id 只差最后一段（``...-1-1`` / ``...-1-2``）。隐藏层才是信息最全的那份，
旧实现只读可见层，因此丢掉了教师和精确节次。本模块同时读取两层，
以隐藏层为字段来源、可见层为兜底。

一个格子里可能有多个课程块，块之间用连续的短横线分隔，必须拆成多个 item。
"""

import html
import re
from urllib.parse import urlencode

from . import trace
from .jwxt_auth import contains_any, strip_tags
from .jwxt_client import SCHEDULE_URL, JWXTError
from .jwxt_html import LOGIN_MARKERS, parse_table_html
from .result import success

KBTABLE_RE = re.compile(r'(?is)<table\b[^>]*\bid=["\']kbtable["\'][^>]*>(.*?)</table\s*>')
DIV_RE = re.compile(r"(?is)<div\b([^>]*)>(.*?)</div\s*>")
WEEKDAY_LABELS = (
    ("星期一", "周一"),
    ("星期二", "周二"),
    ("星期三", "周三"),
    ("星期四", "周四"),
    ("星期五", "周五"),
    ("星期六", "周六"),
    ("星期日", "周日"),
    ("星期天", "周日"),
    ("周一", "周一"),
    ("周二", "周二"),
    ("周三", "周三"),
    ("周四", "周四"),
    ("周五", "周五"),
    ("周六", "周六"),
    ("周日", "周日"),
)

# 分块分隔符：教务在同一个格子里用连续短横线分隔多门课
SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$")

# 周次(节次) 字段：1-16(周)、1-11(周)[10-11节]、1-16(周)[07节]、1-16(周)[06-07-08节]
WEEK_FIELD_RE = re.compile(r"([\d,\-]+)\s*\(\s*周\s*\)")
SECTION_FIELD_RE = re.compile(r"\[\s*([\d\-]+)\s*节\s*\]")
PERIOD_RANGE_RE = re.compile(r"第\s*(\d+)\s*[~～\-—]\s*(\d+)\s*节")
PERIOD_SINGLE_RE = re.compile(r"第\s*(\d+)\s*节")

# 隐藏层里带 title 的字段
TITLE_FIELD_RE = re.compile(r"(?is)<font\b[^>]*title=['\"]([^'\"]*)['\"][^>]*>(.*?)</font\s*>")


def kbtable_html(raw):
    match = KBTABLE_RE.search(raw)
    if match is None:
        return raw
    return match.group(0)


def weekday_name(text):
    for label, normalized in WEEKDAY_LABELS:
        if label in text:
            return normalized
    return ""


def clean_text(text):
    """去标签、unescape 实体、清掉 nbsp 与多余空白。"""
    value = strip_tags(text or "")
    value = html.unescape(value)
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


def is_empty_cell(text):
    cleaned = clean_text(text)
    return cleaned == "" or cleaned == "-"


def title_fields(inner):
    """取隐藏层里 ``<font title='老师'>X</font>`` 这类键值对。

    注意：**只对单个课程块调用**。一个格子里有多门课时，同一个 title
    会出现多次，用 dict 收集会互相覆盖，必须先把块切开再取字段。
    """
    fields = {}
    for title, body in TITLE_FIELD_RE.findall(inner or ""):
        key = html.unescape(title or "").strip()
        value = clean_text(body)
        if key and value:
            fields[key] = value
    return fields


def kbcontent_blocks(cell_html):
    """按出现顺序取出格子里的课程块（含隐藏层），保持可见/隐藏配对。

    返回 ``[{"visible": str, "hidden": str}, ...]``。
    """
    blocks = []
    for attrs, inner in DIV_RE.findall(cell_html):
        lower = attrs.lower().replace(" ", "")
        if "kbcontent" not in lower:
            continue
        is_hidden_layer = "kbcontent1" not in lower
        if is_empty_cell(inner):
            continue
        blocks.append({"hidden" if is_hidden_layer else "visible": inner})
    return blocks


BR_RE = re.compile(r"(?i)<br\s*/?>")


def cell_lines_raw(cell_html):
    """把 ``<br>`` 当成换行切分，再逐个去标签。

    必须在 strip_tags 之前切 —— strip_tags 会把 ``<br/>`` 压成空格，
    之后再 splitlines 就只剩一行，多课程块会被粘连成一条。
    """
    lines = []
    for raw_line in BR_RE.split(cell_html or ""):
        text = clean_text(raw_line)
        if text:
            lines.append(text)
    return lines


def split_segments(lines):
    """把行列表按连续短横线切块，并丢掉分隔符本身。"""
    segments = []
    current = []
    for line in lines:
        if SEPARATOR_RE.match(line):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(line)
    if current:
        segments.append(current)
    return segments


def parse_weeks(text):
    """``1-16(周)`` / ``1-2,5-16(周)`` -> ``[1..16]``（升序去重）。"""
    match = WEEK_FIELD_RE.search(text or "")
    if match is None:
        return []
    weeks = set()
    for chunk in match.group(1).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            bounds = chunk.split("-", 1)
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for week in range(start, end + 1):
                weeks.add(week)
        else:
            try:
                weeks.add(int(chunk))
            except ValueError:
                continue
    return sorted(weeks)


def parse_sections(text):
    """``[01-02节]`` -> ``[1, 2]``；``[05节]`` -> ``[5]``；``[06-07-08节]`` -> ``[6,7,8]``。"""
    match = SECTION_FIELD_RE.search(text or "")
    if match is None:
        return []
    sections = []
    for chunk in match.group(1).split("-"):
        chunk = chunk.strip()
        if chunk.isdigit():
            sections.append(int(chunk))
    return sections


def parse_period_range(period_text):
    """表头节次 ``第3~5节`` -> ``(3, 5)``；解析不出返回 ``(None, None)``。"""
    text = clean_text(period_text)
    match = PERIOD_RANGE_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = PERIOD_SINGLE_RE.search(text)
    if match:
        value = int(match.group(1))
        return value, value
    return None, None


def course_name_of(block_html, lines):
    """提取课程名。

    两种排版都要支持：

    1. QFNU 等学校用 ``<font title='老师'>`` 标注字段::

           数学分析3<br/><font title='老师'>刘存明</font><br/>...

       此时课程名是 ``<font>`` 之外的裸文本。

    2. 另一种排版是纯文本逐行::

           大学英语<br>张三<br>教1-101

       此时课程名就是第一行。

    做法：先按 ``<br>`` 切成行，再逐行判定。带 ``title`` 的行属于字段，
    直接跳过；第一行非字段文本即为课程名。
    """
    # 每个 <br> 行单独取文本；带 font title 的行标记为字段行
    rows = []
    for raw_line in BR_RE.split(block_html or ""):
        has_title = bool(re.search(r"(?is)<font\b[^>]*title=", raw_line or ""))
        text = clean_text(raw_line)
        rows.append((text, has_title))
    if not rows:
        rows = [(clean_text(line), False) for line in (lines or [])]

    for text, has_title in rows:
        if not text:
            continue
        if has_title:
            continue
        if WEEK_FIELD_RE.search(text) or SECTION_FIELD_RE.search(text):
            continue
        if text.startswith("(") or text.startswith("（"):
            continue
        return text
    for text, _has_title in rows:
        if text:
            return text
    return lines[0] if lines else ""


def course_from_segment(lines, hidden_fields, day, period, row_sections, block_html=""):
    """把一个文本块整理成课程条目。"""
    if not lines:
        return None
    name = course_name_of(block_html, lines)
    if not name:
        return None
    if weekday_name(name) and len(lines) == 1:
        return None

    joined = " ".join(lines)

    # 按 <br> 切开的原始行，用于纯文本排版的兜底识别
    text_rows = []
    for raw_line in BR_RE.split(block_html or ""):
        text = clean_text(raw_line)
        if text:
            text_rows.append(text)
    if not text_rows:
        text_rows = list(lines)

    # 周次：优先隐藏层字段，其次可见层文本
    weeks = parse_weeks(hidden_fields.get("周次(节次)", "")) or parse_weeks(joined)

    # 节次：优先隐藏层 [xx节]，否则用表头区间
    sections = parse_sections(hidden_fields.get("周次(节次)", ""))
    if not sections:
        sections = parse_sections(joined)
    if not sections and row_sections[0] is not None:
        sections = list(range(row_sections[0], row_sections[1] + 1))

    # 教师：优先隐藏层字段
    teacher = hidden_fields.get("老师", "") or hidden_fields.get("教师", "")

    # 教室：优先隐藏层，其次在纯文本行里找
    room = hidden_fields.get("教室", "") or hidden_fields.get("地点", "")
    if not room:
        for line in text_rows:
            if line == name:
                continue
            if WEEK_FIELD_RE.search(line) or SECTION_FIELD_RE.search(line):
                continue
            if line.startswith("(") or line.startswith("（"):
                continue
            if teacher and line == teacher:
                continue
            room = line
            break

    # 纯文本排版下没有 title 字段：课程名后的第 1 行通常是教师，第 2 行是教室
    if not teacher and len(text_rows) >= 2:
        candidate = text_rows[1]
        if candidate != name and not WEEK_FIELD_RE.search(candidate):
            teacher = candidate
    if not room and len(text_rows) >= 3:
        candidate = text_rows[2]
        if not WEEK_FIELD_RE.search(candidate):
            room = candidate

    return {
        "day": day,
        "period": clean_text(period),
        "course_name": name,
        "teacher": teacher,
        "room": room,
        "weeks": weeks,
        "sections": sections,
        "start_section": sections[0] if sections else None,
        "end_section": sections[-1] if sections else None,
        "lines": lines,
        "text": joined,
    }


def split_blocks_html(inner):
    """把一个 div 的 HTML 按连续短横线切成多个课程块（保持 HTML 结构）。

    必须在提取 title 字段**之前**切：否则同一格里的多门课会共用一份
    ``title_fields`` 字典，后一个块会把前一个块的教室/节次覆盖掉。
    """
    blocks = []
    current = []
    for raw_line in BR_RE.split(inner or ""):
        text = clean_text(raw_line)
        if SEPARATOR_RE.match(text):
            if current:
                blocks.append("<br/>".join(current))
                current = []
            continue
        current.append(raw_line)
    if current:
        blocks.append("<br/>".join(current))
    return [block for block in blocks if clean_text(block)]


def cell_courses(cell_html, day, period, row_sections):
    """解析一个 td，返回该格里的全部课程（可能多门）。"""
    blocks = kbcontent_blocks(cell_html)
    if not blocks:
        text = clean_text(cell_html)
        if is_empty_cell(text):
            return []
        blocks = [{"visible": cell_html}]

    # 可见层与隐藏层按顺序配对；隐藏层信息更全
    pairs = []
    pending_visible = None
    for block in blocks:
        if "visible" in block:
            if pending_visible is not None:
                pairs.append((pending_visible, None))
            pending_visible = block["visible"]
        else:
            pairs.append((pending_visible, block["hidden"]))
            pending_visible = None
    if pending_visible is not None:
        pairs.append((pending_visible, None))

    courses = []
    for visible, hidden in pairs:
        source = hidden if hidden else visible
        # 先按分隔符切成独立课程块，再逐块提取字段，避免字段互相覆盖
        for sub_html in split_blocks_html(source or ""):
            fields = title_fields(sub_html)
            segment = cell_lines_raw(sub_html)
            if not segment:
                continue
            course = course_from_segment(segment, fields, day, period, row_sections, sub_html)
            if course is not None:
                courses.append(course)
    return courses


def header_days(header_row):
    days = []
    for cell in header_row:
        days.append(weekday_name(strip_tags(cell)))
    return days


def parse_schedule(raw):
    rows = parse_table_html(kbtable_html(raw))
    if not rows:
        return rows, []
    days = header_days(rows[0])
    items = []
    if not any(days):
        return rows, items
    for row in rows[1:]:
        period = strip_tags(row[0]) if row else ""
        if weekday_name(period):
            continue
        row_sections = parse_period_range(period)
        for index in range(1, len(row)):
            day = days[index] if index < len(days) else ""
            if day == "":
                continue
            items.extend(cell_courses(row[index], day, period, row_sections))
    return rows, items


def schedule_url(semester, week, mode):
    params = {"sfFD": "1"}
    if semester:
        params["xnxq01id"] = semester
    if week:
        params["zc"] = week
    if mode:
        params["kbjcmsid"] = mode
    return SCHEDULE_URL + "?" + urlencode(params)


def schedule(client, semester, week, mode):
    semester = (semester or "").strip()
    week = (week or "").strip()
    mode = (mode or "").strip()
    target = schedule_url(semester, week, mode)
    status, final_url, raw = client.text("GET", target)
    if status != 200 or contains_any(raw, LOGIN_MARKERS):
        raise JWXTError(
            "schedule page requires login",
            "run easy-qfnu jwxt status or login again",
        )
    rows, items = parse_schedule(raw)
    if not items:
        trace.note("schedule table: " + str(len(rows)) + " rows, 0 data cells")
    return success(
        "jwxt",
        {
            "semester": semester,
            "week": week,
            "kbjcmsid": mode,
            "count": len(items),
            "items": items,
            "schedule": items,
            "url": final_url,
            "session_path": client.session_path,
        },
    )
