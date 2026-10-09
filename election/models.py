from django.db import models


class Identity(models.Model):
    name = models.CharField(max_length=80)
    is_admin = models.BooleanField(default=False)
    password_index = models.CharField(max_length=64, unique=True)
    encrypted_password = models.TextField(blank=True)

    class Meta:
        ordering = ['id']


class Candidate(models.Model):
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ['id']


class Election(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    is_open = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='single_election')]


class Ballot(models.Model):
    voter = models.OneToOneField(Identity, on_delete=models.PROTECT)
    submitted_at = models.DateTimeField(auto_now=True)
    choices = models.ManyToManyField(Candidate, through='Choice')


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
