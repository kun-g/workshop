"""规则路径匹配：目录前缀、glob；重命名的前后路径必须一起判定。"""
from fnmatch import fnmatchcase


def only_ignored_paths(files, patterns):
    if not patterns:
        return False
    if (not isinstance(patterns, list) or not all(isinstance(p, str) and p and not p.startswith('/')
            and '..' not in p.split('/') for p in patterns)):
        raise ValueError('release_ignored_paths 必须为相对目录或 glob 列表')
    if not isinstance(files, list):
        raise ValueError('没有取得完整改动文件列表')
    def matched(path):
        if not isinstance(path, str) or not path or path.startswith('/') or '..' in path.split('/'):
            raise ValueError('改动路径无效')
        for pattern in patterns:
            glob = pattern + '*' if pattern.endswith('/') else pattern
            if fnmatchcase(path, glob) or glob.startswith('**/') and fnmatchcase(path, glob[3:]):
                return True
        return False
    for row in files:
        if not isinstance(row, dict) or not matched(row.get('filename')):
            return False
        if row.get('status') == 'renamed' and not row.get('previous_filename'):
            return False
        if row.get('previous_filename') and not matched(row['previous_filename']):
            return False
    return True
