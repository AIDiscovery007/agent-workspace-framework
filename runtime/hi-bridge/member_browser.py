"""Trusted browser bootstrap; model commands keep their original Codex sandbox.

The browser daemon inherits a separate macOS filesystem sandbox. This permits
Chromium's required Mach services without granting the model broad OS access.
"""
import json
import hashlib
import os
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent
# Only a synthetic fixture identity; no deployed account is special.
LEGACY_SENDER = 'member-a@example.invalid'
BR_CONFIG = json.loads((BASE/'config.json').read_text()) if (BASE/'config.json').exists() else {}
DOMAIN = 'example.invalid'

def domain(config):
    return config.get('browser_domain') or DOMAIN
CLI = Path(BR_CONFIG.get('browser_bin') or os.environ.get('AW_BROWSER_BIN', '/nonexistent/agent-browser'))
ENGINE_DIR = Path(BR_CONFIG.get('browser_engine_dir') or os.environ.get('AW_BROWSER_ENGINE_DIR', '/nonexistent/browser-engine'))


def enabled(config, sender):
    return (isinstance(sender, str) and bool(sender)
            and (sender == config.get('allowed_sender') or sender in config.get('member_workspaces', {}))
            and config.get('member_browser', {}).get(sender) is True)


def account_key(config, sender):
    if not enabled(config, sender):
        raise ValueError('本账号未启用专属浏览器')
    # Preserve the already deployed member profile, sockets and login state.
    return 'account-' + hashlib.sha256(sender.encode()).hexdigest()[:16]


def session(config, sender):
    key = account_key(config, sender)
    return 'hi-' + key


def policy_dir(config, sender):
    return BASE/'browser-runtime'/account_key(config, sender)


def runtime_dir(config, sender, own):
    key = account_key(config, sender)
    root = Path(config['workspace'])
    entry = config.get('member_workspaces', {}).get(sender)
    expected = root/entry if entry is not None else root
    if own.absolute() != expected.absolute() or own.resolve() != own.absolute():
        raise ValueError('浏览器账号与工作区绑定不匹配')
    return own/'.browser'/key


def environment(config, sender, own):
    runtime = runtime_dir(config, sender, own)
    return {
        'AGENT_BROWSER_SOCKET_DIR': str(runtime/'sockets'),
        'AGENT_BROWSER_CONFIG': str(policy_dir(config, sender)/'config.json'),
        'AGENT_BROWSER_SESSION': session(config, sender),
        'AGENT_BROWSER_PROFILE': str(runtime/'profile'),
        'AGENT_BROWSER_EXECUTABLE_PATH': str(ENGINE_DIR/'chrome-headless-shell'),
        # Chromium cannot nest its Seatbelt sandbox; the outer OS sandbox below
        # remains active for the daemon, browser and all of its child processes.
        'AGENT_BROWSER_ARGS': '--no-sandbox',
        'AGENT_BROWSER_ALLOWED_DOMAINS': domain(config),
        'AGENT_BROWSER_DOWNLOAD_PATH': str(runtime/'downloads'),
        'AGENT_BROWSER_SCREENSHOT_DIR': str(runtime/'screenshots'),
        'AGENT_BROWSER_AUTO_CONNECT': 'false',
        'AGENT_BROWSER_ALLOW_FILE_ACCESS': 'false',
        'TMPDIR': str(runtime/'tmp'),
    }


def owner_cli_overrides(config, sender, own):
    if sender != config.get('allowed_sender') or not enabled(config, sender):
        return []
    # Keep workspace-write/on-request; only bind browser environment to this account.
    env = environment(config, sender, own)
    env['WORK_ROOT'] = str(own)
    args = ['-c', 'shell_environment_policy.exclude=["AGENT_BROWSER_*"]']
    for key, value in env.items():
        args.extend(['-c', 'shell_environment_policy.set.'+key+'='+json.dumps(value, ensure_ascii=False)])
    return args


def sandbox_profile(config, sender, own):
    text = '(version 1)\n(allow default)\n(deny file-read* file-write*)\n(allow file-read-metadata)\n(allow file-read* (literal "/"))\n'
    for path in ['/System','/Library','/usr','/bin','/sbin','/dev','/private/etc','/private/var/db',str(ENGINE_DIR),str(policy_dir(config, sender)/'config.json')]:
        text += '(allow file-read* (subpath '+json.dumps(path)+'))\n'
    text += '(allow file-read* file-write* (subpath '+json.dumps(str(own),ensure_ascii=False)+'))\n'
    return text + '(allow file-write* (subpath "/dev"))\n'


def ensure(config, sender, own):
    if not enabled(config, sender):
        return
    if domain(config) == 'example.invalid':
        raise RuntimeError('需要配置已授权的 browser_domain')
    if not CLI.is_file() or not (ENGINE_DIR/'chrome-headless-shell').is_file():
        raise RuntimeError('本账号的独立浏览器依赖缺失，请所有者维护；不改用其他浏览器')
    runtime = runtime_dir(config, sender, own)
    env = environment(config, sender, own)
    for path in [runtime, *[runtime/n for n in ['sockets','profile','downloads','screenshots']], Path(env['TMPDIR'])]:
        if path.resolve() != path.absolute():
            raise RuntimeError('浏览器目录含符号链接，停止启动')
        path.mkdir(parents=True, exist_ok=True)
    policy = policy_dir(config, sender)
    for path in (policy, policy/'config.json', policy/'browser.sb'):
        if path.resolve() != path.absolute():
            raise RuntimeError('浏览器策略路径含符号链接，停止启动')
    policy.mkdir(parents=True, exist_ok=True)
    (policy/'config.json').write_text('{}\n')
    profile = policy/'browser.sb'
    profile.write_text(sandbox_profile(config, sender, own))
    # Never inherit a CDP endpoint/provider or auth state from the host shell.
    env = {**{k:v for k,v in os.environ.items() if not k.startswith('AGENT_BROWSER_')}, **env}
    result = subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),str(CLI),'get','url'],
                            cwd=own, env=env, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError('独立浏览器未就绪，停止本次任务；需所有者检查专属浏览器')
