import ipaddress
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse
from django.middleware.csrf import rotate_token
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST
from reportlab.pdfbase.ttfonts import TTFError

from .models import Ballot, Candidate, Election, Identity
from .pdf import credential_pdf
from .services import authenticate, create_identity, manage_election, password_for, submit_ballot


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
    if identity:
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
            submit_ballot(request.identity, request.POST.getlist('candidate'), request.POST.get('version'), request.POST.get('blank') == 'yes')
        except ValidationError as exc:
            error = ' '.join(exc.messages)
        else:
            messages.success(request, '選票已儲存。開放期間可再次提交，以最後成功提交的選票為準。')
            return redirect('vote')
    with transaction.atomic():
        election = Election.objects.get(pk=1)
        ballot = Ballot.objects.filter(voter=request.identity).first()
        selected = set(ballot.choices.values_list('pk', flat=True)) if ballot else set()
        candidates = list(Candidate.objects.all())
    if error and str(election.version) == request.POST.get('version') and election.is_open:
        selected = {int(value) for value in request.POST.getlist('candidate') if value.isdecimal() and len(value) < 12}
    return render(request, 'election/vote.html', {'identity': request.identity, 'election': election, 'ballot': ballot, 'selected': selected, 'candidates': candidates, 'error': error})


@access(admin=True)
@require_http_methods(['GET', 'POST'])
def dashboard(request):
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            if action == 'user':
                create_identity(request.POST.get('name', ''))
            else:
                manage_election(action, request.POST.get('confirmation' if action == 'clear' else 'name', ''))
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        else:
            messages.success(request, '操作已完成。')
        return redirect('dashboard')
    with transaction.atomic():
        election = Election.objects.get(pk=1)
        users = list(Identity.objects.filter(is_admin=False).annotate(ballot_count=Count('ballot')))
        results = list(Candidate.objects.annotate(votes=Count('choice')).order_by('-votes', 'id'))
        voted = Ballot.objects.count()
        blank = Ballot.objects.annotate(total=Count('choices')).filter(total=0).count()
    last_votes, rank = None, 0
    for position, candidate in enumerate(results, 1):
        if candidate.votes != last_votes:
            rank = position
        candidate.rank = rank
        last_votes = candidate.votes
    for user in users:
        user.password_display = password_for(user)
    return render(request, 'election/dashboard.html', {'identity': request.identity, 'election': election, 'users': users, 'results': results, 'voted': voted, 'blank': blank, 'total': len(users), 'unvoted': len(users) - voted})


@access(admin=True)
@require_POST
def credentials(request):
    users = Identity.objects.filter(is_admin=False)
    if request.POST.get('scope') != 'all':
        ids = request.POST.getlist('users')
        if any(not value.isdecimal() or len(value) > 12 for value in ids):
            return render(request, 'election/error.html', {'error': '用戶資料無效。'}, status=400)
        users = users.filter(pk__in=ids)
    users = list(users)
    if not users:
        messages.error(request, '請至少選擇一位用戶。')
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
