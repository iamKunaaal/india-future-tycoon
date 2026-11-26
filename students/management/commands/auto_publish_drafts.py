"""Auto-publish saved drafts right after the idea-submission lock.

Runs once via a Railway cron at 30 Nov 00:05 IST (18:35 UTC on 29 Nov):
    cron:  35 18 29 11 *
    start: python manage.py auto_publish_drafts

Safety:
- Refuses to run before IDEA_SUBMISSION_DEADLINE + 5 min (drafts are still
  editable until the lock), unless --force.
- Publishes only drafts of PAID students that have at least one of the 12
  questions answered (totally empty drafts stay drafts).
- Idempotent: only status='draft' rows are touched, so a re-run is a no-op.
- Each idea publishes in its own transaction; one bad row can't stop the rest.
- Then generates the AI summary for each (same as a manual publish), in a few
  threads; failures are stored in ai_processing_error and don't undo publish.
- Student emails + participation certificates go out separately at 10 AM on
  30 Nov (send_idea_deadline_reminder, kind 'auto_published').

    python manage.py auto_publish_drafts --dry-run
    python manage.py auto_publish_drafts --skip-summaries
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.utils import timezone

QUESTION_FIELDS = ['q1_target_group', 'q2_exact_problem', 'q3_solution_simple', 'q4_differentiation',
                   'q5_build_steps', 'q6_resources', 'q7_positive_change', 'q8_challenges',
                   'q9_team_fit', 'q10_feedback', 'q11_creative_element', 'q12_pitch']
GRACE = timedelta(minutes=5)
SUMMARY_WORKERS = 4


def answered_count(sub):
    return sum(1 for f in QUESTION_FIELDS if (getattr(sub, f, '') or '').strip())


def _auto_title(sub):
    if sub.title:
        return sub.title
    src = (sub.q3_solution_simple or sub.q2_exact_problem or sub.q1_target_group or '').strip()
    if not src:
        return ''
    title = src[:80]
    if len(src) > 80:
        cut = title.rfind(' ')
        if cut > 40:
            title = title[:cut]
    return title


class Command(BaseCommand):
    help = 'Auto-publish non-empty drafts after the idea submission deadline.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show what would be published; change nothing.')
        parser.add_argument('--force', action='store_true', help='Ignore the deadline time gate (testing only).')
        parser.add_argument('--skip-summaries', action='store_true', help='Publish only; no AI summaries.')

    def handle(self, *args, **opts):
        from students.idea_window import submission_deadline
        from students.models import IdeaSubmission
        from students.views import create_notification

        now = timezone.now()
        gate = submission_deadline() + GRACE
        if now < gate and not opts['force']:
            self.stdout.write(f'Not yet: auto-publish allowed from {timezone.localtime(gate)} (now {timezone.localtime(now)}). Nothing done.')
            return

        drafts = list(IdeaSubmission.objects.filter(status='draft', student__is_paid=True)
                      .select_related('student__user').order_by('id'))
        eligible = [d for d in drafts if answered_count(d) >= 1]
        empty = len(drafts) - len(eligible)
        unpaid = IdeaSubmission.objects.filter(status='draft', student__is_paid=False).count()
        self.stdout.write(f'drafts(paid)={len(drafts)} to_publish={len(eligible)} skipped_empty={empty} skipped_unpaid={unpaid}')
        if opts['dry_run']:
            for d in eligible:
                self.stdout.write(f'  would publish #{d.id} ({answered_count(d)}/12) {d.student.user.email or d.student.user.username}')
            return

        published = []
        for d in eligible:
            try:
                with transaction.atomic():
                    sub = IdeaSubmission.objects.select_for_update().get(id=d.id)
                    if sub.status != 'draft':
                        continue  # published meanwhile / re-run
                    sub.title = _auto_title(sub)
                    sub.status = 'submitted'
                    sub.submitted_at = timezone.now()
                    sub.auto_published = True
                    sub.save(update_fields=['title', 'status', 'submitted_at', 'auto_published', 'updated_at'])
                published.append(sub.id)
                try:
                    create_notification(d.student.user, 'submission', 'Idea Published',
                                        'Your draft was auto-published after the submission deadline.',
                                        'rocket_launch', '/my-idea/', 'View Idea')
                except Exception:
                    pass
            except Exception as e:
                self.stderr.write(f'  FAILED to publish #{d.id}: {e}')

        self.stdout.write(self.style.SUCCESS(f'Published {len(published)} of {len(eligible)} drafts.'))

        if opts['skip_summaries'] or not published:
            return

        from ai_assistant.processors import generate_summary

        def summarise(sub_id):
            close_old_connections()
            try:
                sub = IdeaSubmission.objects.get(id=sub_id)
                generate_summary(sub)
                IdeaSubmission.objects.filter(id=sub_id).update(ai_processed=True, ai_processing_error='')
                return True
            except Exception as e:
                IdeaSubmission.objects.filter(id=sub_id).update(ai_processing_error=str(e)[:500])
                return False
            finally:
                close_old_connections()

        if SUMMARY_WORKERS <= 1:
            ok = sum(summarise(i) for i in published)
        else:
            with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as pool:
                ok = sum(pool.map(summarise, published))
        self.stdout.write(self.style.SUCCESS(f'AI summaries: ok={ok} failed={len(published) - ok}'))
