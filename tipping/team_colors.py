TEAM_COLORS = {
    # AFL / AFLW
    "adelaide-crows": {"primary": "#002B5C", "secondary": "#FFD200", "accent": "#E21937", "text_on_primary": "#FFFFFF"},
    "brisbane-lions": {"primary": "#A30046", "secondary": "#FDBE57", "accent": "#0055A3", "text_on_primary": "#FFFFFF"},
    "carlton": {"primary": "#031A29", "secondary": "#FFFFFF", "accent": "#031A29", "text_on_primary": "#FFFFFF"},
    "collingwood": {"primary": "#000000", "secondary": "#FFFFFF", "accent": "#000000", "text_on_primary": "#FFFFFF"},
    "essendon": {"primary": "#CC2031", "secondary": "#000000", "accent": "#CC2031", "text_on_primary": "#FFFFFF"},
    "fremantle": {"primary": "#2A0D54", "secondary": "#FFFFFF", "accent": "#2A0D54", "text_on_primary": "#FFFFFF"},
    "geelong-cats": {"primary": "#002B5C", "secondary": "#FFFFFF", "accent": "#002B5C", "text_on_primary": "#FFFFFF"},
    "gold-coast-suns": {"primary": "#D71920", "secondary": "#FFCC00", "accent": "#D71920", "text_on_primary": "#FFFFFF"},
    "gws-giants": {"primary": "#F47920", "secondary": "#231F20", "accent": "#F47920", "text_on_primary": "#FFFFFF"},
    "hawthorn": {"primary": "#4D2004", "secondary": "#FBBF15", "accent": "#4D2004", "text_on_primary": "#FFFFFF"},
    "melbourne": {"primary": "#0F1131", "secondary": "#CC2031", "accent": "#0F1131", "text_on_primary": "#FFFFFF"},
    "north-melbourne": {"primary": "#003B7C", "secondary": "#FFFFFF", "accent": "#003B7C", "text_on_primary": "#FFFFFF"},
    "port-adelaide": {"primary": "#008AAB", "secondary": "#000000", "accent": "#008AAB", "text_on_primary": "#FFFFFF"},
    "richmond": {"primary": "#FFD200", "secondary": "#000000", "accent": "#000000", "text_on_primary": "#000000"},
    "st-kilda": {"primary": "#ED1B2F", "secondary": "#000000", "accent": "#ED1B2F", "text_on_primary": "#FFFFFF"},
    "sydney-swans": {"primary": "#ED1B2F", "secondary": "#FFFFFF", "accent": "#ED1B2F", "text_on_primary": "#FFFFFF"},
    "west-coast-eagles": {"primary": "#003087", "secondary": "#F1B434", "accent": "#003087", "text_on_primary": "#FFFFFF"},
    "western-bulldogs": {"primary": "#014694", "secondary": "#E61E2B", "accent": "#FFFFFF", "text_on_primary": "#FFFFFF"},

    # NRL / NRLW
    "brisbane-broncos": {"primary": "#7B003C", "secondary": "#FFC72C", "accent": "#7B003C", "text_on_primary": "#FFFFFF"},
    "canberra-raiders": {"primary": "#88BD46", "secondary": "#FFFFFF", "accent": "#1A2B5F", "text_on_primary": "#000000"},
    "bulldogs": {"primary": "#0046AD", "secondary": "#FFFFFF", "accent": "#0046AD", "text_on_primary": "#FFFFFF"},
    "sharks": {"primary": "#00A6A0", "secondary": "#000000", "accent": "#00A6A0", "text_on_primary": "#FFFFFF"},
    "dolphins": {"primary": "#BE0027", "secondary": "#FFD200", "accent": "#BE0027", "text_on_primary": "#FFFFFF"},
    "titans": {"primary": "#FFB81C", "secondary": "#003F87", "accent": "#003F87", "text_on_primary": "#000000"},
    "sea-eagles": {"primary": "#6E0F3C", "secondary": "#FFFFFF", "accent": "#6E0F3C", "text_on_primary": "#FFFFFF"},
    "storm": {"primary": "#1B1464", "secondary": "#92278F", "accent": "#FFCD00", "text_on_primary": "#FFFFFF"},
    "knights": {"primary": "#EE3524", "secondary": "#003D7C", "accent": "#EE3524", "text_on_primary": "#FFFFFF"},
    "warriors": {"primary": "#231F20", "secondary": "#9B1B30", "accent": "#9B1B30", "text_on_primary": "#FFFFFF"},
    "cowboys": {"primary": "#002B5C", "secondary": "#FFC72C", "accent": "#002B5C", "text_on_primary": "#FFFFFF"},
    "eels": {"primary": "#006EB5", "secondary": "#FFD200", "accent": "#006EB5", "text_on_primary": "#FFFFFF"},
    "panthers": {"primary": "#000000", "secondary": "#00543D", "accent": "#000000", "text_on_primary": "#FFFFFF"},
    "rabbitohs": {"primary": "#00543D", "secondary": "#E30613", "accent": "#00543D", "text_on_primary": "#FFFFFF"},
    "dragons": {"primary": "#E30613", "secondary": "#FFFFFF", "accent": "#E30613", "text_on_primary": "#FFFFFF"},
    "roosters": {"primary": "#002B5C", "secondary": "#E30613", "accent": "#002B5C", "text_on_primary": "#FFFFFF"},
    "wests-tigers": {"primary": "#F68B1F", "secondary": "#000000", "accent": "#000000", "text_on_primary": "#000000"},
}

DEFAULT_COLORS = {"primary": "#2B463D", "secondary": "#FFFFFF", "accent": "#7A8F87", "text_on_primary": "#FFFFFF"}


def get_team_colors(slug: str) -> dict:
    return TEAM_COLORS.get(slug, DEFAULT_COLORS)


def team_initials(name: str, max_chars: int = 3) -> str:
    if not name:
        return "?"
    words = [w for w in name.replace("-", " ").replace(".", " ").split() if w]
    if not words:
        return name[:1].upper()
    if len(words) == 1:
        return words[0][:1].upper()
    return "".join(w[:1].upper() for w in words[:max_chars])
