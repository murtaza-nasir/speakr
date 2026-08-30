"""Outbound email carries Date and Message-ID headers (#364).

Both headers matter for deliverability scoring and client threading; before
the fix neither was set, which some receiving servers penalize as spam.
Message-ID must use the domain of the configured from address.
"""

import email as email_lib
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.services.email as email_service


def _capture_send(from_address):
    cfg = dict(
        smtp_host='smtp.test', smtp_port=25,
        smtp_username='u', smtp_password='p',
        smtp_use_ssl=False, smtp_use_tls=False,
        from_name='Speakr', from_address=from_address,
    )
    sent = {}
    with patch.object(email_service, 'get_email_config', return_value=cfg), \
         patch.object(email_service, 'is_smtp_configured', return_value=True), \
         patch.object(email_service.smtplib, 'SMTP') as smtp:
        smtp.return_value.sendmail.side_effect = lambda f, t, d: sent.update(data=d)
        email_service._send_email('to@example.com', 'Subject', '<b>hi</b>', 'hi')
    return email_lib.message_from_string(sent['data'])


def test_outbound_mail_has_date_and_message_id():
    msg = _capture_send('noreply@example.org')
    assert msg['Date'], 'Date header missing'
    assert msg['Message-ID'], 'Message-ID header missing'
    assert msg['Message-ID'].strip().endswith('@example.org>'), msg['Message-ID']


def test_message_id_domain_follows_from_address():
    msg = _capture_send('alerts@speakr.example.com')
    assert msg['Message-ID'].strip().endswith('@speakr.example.com>'), msg['Message-ID']


if __name__ == '__main__':
    test_outbound_mail_has_date_and_message_id()
    test_message_id_domain_follows_from_address()
    print('OK')
