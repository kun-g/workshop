#!/usr/bin/env python3
"""#245：只读 GitHub 历史材料，在临时插件状态目录回放需求文档核对。"""
import argparse
import json
from pathlib import Path
import tempfile

from workshop import github_identity
from workshop.deployment import assemble
from workshop.plugins.issue_delivery import IssueDelivery
from workshop.plugins.issue_delivery.io import IO
from workshop.plugins.issue_delivery.requirements import change_documents, missing_documents, section


@github_identity.scoped
def replay(target):
    with tempfile.TemporaryDirectory(prefix='workshop-requirements-') as directory:
        io = IO(gh=target['github_identity']['gh'], state_dir=directory)
        flow = IssueDelivery(io)
        owner, repo = target['repo'].split('/')
        query = '''query($owner:String!,$repo:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$repo) { issue(number:$number) {
            userContentEdits(first:100,after:$cursor) {
              nodes { editedAt diff } pageInfo { hasNextPage endCursor }
            }
          } }
        }'''
        documents = io.requirement_documents(target)
        print('身份：规则 github_identity.mode=app；只读 GitHub，状态写入临时隔离目录。')
        for number, edited_at, current, previous in (
            (173, '2026-11-18T09:30:07Z', 244, 234),
            (242, '2026-11-18T10:58:02Z', 243, None),
        ):
            cursor, body = None, None
            while True:
                # GraphQL query 通过 POST 读取；没有 mutation。
                response = io.api('graphql', data={'query': query, 'variables': {
                    'owner': owner, 'repo': repo, 'number': number, 'cursor': cursor}})
                if response.get('errors'):
                    raise RuntimeError(response['errors'])
                edits = response['data']['repository']['issue']['userContentEdits']
                body = next((n['diff'] for n in edits['nodes'] if n['editedAt'] == edited_at), None)
                if body is not None or not edits['pageInfo']['hasNextPage']:
                    break
                cursor = edits['pageInfo']['endCursor']
            assert body, f'找不到 #{number} 在 {edited_at} 的正文'
            item = {'id': f'workshop:{number}'}
            state = io.read(item, target)
            if previous:
                pr = io.api(f'repos/{target["repo"]}/pulls/{previous}')
                assert pr['merged'] and pr['merge_commit_sha']
                assert f'<!-- workshop-item: {item["id"]} -->' in pr['body']
                flow._remember_requirements(pr, target, state)
                io.save(item, target, state)
                state = io.read(item, target)
                print(json.dumps({'pr': previous, 'merge': pr['merge_commit_sha'],
                                  '已改文档': state['requirement_changes']}, ensure_ascii=False, sort_keys=True))
            files, _ = flow._changed_paths({'number': current}, target)
            basis = section(body, '依据')
            required = change_documents(basis, documents, target)
            missing = missing_documents(basis, documents, target, files, state)
            expected = ({'docs/requirements/介入点.md', 'docs/requirements/issue交付.md'} if number == 173
                        else {'docs/requirements/定时发版.md'})
            assert required == expected, required
            assert not missing, missing
            print(json.dumps({'issue': number, '正文版本': edited_at, 'pr': current,
                              '真实文件列表': [{'path': f['filename'], 'status': f['status']} for f in files],
                              '要求文档': sorted(required), '缺少文档': sorted(missing), '结果': '放行'},
                             ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rules', type=Path, default=Path(__file__).resolve().parents[1] / 'rules/workshop.yaml')
    args = parser.parse_args()
    target = assemble(args.rules).rules.target('workshop', 'issue_delivery')
    assert target['github_identity']['mode'] == 'app'
    replay(target)
