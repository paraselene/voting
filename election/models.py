from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import m2m_changed
from django.dispatch import receiver


class Identity(models.Model):
    name = models.CharField(max_length=80, blank=True)
    number = models.PositiveSmallIntegerField(null=True, unique=True)
    is_admin = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    password_index = models.CharField(max_length=64, unique=True)
    encrypted_password = models.TextField(blank=True)

    class Meta:
        ordering = ['id']
        constraints = [models.CheckConstraint(condition=(models.Q(is_admin=True, number__isnull=True) | models.Q(is_admin=False, name='', number__gte=1, number__lte=19, number__isnull=False)), name='numbered_voter_or_admin')]


class Candidate(models.Model):
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ['id']


class Election(models.Model):
    title = models.CharField(max_length=40)
    max_votes = models.PositiveSmallIntegerField()
    candidates = models.ManyToManyField(Candidate)
    created_at = models.DateTimeField(auto_now_add=True)
    is_open = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['is_open'], condition=models.Q(is_open=True), name='one_open_election'),
            models.CheckConstraint(condition=models.Q(max_votes__gte=1, max_votes__lte=19), name='valid_vote_limit'),
        ]

    @property
    def number(self):
        # Keep internal IDs unique so pre-reset forms cannot target a new round.
        return Election.objects.filter(pk__lte=self.pk).count()

    def save(self, *args, **kwargs):
        if self.pk:
            old = Election.objects.filter(pk=self.pk).first()
            if old and (old.title != self.title or old.max_votes != self.max_votes):
                raise ValidationError('已建立的輪次設定不能更改。')
        super().save(*args, **kwargs)


class Ballot(models.Model):
    election = models.ForeignKey(Election, on_delete=models.CASCADE)
    voter = models.ForeignKey(Identity, on_delete=models.PROTECT)
    submitted_at = models.DateTimeField(auto_now=True)
    choices = models.ManyToManyField(Candidate, through='Choice')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['election', 'voter'], name='one_ballot_per_round')]


class Choice(models.Model):
    ballot = models.ForeignKey(Ballot, on_delete=models.CASCADE)
    candidate = models.ForeignKey(Candidate, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['ballot', 'candidate'], name='unique_ballot_candidate')]


class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    failures = models.PositiveIntegerField(default=0)
    last_failure = models.DateTimeField()
    blocked_until = models.DateTimeField()


# Membership is written once when the round is created.


@receiver(m2m_changed, sender=Election.candidates.through)
def freeze_candidates(sender, instance, action, reverse, pk_set, **kwargs):
    if action in ('pre_clear', 'pre_remove') and (pk_set is None or pk_set):
        raise ValidationError('已建立的候選人名單不能更改。')
    if action == 'pre_add' and pk_set:
        if reverse or instance.candidates.exists():
            raise ValidationError('已建立的候選人名單不能更改。')
