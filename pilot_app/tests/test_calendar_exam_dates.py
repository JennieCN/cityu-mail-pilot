"""Regression for hyphenated exam dates and a separate cancelled lecture."""
import datetime as dt
import unittest

from pilot_app import prompts, reports, taskexport


SOURCE = """Dear Class,
Date: Saturday, Oct-17
Time: 17:00 – 19:00
Location: LT-1 and LT-18. Please refer to the seat plan attached.
The midterm will cover Lecture 1-6 and Lab 1-6.
The lecture originally planned for Wednesday Oct-14 will be cancelled.
"""


class ExamDateTests(unittest.TestCase):
    def test_hyphenated_dates_share_parser(self):
        for text in ("Oct-17", "Oct – 17", "17-Oct", "October-17"):
            with self.subTest(text=text):
                self.assertEqual(reports.date_candidates(text), [(0, 0, 10, 17)])
        for text in ("May 2026", "Lecture 1-6 and Lab 1-6", "Oct-32", "Feb-30"):
            self.assertEqual(reports.date_candidates(text), [])

    def test_evidence_guard_does_not_invent_year(self):
        for text in ("2026-10-17", "2027/10/17", "2026年10月17日", "Oct-17, 2026",
                     "17 October 2026"):
            with self.subTest(text=text):
                self.assertEqual(prompts.sanitize_calendar_dates(text, SOURCE), "10月17日")
        self.assertEqual(prompts.sanitize_calendar_dates("2026-10-18", SOURCE),
                         "邮件未提供具体日期")
        self.assertEqual(prompts.sanitize_calendar_dates("2027-10-17", "Oct-17, 2026"),
                         "邮件未提供具体日期")
        self.assertEqual(prompts.sanitize_calendar_dates("2026-10-17", "17 Oct 2026"),
                         "2026-10-17")
        self.assertEqual(prompts.sanitize_calendar_dates("2027-10-17", "2026年10月17日"),
                         "邮件未提供具体日期")

    def test_cancelled_lecture_not_exam_date(self):
        self.assertEqual(reports.deadline_of(SOURCE), "10月17日 17:00–19:00")
        self.assertEqual(reports.deadline_of(
            "Attend exam on Oct-17 17:00 – 19:00; lecture on Oct-14 cancelled"),
            "10月17日 17:00–19:00")
        self.assertEqual(reports.deadline_of("10月17日参加考试；10月14日课程取消"), "10月17日")
        self.assertEqual(reports.deadline_of("Lecture on Oct-14 not cancelled; exam Oct-17"),
                         "10月17日")
        # Separate actual deadlines keep the established last-clock behaviour.
        self.assertEqual(reports.deadline_of("10月17日 12:00 前提交，最晚 23:59 截止"),
                         "10月17日 23:59")

    def test_source_to_guard_to_calendar(self):
        generated = prompts.sanitize_calendar_dates(
            "参加期中考试，2026-10-17 17:00 – 19:00，LT-1 / LT-18", SOURCE)
        deadline = reports.deadline_of(generated)
        task = {"task_key": "exam-regression", "task_day": "2026-10-05",
                "action": generated, "deadline": deadline, "priority": "high"}
        raw = taskexport.build_ics([task], today=dt.date(2026, 10, 5), timezone="Asia/Hong_Kong")
        self.assertIn("DTSTART;TZID=Asia/Hong_Kong:20261017T170000", raw)
        self.assertIn("DTEND;TZID=Asia/Hong_Kong:20261017T190000", raw)
        self.assertNotIn("20261014", raw)

    def test_no_year_uses_mail_day_not_export_day(self):
        self.assertEqual(taskexport.event_day(
            {"task_day": "2026-10-05", "deadline": "Oct-17 17:00–19:00"},
            today=dt.date(2027, 1, 1)), dt.date(2026, 10, 17))

    def test_invalid_interval_does_not_guess_next_day(self):
        self.assertIsNone(reports.clock_range("23:00–01:00"))
        self.assertIsNone(reports.clock_range("17:00–17:00"))
