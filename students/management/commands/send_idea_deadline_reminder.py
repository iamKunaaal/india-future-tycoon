"""Idea-submission deadline reminders to ALL students (client request, Nov 2026).

- 26, 27, 28 Nov -> "Submit your IFT idea today!"
- 29 Nov         -> "FINAL CALL!!! Submit your IFT idea today."

Runs on a Railway cron DAILY at 10:00 AM IST (04:30 UTC):
    cron:  30 4 * * *
    start: python manage.py send_idea_deadline_reminder

The command self-checks the date (IST): on any other day it does nothing, so
the daily cron is safe. Each student gets each day's email at most once (dedup
via MilestoneEmailLog key 'idl_<date>'); only successful sends are logged, so a
re-run retries failures. Sends in parallel threads so ~12k mails finish fast.

    python manage.py send_idea_deadline_reminder                    # today's email, if scheduled
    python manage.py send_idea_deadline_reminder --dry-run          # count only, send nothing
    python manage.py send_idea_deadline_reminder --test EMAIL [--kind final]
    python manage.py send_idea_deadline_reminder --date 2026-11-26  # force a scheduled day
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

KINDS = {
    'daily': ('Submit your IFT idea today!',
              'students/email_idea_deadline_reminder.html'),
    'final': ('FINAL CALL!!! Submit your IFT idea today.',
              'students/email_idea_final_call.html'),
}
SCHEDULE = {
    date(2026, 11, 26): 'daily',
    date(2026, 11, 27): 'daily',
    date(2026, 11, 28): 'daily',
    date(2026, 11, 29): 'final',
}
WORKERS = 6


def _name(user):
    return (user.first_name or '').strip().title() or 'Student'


class Command(BaseCommand):
    help = 'Send the 26-29 Nov idea-submission deadline reminders to all students.'

    def add_arguments(self, parser):
        parser.add_argument('--test', help='Send one preview email to this address only.')
        parser.add_argument('--kind', choices=list(KINDS), default='daily', help='Template for --test.')
        parser.add_argument('--date', help='Force a scheduled date YYYY-MM-DD (testing / manual catch-up).')
        parser.add_argument('--dry-run', action='store_true', help='Count recipients, send nothing.')

    def handle(self, *args, **opts):
        from accounts.emails import send_branded_email

        if opts.get('test'):
            subject, template = KINDS[opts['kind']]
            r = send_branded_email(subject, opts['test'], template, {'name': 'Student'})
            self.stdout.write(self.style.SUCCESS(f'Preview ({opts["kind"]}) to {opts["test"]}: result={r}'))
            return

        day = date.fromisoformat(opts['date']) if opts.get('date') else timezone.localdate()
        kind = SCHEDULE.get(day)
        if not kind:
            self.stdout.write(f'No idea-deadline reminder scheduled for {day} — nothing to send.')
            return

        from students.models import Student
        from admins.models import MilestoneEmailLog
        from admins.views import _excluded_students_q

        subject, template = KINDS[kind]
        key = f'idl_{day.isoformat()}'
        done = set(MilestoneEmailLog.objects.filter(milestone=key).values_list('student_id', flat=True))
        students = (Student.objects.select_related('user')
                    .filter(user__is_active=True)
                    .exclude(user__email='')
                    .exclude(_excluded_students_q())
                    .exclude(id__in=done)
                    .order_by('id'))

        # One mail per address per day (siblings / shared parent email); every
        # student on that address is logged once the mail succeeds.
        by_email = {}
        for st in students:
            email = (st.user.email or '').strip().lower()
            if email:
                by_email.setdefault(email, {'name': _name(st.user), 'ids': []})['ids'].append(st.id)
        jobs = [(email, v['name'], v['ids']) for email, v in by_email.items()]

        n_students = sum(len(j[2]) for j in jobs)
        self.stdout.write(f'{day} [{kind}] emails_to_send={len(jobs)} students={n_students} already_sent={len(done)}')
        if opts['dry_run']:
            return

        def send(job):
            email, name, ids = job
            close_old_connections()
            try:
                ok = send_branded_email(subject, email, template, {'name': name})
                if ok:
                    MilestoneEmailLog.objects.bulk_create(
                        [MilestoneEmailLog(student_id=i, milestone=key) for i in ids])
                return bool(ok)
            except Exception:
                return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            sent = sum(pool.map(send, jobs))

        self.stdout.write(self.style.SUCCESS(
            f'Idea deadline reminder {day} [{kind}]: sent={sent} failed={len(jobs) - sent}'))
