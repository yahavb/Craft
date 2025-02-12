from math import floor
from world import World
import queue
import socketserver
import datetime
import random
import re
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import sys
import threading
import time
import traceback
import os
import psycopg2
import signal
import datetime
from datetime import datetime, timezone
from datetime import datetime as dt

#cmd = 'rm -rf /tmp/healthy'
cmd = '/craft/release_gs.sh'

user=os.environ['PGUSER']
password=os.environ['PGPASSWORD']
host=os.environ['PGHOST']
database=os.environ['PGDATABASE']
dbport=os.environ['PGPORT']
pod_name=os.environ['MY_POD_NAME']
node_name=os.environ['MY_NODE_NAME']
DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = int(os.environ['SERVERPORT'])
IS_AGONES=os.environ['IS_AGONES']

if IS_AGONES == 'True':
  AGONES_SDK_HTTP_PORT=os.environ['AGONES_SDK_HTTP_PORT']
  AGONES_HEALTH_THREAD=0

DB_PATH = 'craft.db'
LOG_PATH = 'log.txt'

CHUNK_SIZE = 32
BUFFER_SIZE = 4096
COMMIT_INTERVAL = 5

AUTH_REQUIRED = os.environ['USE_AUTH']
AUTH_URL = os.environ['AUTH_SRV']

DAY_LENGTH = 600
SPAWN_X=int(os.environ['SPAWN_X'])
SPAWN_Y=int(os.environ['SPAWN_Y'])
SPAWN_Z=int(os.environ['SPAWN_Z'])
SPAWN_POINT = (SPAWN_X, SPAWN_Y, SPAWN_Z, 0, 0)
RATE_LIMIT = False
RECORD_HISTORY =os.environ['RECORD_HISTORY']
INDESTRUCTIBLE_ITEMS = set([16])
ALLOWED_ITEMS = set([
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    17, 18, 19, 20, 21, 22, 23,
    32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47,
    48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63])

AUTHENTICATE = 'A'
BLOCK = 'B'
CHUNK = 'C'
DISCONNECT = 'D'
KEY = 'K'
LIGHT = 'L'
NICK = 'N'
POSITION = 'P'
REDRAW = 'R'
SIGN = 'S'
TALK = 'T'
TIME = 'E'
VERSION = 'V'
YOU = 'U'
GETSERVERS = 'G'

try:
    from config import *
except ImportError:
    pass

def log(*args):
    now = dt.now()
    line = ' '.join(map(str, (now,) + args))
    print(line)
    with open(LOG_PATH, 'a') as fp:
        fp.write('%s\n' % line)
    sys.stdout.flush()

def sig_handler(signum,frame):
  global AGONES_HEALTH_THREAD
  AGONES_HEALTH_THREAD=0
  log('in sig_handler:Signal hanlder called with signal',signum)
  log('in sig_handler:execute ',cmd)
  os.system(cmd)
  model.is_going_down=1
  model.send_talk("Game server maintenance is pending - pls reconnect")
  model.send_talk("Don't worry, your universe is saved with us")
  model.send_talk('Removing the server from load balancer %s'%(cmd))
  headers={'Content-Type':'application/json'}
  try:
    url='http://localhost:'+AGONES_SDK_HTTP_PORT+'/shutdown'
    r=requests.post(url,headers=headers,json={},timeout=100)
    log('in sig_handler:shutdown:url:',url, ' response.status_code:',r.status_code,' response.headers:',r.headers)
  except Exception as error:
    log('in sig_handler:error',error)

def pg_read(sql,param):
  try:
    connection = psycopg2.connect(user=user,
                                  password=password,
                                  host=host,
                                  port=dbport,
                                  database=database)
    cursor = connection.cursor()
    cursor.execute(sql,param)
    rows = cursor.fetchall()
    #log('in pg_read:','sql=',sql,'param=',param,'rows=',rows)
    return rows
  except (Exception, psycopg2.Error) as error:
    log('Failed to select:',error,' sql:',sql,' param:',param)
  finally:
    if connection:
        cursor.close()
        connection.close()

