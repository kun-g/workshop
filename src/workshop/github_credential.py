"""Git helper: return only the current process token for its exact repository."""
import os
import sys


def main():
    if sys.argv[1:] != ['get']:
        return
    fields = dict(line.rstrip('\n').split('=', 1) for line in sys.stdin if '=' in line)
    repo = fields.get('path', '').removesuffix('.git').strip('/')
    if (fields.get('protocol') == 'https' and fields.get('host') == 'github.com'
            and repo.casefold() == os.environ.get('WORKSHOP_GITHUB_REPO', '').casefold()
            and repo and os.environ.get('GH_TOKEN')):
        print('username=x-access-token\npassword=' + os.environ['GH_TOKEN'])


if __name__ == '__main__':
    main()
