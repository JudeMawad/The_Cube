"""Spoken media text, shared by dialogue and asynchronous events."""

def request_ack(title, tracked=True):
    return f"Requested {title}." + (" I'll let you know when it's ready." if tracked else "")

RECENT_REQUEST_SECONDS = 120


def download_started(title, requested_at, now, media_type="movie"):
    if now - requested_at < RECENT_REQUEST_SECONDS:
        return f"{title} just started downloading."
    noun = "movie" if media_type == "movie" else "episode"
    return f"Hey, the {noun} you requested earlier, {title}, just started downloading."


def ready_to_watch(title):
    return f"Hey, quick update — {title} is ready to watch."


def clarification(movies):
    if len(movies) > 3:
        return "I found several possible matches. What is the full title and year?"
    years = [m.year for m in movies]
    if len({m.title.lower() for m in movies}) == 1 and all(years) and len(set(years)) == len(years):
        choices = " or ".join(f"the {year} one" for year in sorted(years))
        return f"Which one do you mean — {choices}?"
    return "Which one do you mean — " + " or ".join(m.label for m in movies) + "?"
