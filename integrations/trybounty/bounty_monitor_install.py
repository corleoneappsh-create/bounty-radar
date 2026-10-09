#!/usr/bin/env python3
"""Read-only Bounty monitor for macOS. Python 3.9+; standard library only.
Run without arguments to test, preflight and install. No secrets are embedded.
Official contract checked 2026-10-09: https://api.trybounty.ai/openapi.json
Docs: https://docs.trybounty.ai/agents/raw-interface/
"""
import fcntl
from email.utils import parsedate_to_datetime
import math
import http.client
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

LABEL = 'com.bounty.readonly-monitor'
HOME = Path.home()
APP = HOME / 'Library/Application Support/BountyReadonlyMonitor'
PLIST = HOME / 'Library/LaunchAgents' / (LABEL + '.plist')
KEYFILE = HOME / 'System/HermesCandidateHome/.env'
HOST = 'api.trybounty.ai'
ROUTE = '/mcp/readonly'
PROTOCOL = '2025-03-26'
INTERVAL = 30
MAX_BACKOFF = 3600
MAX_PAGES = 20
MAX_SEEN = 100000
NOTICE = 'display notification "New Bounty jobs are available. Open Bounty to review." with title "Bounty monitor"'

class Problem(Exception):
    def __init__(self, code, retry=0, pause=False):
        super().__init__(code)
        self.code, self.retry, self.pause = code, retry, pause


def read_key(path=KEYFILE):
    # Parse only this existing assignment. Never source/evaluate the .env file.
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 1048576:
            raise Problem('credential_file_unsafe', pause=True)
        with path.open(encoding='utf-8') as f:
            lines = f.readlines()
    except (OSError, UnicodeError):
        raise Problem('credential_file_unreadable', pause=True) from None
    found = []
    for line in lines:
        m = re.match(r'^\s*(?:export\s+)?MCP_BOUNTY_API_KEY\s*=\s*(.*?)\s*$', line)
        if not m:
            continue
        value = m.group(1)
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end+1:].strip() and not value[end+1:].lstrip().startswith('#')):
                raise Problem('credential_format_unsupported', pause=True)
            value = value[1:end]
        else:
            value = value.split(' #', 1)[0].strip()
        if not re.fullmatch(r'[A-Za-z0-9_./+=:-]{8,4096}', value):
            raise Problem('credential_format_unsupported', pause=True)
        found.append(value)
    if len(found) != 1:
        raise Problem('credential_missing_or_ambiguous', pause=True)
    return found[0]


def check_http(status, retry_after=''):
    if status in (401, 403):
        raise Problem('authentication_paused', pause=True)
    if status == 429:
        if not retry_after:
            retry=60
        elif re.fullmatch(r'[0-9]{1,8}',retry_after):
            retry=int(retry_after)
        else:
            try:
                deadline=parsedate_to_datetime(retry_after)
                if deadline.tzinfo is None: raise ValueError()
                retry=max(0,math.ceil(deadline.timestamp()-time.time()))
            except Exception:
                raise Problem('rate_limit_invalid_pause',pause=True) from None
        if retry > 86400:
            raise Problem('rate_limit_long_pause', pause=True)
        raise Problem('rate_limited', retry=max(INTERVAL, retry))
    if 300 <= status < 400:
        raise Problem('redirect_blocked', pause=True)
    if status not in (200, 202):
        raise Problem('server_error' if status >= 500 else 'request_rejected', pause=status < 500)


def rpc_result(value, request_id):
    if not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or value.get('id') != request_id:
        raise Problem('malformed_rpc')
    if 'error' in value:
        error = value['error']
        data = error.get('data', {}) if isinstance(error, dict) else {}
        code = data.get('code') if isinstance(data, dict) else None
        if code == 'RATE_LIMITED':
            details = data.get('details', {})
            retry = details.get('retry_after_seconds', 60) if isinstance(details,dict) else 60
            check_http(429, str(retry))
        if code in ('UNAUTHORIZED','INVALID_API_KEY','FORBIDDEN'):
            raise Problem('authentication_paused', pause=True)
        raise Problem('rpc_error', pause=True)
    if not isinstance(value.get('result'), dict):
        raise Problem('malformed_rpc')
    return value['result']


