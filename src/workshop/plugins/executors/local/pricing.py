"""2026-11-13, USD / million tokens, standard global API list prices.

Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
OpenAI: https://developers.openai.com/api/docs/pricing
Columns: ordinary input, cache read, cache write short, cache write long, output.
Claude short/long = 5m/1h; OpenAI = 30m/12h. Unknown models/TTL => unknown cost.
Subscription usage is valued at these API list prices, not the subscription bill.
"""
import re

PRICES = {
    'claude-opus-5-5': (4, .2, 5, 8, 20),
    'claude-sonnet-5-5': (2, .1, 2.5, 4, 10),
    'claude-opus-4-8': (5, .5, 6.25, 10, 25),
    'claude-opus-4-7': (5, .5, 6.25, 10, 25),
    'claude-opus-4-6': (5, .5, 6.25, 10, 25),
    'claude-opus-4-5': (5, .5, 6.25, 10, 25),
    'claude-sonnet-4-6': (3, .3, 3.75, 6, 15),
    'claude-sonnet-4-5': (3, .3, 3.75, 6, 15),
    'claude-haiku-4-5': (1, .1, 1.25, 2, 5),
    'gpt-6-astra': (10, 1, 12.5, 25, 50),
    'gpt-6.1-sol': (2, .1, 2.5, 5, 10),
    'gpt-6.1-terra': (1, .05, 1.25, 2.5, 5),
    'gpt-6-luna': (.1, .01, .125, .25, .5),
}


def cost(model, ordinary, read, short, long, output):
    name = re.sub(r'-\d{8}$', '', model or '')
    prices = PRICES.get(name)
    values = (ordinary, read, short, long, output)
    if prices is None or any(value is None for value in values):
        return None
    return sum(value * price for value, price in zip(values, prices)) / 1_000_000