def pg_write(sql,param):
  try:
    connection = psycopg2.connect(user=user,
                                  password=password,
                                  host=host,
                                  port=dbport,
                                  database=database)
    cursor = connection.cursor()
    cursor.execute(sql,param)
    connection.commit()
    count = cursor.rowcount
    #log('in pg_write:','sql=',sql,'param=',param,'count=',count)
  except (Exception, psycopg2.Error) as error:
    log('Failed to insert/update:',error,' sql:',sql,' param:',param)
    traceback.print_exc()
  finally:
    if connection:
        cursor.close()
        connection.close()

def chunked(x):
    return int(floor(round(x) / CHUNK_SIZE))

def packet(*args):
    return ('%s\n' % ','.join(map(str, args))).encode("UTF-8")

class RateLimiter(object):
    def __init__(self, rate, per):
        self.rate = float(rate)
        self.per = float(per)
        self.allowance = self.rate
        self.last_check = time.time()
    def tick(self):
        if not RATE_LIMIT:
            return False
        now = time.time()
        elapsed = now - self.last_check
        self.last_check = now
        self.allowance += elapsed * (self.rate / self.per)
        if self.allowance > self.rate:
            self.allowance = self.rate
        if self.allowance < 1:
            return True # too fast
        else:
            self.allowance -= 1
            return False # okay

class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

class Handler(socketserver.BaseRequestHandler):
    def setup(self):
        global AGONES_HEALTH_THREAD
        self.position_limiter = RateLimiter(100, 5)
        self.limiter = RateLimiter(1000, 10)
        self.version = None
        self.client_id = None
        self.user_id = None
        self.nick = None
        self.queue = queue.Queue()
        self.running = True
        self.start()
        if IS_AGONES == 'True':
          AGONES_HEALTH_THREAD=1
          self.start_agones_health()
    def handle(self):
        model = self.server.model
        model.enqueue(model.on_connect, self)
        try:
            buf = b''
            while True:
                data = self.request.recv(BUFFER_SIZE)
                if not data:
                    break
                buf += data.replace(b'\r\n', b'\n')
                while b'\n' in buf:
                    index = buf.index(b'\n')
                    line = buf[:index]
                    buf = buf[index + 1:]
                    command = line.decode("utf-8")
                    if command[0] == POSITION:
                        if self.position_limiter.tick():
                            self.stop()
                            return
                    else:
                        if self.limiter.tick():
                            self.stop()
                            return
                    model.enqueue(model.on_data, self, command)
        finally:
            model.enqueue(model.on_disconnect, self)
    def agones_shutdown(self):
      try:
        headers={'Content-Type':'application/json'}
        url='http://localhost:'+AGONES_SDK_HTTP_PORT+'/shutdown'
        r=requests.post(url,headers=headers,json={})
        log('in Handler:run:response-agones:url:',url, ' response.status_code:',r.status_code,' response.headers:',r.headers)
      except Exception as error:
        log('agones_shutdown:',error)
    def finish(self):
        #if IS_AGONES == 'True':
        #  self.agones_shutdown()
        self.running = False
    def stop(self):
        self.request.close()
    def start(self):
        thread = threading.Thread(target=self.run)
        thread.setDaemon=True
        thread.start()
    def start_agones_health(self):
        thread = threading.Thread(target=self.agones_health)
        #thread.setDaemon=True
        thread.start()
    def agones_health(self):
      global AGONES_HEALTH_THREAD
      #log('in agones_health:self.running',self.running,' :AGONES_HEALTH_THREAD:',AGONES_HEALTH_THREAD)
      if AGONES_HEALTH_THREAD == 1:
        try:
          headers={'Content-Type':'application/json'}
          url='http://localhost:'+AGONES_SDK_HTTP_PORT+'/health'
          r=requests.post(url,headers=headers,json={})
          #log('in Handler:agones_health:url:',url, ' response.status_code:',r.status_code,' response.headers:',r.headers)
        except Exception as error:
          log('agones_health:error',error)

    def run(self):
        while self.running:
            try:
                buf = b''
                try:
                    buf += self.queue.get(timeout=5)
                    try:
                        while True:
                            buf += self.queue.get_nowait()
                    except queue.Empty:
                        pass
                    #log('in Handler:run:buf:',buf)
                except queue.Empty:
                    continue
                self.request.sendall(buf)
            except Exception as error:
                self.request.close()
                #raise

    def send_raw(self, data):
        if data:
            self.queue.put(data)

    def send(self, *args):
        self.send_raw(packet(*args))

