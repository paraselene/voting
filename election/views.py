import ipaddress
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.middleware.csrf import rotate_token
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST
from reportlab.pdfbase.ttfonts import TTFError

from .models import Ballot, Candidate, Election, Identity
from .pdf import credential_pdf
from .services import authenticate, manage_election, submit_ballot, voter_numbers


def access(admin=False):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            identity = Identity.objects.filter(pk=request.session.get('identity')).first()
            if identity is None:
                return redirect('login')
            if identity.is_admin != admin:
                return render(request, 'election/error.html', {'error': '你沒有此頁面的存取權限。'}, status=403)
            request.identity = identity
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


def client_ip(request):
    remote = request.META.get('REMOTE_ADDR', '')
    if remote in settings.TRUSTED_PROXY_IPS:
        try:
            return str(ipaddress.ip_address(request.META.get('HTTP_X_REAL_IP', '')))
        except ValueError:
            pass
    return remote


@sensitive_post_parameters('password')
@require_http_methods(['GET', 'POST'])
def login(request):
    identity = Identity.objects.filter(pk=request.session.get('identity')).first()
    if identity and request.GET.get('qr') != '1':
        return redirect('dashboard' if identity.is_admin else 'vote')
    error = ''
    if request.method == 'POST':
        identity, error = authenticate(request.POST.get('password', ''), client_ip(request))
        if identity:
            request.session.cycle_key()
            rotate_token(request)
            request.session['identity'] = identity.pk
            return redirect('dashboard' if identity.is_admin else 'vote')
    return render(request, 'election/login.html', {'error': error})


@require_POST
def logout(request):
    request.session.flush()
    return redirect('login')


@access()
@require_http_methods(['GET', 'POST'])
def vote(request):
    error = ''
    if request.method == 'POST':
        try:
            submit_ballot(request.identity, request.POST.getlist('candidate'), request.POST.get('election_id'), request.POST.get('version'), request.POST.get('blank') == 'yes')
        except ValidationError as exc:
            error = ' '.join(exc.messages)
        else:
            messages.success(request, '選票已儲存。開放期間可再次提交，以最後成功提交的選票為準。')
            return redirect('vote')
    with transaction.atomic():
        election = Election.objects.order_by('-pk').first()
        ballot = Ballot.objects.filter(election=election, voter=request.identity).first()
        selected = set(ballot.choices.values_list('pk', flat=True)) if ballot else set()
        candidates = list(election.candidates.all()) if election else []
        request.identity.display_number = voter_numbers()[request.identity.pk]
    if error and election and str(election.pk) == request.POST.get('election_id') and str(election.version) == request.POST.get('version') and election.is_open:
        selected = {int(value) for value in request.POST.getlist('candidate') if value.isdecimal() and len(value) < 12}
    return render(request, 'election/vote.html', {'identity': request.identity, 'election': election, 'ballot': ballot, 'selected': selected, 'candidates': candidates, 'error': error})


@access(admin=True)
@require_http_methods(['GET', 'POST'])
def dashboard(request):
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            manage_election(action, request.POST.get('confirmation', ''), election_id=request.POST.get('election_id'), version=request.POST.get('version'), title=request.POST.get('title', ''), selected=request.POST.getlist('candidate'), maximum=request.POST.get('maximum'))
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        else:
            messages.success(request, '操作已完成。')
        return redirect('dashboard')
    with transaction.atomic():
        latest = Election.objects.order_by('-pk').first()
        rounds = list(Election.objects.order_by('-pk'))
        election = latest
        requested = request.GET.get('round', '')
        if requested.isdecimal() and len(requested) < 12:
            election = Election.objects.filter(pk=requested).first() or latest
        users = list(Identity.objects.filter(is_admin=False).annotate(ballot_count=Count('ballot', filter=Q(ballot__election=election))))
        results = election.candidates.all() if election else Candidate.objects.none()
        if election and not election.is_open:
            results = results.annotate(votes=Count('choice', filter=Q(choice__ballot__election=election))).order_by('-votes', 'id')
        results = list(results)
        ballots = Ballot.objects.filter(election=election)
        voted = ballots.count()
    if election and not election.is_open:
        last_votes, rank = None, 0
        for position, candidate in enumerate(results, 1):
            if candidate.votes != last_votes:
                rank = position
            candidate.rank = rank
            last_votes = candidate.votes
    return render(request, 'election/dashboard.html', {'identity': request.identity, 'latest': latest, 'rounds': rounds, 'titles': settings.ELECTION_TITLES, 'roster': Candidate.objects.all(), 'election': election, 'users': users, 'results': results, 'voted': voted, 'total': len(users), 'unvoted': len(users) - voted})


@access(admin=True)
@require_POST
def credentials(request):
    users = list(Identity.objects.filter(is_admin=False))
    if not users:
        messages.error(request, '尚未建立用戶，請先初始化資料。')
        return redirect('dashboard')
    try:
        data = credential_pdf(users)
    except (OSError, ValueError, TTFError):
        return render(request, 'election/error.html', {'error': '無法產生 PDF，請檢查中文字型及憑證內容長度。'}, status=503)
    response = HttpResponse(data, content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="voting-credentials.pdf"'
    return response


def csrf_failure(request, reason=''):
    return render(request, 'election/error.html', {'error': '頁面已過期或驗證失敗，請重新載入後再試。'}, status=403)
