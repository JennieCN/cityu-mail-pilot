# Manual Calendar Events (P2)

This document records the P2 manual-event slice. It adds user-authored facts to the existing month calendar. It does not register devices or send notifications.

## User behavior

- Add an all-day or timed event from the calendar header or a selected day.
- Edit, complete, reopen, or delete an event from its day. Completed manual events stay available in the collapsed “已完成（自己添加）” list.
- Manual events remain available while the independent email calendar index is building or unreadable. Mail entries retain their existing source and original-email controls; manual entries never show those controls.
- Event titles and notes render as text. Save conflicts keep the current form draft on screen.

## API and persistence

- POST /api/calendar/events accepts a per-save create_key. A replay within seven days returns the original event; reusing the key with different normalized content returns 409. Expired keys are swept globally at service startup and whenever any account creates an event. If the service has no restart and nobody creates an event, physical removal waits for the next one of those triggers.
- GET /api/calendar/events/{id}, PUT, and DELETE are scoped to the signed-in user. Mutations require expected_revision; stale updates return 409. Repeated deletion returns the tombstone without restoring text.
- Title and notes use the existing authenticated encryption helper with per-user/per-event context. Date, time, time zone, all-day flag, UTC instant, state, and revisions remain queryable metadata while an event is active. Deletion clears the content and schedule metadata, leaving only the minimal ownership, identity, revision and deletion timestamps needed for a tombstone. Account export includes active manual events; account deletion cascades their event and create-key rows.
- calendar_schema_version=1 and both additive tables are created in one SQLite transaction. P2 adds no reminder, device, delivery, or outbox table.

## Time rules

- All-day events keep their original calendar date. Timed events are filtered into the month shown in the user's current profile time zone using a UTC half-open month interval.
- The event's IANA time zone and server-derived UTC instant are stored. A daylight-saving gap is rejected. A repeated local time requires an explicit first/second occurrence. Updating a time zone/date into a repeated time also requires an explicit choice.
- Midnight is a real 00:00 timed event. All-day events cannot carry a clock time.

## Verification

Automated coverage is in pilot_app/tests/test_calendar_events.py and the manual-event cases in pilot_app/tests/test_task_calendar.py. The mother-review P2 acceptance list remains authoritative for interpreter versions, browser widths, and remaining evidence.

