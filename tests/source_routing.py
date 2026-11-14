"""原有来源回放现在同时经过旧规则的触发器；纯来源另有独立测试。"""
from dataclasses import replace
from workshop.core.flows import matches
from workshop.core.models import Poll
from workshop.plugins.issue_delivery import IssueDelivery
from workshop.plugins.sources.github import GitHubSource


class RoutedGitHubSource(GitHubSource):
    def poll(self, target, cursor):
        batch = super().poll(target, cursor)
        triggers = IssueDelivery.configure_target(target)['legacy_triggers']['github']
        events = []
        for event in batch.events:
            action = next(t['action'] for t in triggers if matches(event.body, t.get('when', {})))
            events.append(replace(event, action=action,
                                  ext_id=event.action_id if action != 'update' else event.ext_id))
        return Poll(events, batch.cursor, normalized=True)
