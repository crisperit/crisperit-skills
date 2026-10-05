"""Throwaway file for a code-walkthrough live-mode PR test. Do not merge."""


def clamp(value, low, high):
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(value, high))


def mean(values):
    if not values:
        raise ValueError("mean of an empty list")
    return sum(values) / len(values)
