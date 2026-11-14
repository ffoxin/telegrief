"""Errors shared by the services the search index calls."""


class Busy(Exception):
    """The service is rate-limiting us: wait and try again, nothing is wrong with the post."""
