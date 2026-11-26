"""Post-deadline auto-publish (30 Nov 00:05 IST) + 30 Nov 10 AM emails."""
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from admins.models import MilestoneEmailLog
from .models import IdeaSubmission, Notification, Team, TeamMembership
from .tests import make_student, SAMPLE_ANSWERS

DEADLINE = timezone.make_aware(datetime(2026, 11, 29, 23, 59, 59))
AT_0005 = timezone.make_aware(datetime(2026, 11, 30, 0, 5, 0))
EMPTY = {k: '' for k in SAMPLE_ANSWERS}


def run(*args, now=AT_0005, **kw):
    out = StringIO()
    with mock.patch('students.management.commands.auto_publish_drafts.timezone.now', return_value=now), \
         mock.patch('ai_assistant.processors.generate_summary') as gs:
        call_command('auto_publish_drafts', *args, stdout=out, stderr=out, **kw)
    return out.getvalue(), gs


def paid(username):
    s = make_student(username)
    s.is_paid = True
    s.save()
    return s


@override_settings(IDEA_SUBMISSION_DEADLINE='2026-11-29T23:59:59')
@mock.patch('students.management.commands.auto_publish_drafts.SUMMARY_WORKERS', 1)  # sqlite: no parallel writes
class AutoPublishTest(TestCase):
    def setUp(self):
        self.full = IdeaSubmission.objects.create(student=paid('full'), status='draft', **SAMPLE_ANSWERS)
        self.partial = IdeaSubmission.objects.create(
            student=paid('partial'), status='draft', **{**EMPTY, 'q2_exact_problem': 'Only the problem is written here.'})
        self.empty = IdeaSubmission.objects.create(student=paid('empty'), status='draft', title='Has title only', **EMPTY)
        self.unpaid = IdeaSubmission.objects.create(student=make_student('unpaid'), status='draft', **SAMPLE_ANSWERS)
        self.already = IdeaSubmission.objects.create(student=paid('already'), status='evaluated', **SAMPLE_ANSWERS)

    def _state(self):
        return {s.student.user.username: (s.status, s.auto_published) for s in IdeaSubmission.objects.all()}

    def test_refuses_before_gate(self):
        for t in (DEADLINE - timedelta(hours=1), DEADLINE, DEADLINE + timedelta(minutes=4, seconds=59)):
            out, gs = run(now=t)
            self.assertIn('Not yet', out)
        self.assertEqual(IdeaSubmission.objects.filter(status='draft').count(), 4)

    def test_runs_exactly_at_0005(self):
        out, gs = run(now=DEADLINE + timedelta(minutes=5))
        self.assertIn('Published 2 of 2', out)

    def test_publishes_only_non_empty_paid_drafts(self):
        out, gs = run()
        st = self._state()
        self.assertEqual(st['full'], ('submitted', True))
        self.assertEqual(st['partial'], ('submitted', True))
        self.assertEqual(st['empty'], ('draft', False))
        self.assertEqual(st['unpaid'], ('draft', False))
        self.assertEqual(st['already'], ('evaluated', False))
        self.assertIn('skipped_empty=1', out)
        self.assertIn('skipped_unpaid=1', out)
        self.assertEqual(gs.call_count, 2)

    def test_published_fields(self):
        run()
        p = IdeaSubmission.objects.get(id=self.partial.id)
        self.assertIsNotNone(p.submitted_at)
        self.assertTrue(p.title.startswith('Only the problem'))      # auto title from q2
        f = IdeaSubmission.objects.get(id=self.full.id)
        self.assertTrue(f.title.startswith('A mobile app'))           # auto title from q3
        self.assertTrue(f.ai_processed)
        self.assertTrue(Notification.objects.filter(user=f.student.user, title='Idea Published').exists())

    def test_dry_run_changes_nothing(self):
        out, gs = run('--dry-run')
        self.assertIn('would publish', out)
        self.assertEqual(IdeaSubmission.objects.filter(status='draft').count(), 4)
        self.assertEqual(gs.call_count, 0)

    def test_rerun_is_noop(self):
        run()
        out, gs = run()
        self.assertIn('to_publish=0', out)
        self.assertEqual(gs.call_count, 0)

    def test_summary_failure_keeps_publish_and_records_error(self):
        out = StringIO()
        with mock.patch('students.management.commands.auto_publish_drafts.timezone.now', return_value=AT_0005), \
             mock.patch('ai_assistant.processors.generate_summary', side_effect=RuntimeError('openrouter down')):
            call_command('auto_publish_drafts', stdout=out, stderr=out)
        f = IdeaSubmission.objects.get(id=self.full.id)
        self.assertEqual(f.status, 'submitted')
        self.assertIn('openrouter down', f.ai_processing_error)
        self.assertIn('failed=2', out.getvalue())

    def test_skip_summaries(self):
        out, gs = run('--skip-summaries')
        self.assertEqual(gs.call_count, 0)
        self.assertIn('Published 2', out)

    def test_auto_published_idea_is_locked_for_student(self):
        run()
        from django.test import Client
        c = Client()
        c.login(username='full', password='testpass123')
        with override_settings(IDEA_SUBMISSION_DEADLINE='2000-01-01T00:00:00'):
            c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'x', 'save_type': 'draft'})
        f = IdeaSubmission.objects.get(id=self.full.id)
        self.assertEqual((f.status, f.q1_target_group != 'x'), ('submitted', True))


