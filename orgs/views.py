from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.views import JOIN_SESSION_KEY
from .models import OrgMember, Organisation
from .signing import parse_join_token


def join_view(request, org_id: int, token: str):
    parsed_id = parse_join_token(token)
    if parsed_id is None or parsed_id != org_id:
        return render(request, "join_invalid.html", status=400)
    org = get_object_or_404(Organisation, pk=org_id)
    if request.user.is_authenticated:
        OrgMember.objects.get_or_create(user=request.user, org=org, defaults={"role": "member"})
        messages.success(request, f"Joined {org.name}.")
        return redirect("dashboard")
    request.session[JOIN_SESSION_KEY] = org.id
    signup_url = reverse("accounts:signup")
    return render(request, "join_prompt.html", {"org": org, "signup_url": signup_url})
