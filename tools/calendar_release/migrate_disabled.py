"""Fixed offline migration entry point; no provider or mailbox calls."""
import json
from pilot_app.database import Database

if __name__ == '__main__':
    db = Database('/var/lib/cityu-mail-pilot/pilot.sqlite3')
    db.initialize()
    db.set_setting('notification_channel_paused', '1')
    if db.get_setting('calendar_schema_version') != '3':
        raise SystemExit('Unexpected calendar schema; keep services stopped')
    print(json.dumps({'calendar_schema_version': '3', 'notification_channel_paused': '1',
                      'recovery': db.calendar_recovery_state()['status']}))
