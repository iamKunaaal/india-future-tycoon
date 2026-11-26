"""Edge cases for the 29 Nov idea lock (+ inert resubmit defaults)."""
from datetime import datetime, timedelta, timezone as dt_tz
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.utils import timezone

from ai_assistant.models import AIEvaluation
from admins.models import ResultsRelease
from students import idea_window
from .models import IdeaSubmission, IdeaSuggestion, Team, TeamMembership, UploadedFile
from .tests import make_student, SAMPLE_ANSWERS

CLOSED = dict(IDEA_SUBMISSION_DEADLINE='2000-01-01T23:59:59', RESUBMIT_OPEN=False)
OPEN = dict(IDEA_SUBMISSION_DEADLINE='2099-01-01T23:59:59', RESUBMIT_OPEN=False)
IST_DEADLINE = timezone.make_aware(datetime(2026, 11, 29, 23, 59, 59))


def paid(username):
    s = make_student(username)
    s.is_paid = True
    s.has_seen_welcome_popup = True
    s.has_seen_deadline_extended_popup = True
    s.save()
    return s


def login(username):
    c = Client()
    c.login(username=username, password='testpass123')
    return c


class DeadlineParsingTest(TestCase):
    @override_settings(IDEA_SUBMISSION_DEADLINE='2026-11-29T23:59:59')
    def test_boundary_exact_second(self):
        self.assertTrue(idea_window.submissions_open(IST_DEADLINE - timedelta(seconds=1)))
        self.assertTrue(idea_window.submissions_open(IST_DEADLINE))            # 23:59:59 still open
        self.assertFalse(idea_window.submissions_open(IST_DEADLINE + timedelta(seconds=1)))

    @override_settings(IDEA_SUBMISSION_DEADLINE='2026-11-29T23:59:59')
    def test_naive_value_is_ist_not_utc(self):
        self.assertEqual(idea_window.submission_deadline().utcoffset(), timedelta(hours=5, minutes=30))
        utc_1830 = datetime(2026, 11, 29, 18, 30, 0, tzinfo=dt_tz.utc)   # = 30 Nov 00:00 IST
        self.assertFalse(idea_window.submissions_open(utc_1830))
        utc_1829 = datetime(2026, 11, 29, 18, 29, 0, tzinfo=dt_tz.utc)   # = 29 Nov 23:59 IST
        self.assertTrue(idea_window.submissions_open(utc_1829))

    @override_settings(IDEA_SUBMISSION_DEADLINE='2026-11-29T23:59:59+05:30')
    def test_explicit_offset_respected(self):
        self.assertEqual(idea_window.submission_deadline(), IST_DEADLINE)

    @override_settings(IDEA_SUBMISSION_DEADLINE='2026-11-29T18:29:59Z')
    def test_utc_z_suffix(self):
        self.assertEqual(idea_window.submission_deadline(), IST_DEADLINE)

    @override_settings(IDEA_SUBMISSION_DEADLINE='29/11/2026 11:59 PM')
    def test_malformed_env_falls_back_to_default(self):
        self.assertEqual(idea_window.submission_deadline(), IST_DEADLINE)

    @override_settings(IDEA_SUBMISSION_DEADLINE='')
    def test_empty_env_falls_back_to_default(self):
        self.assertEqual(idea_window.submission_deadline(), IST_DEADLINE)

    @override_settings(IDEA_SUBMISSION_DEADLINE='2026-11-29')
    def test_date_only_means_midnight_start(self):
        # documents behaviour: date-only = 29 Nov 00:00 IST (whole day locked)
        self.assertEqual(idea_window.submission_deadline(),
                         timezone.make_aware(datetime(2026, 11, 29, 0, 0, 0)))

    def test_production_defaults(self):
        self.assertEqual(idea_window.submission_deadline(), IST_DEADLINE)
        self.assertEqual(idea_window.resubmit_deadline(),
                         timezone.make_aware(datetime(2026, 12, 10, 23, 59, 59)))

    def test_resubmit_inert_by_default(self):
        from django.conf import settings
        self.assertFalse(settings.RESUBMIT_OPEN)


