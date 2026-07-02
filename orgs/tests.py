from django.contrib.auth import get_user_model
from django.test import TestCase

from catalog.models import Charity, Season, Sport

from .models import (
    CharityVote,
    CharityVoteOption,
    OrgCharitySelection,
    OrgMember,
    Organisation,
)
from .services import (
    cast_charity_ballot,
    close_charity_vote,
    open_charity_vote,
    record_charity_selection,
    set_org_charity,
)

User = get_user_model()


class CharityTimelineTests(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.lifeline, _ = Charity.objects.get_or_create(slug="lifeline", defaults={"name": "Lifeline", "is_approved": True})
        self.beyondblue, _ = Charity.objects.get_or_create(slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True})
        self.org = Organisation.objects.create(name="Acme", season=self.season, charity=self.lifeline)

    def test_record_initial_then_change_appends_history(self):
        record_charity_selection(self.org, self.lifeline, source=OrgCharitySelection.SOURCE_INITIAL)
        # Re-recording the same charity is a no-op (no duplicate row).
        record_charity_selection(self.org, self.lifeline, source=OrgCharitySelection.SOURCE_INITIAL)
        self.assertEqual(self.org.charity_selections.count(), 1)

        set_org_charity(self.org, self.beyondblue, source=OrgCharitySelection.SOURCE_MANUAL)
        self.org.refresh_from_db()
        self.assertEqual(self.org.charity, self.beyondblue)

        history = list(self.org.charity_selections.values_list("charity__name", flat=True))
        # Newest first per Meta.ordering.
        self.assertEqual(history, ["Beyond Blue", "Lifeline"])
        self.assertEqual(self.org.charity_selections.count(), 2)

    def test_closing_vote_records_a_selection(self):
        vote = open_charity_vote(self.org, [self.lifeline, self.beyondblue])
        voter = User.objects.create_user(email="v@example.com", password="x", display_name="V")
        OrgMember.objects.create(user=voter, org=self.org, role=OrgMember.ROLE_PARTICIPANT)
        option = vote.options.get(charity=self.beyondblue)
        cast_charity_ballot(user=voter, vote=vote, option=option)

        winner = close_charity_vote(vote)
        self.assertEqual(winner, self.beyondblue)
        self.org.refresh_from_db()
        self.assertEqual(self.org.charity, self.beyondblue)
        latest = self.org.charity_selections.first()
        self.assertEqual(latest.charity, self.beyondblue)
        self.assertEqual(latest.source, OrgCharitySelection.SOURCE_VOTE)
        self.assertEqual(latest.season, self.season)


class PaymentCharityFreezeTests(TestCase):
    def setUp(self):
        from billing import donations

        self.donations = donations
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.lifeline, _ = Charity.objects.get_or_create(slug="lifeline", defaults={"name": "Lifeline", "is_approved": True})
        self.beyondblue, _ = Charity.objects.get_or_create(slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True})
        self.org = Organisation.objects.create(name="Acme", season=self.season, charity=self.lifeline)
        self.user = User.objects.create_user(email="p@example.com", password="x", display_name="P")

    def test_payment_keeps_charity_after_org_switches(self):
        from decimal import Decimal

        pledge = self.donations.set_pledge(self.org, pledged_amount=Decimal("100"))
        self.donations.record_topup(pledge, participant=self.user, amount=Decimal("20"))
        early_payment = pledge.payments.first()
        self.assertEqual(early_payment.charity, self.lifeline)

        # Org switches charity; re-pledge mirrors the new one onto the pledge.
        set_org_charity(self.org, self.beyondblue, source=OrgCharitySelection.SOURCE_MANUAL)
        self.donations.set_pledge(self.org, pledged_amount=Decimal("100"))

        early_payment.refresh_from_db()
        # The historical payment must still point at the original charity.
        self.assertEqual(early_payment.charity, self.lifeline)
