"""开单提示（ws#326）：约束和 DoD、正文里机械能查的路径、命令冲突。

只提示，不阻断接单，不改登记核对的结论（dod.registration 照旧）。语义上的矛盾查不出，仍靠开单前另找模型看。
比对范围收紧：只认约束里反引号写的「只改」「不改」路径；正文、DoD 里只认同一句带未否定的改动动词的路径；
代码块、「约束」「Kun 的原话」两节、需求变更那一段不比对路径。
"""
import re

from .dod import items
from .requirements import change_documents, changed_requirement, registered_paths, requirement_basis, section

# 子命令只对车间自己的交付对象查：命令表是车间命令行的。
SELF = "workshop"
SKIPPED = ("约束", "Kun 的原话")
SCOPE = re.compile(r"(只改|不改)(?!变)[^`。；;，,\n]*?((?:`[^`\n]+`[\s、,，和及与或]*)+)")
VERB = re.compile(r"(?<![不无别勿未])(?<!没有)(?<!不要)(?<!不得)(?<!不会)(?<!不用)(?:新增|修改|改成|写入|删除)")
SENTENCE = re.compile(r"[。；;！？!?\n]")
FENCED = re.compile(r"^\s*(```|~~~).*?^\s*\1[^\n]*$", re.M | re.S)
URL = re.compile(r"\S+://\S+")
BARE = re.compile(r"(?<![\w/.\-`])((?:[A-Za-z0-9_.\-]+/)+(?:[^\s/`、，。；：:,;()（）「」]*?\.[A-Za-z][A-Za-z0-9]*)?)"
                  r"(?![A-Za-z0-9_\-])")
CHECK = "scripts/check.sh"


def _path(token):
    """像仓库内路径：以 / 结尾的目录，或带短扩展名的文件；仓库名、R12/R13 这类不算。"""
    token = token.strip()
    if not token or re.search(r"\s|://|[#()]", token) or token.startswith("/"):
        return False
    return token.endswith("/") or bool(re.search(r"\.[A-Za-z][A-Za-z0-9]{0,4}$", token.rsplit("/", 1)[-1]))


def _under(path, entry):
    """path 落在 entry 里；path 可以从 entry 的某一级目录写起（issue_delivery/dod.py）。"""
    p, e = path.strip("/").split("/"), entry.strip("/").split("/")
    return any(p[:len(e) - i] == e[i:] for i in range(len(e)))


def _scopes(body):
    """约束条目里的「只改」「不改」路径：[(条目原文, 只改或不改, [路径])]。"""
    result = []
    for entry in items(section(body, "约束")):
        text = entry["text"] + "\n" + entry["body"]
        for kind, run in SCOPE.findall(text):
            paths = [p for p in re.findall(r"`([^`\n]+)`", run) if _path(p)]
            if paths:
                result.append((entry["text"], kind, paths))
    return result


def _sentences(text):
    text = FENCED.sub("", text or "")
    return [s.strip() for s in SENTENCE.split(text) if s.strip()]


def _places(body):
    """要比对的句子：[(合并前 DoD 或节名, 句子)]，DoD 在前。"""
    places = [("合并前 DoD", s) for entry in items(section(body, "合并前 DoD"))
              for s in _sentences(entry["text"] + "\n" + entry["body"])]
    basis = requirement_basis(body)
    for heading, text in re.findall(r"^## (.+?)[ \t]*\n(.*?)(?=^## |\Z)", body, re.M | re.S):
        if heading in SKIPPED or heading == "合并前 DoD":
            continue
        if basis and changed_requirement(basis) is not None:
            text = text.replace(basis, "")  # 需求变更那一段由需求文档的提示管。
        places += [(heading, s) for s in _sentences(text)]
    return places


def _changed_paths(sentence):
    if not VERB.search(sentence):
        return []
    found = []
    for span in re.findall(r"`([^`\n]*)`", sentence):
        if _path(span) and span != CHECK:
            found.append(span)  # 带空格的是命令（检查命令写「通过」的那种），不算要改的路径。
    rest = URL.sub(" ", re.sub(r"`[^`\n]*`", " ", sentence))
    found += [p for p in BARE.findall(rest) if _path(p) and p != CHECK]
    return list(dict.fromkeys(found))


def _path_hints(body, scopes):
    result = []
    for where, sentence in _places(body):
        for path in _changed_paths(sentence):
            for constraint, kind, entries in scopes:
                if kind == "只改" and not any(_under(path, e) or _under(e, path) for e in entries):
                    result.append(f"提示：约束「{constraint}」只改范围外：{where}「{sentence}」要改 {path}")
                elif kind == "不改":
                    hit = next((e for e in entries if _under(path, e)), None)
                    if hit:
                        result.append(f"提示：约束「{constraint}」不改 {hit}：{where}「{sentence}」要改 {path}")
    return result


def _requirement_hints(body, target, documents, scopes):
    only = [(c, entries) for c, kind, entries in scopes if kind == "只改"]
    basis = requirement_basis(body)
    change = changed_requirement(basis) if basis else None
    if not only or change is None:
        return []
    wanted = set()
    if documents is not None:
        try:
            wanted = change_documents(basis, documents, target)
        except ValueError:
            pass
    if not wanted:
        wanted = {p for p in re.findall(r"[\w./\-]+\.md", change) if "/" in p}
    if not wanted:
        try:
            wanted = set(registered_paths(target))
        except ValueError:
            return []
    allowed = [e for _, entries in only for e in entries]
    outside = sorted(d for d in wanted if not any(_under(d, e) for e in allowed))
    if not outside:
        return []
    names = "、".join(f"「{c}」" for c, _ in only)
    return [f"提示：需求变更要改 {'、'.join(outside)}，不在约束{names}的只改范围内"]


def _unknown(tokens, tree):
    """命令表里找不到的那一段（如 reports outbox）；找得到或说不清时返回 None。"""
    words, index = [], 0
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 1 if "=" in tokens[index] else 2  # 全局选项和它的值
    node = tree
    for token in tokens[index:]:
        if not node or token.startswith(("-", "<")):
            return None
        if "/" in token:
            # add/list/disable：斜杠并列的几个子命令逐个查，之后的词分不清跟哪个，不再往下查。
            choices = token.split("/")
            if not all(choices):
                return None
            missing = next((c for c in choices if c not in node), None)
            return " ".join(words + [missing]) if missing else None
        words.append(token)
        if token not in node:
            return " ".join(words)
        node = node[token]
    return None


def _command_hints(body, commands):
    result, seen = [], set()
    for entry in items(section(body, "合并前 DoD")):
        for sentence in _sentences(entry["text"] + "\n" + entry["body"]):
            if "新增" in sentence:
                continue
            for span in re.findall(r"`(workshop\s[^`\n]*)`", sentence):
                missing = _unknown(span.split()[1:], commands)
                if missing and span not in seen:
                    seen.add(span)
                    result.append(f"提示：合并前 DoD「{sentence}」用到 `{span}`，现有命令里没有 {missing}，同一句也没写「新增」")
    return result


def hints(body, target, documents=None, commands=None):
    """返回提示行（不是缺项）。documents 是登记的需求文档；commands 是车间命令行的子命令树
    （{名字: 子树}，取自 cli.parser()），插件不引用命令行，由调用方给；没给就不查子命令。"""
    body = body or ""
    scopes = _scopes(body)
    result = _path_hints(body, scopes) + _requirement_hints(body, target, documents, scopes)
    if commands is not None and target.get("name") == SELF:
        result += _command_hints(body, commands)
    return result