class LiveClockFlipTest(TestCase):
    """Same request flow before vs after the deadline using a mocked clock."""

    def setUp(self):
        self.s = paid('clock1')
        self.c = login('clock1')
        self.sub = IdeaSubmission.objects.create(student=self.s, status='draft', **SAMPLE_ANSWERS)

    @override_settings(IDEA_SUBMISSION_DEADLINE='2026-11-29T23:59:59')
    def test_edit_allowed_at_2359_blocked_at_0000(self):
        with mock.patch('students.idea_window.timezone.now', return_value=IST_DEADLINE - timedelta(seconds=30)):
            self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'just in time', 'save_type': 'draft'})
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.q1_target_group, 'just in time')
        with mock.patch('students.idea_window.timezone.now', return_value=IST_DEADLINE + timedelta(seconds=1)):
            self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'too late', 'save_type': 'draft'})
            r = self.c.post(f'/idea/{self.sub.id}/publish/')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.q1_target_group, 'just in time')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.sub.status, 'draft')


@override_settings(**CLOSED)
class LockedEdgeCasesTest(TestCase):
    def setUp(self):
        self.s = paid('lock1')
        self.c = login('lock1')

    def test_get_submit_without_idea_redirects_dashboard(self):
        r = self.c.get('/submit/')
        self.assertRedirects(r, '/dashboard/', fetch_redirect_response=False)

    def test_get_submit_with_draft_redirects_my_idea(self):
        IdeaSubmission.objects.create(student=self.s, status='draft', **SAMPLE_ANSWERS)
        r = self.c.get('/submit/')
        self.assertRedirects(r, '/my-idea/', fetch_redirect_response=False)

    def test_classic_page_redirects(self):
        r = self.c.get('/submit/classic/')
        self.assertEqual(r.status_code, 302)

    def test_submit_for_review_post_blocked(self):
        sub = IdeaSubmission.objects.create(student=self.s, status='draft', title='T', **SAMPLE_ANSWERS)
        self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q2_exact_problem': 'late', 'save_type': 'submit'})
        sub.refresh_from_db()
        self.assertNotEqual(sub.q2_exact_problem, 'late')

    def test_file_upload_blocked(self):
        sub = IdeaSubmission.objects.create(student=self.s, status='draft', **SAMPLE_ANSWERS)
        f = SimpleUploadedFile('late.pdf', b'%PDF-1.4 late', content_type='application/pdf')
        self.c.post('/submit/', {**SAMPLE_ANSWERS, 'save_type': 'draft', 'document_file': f})
        self.assertFalse(UploadedFile.objects.filter(submission=sub).exists())

    def test_all_published_statuses_locked(self):
        for i, st in enumerate(['submitted', 'under_review', 'evaluated', 'reviewed']):
            stu = paid(f'st{i}')
            sub = IdeaSubmission.objects.create(student=stu, status=st, **SAMPLE_ANSWERS)
            c = login(f'st{i}')
            c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'x', 'save_type': 'draft'})
            sub.refresh_from_db()
            self.assertEqual((sub.status, sub.q1_target_group != 'x'), (st, True), st)

    def test_publish_other_users_idea_still_403(self):
        other = paid('other1')
        sub = IdeaSubmission.objects.create(student=other, status='draft', **SAMPLE_ANSWERS)
        r = self.c.post(f'/idea/{sub.id}/publish/')
        self.assertEqual(r.status_code, 403)

    def test_unpaid_student_goes_to_payment_not_crash(self):
        u = make_student('unpaid1')
        c = login('unpaid1')
        r = c.get('/submit/')
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('/my-idea/', r['Location'])

    def test_pages_render_after_lock(self):
        IdeaSubmission.objects.create(student=self.s, status='draft', **SAMPLE_ANSWERS)
        for url in ['/dashboard/', '/my-idea/']:
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
        page = self.c.get('/my-idea/').content.decode()
        self.assertIn('Idea submission closed', page)
        self.assertNotIn('openPublishModal()"', page.split('function openPublishModal')[0])

    def test_published_idea_page_has_no_resubmit_button(self):
        sub = IdeaSubmission.objects.create(student=self.s, status='submitted', **SAMPLE_ANSWERS)
        AIEvaluation.objects.create(submission=sub, is_top_400=True, rank=1)
        ResultsRelease.objects.create(is_published=True)
        self.assertNotIn('Edit &amp; Resubmit', self.c.get('/my-idea/').content.decode())
        self.assertNotIn('top400Banner', self.c.get('/dashboard/').content.decode())


