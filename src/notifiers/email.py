from __future__ import absolute_import
from email.mime.text import MIMEText
import smtplib
import logging
import kaa

from .base import NotifierBase, NotifierError
from .email_config import config as modconfig

__all__ = ['Notifier']

log = logging.getLogger('stagehand.notifiers.email')

class Notifier(NotifierBase):
    @kaa.threaded()
    def _do_smtp(self, mime, recipients):
        """
        Send email over SMTP.

        smtplib uses blocking sockets, so we do this in a thread.
        """
        mail = smtplib.SMTP(modconfig.hostname, modconfig.port)
        mail.sendmail(modconfig.sender, recipients, mime.as_string())
        mail.quit()


    @kaa.coroutine()
    def _notify(self, episodes):
        # Sanity check configuration
        if '@' not in modconfig.recipients:
            log.error('invalid recipients, skipping email notification')
            yield

        summary = u'Summary of Episodes\n'
        overview = u'\nOverview of Episodes\n'
        recipients = [addr.strip() for addr in modconfig.recipients.split(',')]

        for i, ep in enumerate(episodes, 1):
            summary += u'%02d: %s %s %s\n' % (i, ep.series.name, ep.code, ep.name)
            overview += u'%02d: %s %s %s (%s)\n%s\n\n' % (i, ep.series.name, ep.code, ep.name, ep.airdatetime, ep.overview)

        mime = MIMEText('%s\n%s' % (kaa.py3_b(summary, 'utf-8'), kaa.py3_b(overview, 'utf-8')), 'plain', 'utf-8')
        mime['Subject'] = '[stagehand] downloaded %d episodes' % len(episodes)
        mime['From'] = modconfig.sender
        mime['To'] = ', '.join(recipients)

        try:
            yield self._do_smtp(mime, recipients)
        except smtplib.SMTPException as e:
            log.error('unable to send email notification: %s', e)
