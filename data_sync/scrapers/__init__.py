"""Scrapers that read official league sites directly.

Preferred over third-party sports APIs for two reasons that showed up in
practice rather than in theory:

  * the API path never worked. api.thesportsapi.com had no DNS record at all,
    and its replacement (API-SPORTS) needs a paid key that was never issued,
    so every NRL sync since the project began has failed.
  * the free community API that did work for AFL (Squiggle) rate-limits, and
    started returning 403 the moment traffic-driven syncing turned on.

The official sites embed their own data as JSON inside the page, which is what
makes this reliable rather than the usual brittle HTML scraping: we read the
same payload the site's own front-end reads, so a restyle does not break us.
"""