@override_settings(**CLOSED)
class LockedTeamTest(TestCase):
    def setUp(self):
        self.leader = paid('lead9')
        self.member = paid('mem9')
        self.team = Team.objects.create(name='T', team_code='T9', leader=self.leader.user)
        TeamMembership.objects.create(team=self.team, student=self.leader, role='leader')
        TeamMembership.objects.create(team=self.team, student=self.member, role='member')
        self.sub = IdeaSubmission.objects.create(student=self.leader, status='draft', **SAMPLE_ANSWERS)
        self.lc, self.mc = login('lead9'), login('mem9')

    def test_member_submit_page_still_renders_member_view(self):
        r = self.mc.get('/submit/')
        self.assertEqual(r.status_code, 200)

    def test_member_suggest_get_redirects_and_post_blocked(self):
        r = self.mc.get(f'/idea/{self.sub.id}/suggest/')
        self.assertRedirects(r, '/my-idea/', fetch_redirect_response=False)
        r = self.mc.post(f'/idea/{self.sub.id}/suggest/', {**SAMPLE_ANSWERS, 'q1_target_group': 'late'})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(IdeaSuggestion.objects.exists())

    def test_member_my_idea_hides_suggest_button(self):
        page = self.mc.get('/my-idea/').content.decode()
        self.assertNotIn(f'/idea/{self.sub.id}/suggest/', page)

    def test_old_pending_suggestion_cannot_be_merged_but_can_be_rejected(self):
        sug = IdeaSuggestion.objects.create(submission=self.sub, suggested_by=self.member.user,
                                            changes={'q1_target_group': 'pre-deadline suggestion'})
        r = self.lc.post(f'/suggestion/{sug.id}/handle/', {'action': 'approve'})
        self.assertEqual(r.status_code, 400)
        self.sub.refresh_from_db()
        self.assertNotEqual(self.sub.q1_target_group, 'pre-deadline suggestion')
        r = self.lc.post(f'/suggestion/{sug.id}/handle/', {'action': 'reject', 'reason': 'closed'})
        self.assertEqual(r.json()['success'], True)

    def test_partial_field_merge_also_blocked(self):
        sug = IdeaSuggestion.objects.create(submission=self.sub, suggested_by=self.member.user,
                                            changes={'q1_target_group': 'a', 'q2_exact_problem': 'b'})
        r = self.lc.post(f'/suggestion/{sug.id}/handle/',
                         data='{"action":"approve","fields":["q1_target_group"]}',
                         content_type='application/json')
        self.assertEqual(r.status_code, 400)


@override_settings(**OPEN)
class OpenWindowUnchangedTest(TestCase):
    """Before the deadline everything behaves exactly as before."""

    def setUp(self):
        self.leader = paid('lead8')
        self.member = paid('mem8')
        team = Team.objects.create(name='T', team_code='T8', leader=self.leader.user)
        TeamMembership.objects.create(team=team, student=self.leader, role='leader')
        TeamMembership.objects.create(team=team, student=self.member, role='member')
        self.lc, self.mc = login('lead8'), login('mem8')

    def test_full_flow_new_idea_draft_edit_publish(self):
        self.lc.post('/submit/', {**SAMPLE_ANSWERS, 'save_type': 'draft'})
        self.lc.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'edited', 'save_type': 'submit'})
        sub = IdeaSubmission.objects.get()
        self.assertEqual(sub.q1_target_group, 'edited')
        self.assertEqual(self.lc.post(f'/idea/{sub.id}/publish/').json()['success'], True)

    def test_suggestion_merge_on_published_idea_still_allowed(self):
        sub = IdeaSubmission.objects.create(student=self.leader, status='submitted', **SAMPLE_ANSWERS)
        self.assertEqual(self.mc.get(f'/idea/{sub.id}/suggest/').status_code, 200)
        self.mc.post(f'/idea/{sub.id}/suggest/', {**SAMPLE_ANSWERS, 'q1_target_group': 'team idea'})
        sug = IdeaSuggestion.objects.get()
        self.assertEqual(self.lc.post(f'/suggestion/{sug.id}/handle/', {'action': 'approve'}).json()['success'], True)
        sub.refresh_from_db()
        self.assertEqual(sub.q1_target_group, 'team idea')
        self.assertIsNone(sub.resubmitted_at)   # not a resubmission before deadline

    def test_empty_draft_guard_unchanged(self):
        self.lc.post('/submit/', {'save_type': 'draft'})
        self.assertFalse(IdeaSubmission.objects.exists())