class MCP:
    """Restricted Streamable HTTP client: fixed endpoint, no server-initiated actions."""
    def __init__(self, key):
        self.key, self.session, self.serial = key, None, 0
        result = self.rpc('initialize', {'protocolVersion':PROTOCOL, 'capabilities':{},
            'clientInfo':{'name':'BountyReadonlyMonitor','version':'1.0'}})
        if result.get('protocolVersion') != PROTOCOL:
            raise Problem('unsupported_mcp_version', pause=True)
        self.rpc('notifications/initialized', {}, notification=True)
        listing = self.rpc('tools/list', {})
        listed = listing.get('tools')
        if not isinstance(listed,list) or listing.get('nextCursor'):
            raise Problem('unexpected_tool_catalog', pause=True)
        names = {t.get('name') for t in listed if isinstance(t,dict)}
        if 'bounty_list_open' not in names or not names <= {'bounty_list_open','bounty_get'}:
            raise Problem('readonly_boundary_changed', pause=True)
        tool = next(t for t in listed if t.get('name')=='bounty_list_open')
        schema = tool.get('inputSchema', {})
        properties = schema.get('properties', {})
        if schema.get('type') != 'object' or not isinstance(properties,dict):
            raise Problem('unsupported_tool_schema', pause=True)
        if not set(schema.get('required',[])) <= {'cursor','limit'}:
            raise Problem('unsupported_tool_arguments', pause=True)
        self.properties = properties

    def rpc(self, method, params, notification=False):
        if method not in ('initialize','notifications/initialized','tools/list','tools/call'):
            raise Problem('method_blocked', pause=True)
        if method=='tools/call' and params.get('name')!='bounty_list_open':
            raise Problem('tool_blocked', pause=True)
        self.serial += 1
        message={'jsonrpc':'2.0','method':method,'params':params}
        if not notification: message['id']=self.serial
        headers={'Authorization':'Bearer '+self.key, 'Content-Type':'application/json',
                 'Accept':'application/json, text/event-stream',
                 'MCP-Protocol-Version':PROTOCOL, 'User-Agent':'BountyReadonlyMonitor/1.0'}
        if self.session: headers['Mcp-Session-Id']=self.session
        conn=http.client.HTTPSConnection(HOST,timeout=15)
        try:
            conn.request('POST',ROUTE,body=json.dumps(message).encode(),headers=headers)
            response=conn.getresponse()
            check_http(response.status,response.getheader('Retry-After',''))
            session=response.getheader('Mcp-Session-Id')
            if session:
                if len(session)>4096 or not all(33<=ord(c)<=126 for c in session):
                    raise Problem('invalid_session')
                self.session=session
            if notification:
                if response.status!=202: raise Problem('notification_not_accepted')
                return {}
            if response.status!=200: raise Problem('missing_rpc_response')
            kind=response.getheader('Content-Type','').split(';')[0].strip()
            if kind=='application/json':
                raw=response.read(1048577)
                if len(raw)>1048576: raise Problem('response_too_large')
                return rpc_result(json.loads(raw),self.serial)
            if kind=='text/event-stream':
                # Only matching responses are consumed; instructions/requests are ignored.
                size, parts, started=0,[],time.monotonic()
                for _ in range(1000):
                    if time.monotonic()-started>30: raise Problem('stream_timeout')
                    line=response.readline(1048577)
                    size+=len(line)
                    if size>1048576: raise Problem('response_too_large')
                    if not line: break
                    line=line.decode('utf-8').rstrip('\r\n')
                    if line.startswith('data:'): parts.append(line[5:].lstrip(' '))
                    elif not line and parts:
                        item=json.loads('\n'.join(parts));parts=[]
                        if isinstance(item,dict) and item.get('id')==self.serial and 'method' not in item:
                            return rpc_result(item,self.serial)
                raise Problem('missing_rpc_response')
            raise Problem('unsupported_response_type')
        except Problem:
            raise
        except (ValueError,UnicodeError):
            raise Problem('malformed_json') from None
        except Exception:
            raise Problem('network_error') from None
        finally:
            conn.close()

    def page(self, key, cursor):
        args={}
        if 'limit' in self.properties: args['limit']=100
        if cursor:
            if 'cursor' not in self.properties: raise Problem('cursor_not_supported',pause=True)
            args['cursor']=cursor
        result=self.rpc('tools/call',{'name':'bounty_list_open','arguments':args})
        if result.get('isError'):
            # Do not print server messages or arbitrary content.
            raise Problem('tool_error',pause=True)
        structured=result.get('structuredContent')
        if isinstance(structured,dict): return structured
        content=result.get('content')
        if not isinstance(content,list): raise Problem('malformed_tool_result')
        texts=[c.get('text') for c in content if isinstance(c,dict) and c.get('type')=='text']
        if len(texts)!=1 or not isinstance(texts[0],str): raise Problem('malformed_tool_result')
        try: return json.loads(texts[0])
        except ValueError: raise Problem('malformed_tool_result') from None


