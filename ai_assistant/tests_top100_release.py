"""Top 400 release must NOT leak Top 100 / School Winner / Top 12 (curated later)."""
from unittest import mock

from django.test import TestCase, Client

from admins.models import ResultsRelease
from ai_assistant.evaluator import _send_ranking_milestone_emails, _send_top100_milestone_emails
from ai_assistant.models import AIEvaluation
from students.models import IdeaSubmission
from students.tests import make_student, SAMPLE_ANSWERS


def idea(username, rank, top400=True, top100=False, top12=False):
    s = make_student(username)
    s.is_paid = True
    s.has_seen_welcome_popup = True
    s.has_seen_deadline_extended_popup = True
    s.save()
    sub = IdeaSubmission.objects.create(student=s, status='evaluated', **SAMPLE_ANSWERS)
    AIEvaluation.objects.create(submission=sub, rank=rank, is_top_400=top400, is_top_100=top100, is_top_12=top12)
    return s


class Top400ReleaseNoTop100LeakTest(TestCase):
    def test_release_sends_only_top400(self):
        idea('r1', 1)
        idea('r50', 50)
        idea('r300', 300)
        idea('r900', 900, top400=False)
        with mock.patch('students.milestone_emails.send_milestone_email') as send:
            _send_ranking_milestone_emails()
        kinds = sorted({c.args[1] for c in send.call_args_list})
        self.assertEqual(kinds, ['top400'])
        self.assertEqual(send.call_count, 3)

    def test_top100_emails_follow_curated_flag_not_rank(self):
        idea('ai_rank_1_not_picked', 1)
        jury_pick = idea('jury_pick_rank_250', 250, top100=True)
        with mock.patch('students.milestone_emails.send_milestone_email') as send:
            _send_top100_milestone_emails()
        top100_to = [c.args[0].user.username for c in send.call_args_list if c.args[1] == 'top100']
        self.assertEqual(top100_to, [jury_pick.user.username])


class DashboardBadgeTest(TestCase):
    def setUp(self):
        ResultsRelease.objects.create(is_published=True)

    def _badges(self, username):
        c = Client()
        c.login(username=username, password='testpass123')
        html = c.get('/dashboard/').content.decode()
        lit = lambda label, color: f'color:{color};">{label}</p>' in html
        return lit('Innovator', '#dc2626'), lit('Problem Solver', '#16a34a'), lit('Pitch Star', '#d946ef')

    def test_ai_rank_5_after_top400_release_shows_only_innovator(self):
        idea('rank5', 5)
        self.assertEqual(self._badges('rank5'), (True, False, False))

    def test_curated_top100_lights_problem_solver(self):
        idea('cur100', 180, top100=True)
        self.assertEqual(self._badges('cur100'), (True, True, False))

    def test_curated_top12_lights_all(self):
        idea('cur12', 40, top12=True)
        self.assertEqual(self._badges('cur12'), (True, True, True))


class TickTop100SendsNothingTest(TestCase):
    def test_admin_tick_is_top_100_sends_no_email(self):
        s = idea('tick', 70)
        ev = AIEvaluation.objects.get(submission__student=s)
        with mock.patch('students.milestone_emails.send_milestone_email') as send:
            ev.is_top_100 = True
            ev.save()
        self.assertEqual(send.call_count, 0)
