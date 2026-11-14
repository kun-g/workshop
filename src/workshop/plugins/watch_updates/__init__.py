"""盯更新：同一个插件提供来源和流程两个端口。"""
from .flow import Flow
from .source import Source


def make_flow():
    return Flow()


def make_source():
    return Source()
