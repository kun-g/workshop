"""Standard global API USD / million tokens; every row carries its dated primary source.

Rate order: ordinary input, cache read, short write, long write, output.
Claude write TTLs: 5m/1h. OpenAI: 30m/12h. Unknown model/TTL => null cost.
"""
from dataclasses import dataclass
import re

ANTHROPIC = 'https://platform.claude.com/docs/en/about-claude/pricing'
OPENAI = 'https://developers.openai.com/api/docs/pricing'


@dataclass(frozen=True)
class Price:
    rates: tuple[float, float, float, float, float]
    source: str
    checked: str
    over_100k: tuple[float, float, float, float, float] | None = None


PRICES = {
    'claude-opus-5-5': Price((4, .2, 5, 8, 20), ANTHROPIC, '2026-11-13'),
    'claude-sonnet-5-5': Price((2, .1, 2.5, 4, 10), ANTHROPIC, '2026-11-13'),
    'claude-haiku-5-5': Price((.1, .01, .125, .2, .5), ANTHROPIC, '2026-11-13', (.5, .05, .625, 1, 2.5)),
    'claude-opus-5': Price((5, .5, 6.25, 10, 25), ANTHROPIC, '2026-11-13'),
    'claude-sonnet-5': Price((2, .2, 2.5, 4, 10), ANTHROPIC, '2026-11-13'),
    'claude-fable-5': Price((10, 1, 12.5, 20, 50), ANTHROPIC, '2026-11-13'),
    'claude-fable-5-1': Price((10, .25, 12.5, 20, 50), ANTHROPIC, '2026-11-13'),
    'claude-fable-5-2': Price((8, .2, 10, 16, 40), ANTHROPIC, '2026-11-13'),
    'claude-opus-4-8': Price((5, .5, 6.25, 10, 25), ANTHROPIC, '2026-11-13'),
    'claude-opus-4-7': Price((5, .5, 6.25, 10, 25), ANTHROPIC, '2026-11-13'),
    'claude-opus-4-6': Price((5, .5, 6.25, 10, 25), ANTHROPIC, '2026-11-13'),
    'claude-opus-4-5': Price((5, .5, 6.25, 10, 25), ANTHROPIC, '2026-11-13'),
    'claude-sonnet-4-6': Price((3, .3, 3.75, 6, 15), ANTHROPIC, '2026-11-13'),
    'claude-sonnet-4-5': Price((3, .3, 3.75, 6, 15), ANTHROPIC, '2026-11-13'),
    'claude-haiku-4-5': Price((1, .1, 1.25, 2, 5), ANTHROPIC, '2026-11-13'),
    'gpt-6-astra': Price((10, 1, 12.5, 25, 50), OPENAI, '2026-11-13'),
    'gpt-6.1-sol': Price((2, .1, 2.5, 5, 10), OPENAI, '2026-11-13'),
    'gpt-6.1-terra': Price((1, .05, 1.25, 2.5, 5), OPENAI, '2026-11-13'),
    'gpt-6-luna': Price((.1, .01, .125, .25, .5), OPENAI, '2026-11-13'),
}


def cost(model, ordinary, read, short, long, output):
    name = re.sub(r'-\d{8}$', '', model or '')
    price = PRICES.get(name)
    values = (ordinary, read, short, long, output)
    if price is None or any(value is None for value in values):
        return None
    rates = price.over_100k if price.over_100k and sum(values[:4]) > 100_000 else price.rates
    return sum(value * rate for value, rate in zip(values, rates)) / 1_000_000
