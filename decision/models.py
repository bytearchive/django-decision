import os

from django.conf import settings
from django.utils.translation import ugettext as _
from django.db import models
from django.db.models import Count, Q, signals
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection

from .exceptions import *


def get_poll_choice_cache_key(poll, choice):
    poll_pk = poll if isinstance(poll, int) else poll.pk
    choice_pk = choice if isinstance(choice, int) else choice.pk
    return 'decision:pc:%s:%s' % (poll_pk, choice)


def get_user_choice_cache_key(poll, user):
    poll_pk = poll if isinstance(poll, int) else poll.pk
    user_pk = user if isinstance(user, int) else user.pk
    return 'decision:uv:%s:%s' % (poll_pk, user.pk)


class Category(models.Model):
    name = models.CharField(max_length=200)

    def __unicode__(self):
        return self.name


class Poll(models.Model):
    name = models.CharField(max_length=255)
    category = models.ForeignKey(Category, null=True, blank=True)
    is_open = models.BooleanField(default=True)

    def set_vote(self, user, choice, delegate=None, secure=True):
        """ Ensure the vote is legal, save it and propagate it. """
        if not self.is_open:
            raise PollClosed()

        if not isinstance(choice, Choice) or choice.poll != self:
            raise InvalidChoice()

        if secure and delegate:
            try:
                Delegation.objects.get(Q(categories=None) | Q(
                    categories=self.category),
                    leader=delegate, follower=user)
            except Delegation.DoesNotExist:
                raise Exception()

        try:
            vote = self.votes.get(user=user)
        except Vote.DoesNotExist:
            vote = self.votes.create(user=user, choice=choice,
                    delegate=delegate)
        else:
            if secure and delegate and vote.delegate is None:
                raise Exception()

            vote.choice = choice
            vote.delegate = delegate
            vote.save()

        return vote

    def get_vote(self, user):
        return self.votes.get(user=user)

    def get_user_choice(self, user):
        try:
            return self.get_vote(user=user).choice
        except Vote.DoesNotExist:
            return


class Choice(models.Model):
    poll = models.ForeignKey(Poll, related_name='choices')
    name = models.CharField(max_length=255)
    vote_count = models.IntegerField(default=0)


class Vote(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='votes')
    poll = models.ForeignKey('Poll', related_name='votes')
    choice = models.ForeignKey(Choice, related_name='votes')
    delegate = models.ForeignKey(settings.AUTH_USER_MODEL,
            related_name='delegated_votes', null=True, blank=True)

    class Meta:
        unique_together = ('user', 'poll')
        ordering = ('pk',)


class Delegation(models.Model):
    follower = models.ForeignKey(settings.AUTH_USER_MODEL,
        related_name='delegations_as_follower')
    leader = models.ForeignKey(settings.AUTH_USER_MODEL,
        related_name='delegations_as_leader')
    categories = models.ManyToManyField(Category, blank=True)

    class Meta:
        unique_together = ('leader', 'follower')


def prevent_delegation_to_self(sender, instance, **kwargs):
    if instance.leader == instance.follower:
        raise CantDelegateToSelf()
signals.pre_save.connect(prevent_delegation_to_self, sender=Delegation)


def propagate_vote(sender, instance, **kwargs):
    if instance.poll.category:
        extra_condition = '''
            d.id NOT IN (
                SELECT
                    dc.delegation_id
                FROM
                    decision_delegation_categories AS dc
            )
            OR
            d.id IN (
                SELECT
                    dc.delegation_id
                FROM
                    decision_delegation_categories AS dc
                WHERE
                    dc.category_id = %s
            )
        ''' % instance.poll.category_id
    else:
        extra_condition = '''
            (
                SELECT
                    COUNT(id)
                FROM
                    decision_delegation_categories AS dc
                WHERE
                    dc.delegation_id = d.id
            ) < 1
        '''

    if 'postgres' in settings.DATABASES['default']['ENGINE']:
        null = 'null::INTEGER'
    else:
        null = 'null'

    sql = '''
    INSERT INTO decision_vote (
            user_id,
            poll_id,
            choice_id,
            delegate_id
        ) WITH RECURSIVE dlg(dlg_user_id, dlg_poll_id,
                             dlg_choice_id, dlg_delegate_id) as (
            VALUES(%s, %s, %s, {null})
            UNION
            SELECT
                d.follower_id,
                dlg_poll_id,
                dlg_choice_id,
                d.leader_id
            FROM
                decision_delegation AS d
            INNER JOIN dlg ON dlg.dlg_user_id = d.leader_id
            WHERE
                d.leader_id = dlg_user_id
                AND
                (
                    {extra_condition}
                )
                AND
                (
                    SELECT
                        COUNT(v.id)
                    FROM
                        decision_vote AS v
                    WHERE
                        v.user_id = d.follower_id
                        AND
                        v.poll_id = dlg_poll_id
                ) < 1
        )
        SELECT
            dlg_user_id,
            dlg_poll_id,
            dlg_choice_id,
            dlg_delegate_id
        FROM
            dlg
        WHERE
            dlg_user_id != %s
    '''

    sql = sql.replace('{extra_condition}', extra_condition)
    sql = sql.replace('{null}', null)

    if os.environ.get('TRAVIS', False):
        print [instance.user_id, instance.poll_id,
            instance.choice_id, instance.user_id], sql

    with connection.cursor() as c:
        c.execute(sql, [instance.user_id, instance.poll_id,
            instance.choice_id, instance.user_id])
signals.post_save.connect(propagate_vote, sender=Vote)
