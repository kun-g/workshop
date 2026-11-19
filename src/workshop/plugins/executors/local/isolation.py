"""Strip ambient deployment credentials while keeping the CLI's original subscription login."""
import os
from pathlib import Path
from workshop.github_identity import environment as identity_environment

# 订阅计费的会话不能被进程环境里的按量密钥改走按量。
SUBSCRIPTION_SECRETS = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY', 'CURSOR_API_KEY')


def environment(gh_config: Path, source=None):
    source = os.environ if source is None else source
    prefixes = ('AWS_', 'AZURE_', 'ARM_', 'GOOGLE_', 'GCLOUD_', 'CLOUDSDK_', 'GIT_', 'GH_', 'GITHUB_',
                'SSH_', 'KUBE', 'CLOUDFLARE_', 'CF_', 'DIGITALOCEAN_', 'DO_', 'ALIYUN_', 'ALIBABA_',
                'TENCENT_', 'HCLOUD_', 'OS_', 'VAULT_')
    result = {key: value for key, value in source.items() if not key.startswith(prefixes)}
    gh_config.mkdir(parents=True, exist_ok=True, mode=0o700)
    gh_config.chmod(0o700)
    result.update(GH_CONFIG_DIR=str(gh_config), GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                  GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='credential.helper',
                  GIT_CONFIG_VALUE_0='', KUBECONFIG='/dev/null', AWS_CONFIG_FILE='/dev/null',
                  AWS_SHARED_CREDENTIALS_FILE='/dev/null')
    return result


def session_environment(spec, base=None):
    """Called in the actual terminal/runner, never serialize the daemon's environment."""
    env = dict(os.environ if base is None else base)
    if spec.get('billing') == 'subscription':
        # 只清进程环境里带来的密钥；配置档自己声明的变量（如本机网关的 ANTHROPIC_AUTH_TOKEN）在 spec['env'] 里，随后加回。
        for key in SUBSCRIPTION_SECRETS:
            env.pop(key, None)
    env.update(spec['env'])
    if spec['readonly']:
        env = environment(Path(spec['gh_config']), env)
    elif spec.get('github_identity', {}).get('mode') == 'app':
        config = spec['github_identity']
        env = identity_environment(config, spec['repo'], env)
        env['PATH'] = str(Path(config['gh']).expanduser().parent) + os.pathsep + env.get('PATH', '')
    for key in ('CLAUDECODE', 'WORKSHOP_RULES'):
        env.pop(key, None)
    return env
