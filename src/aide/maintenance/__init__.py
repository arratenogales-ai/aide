"""Weekly maintenance.

A scheduled task of type “maintenance” (by default, Sunday at 19:00; configured in
settings.yaml) reviews security, usage, improvements, cleanup, backup, updates and the
Google authorization. Everything that can be checked with code is done with code; the
model (role “maintenance”, Sonnet with low effort) only writes the report and the
improvement proposals from that data. The user receives a single report by Telegram,
with what fails first and what to do.
"""
