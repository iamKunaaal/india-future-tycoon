"""Idea submission lock + Top 400 Edit & Resubmit window — single source of truth.

Phases:
1. Until IDEA_SUBMISSION_DEADLINE (29 Nov 23:59 IST): normal flow — drafts are
   editable/publishable; a published idea is locked (view only).
2. After the deadline: EVERYTHING is locked (no new idea, edit, publish or
   suggestion merge), so the Top 400 ranking can't shift under evaluation.
3. Resubmit window: only when RESUBMIT_OPEN=true (env master switch) AND Top 400
   results are released AND before RESUBMIT_DEADLINE — the owner (solo student
   / team leader) of an `is_top_400` idea may edit & resubmit it. The idea is
   overwritten in place and stamped `resubmitted_at` for batch re-evaluation.
"""
from datetime import datetime

from django.conf import settings
from django.utils import timezone


_DEFAULTS = {
    'IDEA_SUBMISSION_DEADLINE': '2026-11-29T23:59:59',
    'RESUBMIT_DEADLINE': '2026-12-10T23:59:59',
}


def _deadline(name):
    """Parse an ISO deadline setting. Naive = IST (TIME_ZONE); an explicit
    offset (e.g. +05:30) is respected. A malformed env value must never 500
    the site — fall back to the default and log loudly."""
    raw = str(getattr(settings, name, '') or '').strip()
    for value in (raw, _DEFAULTS[name]):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (TypeError, ValueError):
            print(f'[IDEA_WINDOW] Invalid {name}={value!r}; using default {_DEFAULTS[name]}', flush=True)
            continue
        return dt if timezone.is_aware(dt) else timezone.make_aware(dt)
    raise RuntimeError(f'Default {name} is invalid')  # unreachable


def submission_deadline():
    return _deadline('IDEA_SUBMISSION_DEADLINE')


def resubmit_deadline():
    return _deadline('RESUBMIT_DEADLINE')


def submissions_open(now=None):
    return (now or timezone.now()) <= submission_deadline()


def resubmit_window_open(now=None):
    if not settings.RESUBMIT_OPEN:
        return False
    if (now or timezone.now()) > resubmit_deadline():
        return False
    from admins.models import ResultsRelease
    return ResultsRelease.is_live()


def is_top_400(submission):
    try:
        return bool(submission.ai_evaluation.is_top_400)
    except Exception:
        return False


def can_resubmit(submission):
    """Top 400 idea, published, window open."""
    return (submission is not None
            and submission.status != 'draft'
            and resubmit_window_open()
            and is_top_400(submission))


def can_edit(submission):
    """May the owner change this idea's content right now?"""
    if submission is None:
        return submissions_open()
    if submission.status == 'draft':
        return submissions_open()
    return can_resubmit(submission)


def edit_mode(submission):
    """'normal' | 'resubmit' | 'locked' — what the edit form should do."""
    if submission is not None and submission.status != 'draft':
        return 'resubmit' if can_resubmit(submission) else 'locked'
    return 'normal' if submissions_open() else 'locked'