class Model(object):
    def __init__(self, seed):
        self.world = World(seed)
        self.clients = []
        self.is_going_down=0
        self.queue = queue.Queue()
        self.commands = {
            AUTHENTICATE: self.on_authenticate,
            CHUNK: self.on_chunk,
            BLOCK: self.on_block,
            LIGHT: self.on_light,
            POSITION: self.on_position,
            TALK: self.on_talk,
            SIGN: self.on_sign,
            VERSION: self.on_version,
            GETSERVERS: self.on_get_servers,
        }
        self.patterns = [
            (re.compile(r'^/nick(?:\s+([^,\s]+))?$'), self.on_nick),
            (re.compile(r'^/spawn$'), self.on_spawn),
            (re.compile(r'^/goto(?:\s+(\S+))?$'), self.on_goto),
            (re.compile(r'^/pq\s+(-?[0-9]+)\s*,?\s*(-?[0-9]+)$'), self.on_pq),
            (re.compile(r'^/help(?:\s+(\S+))?$'), self.on_help),
            (re.compile(r'^/list$'), self.on_list),
            (re.compile(r'^/servers$'), self.on_get_servers),
        ]
    def start(self):
        thread = threading.Thread(target=self.run)
        thread.daemon= True
        thread.start()
    def run(self):
        while True:
            try:
                self.dequeue()
            except Exception:
                traceback.print_exc()

    def enqueue(self, func, *args, **kwargs):
        try:
          self.queue.put((func, args, kwargs))
        except Exception as error:
          log('in enqueue:Exception:',error)
          traceback.print_exc()

    def dequeue(self):
        try:
            func, args, kwargs = self.queue.get(timeout=5)
            func(*args, **kwargs)
        except queue.Empty:
            pass

    def get_default_block(self, x, y, z):
        p, q = chunked(x), chunked(z)
        chunk = self.world.get_chunk(p, q)
        return chunk.get((x, y, z), 0)

    def get_block(self, x, y, z):
        p, q = chunked(x), chunked(z)
        sql = """select w from block where p = %s and q = %s and x = %s and y = %s and z = %s;"""
        params = [p,q,x,y,z]
        rows = list(pg_read(sql,params))
        if rows:
            return rows[0][0]
        return self.get_default_block(x, y, z)

    def next_client_id(self):
        result = 1
        client_ids = set(x.client_id for x in self.clients)
        while result in client_ids:
            result += 1
        return result

    def agones_player(self,client_nick,action):
        try:
          headers={'Content-Type':'application/json'}
          url='http://localhost:'+AGONES_SDK_HTTP_PORT+'/alpha/player/'+action
          payload={"playerID":client_nick}
          r=requests.post(url,headers=headers,json={})
          log('in Model:run:response-agones:url:',url, ' response.status_code:',r.status_code,' response.headers:',r.headers,' payload:',payload)
        except Exception as error:
          log('agones_player_',action,':error',error)
        
    def on_connect(self, client):
        client.client_id = self.next_client_id()
        client.nick = 'guest%d' % client.client_id
        log('on_connect:', client.client_id, *client.client_address)
        self.clients.append(client)
        client.send(TIME, time.time(), DAY_LENGTH)
        client.send(TALK, 'Welcome to Craft!')
        client.send(TALK, 'Type "/help" for a list of commands.')
        self.send_nick(client)
        self.send_nicks(client)

    def on_data(self, client, data):
        args = data.split(',')
        command, args = args[0], args[1:]
        if command in self.commands:
            func = self.commands[command]
            func(client, *args)

    def on_disconnect(self, client):
        log('on_disconnect:',self.next_client_id())
        if IS_AGONES == 'True':
          if 'guest' not in client.nick:
            self.agones_player(client.nick,'disconnect')
          #else:
          #  log('on_disconnect:skipping:',client.nick)
        self.clients.remove(client)
        self.send_disconnect(client)

    def on_get_servers(self,client):
        client.send(TALK, 'Availible servers for you:')
        sql = """select pod,endpoint from game_server_pool where status = 1;"""
        params = []
        rows = list(pg_read(sql,params))
        log("in on_get_servers:availiable endpoints",rows)
        for row in rows:
          client.send(TALK, row)

    def on_version(self, client, version):
        if client.version is not None:
            return
        version = int(version)
        if version != 1:
            client.stop()
            return
        client.version = version
        # TODO: client.start() here

    def on_authenticate(self, client, username, access_token):
        global SPAWN_POINT
        log('on_authenticate:',' client:',client,' username:',username,' access_token:',access_token)
        user_id = None
        if username and access_token:
            payload = {
                'username': username,
                'access_token': access_token,
            }
            log('on_authenticate:payload',payload)
            response = requests.post(AUTH_URL, json=payload)
            log('on_authenticate:response.status_code',response.status_code)
            #if response.status_code == 200 and response.text.isdigit():
            if response.status_code == 200:
                user_id = username
        if user_id is None:
          client.nick = 'guest%d' % client.client_id
          client.user_id='guest%d' % client.client_id
          client.send(TALK, 'Visit http://craft.auth.yahav.sa.aws.dev/auth/adduser/?username=myuser to register!')
        else:
          client.nick = username
          client.user_id = user_id
          self.send_nick(client)
        #log('on_authenticate:client.nick:',client.nick)
        log('on_authenticate:1:SPAWN_POINT:',SPAWN_POINT)
        sql="""select x,y,z from user_recent_pos where user_id=%s"""
        params=[client.nick]
        rows=list(pg_read(sql,params))
        if rows:
          log('on_authenticate:client.nick:',client.nick,' last_pos:',rows,' rows[0][0]=',rows[0][0])
          x=rows[0][0]
          y=rows[0][1]
          z=rows[0][2]
          SPAWN_POINT=(x,y,z,0,0)
          log('on_authenticate:x,y,z:',x,y,z)
        log('on_authenticate:2:SPAWN_POINT:',SPAWN_POINT)
        client.position = SPAWN_POINT
        self.send_position(client)
        self.send_positions(client)
        client.send(YOU, client.client_id, *client.position)
        client.send(TALK, 'Current pod is '+pod_name)
        client.send(TALK, 'Current node is '+node_name)
        self.send_talk('%s has joined the game.' % client.nick)
        if IS_AGONES == 'True':
          self.agones_player(client.nick,'connect')
          headers={'Content-Type':'application/json'}
          url='http://localhost:'+AGONES_SDK_HTTP_PORT+'/allocate'
          r=requests.post(url,headers=headers,json={})
          log('in Model:on_authenticate:url:',url, ' response.status_code:',r.status_code,' response.headers:',r.headers,' payload:',payload)

    def on_chunk(self, client, p, q, key=0):
        packets = b''
        p, q, key = map(int, (p, q, key))
        query="""select rowid, x, y, z, w from block where p = %s and q = %s and rowid > %s;"""
        params=[p,q,key]
        rows=pg_read(query,params)
        max_rowid = 0
        blocks = 0
        if rows is not None:
          for rowid, x, y, z, w in rows:
            blocks += 1
            packets += packet(BLOCK, p, q, x, y, z, w)
            max_rowid = max(max_rowid, rowid)
        query="""select x, y, z, w from light where p=%s and q=%s;"""
        params=[p,q]
        rows=pg_read(query,params)
        lights = 0
        for x, y, z, w in rows:
            lights += 1
            packets += packet(LIGHT, p, q, x, y, z, w)
        query="""select x, y, z, face, text from sign where p = %s and q = %s;"""
        params=[p,q]
        rows=pg_read(query,params)
        signs = 0
        for x, y, z, face, text in rows:
            signs += 1
            packets += packet(SIGN, p, q, x, y, z, face, text)
        if blocks:
            packets += packet(KEY, p, q, max_rowid)
        if blocks or lights or signs:
            packets += packet(REDRAW, p, q)
        packets += packet(CHUNK, p, q)
        client.send_raw(packets)

    def on_block(self, client, x, y, z, w):
        #log('in on_block:',x,y,z,w)
        x, y, z, w = map(int, (x, y, z, w))
        p, q = chunked(x), chunked(z)
        previous = self.get_block(x, y, z)
        message = None
        if AUTH_REQUIRED and client.user_id is None:
            message = 'in on_block - Only logged in users are allowed to build.' 
        elif y <= 0 or y > 255:
            message = 'Invalid block coordinates.'
        elif w not in ALLOWED_ITEMS:
            message = 'That item is not allowed.'
        elif w and previous:
            message = 'Cannot create blocks in a non-empty space.'
        elif not w and not previous:
            message = 'That space is already empty.'
        elif previous in INDESTRUCTIBLE_ITEMS:
            message = 'Cannot destroy that type of block.'
        if message is not None:
            client.send(BLOCK, p, q, x, y, z, previous)
            client.send(REDRAW, p, q)
            client.send(TALK, message)
            return
        now = dt.now()
        if RECORD_HISTORY:
            sql = """insert into block_history (created_at,user_id,p,q,x,y,z,w) values (%s,%s,%s,%s,%s,%s,%s,%s)"""
            params=[now,client.user_id,p,q,x,y,z,w]
            response=pg_write(sql,params)
        sql = """insert into block (updated_at,user_id,p,q,x,y,z,w) values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict on constraint unique_block_pqxyz do UPDATE SET w =%s,updated_at=%s"""
        params=[now,client.user_id,p,q,x,y,z,w,w,now]
        response=pg_write(sql,params)
        self.send_block(client, p, q, x, y, z, w)
        for dx in range(-1, 2):
            for dz in range(-1, 2):
                if dx == 0 and dz == 0:
                    continue
                if dx and chunked(x + dx) == p:
                    continue
                if dz and chunked(z + dz) == q:
                    continue
                np, nq = p + dx, q + dz
                #params=[now,np,nq,x,y,z,-w]
                params=[now,client.user_id,np,nq,x,y,z,-w,-w,now]
                response=pg_write(sql,params)
                self.send_block(client, np, nq, x, y, z, -w)
        if w == 0:
            sql = """delete from sign where x = %s and y = %s and z = %s"""
            params=[x,y,z]
            response=pg_write(sql,params)
            sql = """update light set w = 0 where x = %s and y = %s and z = %s"""
            params=[x,y,z]
            response=pg_write(sql,params)
            
    def on_light(self, client, x, y, z, w):
        x, y, z, w = map(int, (x, y, z, w))
        p, q = chunked(x), chunked(z)
        block = self.get_block(x, y, z)
        message = None
        if AUTH_REQUIRED and client.user_id is None:
            #message = 'Only logged in users are allowed to build.'
            message = 'in on_block - Only logged in users are allowed to build.'
        elif block == 0:
            message = 'Lights must be placed on a block.'
        elif w < 0 or w > 15:
            message = 'Invalid light value.'
        if message is not None:
            # TODO: client.send(LIGHT, p, q, x, y, z, previous)
            client.send(REDRAW, p, q)
            client.send(TALK, message)
            return
        sql = """insert or replace into light (p, q, x, y, z, w) values (%s,%s,%s,%s,%s,%s)"""
        params=[p,q,x,y,z,w]
        response=pg_write(sql,params)
        self.send_light(client, p, q, x, y, z, w)

    def on_sign(self, client, x, y, z, face, *args):
        if AUTH_REQUIRED and client.user_id is None:
            # client.send(TALK, 'Only logged in users are allowed to build.')
            client.send(TALK, 'in on_block - Only logged in users are allowed to build.')
            return
        text = ','.join(args)
        x, y, z, face = map(int, (x, y, z, face))
        if y <= 0 or y > 255:
            return
        if face < 0 or face > 7:
            return
        if len(text) > 48:
            return
        p, q = chunked(x), chunked(z)
        if text:
            sql = """insert into sign (p, q, x, y, z, face, text) values (%s,%s,%s,%s,%s,%s,%s) on conflict on constraint sign_xyzface_idx do update set text=%s"""
            params[p,q,x,y,z,face,text,text]
            response=pg_write(sql,params)
        else:
            sql = """delete from sign where x = %s and y = %s and z = %s and face = %s"""
            params=[x,y,z,face]
            response=pg_write(sql,params)
        self.send_sign(client, p, q, x, y, z, face, text)

    def on_position(self, client, x, y, z, rx, ry):
        x, y, z, rx, ry = map(float, (x, y, z, rx, ry))
        client.position = (x, y, z, rx, ry)
        self.send_position(client)
        if self.is_going_down==1:
          now = dt.now()
          log('on_position:is_going_down:',self.is_going_down,' client.nick:',client.nick,' x,y,z:',x,y,z)
          sql = """insert into user_recent_pos (updated_at,user_id,x,y,z) values (%s,%s,%s,%s,%s) on conflict on constraint unique_username do update set x=%s,y=%s,z=%s"""
          params=[now,client.nick,x,y,z,x,y,z]
          response=pg_write(sql,params)

    def on_talk(self, client, *args):
        text = ','.join(args)
        if text.startswith('/'):
            for pattern, func in self.patterns:
                match = pattern.match(text)
                if match:
                    func(client, *match.groups())
                    break
            else:
                client.send(TALK, 'Unrecognized command: "%s"' % text)
        elif text.startswith('@'):
            nick = text[1:].split(' ', 1)[0]
            for other in self.clients:
                if other.nick == nick:
                    client.send(TALK, '%s> %s' % (client.nick, text))
                    other.send(TALK, '%s> %s' % (client.nick, text))
                    break
            else:
                client.send(TALK, 'Unrecognized nick: "%s"' % nick)
        else:
            self.send_talk('%s> %s' % (client.nick, text))

    def on_nick(self, client, nick=None):
        if AUTH_REQUIRED:
            client.send(TALK, 'You cannot change your nick on this server.')
            return
        if nick is None:
            client.send(TALK, 'Your nickname is %s' % client.nick)
        else:
            self.send_talk('%s is now known as %s' % (client.nick, nick))
            client.nick = nick
            self.send_nick(client)

    def on_spawn(self, client):
        client.position = SPAWN_POINT
        client.send(YOU, client.client_id, *client.position)
        self.send_position(client)

    def on_goto(self, client, nick=None):
        if nick is None:
            clients = [x for x in self.clients if x != client]
            other = random.choice(clients) if clients else None
        else:
            nicks = dict((client.nick, client) for client in self.clients)
            other = nicks.get(nick)
        if other:
            client.position = other.position
            client.send(YOU, client.client_id, *client.position)
            self.send_position(client)

    def on_pq(self, client, p, q):
        p, q = map(int, (p, q))
        if abs(p) > 1000 or abs(q) > 1000:
            return
        client.position = (p * CHUNK_SIZE, 0, q * CHUNK_SIZE, 0, 0)
        client.send(YOU, client.client_id, *client.position)
        self.send_position(client)

    def on_help(self, client, topic=None):
        if topic is None:
            client.send(TALK, 'Type "t" to chat. Type "/" to type commands:')
            client.send(TALK, '/goto [NAME], /help [TOPIC], /list, /login NAME, /logout, /nick')
            client.send(TALK, '/offline [FILE], /online HOST [PORT], /pq P Q, /spawn, /view N /servers')
            return
        topic = topic.lower().strip()
        if topic == 'goto':
            client.send(TALK, 'Help: /goto [NAME]')
            client.send(TALK, 'Teleport to another user.')
            client.send(TALK, 'If NAME is unspecified, a random user is chosen.')
        elif topic == 'list':
            client.send(TALK, 'Help: /list')
            client.send(TALK, 'Display a list of connected users.')
        elif topic == 'login':
            client.send(TALK, 'Help: /login NAME')
            client.send(TALK, 'Switch to another registered username.')
            client.send(TALK, 'The login server will be re-contacted. The username is case-sensitive.')
        elif topic == 'logout':
            client.send(TALK, 'Help: /logout')
            client.send(TALK, 'Unauthenticate and become a guest user.')
            client.send(TALK, 'Automatic logins will not occur again until the /login command is re-issued.')
        elif topic == 'offline':
            client.send(TALK, 'Help: /offline [FILE]')
            client.send(TALK, 'Switch to offline mode.')
            client.send(TALK, 'FILE specifies the save file to use and defaults to "craft".')
        elif topic == 'online':
            client.send(TALK, 'Help: /online HOST [PORT]')
            client.send(TALK, 'Connect to the specified server.')
        elif topic == 'nick':
            client.send(TALK, 'Help: /nick [NICK]')
            client.send(TALK, 'Get or set your nickname.')
        elif topic == 'pq':
            client.send(TALK, 'Help: /pq P Q')
            client.send(TALK, 'Teleport to the specified chunk.')
        elif topic == 'spawn':
            client.send(TALK, 'Help: /spawn')
            client.send(TALK, 'Teleport back to the spawn point.')
        elif topic == 'view':
            client.send(TALK, 'Help: /view N')
            client.send(TALK, 'Set viewing distance, 1 - 24.')
        elif topic == 'servers':
            client.send(TALK, 'Help: /servers')
            client.send(TALK, 'List availiable servers')
    def on_list(self, client):
        client.send(TALK,
            'Players: %s' % ', '.join(x.nick for x in self.clients))
    def send_positions(self, client):
        for other in self.clients:
            if other == client:
                continue
            client.send(POSITION, other.client_id, *other.position)
    def send_position(self, client):
        for other in self.clients:
            if other == client:
                continue
            other.send(POSITION, client.client_id, *client.position)
    def send_nicks(self, client):
        for other in self.clients:
            if other == client:
                continue
            client.send(NICK, other.client_id, other.nick)
    def send_nick(self, client):
        for other in self.clients:
            other.send(NICK, client.client_id, client.nick)
    def send_disconnect(self, client):
        for other in self.clients:
            if other == client:
                continue
            other.send(DISCONNECT, client.client_id)
    def send_block(self, client, p, q, x, y, z, w):
        for other in self.clients:
            if other == client:
                continue
            other.send(BLOCK, p, q, x, y, z, w)
            other.send(REDRAW, p, q)
    def send_light(self, client, p, q, x, y, z, w):
        for other in self.clients:
            if other == client:
                continue
            other.send(LIGHT, p, q, x, y, z, w)
            other.send(REDRAW, p, q)
    def send_sign(self, client, p, q, x, y, z, face, text):
        for other in self.clients:
            if other == client:
                continue
            other.send(SIGN, p, q, x, y, z, face, text)
    def send_talk(self, text):
        log(text)
        for client in self.clients:
            client.send(TALK, text)

def agones_ready():
  try:
    headers={'Content-Type':'application/json'}
    url='http://localhost:'+AGONES_SDK_HTTP_PORT+'/ready'
    payload={}
    r=requests.post(url,headers=headers,json={})
    log('in agones_ready:url:',url, ' response.status_code:',r.status_code,' response.headers:',r.headers,' payload:',payload)
  except Exception as error:
    log('agones_ready:',error)

model = Model(None)

def main():
    host, port = DEFAULT_HOST, DEFAULT_PORT
    if len(sys.argv) > 1:
        host = sys.argv[1]
    if len(sys.argv) > 2:
        port = int(sys.argv[2])
    log('SERV', host, port)
    if IS_AGONES == 'True':
      headers={'Content-Type':'application/json'}
      url='http://localhost:'+AGONES_SDK_HTTP_PORT+'/ready'
      payload={}
      r=requests.post(url,headers=headers,json={})
      log('in agones_ready:url:',url, ' response.status_code:',r.status_code,' response.headers:',r.headers,' payload:',payload)
      #agones_ready()
    signal.signal(signal.SIGTERM,sig_handler)
    model.start()
    server = Server((host, port), Handler)
    server.model = model
    server.serve_forever()

if __name__ == '__main__':
    main()
