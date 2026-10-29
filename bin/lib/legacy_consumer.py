"""One consumer adapter over the existing transport/admission API, including older helpers."""
import os
from pathlib import Path
import re
import subprocess


def lstat_if_present(path):
    """Only ENOENT proves absence; permission/IO errors remain unknown."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def server_absent(result):
    """Only tmux's explicit empty-server replies prove an empty inventory."""
    diagnostic = (result.stderr or '').strip()
    return result.returncode == 1 and (
        diagnostic == 'no sessions'
        or re.fullmatch(r'no server running on .+', diagnostic) is not None
        or re.fullmatch(r'error connecting to .+ \(No such file or directory\)', diagnostic) is not None)


class LegacyConsumer:
    """One explicit legacy endpoint and one admission for each target effect."""
    def __init__(self,client_home,*,transport=None,api=None,proof_reader=None,transport_api=None):
        self.tmux=transport_api
        if self.tmux is None:raise RuntimeError("GLOBAL_TARGET_HELPER_UNAVAILABLE")
        self.home=Path(client_home)
        self.transport=self.tmux['legacy_transport']() if transport is None else transport
        self.api=(self.tmux['runtime_api_view']() if 'runtime_api_view' in self.tmux else self.tmux['module_api']('runtime_client')) if api is None else api
        self.proof_reader=proof_reader

    def admission(self,target,operation):
        if target.startswith('='):target=target[1:]
        selector={'pane':target} if re.fullmatch(r'%[0-9]+',target) else ({'window':target} if re.fullmatch(r'@[0-9]+',target) else {'session':target.split(':',1)[0]})
        return self.tmux['legacy_admission'](self.transport,self.home,operation=operation,api=self.api,proof_reader=self.proof_reader,**selector)

    def admitted_argv(self,args,*,_creation=None):
        args=list(args)
        if not args:raise self.tmux['TargetError']('TMUX_TARGET_INPUT_INVALID')
        command=args[0]
        if command=='new-session' and self.transport.identity is None:
            # Absence at bootstrap is not permission to adopt a server which
            # appeared before this admission. Use the existing socket/target
            # checks for both command() and the interactive CLI handoff.
            if lstat_if_present(self.transport.path) is not None:
                self.transport.identity=self.tmux['validate_socket'](self.transport.path)
                server=self.transport.command('display-message','-p','#{pid}')
                if server.returncode or not server.stdout.strip().isdigit() or int(server.stdout.strip())<1:
                    raise self.tmux['TargetError']('LEGACY_TMUX_TRANSPORT_UNAVAILABLE')
                self.transport.server_pid=int(server.stdout.strip())
        if command=='new-session' and '-s' in args and self.transport.identity is not None:
            name_index=args.index('-s')+1
            if name_index>=len(args):raise self.tmux['TargetError']('TMUX_TARGET_INPUT_INVALID')
            existing=self.transport.command('list-sessions','-F','#{session_name}')
            if existing.returncode:
                if not server_absent(existing) or (existing.stdout or '').strip():
                    raise self.tmux['TargetError']('TMUX_TARGET_UNAVAILABLE')
                # Keep this observation local to command(); an interactive
                # handoff only needs the admitted argv. Transport.command()
                # has checked the same safe socket before and after inventory.
                if _creation is not None:_creation['recreating']=True
            if args[name_index] in existing.stdout.splitlines():
                # -A may attach an existing session. Creation is not permission
                # to bypass the same current-target guard.
                self.admission(args[name_index],'attach' if '-A' in args else 'capture')
        operation={'send-keys':'input','paste-buffer':'input','split-window':'split','kill-pane':'close','kill-window':'close','kill-session':'kill-session','attach':'attach','attach-session':'attach'}.get(command,'capture')
        if '-t' in args:
            index=args.index('-t')+1
            if index>=len(args):raise self.tmux['TargetError']('TMUX_TARGET_INPUT_INVALID')
            target=args[index]
            # Exact spelling before admission eliminates tmux prefix aliases.
            proof=self.admission(target,operation)
            if target.startswith(('%','@')):pinned=target
            elif operation=='kill-session':pinned=proof['panes'][0]['session_id']
            elif command in ('has-session','show-environment','attach','attach-session','list-panes','rename-session'):
                pinned=proof['panes'][0]['session_id']
            else:pinned=proof['panes'][0]['pane_id']
            args[index]=pinned
        elif command in ('send-keys','paste-buffer','kill-session','kill-pane','kill-window','split-window','attach','attach-session'):
            raise self.tmux['TargetError']('TMUX_TARGET_INPUT_INVALID')
        return [*self.transport.argv,*args]

    def command(self,args,**kwargs):
        # Absence needs no target/process admission, but must be proved on the
        # same pinned endpoint. Existing targets still pass every normal gate.
        # Do not turn arbitrary pane/socket/permission failures into absence.
        if len(args) == 3 and args[:2] == ['has-session', '-t']:
            target = args[2].removeprefix('=')
            if (args[2].startswith(('=', '$'))
                    and re.fullmatch(r'[A-Za-z0-9_.-]+|\$[0-9]+', target)):
                def names():
                    reply = self.command(['list-sessions', '-F', '#{session_id}\t#{session_name}'])
                    if reply.returncode:
                        raise self.tmux['TargetError']('TMUX_TARGET_UNAVAILABLE')
                    rows = []
                    for line in reply.stdout.splitlines():
                        fields = line.split('\t')
                        if len(fields) != 2 or re.fullmatch(r'\$[0-9]+', fields[0]) is None or not fields[1]:
                            raise self.tmux['TargetError']('TMUX_TARGET_UNAVAILABLE')
                        rows.append(tuple(fields))
                    return rows
                before = names()
                if not any(target in row for row in before):
                    if names() != before:
                        raise self.tmux['TargetError']('TMUX_TARGET_CHANGED')
                    return subprocess.CompletedProcess(self.transport.argv + list(args), 1, '',
                                                       "can't find session: " + target + '\n')
        inventory = bool(args) and args[0] in ('list-sessions', 'list-panes') and '-t' not in args
        if inventory:
            # Read the pinned destination afresh. ENOENT is a known empty
            # inventory, including a server which ended after bootstrap.
            # A newly appeared/substituted or unsafe socket is never adopted.
            current = self.tmux['Transport'](self.transport.path, binary=self.transport.binary,
                                             run=self.transport.run, allow_absent=True)
            if current.identity is None:
                return subprocess.CompletedProcess(self.transport.argv + list(args), 0, '', '')
            if current.identity != self.transport.identity:
                raise self.tmux['TargetError']('TMUX_SOCKET_CHANGED')
        creation={}
        argv=self.admitted_argv(args,_creation=creation) if args and args[0]=='new-session' else self.admitted_argv(args)
        creating=args and args[0]=='new-session' and self.transport.identity is None
        if creating:self.tmux['private_directory'](self.transport.path.parent,create=True)
        elif self.transport.identity is None:raise self.tmux['TargetError']('TMUX_TARGET_UNAVAILABLE')
        if not creating and self.tmux['validate_socket'](self.transport.path)!=self.transport.identity:raise self.tmux['TargetError']('TMUX_SOCKET_CHANGED')
        env=kwargs.pop('env',None) or os.environ
        env={k:v for k,v in env.items() if k not in ('TMUX','TMUX_PANE')}
        kwargs.setdefault('capture_output',True);kwargs.setdefault('text',True);kwargs.setdefault('timeout',5)
        result=self.transport.run(argv,env=env,**kwargs)
        if inventory and server_absent(result):
            current = self.tmux['Transport'](self.transport.path, binary=self.transport.binary,
                                             run=self.transport.run, allow_absent=True)
            if current.identity is not None and current.identity != self.transport.identity:
                raise self.tmux['TargetError']('TMUX_SOCKET_CHANGED')
            return subprocess.CompletedProcess(argv, 0, '', '')
        if (creating or creation.get('recreating')) and result.returncode==0:
            # tmux may replace its stale socket when starting a new server.
            # Re-pin only after a successful creation, never an unknown error.
            self.transport.identity=self.tmux['validate_socket'](self.transport.path)
            if creation.get('recreating'):self.transport.server_pid=None
        return result

    def stop_group(self,target):
        proof=self.admission(target,'signal')
        for row in proof['panes']:
            result=subprocess.run(['ps','-o','pgid=','-p',str(row['pid'])],capture_output=True,text=True,timeout=3)
            pgid=result.stdout.strip()
            if result.returncode or not pgid.isdigit():raise self.tmux['TargetError']('TMUX_TARGET_UNAVAILABLE')
            current=self.admission(target,'signal')
            if current['state_sha256']!=proof['state_sha256']:raise self.tmux['TargetError']('TMUX_TARGET_CHANGED')
            if self.api['process_start'](row['pid'])!=row['process_start']:raise self.tmux['TargetError']('TMUX_TARGET_CHANGED')
            os.killpg(int(pgid),__import__('signal').SIGSTOP)
