"""Idea submission lock (29 Nov) + Top 400 Edit & Resubmit window."""
from django.test import TestCase, Client, override_settings

from ai_assistant.models import AIEvaluation
from admins.models import ResultsRelease
from .models import IdeaSubmission, IdeaSuggestion, Team, TeamMembership
from .tests import make_student, SAMPLE_ANSWERS

OPEN = dict(IDEA_SUBMISSION_DEADLINE='2099-01-01T23:59:59', RESUBMIT_OPEN=False)
CLOSED = dict(IDEA_SUBMISSION_DEADLINE='2000-01-01T23:59:59', RESUBMIT_OPEN=False)
RESUBMIT = dict(IDEA_SUBMISSION_DEADLINE='2000-01-01T23:59:59', RESUBMIT_OPEN=True,
                RESUBMIT_DEADLINE='2099-01-01T23:59:59')


class IdeaWindowTest(TestCase):
    def setUp(self):
        self.student = make_student('leader1')
        self.student.is_paid = True
        self.student.has_seen_welcome_popup = True
        self.student.has_seen_deadline_extended_popup = True
        self.student.save()
        self.c = Client()
        self.c.login(username='leader1', password='testpass123')

    def _published(self, top400=True):
        sub = IdeaSubmission.objects.create(student=self.student, title='Orig', status='submitted', **SAMPLE_ANSWERS)
        AIEvaluation.objects.create(submission=sub, is_top_400=top400, rank=5 if top400 else 900)
        return sub

    def _release(self):
        ResultsRelease.objects.create(is_published=True)

    # ---- before deadline: unchanged behaviour ----
    @override_settings(**OPEN)
    def test_open_draft_save_and_publish(self):
        self.c.post('/submit/', {**SAMPLE_ANSWERS, 'save_type': 'draft'})
        sub = IdeaSubmission.objects.get()
        r = self.c.post(f'/idea/{sub.id}/publish/')
        self.assertEqual(r.json()['success'], True)

    @override_settings(**OPEN)
    def test_open_published_idea_cannot_be_reverted_to_draft(self):
        sub = self._published()
        r = self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'hacked'})
        self.assertRedirects(r, '/my-idea/', fetch_redirect_response=False)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'submitted')
        self.assertNotEqual(sub.q1_target_group, 'hacked')

    # ---- after deadline: everything locked ----
    @override_settings(**CLOSED)
    def test_closed_no_new_idea(self):
        r = self.c.post('/submit/', {**SAMPLE_ANSWERS, 'save_type': 'draft'})
        self.assertRedirects(r, '/dashboard/', fetch_redirect_response=False)
        self.assertFalse(IdeaSubmission.objects.exists())

    @override_settings(**CLOSED)
    def test_closed_draft_cannot_be_edited_or_published(self):
        sub = IdeaSubmission.objects.create(student=self.student, status='draft', **SAMPLE_ANSWERS)
        self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'late edit', 'save_type': 'draft'})
        sub.refresh_from_db()
        self.assertNotEqual(sub.q1_target_group, 'late edit')
        r = self.c.post(f'/idea/{sub.id}/publish/')
        self.assertEqual(r.status_code, 400)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'draft')
        page = self.c.get('/my-idea/').content.decode()
        self.assertIn('Idea submission closed', page)
        self.assertNotIn('Publish Idea', page)

    @override_settings(**CLOSED)
    def test_closed_top400_locked_without_switch(self):
        sub = self._published()
        self._release()
        self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'x'})
        sub.refresh_from_db()
        self.assertNotEqual(sub.q1_target_group, 'x')
        self.assertNotIn('Edit &amp; Resubmit', self.c.get('/my-idea/').content.decode())

    # ---- resubmit window ----
    @override_settings(**RESUBMIT)
    def test_resubmit_needs_results_release(self):
        sub = self._published()
        self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'x'})
        sub.refresh_from_db()
        self.assertNotEqual(sub.q1_target_group, 'x')

    @override_settings(**RESUBMIT)
    def test_resubmit_top400_overwrites_in_place(self):
        sub = self._published()
        self._release()
        page = self.c.get('/my-idea/').content.decode()
        self.assertIn('Edit &amp; Resubmit', page)
        form = self.c.get('/submit/').content.decode()
        self.assertIn('Resubmit Idea', form)
        self.assertNotIn('onclick="saveDraft()"', form)
        r = self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'Refined target group'})
        self.assertRedirects(r, '/my-idea/', fetch_redirect_response=False)
        sub.refresh_from_db()
        self.assertEqual(sub.q1_target_group, 'Refined target group')
        self.assertEqual(sub.status, 'submitted')           # stays published
        self.assertIsNotNone(sub.resubmitted_at)
        self.assertEqual(IdeaSubmission.objects.count(), 1)  # overwrite, no new row

    @override_settings(**RESUBMIT)
    def test_resubmit_rejects_empty_answers(self):
        sub = self._published()
        self._release()
        r = self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q5_build_steps': ''})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Every question must be answered', r.content.decode())
        sub.refresh_from_db()
        self.assertTrue(sub.q5_build_steps)
        self.assertIsNone(sub.resubmitted_at)

    @override_settings(**RESUBMIT)
    def test_resubmit_not_for_non_top400(self):
        sub = self._published(top400=False)
        self._release()
        self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'x'})
        sub.refresh_from_db()
        self.assertNotEqual(sub.q1_target_group, 'x')

    @override_settings(**dict(RESUBMIT, RESUBMIT_DEADLINE='2000-01-02T00:00:00'))
    def test_resubmit_closes_after_deadline(self):
        sub = self._published()
        self._release()
        self.c.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'x'})
        sub.refresh_from_db()
        self.assertNotEqual(sub.q1_target_group, 'x')

    @override_settings(**RESUBMIT)
    def test_resubmit_member_cannot_edit_but_can_suggest(self):
        sub = self._published()
        self._release()
        team = Team.objects.create(name='T', team_code='T1', leader=self.student.user)
        TeamMembership.objects.create(team=team, student=self.student, role='leader')
        member = make_student('member1')
        member.is_paid = True
        member.save()
        TeamMembership.objects.create(team=team, student=member, role='member')
        mc = Client()
        mc.login(username='member1', password='testpass123')
        mc.post('/submit/', {**SAMPLE_ANSWERS, 'q1_target_group': 'member edit'})
        sub.refresh_from_db()
        self.assertNotEqual(sub.q1_target_group, 'member edit')
        r = mc.post(f'/idea/{sub.id}/suggest/', {**SAMPLE_ANSWERS, 'q1_target_group': 'member idea'})
        self.assertEqual(r.json()['success'], True)
        sug = IdeaSuggestion.objects.get()
        r = self.c.post(f'/suggestion/{sug.id}/handle/', {'action': 'approve'})
        self.assertEqual(r.json()['success'], True)
        sub.refresh_from_db()
        self.assertEqual(sub.q1_target_group, 'member idea')
        self.assertIsNotNone(sub.resubmitted_at)

    # ---- dashboard banner ----
    @override_settings(**RESUBMIT)
    def test_dashboard_banner_owner_until_resubmitted(self):
        sub = self._published()
        self._release()
        page = self.c.get('/dashboard/').content.decode()
        self.assertIn('id="top400Banner"', page)
        self.assertIn('Update Idea', page)
        sub.resubmitted_at = sub.created_at
        sub.save()
        self.assertNotIn('id="top400Banner"', self.c.get('/dashboard/').content.decode())

    @override_settings(**RESUBMIT)
    def test_dashboard_banner_member_sees_view_idea(self):
        sub = self._published()
        self._release()
        team = Team.objects.create(name='T', team_code='T3', leader=self.student.user)
        TeamMembership.objects.create(team=team, student=self.student, role='leader')
        member = make_student('member3')
        member.is_paid = True
        member.has_seen_welcome_popup = True
        member.has_seen_deadline_extended_popup = True
        member.save()
        TeamMembership.objects.create(team=team, student=member, role='member')
        mc = Client()
        mc.login(username='member3', password='testpass123')
        page = mc.get('/dashboard/').content.decode()
        self.assertIn('id="top400Banner"', page)
        self.assertIn('Your team leader can refine', page)

    @override_settings(**CLOSED)
    def test_dashboard_banner_hidden_when_window_closed(self):
        self._published()
        self._release()
        self.assertNotIn('id="top400Banner"', self.c.get('/dashboard/').content.decode())

    @override_settings(**CLOSED)
    def test_closed_suggestions_blocked(self):
        sub = self._published()
        team = Team.objects.create(name='T', team_code='T2', leader=self.student.user)
        TeamMembership.objects.create(team=team, student=self.student, role='leader')
        member = make_student('member2')
        TeamMembership.objects.create(team=team, student=member, role='member')
        mc = Client()
        mc.login(username='member2', password='testpass123')
        r = mc.post(f'/idea/{sub.id}/suggest/', {**SAMPLE_ANSWERS, 'q1_target_group': 'late'})
        self.assertEqual(r.status_code, 400)
