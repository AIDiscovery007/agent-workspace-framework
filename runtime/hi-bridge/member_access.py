"""Exact-account workspaces and native Codex filesystem permission profiles."""
import json
from pathlib import Path
import tomllib
import member_browser
import member_upload
import os

PROFILE = 'hi_member'
SOPS = ('成员权限SOP.md', 'Hi群消息排版SOP.md', '错题记录与治理SOP.md')
SKILLS = ('standardize-sop', 'record-incident')

def skill_paths(config, sender):
    paths = [Path(config['workspace'])/'.agents/skills'/name for name in (*SKILLS, *config.get('member_skills', {}).get(sender, []))]
    if member_browser.enabled(config, sender):
        paths.append(Path.home()/'.codex/skills/agent-browser')
    if member_upload.enabled(config, sender):
        paths.append(Path(config['workspace'])/'.agents/skills/hi-pan')
    return paths


def workspace(config, sender):
    entry = config.get('member_workspaces', {}).get(sender)
    if entry is None:
        if sender not in (None, config['allowed_sender']) and config.get('require_member_workspace', True):
            raise ValueError('账号未绑定本人工作区')
        return Path(config['workspace'])
    root = Path(config['workspace']).resolve()
    path = root / entry
    # No alias to a sibling or outside directory, even after a member changes files.
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_relative_to(root / '成员工作区'):
        raise ValueError('成员工作区路径不安全，停止执行')
    if not path.is_dir():
        raise ValueError('成员工作区不存在，停止执行')
    if (path/'.codex').exists() or (path/'.codex').is_symlink():
        raise ValueError('专属目录不允许本地 Codex 配置，停止执行')
    return path

def isolated(config, sender):
    return sender in config.get('member_workspaces', {})

def filesystem(config, sender):
    root = Path(config['workspace'])
    own = workspace(config, sender)
    if not isolated(config, sender):
        raise ValueError('Not an isolated member')
    # :minimal supplies OS runtime support, not the user's home or other projects.
    fs = {':minimal': 'read', str(own): 'write', str(own/'.codex'): 'deny', str(own/'.agents'): 'read'}
    for path in [root/'AGENTS.md', *[root/'SOP'/name for name in SOPS],
                 *skill_paths(config, sender),
                 *([member_upload.TOOLS, *member_upload.sop_paths(root)] if member_upload.enabled(config,sender) else []),
                 *([member_browser.CLI.parent.parent, member_browser.policy_dir(config, sender)/'config.json'] if member_browser.enabled(config, sender) else []),
                 Path('/opt/homebrew'), Path('/usr/local/bin'), Path.home()/'.local/bin/codex', Path.home()/'.codex/packages/standalone']:
        if path.exists():
            fs[str(path)] = 'read'
            fs[str(path.resolve())] = 'read'
    return fs

def overrides(config, sender):
    result = {
        'permissions.' + PROFILE + '.filesystem': filesystem(config, sender),
        'permissions.' + PROFILE + '.network.enabled': config.get('network_access') is True,
        'default_permissions': PROFILE,
        'approval_policy': 'never',
        'features.apps': False,
        'features.computer_use': False,
        'features.multi_agent': False,
        'features.memories': False,
        'features.js_repl': False,
        'plugins._default.enabled': False,
        'skills.bundled.enabled': False,
        'skills.include_instructions': True,
        'shell_environment_policy.inherit': 'core',
        'shell_environment_policy.ignore_default_excludes': False,
        'shell_environment_policy.set': {'TMPDIR': str(workspace(config, sender)/'.tmp'), 'WORK_ROOT':str(workspace(config, sender))},
    }
    # Unrestricted local MCP/desktop servers are outside the command sandbox.
    # Disable explicitly enabled entries as well as plugin defaults.
    for path in [Path.home()/'.codex/config.toml', Path(config['workspace'])/'.codex/config.toml']:
        if path.exists():
            data = tomllib.loads(path.read_text())
            for name in data.get('mcp_servers', {}):
                if '.' in name:
                    raise ValueError('MCP 名称包含点，需先核实禁用配置')
                result[f'mcp_servers.{name}.enabled'] = False
            for name in data.get('plugins', {}):
                result.setdefault('plugins', {})[name] = {'enabled':False}
    result.setdefault('plugins', {})['_default'] = {'enabled':False}
    result.pop('plugins._default.enabled', None)
    if member_browser.enabled(config, sender):
        browser_env = member_browser.environment(config, sender, workspace(config, sender))
        result['shell_environment_policy.set'].update(browser_env)
        result['permissions.' + PROFILE + '.network.unix_sockets'] = {browser_env['AGENT_BROWSER_SOCKET_DIR']:'allow'}
    if member_upload.enabled(config,sender):
        result['shell_environment_policy.set'].update(HI_MEMBER_UPLOAD_SOCKET=str(member_upload.socket_path(workspace(config,sender))), PATH=str(member_upload.TOOLS)+':'+os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin'))
        result.setdefault('permissions.'+PROFILE+'.network.unix_sockets',{})[str(member_upload.socket_path(workspace(config,sender)))]='allow'
    allowed = {(p/'SKILL.md').resolve() for p in skill_paths(config, sender)}
    discovered = set()
    for root in [Path.home()/'.codex/skills', Path.home()/'.agents/skills', Path(config['workspace'])/'.agents/skills']:
        if root.exists():
            discovered.update(p.resolve() for p in root.rglob('SKILL.md'))
    result['skills.config'] = [{'path':str(p), 'enabled':p in allowed} for p in sorted(discovered | allowed)]
    return result

def toml(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k)+'='+toml(v) for k,v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(toml(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False)

def cli_overrides(config, sender):
    args = []
    for key, value in overrides(config, sender).items():
        args.extend(['-c', key + '=' + toml(value)])
    return args
