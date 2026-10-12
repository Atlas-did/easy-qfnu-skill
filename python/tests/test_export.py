"""ICS 导出与作息时间表的单元测试（属于 jwxt export 功能）。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class TimeslotTest(unittest.TestCase):
    """官方作息表：节次 -> 时钟时间。"""

    def test_span_sections(self):
        from qfnu.timeslots import span_for_sections

        self.assertEqual(span_for_sections([1, 2]), ("08:05", "09:30"))
        self.assertEqual(span_for_sections([3, 4, 5]), ("09:50", "12:00"))
        self.assertEqual(span_for_sections([6, 7, 8]), ("14:00", "16:25"))
        self.assertEqual(span_for_sections([10, 11]), ("19:00", "20:25"))

    def test_summer_afternoon_differs(self):
        from qfnu.timeslots import span_for_sections

        self.assertEqual(span_for_sections([6, 7, 8], "summer"), ("14:30", "16:55"))
        # 上午两季相同
        self.assertEqual(span_for_sections([1, 2], "summer"), ("08:05", "09:30"))

    def test_unknown_sections(self):
        from qfnu.timeslots import span_for_sections

        self.assertIsNone(span_for_sections([99]))
        self.assertIsNone(span_for_sections([]))


class IcsExportTest(unittest.TestCase):
    """ICS 输出：结构合法、去重有效、事件数正确。"""

    def _items(self):
        return [
            {"course_name": "数学分析3", "day": "周二", "sections": [1, 2],
             "weeks": [1, 2], "room": "数学楼101", "teacher": "刘存明"},
            # 跨大节重复：教务在两个网格行重复给出同一条
            {"course_name": "数学分析3", "day": "周二", "sections": [1, 2],
             "weeks": [1, 2], "room": "数学楼101", "teacher": "刘存明"},
        ]

    def test_dedupe_removes_grid_span_duplicates(self):
        from qfnu.export import build_events, dedupe_items

        self.assertEqual(len(dedupe_items(self._items())), 1)
        self.assertEqual(len(build_events(self._items())), 2)

    def test_ics_structure_is_valid(self):
        from qfnu.export import build_events, render_ics

        text = render_ics(build_events(self._items()), calendar_name="测试")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(text.count("BEGIN:VEVENT"), text.count("END:VEVENT"))
        self.assertEqual(text.count("BEGIN:VEVENT"), 2)
        # 每行不得超过 75 字节（RFC 5545 折行）
        for line in text.encode("utf-8").split(b"\r\n"):
            self.assertLessEqual(len(line), 75)
        # 不带 TZID 的裸时间不应出现
        self.assertIn("DTSTART;TZID=Asia/Shanghai:", text)

    def test_event_times_match_official_table(self):
        from qfnu.export import build_events

        events = build_events(self._items(), semester_start="2026-09-21")
        self.assertEqual(events[0]["start"], "20260922T080500")
        self.assertEqual(events[0]["end"], "20260922T093000")
