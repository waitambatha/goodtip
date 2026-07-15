#!/usr/bin/env python
"""
Create test data for ian-test@gmail.com user
This script populates the database with sample leagues, members, rounds, matches, and tips
WITHOUT modifying any existing data.
"""

import os
import django
from datetime import timedelta
from django.utils import timezone

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'goodtip.settings')
django.setup()

from django.contrib.auth import get_user_model
from orgs.models import Organisation, OrgMember
from tipping.models import Round, Match, Tip, Team as TippingTeam
from catalog.models import Competition, Series, Season, Sport, Charity

User = get_user_model()

def main():
    print("=" * 60)
    print("Creating Test Data for ian-test@gmail.com")
    print("=" * 60)
    
    # Get the test user
    try:
        user = User.objects.get(email='ian-test@gmail.com')
        print(f"\n✓ Found user: {user.email} ({user.display_name})")
    except User.DoesNotExist:
        print("✗ User ian-test@gmail.com not found!")
        return
    
    # Get season 2026
    season = Season.objects.get(year=2026)
    print(f"✓ Using season: {season.year}")
    
    # Get or create charity
    charity, created = Charity.objects.get_or_create(
        name='Red Cross Australia',
        defaults={'slug': 'red-cross-australia', 'is_approved': True}
    )
    print(f"{'✓ Created' if created else '✓ Using'} charity: {charity.name}")
    
    # Get existing leagues for this user
    existing_leagues = Organisation.objects.filter(
        members__user=user,
        season=season
    ).distinct()
    print(f"\n✓ Found {existing_leagues.count()} existing leagues for user")
    
    # Add members to existing leagues
    print("\n--- Adding Members to Existing Leagues ---")
    for league in existing_leagues:
        existing_members = OrgMember.objects.filter(org=league).count()
        print(f"\nLeague: {league.name}")
        print(f"  Current members: {existing_members}")
        
        # Add 2 test members if not already there
        for j in range(1, 3):
            member_email = f"testmember{j}@example.com"
            try:
                member_user = User.objects.get(email=member_email)
            except User.DoesNotExist:
                member_user = User.objects.create_user(
                    email=member_email,
                    password='testpass123',
                    display_name=f'Test Member {j}'
                )
            
            member_obj, created = OrgMember.objects.get_or_create(
                org=league,
                user=member_user,
                defaults={'role': 'member'}
            )
            if created:
                print(f"  ✓ Added member: {member_email}")
            else:
                print(f"  - Member already exists: {member_email}")
    
    # Create rounds and matches for first league
    if existing_leagues.exists():
        league = existing_leagues.first()
        print(f"\n--- Creating Tipping Data for: {league.name} ---")
        
        # Get AFL series
        try:
            afl_series = Series.objects.get(slug='afl')
            print(f"✓ Using series: {afl_series.name}")
        except Series.DoesNotExist:
            print("✗ AFL series not found!")
            return
        
        # Get or create competition
        competition, created = Competition.objects.get_or_create(
            sport=afl_series.sport,
            season=season,
            slug='afl-2026',
            defaults={'name': 'AFL 2026'}
        )
        if created:
            competition.series.add(afl_series)
            print(f"✓ Created competition: {competition.name}")
        else:
            print(f"✓ Using competition: {competition.name}")
        
        # Add competition to league if not already there
        if not league.competitions.filter(id=competition.id).exists():
            league.competitions.add(competition)
            print(f"✓ Added competition to league")
        
        # Check if we need to seed teams
        teams = list(TippingTeam.objects.filter(series=afl_series))
        if not teams:
            print("\n⚠ No teams found in database. Seeding teams...")
            # Create sample teams
            team_names = [
                'Adelaide Crows', 'Brisbane Lions', 'Carlton', 'Collingwood',
                'Essendon', 'Fremantle', 'Geelong Cats', 'Gold Coast Suns',
                'Greater Western Sydney', 'Hawthorn', 'Melbourne', 'North Melbourne',
                'Port Adelaide', 'Richmond', 'St Kilda', 'Sydney Swans',
                'West Coast Eagles', 'Western Bulldogs'
            ]
            for i, name in enumerate(team_names):
                team, created = TippingTeam.objects.get_or_create(
                    name=name,
                    slug=name.lower().replace(' ', '-'),
                    series=afl_series,
                    defaults={'external_id': f'team_{i+1}'}
                )
                if created:
                    print(f"  ✓ Created team: {name}")
            teams = list(TippingTeam.objects.filter(series=afl_series))
        
        print(f"✓ Total teams available: {len(teams)}")
        
        # Create a round
        if len(teams) >= 4:
            round_obj, created = Round.objects.get_or_create(
                org=league,
                round_number=1,
                series=afl_series,
                defaults={
                    'competition': competition,
                    'lockout_at': timezone.now() + timedelta(days=7),
                    'status': 'open'
                }
            )
            if created:
                print(f"\n✓ Created round: Round {round_obj.round_number}")
            else:
                print(f"\n✓ Round already exists: Round {round_obj.round_number}")
            
            # Create matches
            matches_created = 0
            for i in range(0, min(4, len(teams)-1), 2):
                match, created = Match.objects.get_or_create(
                    round=round_obj,
                    home_team=teams[i],
                    away_team=teams[i+1],
                    defaults={
                        'kickoff_at': timezone.now() + timedelta(days=3),
                        'venue': 'MCG'
                    }
                )
                if created:
                    print(f"  ✓ Created match: {teams[i].name} vs {teams[i+1].name}")
                    matches_created += 1
                
                # Create tips for the user
                tip, created = Tip.objects.get_or_create(
                    match=match,
                    user=user,
                    org=league,
                    defaults={'selection': 'home'}
                )
                if created:
                    print(f"    ✓ User tipped: {teams[i].name}")
            
            print(f"\n✓ Total matches created: {matches_created}")
    
    print("\n" + "=" * 60)
    print("✓ Test Data Creation Complete!")
    print("=" * 60)
    print(f"\nTest User Details:")
    print(f"  Email: ian-test@gmail.com")
    print(f"  Display Name: Ian Test")
    print(f"  Leagues: {existing_leagues.count()}")
    print(f"  Members per league: 2+ (including test members)")
    print(f"\nYour client can now:")
    print(f"  1. Log in with: ian-test@gmail.com")
    print(f"  2. See existing leagues with members")
    print(f"  3. View tipping rounds and matches")
    print(f"  4. See how the system looks with data")
    print(f"\nThen they can create their own account and go through the full journey!")
    print("=" * 60)

if __name__ == '__main__':
    main()