def collect(key, fetch=None):
    if fetch is None:
        fetch = MCP(key).page
    result, cursors, cursor = {}, set(), ''
    for _ in range(MAX_PAGES):
        page = fetch(key, cursor)
        if not isinstance(page, dict) or not isinstance(page.get('bounties'), list) or type(page.get('has_more')) is not bool:
            raise Problem('malformed_page')
        if len(page['bounties']) > 100 or not isinstance(page.get('next_cursor'), str) or len(page['next_cursor']) > 4096:
            raise Problem('malformed_page')
        if 'is_done' in page and (type(page['is_done']) is not bool or page['is_done'] == page['has_more']):
            raise Problem('inconsistent_pagination')
        for item in page['bounties']:
            if not isinstance(item, dict):
                raise Problem('malformed_item')
            ident, version = item.get('_id'), item.get('version')
            if not isinstance(ident, str) or not 1 <= len(ident) <= 256 or type(version) is not int or version < 1:
                raise Problem('malformed_item')
            result[ident] = max(version, result.get(ident, 0))
        if not page['has_more']:
            return result
        cursor = page['next_cursor']
        if not cursor or cursor in cursors:
            raise Problem('pagination_cycle')
        cursors.add(cursor)
    raise Problem('pagination_limit')


def initial_state(seen=None):
    return dict(schema=1, seen=seen or {}, errors=0, next_due=0, paused=False,
                last_status='installed', last_check=0, pending_notification=False,
                notification_due=0, notification_failures=0)


def load_state(path):
    try:
        data = json.loads(path.read_text())
        if data['schema'] != 1 or not isinstance(data['seen'], dict) or len(data['seen']) > MAX_SEEN:
            raise ValueError()
        if any(not isinstance(k,str) or len(k)>256 or type(v) is not int or v < 1 for k,v in data['seen'].items()):
            raise ValueError()
        if type(data['errors']) is not int or not 0 <= data['errors'] <= 20 or type(data['paused']) is not bool:
            raise ValueError()
        if type(data['next_due']) not in (float,int) or not 0 <= data['next_due'] < 1e12:
            raise ValueError()
        if type(data['pending_notification']) is not bool or type(data['notification_failures']) is not int or not 0<=data['notification_failures']<=20:
            raise ValueError()
        if type(data['notification_due']) not in (int,float) or not 0<=data['notification_due']<1e12:
            raise ValueError()
        return data
    except Exception:
        raise Problem('state_invalid_stop', pause=True) from None