class AutoPublishedEmailTest(TestCase):
    def setUp(self):
        self.leader = paid('lead')
        self.member = paid('mem')
        team = Team.objects.create(name='T', team_code='TA', leader=self.leader.user)
        TeamMembership.objects.create(team=team, student=self.leader, role='leader')
        TeamMembership.objects.create(team=team, student=self.member, role='member')
        self.auto = IdeaSubmission.objects.create(student=self.leader, status='submitted', auto_published=True, **SAMPLE_ANSWERS)
        self.manual = IdeaSubmission.objects.create(student=paid('manual'), status='submitted', **SAMPLE_ANSWERS)
        self.noemail = paid('noemail')
        self.noemail.user.email = ''
        self.noemail.user.save()
        IdeaSubmission.objects.create(student=self.noemail, status='submitted', auto_published=True, **SAMPLE_ANSWERS)

    def _run(self, *extra, send_ok=1):
        out = StringIO()
        with mock.patch('accounts.emails.send_branded_email', return_value=send_ok) as em, \
             mock.patch('admins.views._send_certificate', return_value=(True, '')) as cert:
            call_command('send_idea_deadline_reminder', '--date', '2026-11-30', *extra, stdout=out, stderr=out)
        return out.getvalue(), em, cert

    def test_only_auto_published_owners_mailed_with_certificates(self):
        out, em, cert = self._run()
        self.assertEqual([c.args[1] for c in em.call_args_list], ['lead@test.com'])
        self.assertEqual(em.call_args.args[2], 'students/email_idea_auto_published.html')
        cert_emails = sorted(c.args[2] for c in cert.call_args_list)
        self.assertEqual(cert_emails, ['lead@test.com', 'mem@test.com'])   # leader + teammate; no-email owner skipped
        self.assertIn('sent=1 failed=0 no_email=1', out)

    def test_rerun_dedup(self):
        self._run()
        out, em, cert = self._run()
        self.assertIn('owners_to_mail=0', out)
        self.assertEqual(em.call_count, 0)

    def test_failed_email_retried_next_run(self):
        out, em, cert = self._run(send_ok=0)
        self.assertIn('failed=1', out)
        self.assertFalse(MilestoneEmailLog.objects.filter(student=self.leader).exists())
        out, em, cert = self._run()
        self.assertIn('sent=1', out)

    def test_dry_run(self):
        out, em, cert = self._run('--dry-run')
        self.assertIn('owners_to_mail=2', out)
        self.assertEqual(em.call_count, 0)

    def test_other_days_unaffected(self):
        out = StringIO()
        call_command('send_idea_deadline_reminder', '--date', '2026-12-01', stdout=out)
        self.assertIn('nothing to send', out.getvalue())
