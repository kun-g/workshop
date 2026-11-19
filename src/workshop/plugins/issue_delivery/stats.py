"""PR 概况（核心 R10、R24）：合并确认时按文件列表算改动文件数、增删行数和「改了什么」。

按 PR 号记进活状态；改了什么只看路径，不看人写的标签：
文档是 `docs/` 下的文件和 `*.md`；测试是路径里有 `tests/` 的文件和 `test_*.py`；
其余都算程序，包括规则文件、脚本、`.github/`。重命名的文件新旧路径都符合才算。
"""

DOC = "只改文档"
TEST = "只改测试和文档"
PROGRAM = "改了程序"
UNKNOWN = "未知"

# pull files 一次最多给这么多；到了上限就可能没取全，分类不可信。
FILE_LIMIT = 3000

# 多 PR 合起来取最重的一档。
HEAVIER = {DOC: 0, TEST: 1, PROGRAM: 2}


def _doc(path):
    return path.startswith("docs/") or path.endswith(".md")


def _test(path):
    return "tests" in path.split("/") or (path.rsplit("/", 1)[-1].startswith("test_")
                                          and path.endswith(".py"))


def _file_kind(entry):
    paths = [entry.get("filename") or ""]
    if entry.get("previous_filename"):
        paths.append(entry["previous_filename"])
    kinds = {("doc" if _doc(path) else "test" if _test(path) else "program") for path in paths}
    if kinds == {"doc"}:
        return "doc"
    if kinds <= {"doc", "test"}:
        return "test"
    return "program"


def classify(files, changed_files):
    """一个 PR 的概况。文件数超过上限或和 PR 报的总数对不上时，分类记「未知」。"""
    additions = sum(int(entry.get("additions") or 0) for entry in files)
    deletions = sum(int(entry.get("deletions") or 0) for entry in files)
    kinds = {_file_kind(entry) for entry in files}
    if len(files) > FILE_LIMIT or type(changed_files) is not int or len(files) != changed_files:
        kind = UNKNOWN
    elif "program" in kinds:
        kind = PROGRAM
    elif "test" in kinds:
        kind = TEST
    else:
        kind = DOC
    return {"files": len(files), "additions": additions, "deletions": deletions, "kind": kind}


def combine(stats):
    """一件活所有 PR 合起来：行数相加，分类取最重的一档；有一个未知就整件未知。

    没有概况或概况格式不对时返回 None。
    """
    if not isinstance(stats, dict):
        return None
    entries = [value for value in stats.values() if isinstance(value, dict)
               and type(value.get("additions")) is int and type(value.get("deletions")) is int]
    if not entries:
        return None
    kinds = [value.get("kind") for value in entries]
    kind = UNKNOWN if any(kind not in HEAVIER for kind in kinds) else max(kinds, key=HEAVIER.get)
    return {"lines": sum(value["additions"] + value["deletions"] for value in entries), "kind": kind}
