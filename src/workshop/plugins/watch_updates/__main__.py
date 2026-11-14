"""可以独立运行的清单和需要台账检查，不访问网络。"""
import argparse

from .flow import Flow
from .io import IO


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--watchlist', required=True)
    parser.add_argument('--needs', required=True)
    args = parser.parse_args()
    target = {'watch_updates': vars(args)}
    io = IO()
    rows = io.watchlist(target)['software']
    needs = io.needs(target)
    Flow.validate_needs(needs)
    print(f'清单与需要台账检查通过：{len(rows)} 个软件、{len(needs)} 条需要')


if __name__ == '__main__':
    main()