def save_state(path, data):
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, separators=(',', ':'))
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def transition(state, key, now, fetch=None):
    """Pure polling state transition; no notification until durable commit."""
    out = dict(state); out['seen'] = dict(state['seen'])
    if state['paused'] or now < state['next_due']:
        return out, 0
    try:
        current = collect(key, fetch)
        new = {ident for ident,version in current.items() if version>state['seen'].get(ident,0)}
        if len(set(current) | set(state['seen'])) > MAX_SEEN:
            raise Problem('history_limit_paused', pause=True)
        for ident, version in current.items():
            out['seen'][ident] = max(version, out['seen'].get(ident, 0))
        out.update(errors=0, next_due=now+INTERVAL, last_status='ok', last_check=now)
        if new: out['pending_notification']=True
        return out, len(new)
    except Problem as error:
        errors = min(state['errors'] + 1, 20)
        delay = max(error.retry, min(MAX_BACKOFF, INTERVAL * (2 ** errors)))
        out.update(errors=errors, next_due=now+delay, paused=error.pause,
                   last_status=error.code, last_check=now)
        return out, 0


def notify():
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', NOTICE],
                                capture_output=True, timeout=10, check=False)
        return result.returncode == 0
    except Exception:
        return False


def attempt_notification(state, now, send=notify):
    out=dict(state)
    if not out['pending_notification'] or now<out['notification_due']:
        return out
    if send():
        out.update(pending_notification=False,notification_failures=0,
                   notification_due=0,last_notification='requested_not_delivery_verified')
    else:
        failures=min(out['notification_failures']+1,20)
        out.update(notification_failures=failures,
                   notification_due=now+min(MAX_BACKOFF,INTERVAL*(2**failures)),
                   last_notification='unavailable_retry_pending')
    return out


