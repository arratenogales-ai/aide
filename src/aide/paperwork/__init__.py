"""Your documents: contracts, insurance, the permit, town hall paperwork, car
papers...

They arrive on Telegram with “documento” at the start of the caption and the code saves
them in data/documents, outside git and outside what the agent can read, and inside the
weekly backup. When saving them, the model reads them once and extracts their dates
(expirations, renewals, deadlines) and how far ahead notice must be given to cancel
them; the notice deadline is computed by the code, which also checks that each phrase is
in the document. The user approves it with “Save”. Reminders for those dates are by
code, without a model: 60, 30 and 7 days before, and the same day. To ask about them,
the agent has a tool that only searches what is saved and cites the document.
"""

from datetime import date

# Days before each date on which a reminder is sent (and the same day).
NOTICES = (60, 30, 7, 0)
# The months in English, independent of the system locale.
SHORT_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def short_date(day: date) -> str:
    """“Dec 31, 2027”: how a document's date is shown."""
    return f"{SHORT_MONTHS[day.month - 1]} {day.day}, {day.year}"
