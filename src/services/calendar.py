"""
Calendar/ICS file generation services.
"""

import json
import uuid
from datetime import datetime, timedelta


def generate_ics_content(event):
    """Generate ICS calendar file content for an event."""
    import uuid
    from datetime import datetime, timedelta

    # Generate unique ID for the event
    uid = f"{event.id}-{uuid.uuid4()}@speakr.app"

    # Format dates in iCalendar format (YYYYMMDDTHHMMSS)
    def format_ical_date(dt):
        if dt:
            return dt.strftime('%Y%m%dT%H%M%S')
        return None

    # Start building ICS content
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//Speakr//Event Export//EN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'BEGIN:VEVENT',
        f'UID:{uid}',
        f'DTSTAMP:{format_ical_date(datetime.utcnow())}Z',  # UTC, as RFC 5545 requires
    ]

    # Add event details
    if event.start_datetime:
        lines.append(f'DTSTART:{format_ical_date(event.start_datetime)}')

    if event.end_datetime:
        lines.append(f'DTEND:{format_ical_date(event.end_datetime)}')
    elif event.start_datetime:
        # If no end time, default to 1 hour after start
        end_time = event.start_datetime + timedelta(hours=1)
        lines.append(f'DTEND:{format_ical_date(end_time)}')

    # Add title and description
    lines.append(f'SUMMARY:{escape_ical_text(event.title)}')

    if event.description:
        lines.append(f'DESCRIPTION:{escape_ical_text(event.description)}')

    # Add location if available
    if event.location:
        lines.append(f'LOCATION:{escape_ical_text(event.location)}')

    # Add attendees if available
    if event.attendees:
        try:
            attendees_list = json.loads(event.attendees)
            for attendee in attendees_list:
                if attendee:
                    lines.append(f'ATTENDEE:CN={escape_ical_text(attendee)}:mailto:{attendee.replace(" ", ".").lower()}@example.com')
        except:
            pass

    # Add reminder/alarm if specified
    if event.reminder_minutes and event.reminder_minutes > 0:
        lines.extend([
            'BEGIN:VALARM',
            'TRIGGER:-PT{}M'.format(event.reminder_minutes),
            'ACTION:DISPLAY',
            f'DESCRIPTION:Reminder: {escape_ical_text(event.title)}',
            'END:VALARM'
        ])

    # Close event and calendar
    lines.extend([
        'STATUS:CONFIRMED',
        'TRANSP:OPAQUE',
        'END:VEVENT',
        'END:VCALENDAR'
    ])

    return '\r\n'.join(lines)



def escape_ical_text(text):
    """Escape special characters for iCalendar format."""
    if not text:
        return ''
    # Escape special characters
    text = str(text)
    text = text.replace('\\', '\\\\')
    text = text.replace(',', '\\,')
    text = text.replace(';', '\\;')
    text = text.replace('\n', '\\n')
    return text





def generate_combined_ics(events):
    """One calendar file with several events (web download and API v1).

    Each VEVENT comes from generate_ics_content, so escaping, DTSTAMP and the
    default one-hour end are the same in both downloads (#412).
    """
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Speakr//Event Export//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for event in events:
        in_event = False
        for line in generate_ics_content(event).replace("\r\n", "\n").split("\n"):
            if line.startswith("BEGIN:VEVENT"):
                in_event = True
            if in_event:
                lines.append(line)
            if line.startswith("END:VEVENT"):
                in_event = False
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
