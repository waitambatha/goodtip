"""Dealing stories onto the turning news deck.

One deck, three places it appears: the dashboard (two rows), and the member
and public news pages (three rows — client, Sep 2026: "lets have a max, or like
3 rows, then have it switching ... the way we have the news and blogs in the
dashboard"). All of them render partials/_news_deck.html and gt-news-cards.js
turns them. This is the only place that decides which story each place on a
deck shows on each turn.
"""

#: Cards to a row, on every deck.
ROW_CARDS = 3

#: The dashboard's deck: two rows, three full turns of stories.
DECK_ROWS = 2
DECK_STORIES = 18

#: The news pages' deck: three rows, and enough turns that the dots under it
#: are the way back to older stories — five pages of nine.
NEWS_PAGE_ROWS = 3
NEWS_PAGE_STORIES = 45


def deal_news_deck(posts, rows=DECK_ROWS):
    """The context partials/_news_deck.html reads.

    BY POSITION, NOT BY PAGE: each place on the deck turns on its own, so what
    the template needs is, for every place, the run of stories that place will
    show in turn. Wrapped rather than padded: with eight stories on a six-place
    deck the second turn shows 7, 8, 1, 2, 3, 4 instead of two cards and four
    holes. A deck of one turn is the ordinary case for a quiet news week, and
    the template switches the rotation off for it.
    """
    places = ROW_CARDS * rows
    posts = list(posts)
    count = len(posts)
    turns = -(-count // places) if count > places else 1
    slots = [
        [posts[(places * turn + place) % count] for turn in range(turns)]
        for place in range(min(count, places))
    ]
    return {
        "news_turns": turns,
        # Rows of three, because the rows take it in turns to move.
        "news_rows": [slots[i:i + ROW_CARDS] for i in range(0, len(slots), ROW_CARDS)] or [[]],
        "news_any": bool(posts),
    }
