"""Reading the user's Gmail: alerts about what is important.

Read-only: lists and reads new emails, never deletes, archives or marks. First rules without a
model (sender, domain and words); only the doubtful ones go to a cheap model, which sees the
sender, the subject and the first lines. It is off until the user gives the go-ahead
(`email.active` in settings.yaml).
"""