def tick():
    with (APP / 'lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        path = APP / 'state.json'
        state = load_state(path)
        now=time.time()
        if not state['paused'] and now>=state['next_due']:
            try:
                key = read_key()
                state, count = transition(state, key, now)
                del key
            except Problem as error:
                state.update(paused=True, last_status=error.code, last_check=now)
            # Commit dedupe AND pending notification together before any notification.
            save_state(path, state)
        # A crash after notification but before saving may repeat a generic notice.
        state=attempt_notification(state,time.time())
        save_state(path,state)


def launchctl(*args):
    p = subprocess.run(['/bin/launchctl', *args], capture_output=True, timeout=20)
    if p.returncode:
        raise Problem('launchctl_failed')


def install():
    if sys.platform != 'darwin' or os.getuid() == 0:
        raise Problem('mac_same_user_required')
    if APP.exists() or APP.is_symlink() or PLIST.exists() or PLIST.is_symlink():
        raise Problem('existing_files_conflict_stop')
    result = subprocess.run(['/bin/launchctl', 'print', 'gui/%d/%s' % (os.getuid(), LABEL)], capture_output=True, timeout=20)
    if result.returncode == 0:
        raise Problem('existing_label_conflict_stop')
    selftest()
    print('Offline tests passed. Checking Bounty read-only access...')
    existing = collect(read_key())  # No install writes until full live preflight passes.
    print('Read-only access OK; currently visible jobs:', len(existing))
    os.umask(0o077)
    APP.mkdir(parents=True, exist_ok=False)
    script = APP / 'bounty_monitor_install.py'
    shutil.copyfile(Path(__file__).resolve(), script)
    save_state(APP / 'state.json', initial_state(existing))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    config = {'Label':LABEL, 'ProgramArguments':[sys.executable, str(script), '--tick'],
              'RunAtLoad':True, 'StartInterval':INTERVAL, 'ProcessType':'Background',
              'WorkingDirectory':str(APP)}
    # Exclusive creation; never overwrite another LaunchAgent.
    with PLIST.open('xb') as f:
        plistlib.dump(config, f)
    try:
        launchctl('bootstrap', 'gui/%d' % os.getuid(), str(PLIST))
        launchctl('print', 'gui/%d/%s' % (os.getuid(), LABEL))
    except Problem:
        print('Installation files kept; activation not confirmed. Use --status / --uninstall.')
        raise
    print('Installed: read-only checks every 30 seconds while Mac is awake and you are logged in.')
    print('Notifications appear on this Mac only; macOS notification settings may hide them.')
    print('Existing jobs are the baseline. New IDs or increased versions trigger a notification.')
    print('No model, claims, bids, submissions, Telegram or Hermes gateway changes.')
    print('Status: python3 ~/Downloads/bounty_monitor_install.py --status')


def verify_owned_plist():
    if PLIST.is_symlink() or APP.is_symlink():
        raise Problem('local_path_conflict_stop')
    try:
        with PLIST.open('rb') as f: cfg=plistlib.load(f)
        args=cfg.get('ProgramArguments',[])
        if cfg.get('Label')!=LABEL or len(args)!=3 or args[1:]!=[str(APP/'bounty_monitor_install.py'),'--tick']:
            raise ValueError()
    except Exception:
        raise Problem('plist_conflict_stop') from None


def manage(mode):
    if mode != '--status': verify_owned_plist()
    target = 'gui/%d/%s' % (os.getuid(), LABEL)
    if mode == '--status':
        state = load_state(APP / 'state.json')
        p = subprocess.run(['/bin/launchctl','print',target],capture_output=True,timeout=20)
        print(json.dumps({'loaded':p.returncode==0, 'paused':state['paused'],
                          'last_status':state['last_status'], 'last_check_unix':state['last_check'],
                          'known_ids':len(state['seen']), 'next_due_unix':state['next_due'],
                          'notification_pending':state['pending_notification'],
                          'notification':state.get('last_notification','none')},indent=2))
    elif mode in ('--stop', '--uninstall'):
        p = subprocess.run(['/bin/launchctl','print',target],capture_output=True,timeout=20)
        if p.returncode == 0: launchctl('bootout', target)
        if mode == '--uninstall':
            PLIST.unlink()
        print('Stopped. Saved state and source retained at:', APP)
    elif mode == '--resume':
        with (APP/'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            collect(read_key())
            state = load_state(APP/'state.json')
            state.update(paused=False,errors=0,next_due=0)
            save_state(APP/'state.json',state)
        p = subprocess.run(['/bin/launchctl','print',target],capture_output=True,timeout=20)
        if p.returncode != 0: launchctl('bootstrap','gui/%d' % os.getuid(),str(PLIST))
        print('Read-only monitor resumed.')


def page(items=(), more=False, cursor=''):
    return dict(bounties=[dict(_id=i,version=v) for i,v in items],has_more=more,is_done=not more,next_cursor=cursor)

class OfflineTests(unittest.TestCase):
    def test_empty(self):
        s,n=transition(initial_state(),'FAKE',100,lambda k,c:page()); self.assertEqual(n,0);self.assertEqual(s['last_status'],'ok')
    def test_new_duplicate_version(self):
        f=lambda k,c:page([('job',1)])
        s,n=transition(initial_state(),'FAKE',100,f);self.assertEqual(n,1)
        s,n=transition(s,'FAKE',131,f);self.assertEqual(n,0)
        s,n=transition(s,'FAKE',162,lambda k,c:page([('job',2)]));self.assertEqual(n,1);self.assertEqual(s['seen']['job'],2)
    def test_restart_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';s,n=transition(initial_state(),'FAKE',100,lambda k,c:page([('x',1)]));save_state(p,s)
            s,n=transition(load_state(p),'FAKE',131,lambda k,c:page([('x',1)]));self.assertEqual(n,0)
    def test_pagination(self):
        p={'':page([('a',1)],True,'next'),'next':page([('b',1)])}
        self.assertEqual(len(collect('FAKE',lambda k,c:p[c])),2)
    def test_short_empty_page_continues(self):
        p={'':page([],True,'next'),'next':page([('a',1)])};self.assertEqual(len(collect('FAKE',lambda k,c:p[c])),1)
    def test_cycle_no_partial_commit(self):
        s,n=transition(initial_state(),'FAKE',100,lambda k,c:page([('a',1)],True,'loop'))
        self.assertEqual(s['seen'],{});self.assertEqual(s['last_status'],'pagination_cycle')
    def test_429_backoff(self):
        def fail(k,c):raise Problem('rate_limited',retry=600)
        s,n=transition(initial_state(),'FAKE',100,fail);self.assertEqual(s['next_due'],700)
        s2,n=transition(s,'FAKE',699,lambda k,c:self.fail('too soon'));self.assertEqual(s,s2)
    def test_backoff_bounded(self):
        def fail(k,c):raise Problem('network_error')
        s=initial_state()
        for i in range(25):
            now=s['next_due'];s,n=transition(s,'FAKE',now,fail)
        self.assertEqual(s['next_due']-now,MAX_BACKOFF)
    def test_auth_pauses(self):
        def fail(k,c):raise Problem('authentication_paused',pause=True)
        s,n=transition(initial_state(),'FAKE',100,fail);self.assertTrue(s['paused'])
        transition(s,'FAKE',99999,lambda k,c:self.fail('paused'))
    def test_malformed(self):
        for bad in [{},page([('x',True)]),page([],True,''),{'bounties':[],'has_more':'false','next_cursor':''}]:
            s,n=transition(initial_state(),'FAKE',100,lambda k,c:bad);self.assertNotEqual(s['last_status'],'ok');self.assertEqual(s['seen'],{})
    def test_key_not_stored(self):
        secret='FAKE-SECRET-DO-NOT-LOG'
        s,n=transition(initial_state(),secret,100,lambda k,c:page())
        self.assertNotIn(secret,json.dumps(s))
    def test_env_parser(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'.env';p.write_text('OTHER=no\nexport MCP_BOUNTY_API_KEY="fake_key_123"\n')
            self.assertEqual(read_key(p),'fake_key_123')
            p.write_text('MCP_BOUNTY_API_KEY=$(bad)\n')
            with self.assertRaises(Problem):read_key(p)
    def test_fixed_mcp_boundary_and_no_redirect(self):
        from unittest.mock import patch
        calls=[]
        class Response:
            status=302
            def getheader(self,*a):return 'https://evil.invalid/FAKE-SECRET'
        class Fake:
            def __init__(self,host,timeout):calls.append(host)
            def request(self,method,url,body,headers):calls.extend([method,url])
            def getresponse(self):return Response()
            def close(self):pass
        with patch.object(http.client,'HTTPSConnection',Fake):
            with self.assertRaises(Problem) as caught:MCP('FAKE-SECRET')
        self.assertEqual(str(caught.exception),'redirect_blocked')
        self.assertEqual(calls,[HOST,'POST','/mcp/readonly'])
    def test_rpc_error_redaction_and_rate_limit(self):
        with self.assertRaises(Problem) as caught:
            rpc_result({'jsonrpc':'2.0','id':1,'error':{'message':'FAKE-SECRET','data':{'code':'RATE_LIMITED','details':{'retry_after_seconds':500}}}},1)
        self.assertEqual(caught.exception.retry,500);self.assertNotIn('FAKE-SECRET',str(caught.exception))
    def test_write_tool_blocked_before_network(self):
        m=MCP.__new__(MCP)
        with self.assertRaises(Problem):m.rpc('tools/call',{'name':'bounty_claim'})
    def test_transport_json_sse_initialize_and_pagination(self):
        from unittest.mock import patch
        for sse in (False,True):
            calls=[]
            class Response:
                def __init__(self,status,result=None):
                    self.status=status
                    self.raw=io.BytesIO((('data: '+json.dumps(result)+'\n\n') if sse else json.dumps(result)).encode())
                def getheader(self,key,default=None):
                    return {'Content-Type':'text/event-stream' if sse else 'application/json','Mcp-Session-Id':'session_test'}.get(key,default)
                def read(self,n):return self.raw.read(n)
                def readline(self,n):return self.raw.readline(n)
            class Fake:
                def __init__(self,host,timeout):self.host=host
                def request(self,method,url,body,headers):
                    m=json.loads(body);calls.append((method,url,m,headers));self.message=m
                def getresponse(self):
                    m=self.message;method=m['method']
                    if method=='notifications/initialized':return Response(202)
                    if method=='initialize':r={'protocolVersion':PROTOCOL,'capabilities':{}}
                    elif method=='tools/list':r={'tools':[{'name':'bounty_list_open','inputSchema':{'type':'object','properties':{'cursor':{'type':'string'},'limit':{'type':'integer'}}}}]}
                    else:r={'content':[{'type':'text','text':json.dumps(page([('test_job',1)]))}]}
                    return Response(200,{'jsonrpc':'2.0','id':m['id'],'result':r})
                def close(self):pass
            with patch.object(http.client,'HTTPSConnection',Fake):
                self.assertEqual(collect('FAKE-SECRET'),{'test_job':1})
            self.assertEqual(len(calls),4)
            self.assertTrue(all(c[0:2]==('POST','/mcp/readonly') for c in calls))
            self.assertEqual(calls[-1][2]['params']['name'],'bounty_list_open')
            self.assertEqual(calls[-1][3]['Mcp-Session-Id'],'session_test')
    def test_http_auth_429_oversized_retry(self):
        for code in (401,403):
            with self.assertRaises(Problem) as caught:check_http(code)
            self.assertTrue(caught.exception.pause)
        with self.assertRaises(Problem) as caught:check_http(429,'7200')
        self.assertEqual(caught.exception.retry,7200)
        with self.assertRaises(Problem) as caught:check_http(429,'9999999')
        self.assertTrue(caught.exception.pause)
    def test_pagination_cap(self):
        calls=[]
        def f(k,c):calls.append(c);return page([],True,str(len(calls)))
        with self.assertRaises(Problem):collect('FAKE',f)
        self.assertEqual(len(calls),MAX_PAGES)
    def test_notification_is_fixed_argv_not_shell(self):
        from unittest.mock import patch
        with patch.object(subprocess,'run') as run:
            run.return_value.returncode=0
            self.assertTrue(notify())
            args,kwargs=run.call_args
            self.assertEqual(args[0],['/usr/bin/osascript','-e',NOTICE]);self.assertFalse(kwargs.get('shell',False))
    def test_notification_retry_survives_restart(self):
        s,n=transition(initial_state(),'FAKE',100,lambda k,c:page([('x',1)]))
        self.assertTrue(s['pending_notification'])
        s=attempt_notification(s,100,lambda:False)
        self.assertTrue(s['pending_notification']);self.assertEqual(s['notification_due'],160)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';save_state(p,s);s=load_state(p)
            s=attempt_notification(s,159,lambda:self.fail('too early'))
            s=attempt_notification(s,160,lambda:True)
            self.assertFalse(s['pending_notification'])
            self.assertEqual(s['last_notification'],'requested_not_delivery_verified')
    def test_retry_after_http_date_and_invalid(self):
        from unittest.mock import patch
        with patch.object(time,'time',return_value=0):
            with self.assertRaises(Problem) as caught:check_http(429,'Thu, 01 Jan 1970 00:10:00 GMT')
        self.assertEqual(caught.exception.retry,600)
        with self.assertRaises(Problem) as caught:check_http(429,'bad-date')
        self.assertTrue(caught.exception.pause)
    def test_notification_has_no_task_content(self):
        self.assertNotIn('{',NOTICE);self.assertNotIn('do shell script',NOTICE)


def selftest():
    output=io.StringIO()
    result=unittest.TextTestRunner(stream=output,verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(OfflineTests))
    if not result.wasSuccessful():
        raise Problem('offline_tests_failed')
    print('%d offline tests passed' % result.testsRun)


def main():
    mode=sys.argv[1] if len(sys.argv)==2 else ''
    if len(sys.argv)>2 or mode not in ('','--test','--tick','--status','--stop','--uninstall','--resume'):
        print('Use: --test, --status, --stop, --resume, --uninstall');return 2
    try:
        if mode=='--test':selftest()
        elif mode=='--tick':tick()
        elif mode:manage(mode)
        else:install()
        return 0
    except Problem as e:
        if mode != '--tick':print('Stopped safely:',e.code)
        return 1
    except Exception:
        if mode != '--tick':print('Stopped safely: local_operation_failed (no secrets logged)')
        return 1

if __name__=='__main__':
    raise SystemExit(main())
